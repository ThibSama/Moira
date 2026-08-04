"""Agent activity domain — privacy-minimal activity visibility.

Independent from quotas. Persists only runtime, sanitized provider/model
labels, hashed session/turn identity and UTC state timestamps in
``$XDG_STATE_HOME/moira/activity.json`` (mode 0600, atomic writes with
process-safe ``fcntl`` locking). Prompts, responses, transcripts, paths,
raw payloads/errors, accounts and secrets are never stored.

States: RUNNING, COMPLETED, FAILED, INTERRUPTED. Incoming events are
validated fail-closed (malformed, naive, future-skewed, oversized and
unknown events are rejected). Replays are idempotent and a late start can
never replace a terminal event. A completion notifier may record a
runtime-scoped last event (``record_last``) but can never synthesize
RUNNING. The watchdog expires missing terminal events to INTERRUPTED —
never to success.

Display derivation is a pure function (``derive_runtime_activity``): a
runtime shows the spinner while it has running sessions (count above one,
latest sanitized model label), then the most recent terminal event for
exactly ``TERMINAL_WINDOW_SECONDS`` (5 minutes), then hides.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

#: Signed, bounded file size: a larger activity file is treated as corrupt
#: (fail closed to an empty store).
MAX_ACTIVITY_FILE_BYTES = 1_048_576

#: Maximum accepted future skew for an event timestamp (seconds).
MAX_FUTURE_SKEW_SECONDS = 300

#: Maximum sanitized model-label length.
MAX_MODEL_LABEL_LEN = 48

#: Maximum raw session/turn identity length accepted for hashing.
MAX_RAW_IDENTITY_LEN = 256

#: Maximum session records kept per runtime (oldest terminal pruned first;
#: running sessions are never pruned).
MAX_SESSIONS_PER_RUNTIME = 200

#: Terminal state shown for exactly five minutes after the last turn ends.
TERMINAL_WINDOW_SECONDS = 300

#: A running session with no update for this long is expired by the
#: watchdog to INTERRUPTED (never to success).
WATCHDOG_STALE_SECONDS = 1800

#: Persisted activity-file schema version.
ACTIVITY_VERSION = 1

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - POSIX-only environment
    _fcntl = None  # type: ignore[assignment]


class ActivityState(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"

    @property
    def terminal(self) -> bool:
        return self is not ActivityState.RUNNING


class AgentRuntime(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"
    HERMES = "hermes"


class ActivityOutcome(StrEnum):
    """Typed result of applying an activity event."""

    ACCEPTED = "accepted"
    REPLAYED = "replayed"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class ActivityEvent:
    """One validated, privacy-minimal activity event.

    ``session_hash`` is the SHA-256 hex digest of the provider session or
    turn identity — the raw identity never enters the store. ``model`` is
    already sanitized (bounded printable label, empty when unknown).
    ``at`` is a timezone-aware UTC timestamp.
    """

    runtime: AgentRuntime
    state: ActivityState
    session_hash: str
    model: str
    at: datetime


@dataclass(frozen=True, slots=True)
class LastActivityEvent:
    """Runtime-scoped last sanitized event (completion-notifier surface).

    Never carries a session identity and can never be RUNNING: a
    completion notifier may record completion but must not synthesize
    RUNNING.
    """

    runtime: AgentRuntime
    state: ActivityState
    model: str
    at: datetime


@dataclass(frozen=True, slots=True)
class RuntimeActivity:
    """Pure derived display state for one runtime at one instant."""

    runtime: AgentRuntime
    state: ActivityState | None
    active_count: int
    model: str
    at: datetime | None
    visible: bool


def hash_identity(raw: str) -> str:
    """Return the SHA-256 hex digest of a session/turn identity.

    The raw identity must be a non-empty bounded string; anything else is
    rejected by the caller's validation (this helper never raises for
    oversized input — ``validate_identity`` does).
    """
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validate_identity(raw: str) -> str:
    """Validate and hash a raw session/turn identity (fail closed)."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("identity must be a non-empty string")
    if len(raw) > MAX_RAW_IDENTITY_LEN:
        raise ValueError(f"identity exceeds {MAX_RAW_IDENTITY_LEN} characters")
    return hash_identity(raw)


def sanitize_model(raw: object) -> str:
    """Sanitize a model label: bounded printable label, empty when unknown.

    Control characters and the empty value are dropped; the result is
    truncated to ``MAX_MODEL_LABEL_LEN``. Never raises.
    """
    if not isinstance(raw, str):
        return ""
    cleaned = "".join(ch for ch in raw if ch.isprintable())
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > MAX_MODEL_LABEL_LEN:
        cleaned = cleaned[:MAX_MODEL_LABEL_LEN]
    return cleaned


def _validate_timestamp(value: object, now: datetime, label: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{label} must be a datetime")
    if value.tzinfo is None:
        raise ValueError(f"{label} must be timezone-aware (naive rejected)")
    if value.year < 1970 or value.year > 9999:
        raise ValueError(f"{label} outside supported range (oversized)")
    aware = value.astimezone(UTC)
    if (aware - now).total_seconds() > MAX_FUTURE_SKEW_SECONDS:
        raise ValueError(f"{label} is future-skewed")
    return aware


def _validate_state(state: object) -> ActivityState:
    if isinstance(state, ActivityState):
        return state
    if isinstance(state, str):
        try:
            return ActivityState(state)
        except ValueError as exc:
            raise ValueError("unknown activity state") from exc
    raise ValueError("state must be an ActivityState value")


def _validate_runtime(runtime: object) -> AgentRuntime:
    if isinstance(runtime, AgentRuntime):
        return runtime
    if isinstance(runtime, str):
        try:
            return AgentRuntime(runtime)
        except ValueError as exc:
            raise ValueError("unknown agent runtime") from exc
    raise ValueError("runtime must be an AgentRuntime value")


def _validate_model(model: object) -> str:
    return sanitize_model(model)


def _validate_session_hash(value: object) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError("session hash must be a 64-character SHA-256 hex digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError("session hash must be hexadecimal") from exc
    return value.lower()


def activity_path() -> Path:
    """Return the activity store path under ``$XDG_STATE_HOME``."""
    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return state_home / "moira" / "activity.json"


class ActivityStore:
    """Locked, atomic, corruption-tolerant activity store.

    Every mutation re-reads the file under an exclusive ``fcntl`` lock so
    concurrent hook processes never lose updates; writes go through a
    temporary file + ``os.replace`` at mode 0600. Deletion or corruption of
    the file is tolerated (reload falls back to an empty store).
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or activity_path()
        self._data: dict[str, Any] = self._read()

    # ── File I/O ──────────────────────────────────────────────────────────

    @contextmanager
    def _locked(self) -> Iterator[None]:
        lock_path = self.path.with_name(self.path.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+", encoding="utf-8")
        try:
            os.chmod(lock_path, 0o600)
            if _fcntl is not None:
                _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)
            yield
        finally:
            if _fcntl is not None:
                _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
            handle.close()

    def _read(self) -> dict[str, Any]:
        """Load the store, failing closed to an empty store on any issue."""
        try:
            size = self.path.stat().st_size
            if size > MAX_ACTIVITY_FILE_BYTES:
                return self._empty()
            if size == 0:
                return self._empty()
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return self._coerce(raw)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return self._empty()

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"version": ACTIVITY_VERSION, "sessions": {}, "last_events": {}}

    def _coerce(self, raw: object) -> dict[str, Any]:
        """Strictly validate a persisted store dict (fail closed)."""
        if not isinstance(raw, dict) or raw.get("version") != ACTIVITY_VERSION:
            raise ValueError("activity file has an unsupported shape")
        sessions = raw.get("sessions")
        last_events = raw.get("last_events")
        if not isinstance(sessions, dict) or not isinstance(last_events, dict):
            raise ValueError("activity file has an unsupported shape")
        coerced: dict[str, dict[str, dict[str, Any]]] = {}
        for runtime_key, entries in sessions.items():
            runtime = _validate_runtime(runtime_key)
            if not isinstance(entries, dict):
                raise ValueError("activity sessions must be objects")
            coerced[runtime.value] = {}
            for session_hash, entry in entries.items():
                if not isinstance(entry, dict):
                    raise ValueError("activity session entry must be an object")
                state = _validate_state(entry.get("state"))
                model = _validate_model(entry.get("model"))
                started_at = datetime.fromisoformat(str(entry["started_at"]))
                updated_at = datetime.fromisoformat(str(entry["updated_at"]))
                if started_at.tzinfo is None or updated_at.tzinfo is None:
                    raise ValueError("activity session timestamps must be aware")
                _validate_session_hash(session_hash)
                coerced[runtime.value][session_hash] = {
                    "state": state.value,
                    "model": model,
                    "started_at": started_at.astimezone(UTC).isoformat(),
                    "updated_at": updated_at.astimezone(UTC).isoformat(),
                }
        coerced_last: dict[str, dict[str, str]] = {}
        for runtime_key, entry in last_events.items():
            runtime = _validate_runtime(runtime_key)
            if not isinstance(entry, dict):
                raise ValueError("activity last event must be an object")
            state = _validate_state(entry.get("state"))
            if state.terminal is False:
                raise ValueError("a last event must never be RUNNING")
            at = datetime.fromisoformat(str(entry["at"]))
            if at.tzinfo is None:
                raise ValueError("activity last-event timestamp must be aware")
            coerced_last[runtime.value] = {
                "state": state.value,
                "model": _validate_model(entry.get("model")),
                "at": at.astimezone(UTC).isoformat(),
            }
        return {
            "version": ACTIVITY_VERSION,
            "sessions": coerced,
            "last_events": coerced_last,
        }

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _prune(self, data: dict[str, Any]) -> None:
        """Bound stored sessions per runtime (oldest terminal pruned first)."""
        for sessions in data["sessions"].values():
            if len(sessions) <= MAX_SESSIONS_PER_RUNTIME:
                continue
            terminal = [
                (session_hash, entry["updated_at"])
                for session_hash, entry in sessions.items()
                if entry["state"] != ActivityState.RUNNING.value
            ]
            terminal.sort(key=lambda item: item[1])
            overflow = len(sessions) - MAX_SESSIONS_PER_RUNTIME
            for session_hash, _updated_at in terminal[:overflow]:
                sessions.pop(session_hash, None)

    # ── Public API ────────────────────────────────────────────────────────

    def reload(self) -> None:
        """Re-read the file (tolerating deletion/corruption)."""
        self._data = self._read()

    def snapshot(self) -> dict[str, Any]:
        """Return the current in-memory store dict (read-only by contract)."""
        return self._data

    def sessions_for(self, runtime: AgentRuntime) -> dict[str, dict[str, Any]]:
        sessions = self._data.get("sessions", {})
        value = sessions.get(runtime.value)
        return value if isinstance(value, dict) else {}

    def last_event_for(self, runtime: AgentRuntime) -> dict[str, str] | None:
        last_events = self._data.get("last_events", {})
        value = last_events.get(runtime.value)
        return value if isinstance(value, dict) else None

    def record(
        self,
        event: ActivityEvent,
        now: datetime | None = None,
    ) -> ActivityOutcome:
        """Apply one session-scoped event (locked, read-merge-write).

        Rules (fail closed, never partial):
        - RUNNING for an unknown session creates it; for a running session
          it is an idempotent replay; for a terminal session it is rejected
          (a late start can never replace a terminal event).
        - A terminal event transitions a running session; a terminal event
          for a terminal session with the same state is a replay; with a
          different state it is rejected (out-of-order terminal).
        - A terminal event for an unknown session is rejected here — the
          completion-notifier surface is ``record_last``.
        - Any accepted TERMINAL event updates the runtime's sanitized last
          event (last events never carry RUNNING).
        """
        reference = now or datetime.now(UTC)
        runtime = _validate_runtime(event.runtime)
        state = _validate_state(event.state)
        session_hash = _validate_session_hash(event.session_hash)
        model = _validate_model(event.model)
        at = _validate_timestamp(event.at, reference, "event timestamp")
        with self._locked():
            data = self._read()
            sessions = data["sessions"].setdefault(runtime.value, {})
            existing = sessions.get(session_hash)
            if state is ActivityState.RUNNING:
                if existing is not None:
                    outcome = (
                        ActivityOutcome.REPLAYED
                        if existing["state"] == ActivityState.RUNNING.value
                        else ActivityOutcome.REJECTED
                    )
                    if outcome is ActivityOutcome.REJECTED:
                        return outcome
                else:
                    outcome = ActivityOutcome.ACCEPTED
                    sessions[session_hash] = {
                        "state": state.value,
                        "model": model,
                        "started_at": at.isoformat(),
                        "updated_at": at.isoformat(),
                    }
            else:
                if existing is None:
                    return ActivityOutcome.REJECTED
                if existing["state"] != ActivityState.RUNNING.value:
                    return (
                        ActivityOutcome.REPLAYED
                        if existing["state"] == state.value
                        else ActivityOutcome.REJECTED
                    )
                outcome = ActivityOutcome.ACCEPTED
                sessions[session_hash] = {
                    "state": state.value,
                    "model": model,
                    "started_at": existing["started_at"],
                    "updated_at": at.isoformat(),
                }
            if outcome is ActivityOutcome.ACCEPTED and state.terminal:
                data["last_events"][runtime.value] = {
                    "state": state.value,
                    "model": model,
                    "at": at.isoformat(),
                }
            self._prune(data)
            self._write(data)
            self._data = data
        return outcome

    def record_last(
        self,
        event: LastActivityEvent,
        now: datetime | None = None,
    ) -> ActivityOutcome:
        """Record a runtime-scoped last event (completion notifier).

        Never accepts RUNNING — a completion notifier must not synthesize
        RUNNING. Replays of the identical terminal event are idempotent.
        """
        reference = now or datetime.now(UTC)
        runtime = _validate_runtime(event.runtime)
        state = _validate_state(event.state)
        if state is ActivityState.RUNNING:
            raise ValueError("a last event must never be RUNNING")
        model = _validate_model(event.model)
        at = _validate_timestamp(event.at, reference, "event timestamp")
        with self._locked():
            data = self._read()
            previous = data["last_events"].get(runtime.value)
            if previous is not None and previous == {
                "state": state.value,
                "model": model,
                "at": at.isoformat(),
            }:
                self._data = data
                return ActivityOutcome.REPLAYED
            data["last_events"][runtime.value] = {
                "state": state.value,
                "model": model,
                "at": at.isoformat(),
            }
            self._write(data)
            self._data = data
        return ActivityOutcome.ACCEPTED

    def expire_stale(
        self,
        now: datetime | None = None,
        stale_after: float = WATCHDOG_STALE_SECONDS,
    ) -> list[AgentRuntime]:
        """Watchdog: expire running sessions with no recent update.

        Missing terminal events expire to INTERRUPTED — never to success.
        Returns the runtimes whose state changed (persisted atomically).
        """
        reference = now or datetime.now(UTC)
        changed: list[AgentRuntime] = []
        with self._locked():
            data = self._read()
            for runtime_key, sessions in data["sessions"].items():
                stale: list[tuple[str, dict[str, Any]]] = []
                for session_hash, entry in sessions.items():
                    if entry["state"] != ActivityState.RUNNING.value:
                        continue
                    updated = datetime.fromisoformat(entry["updated_at"])
                    if (reference - updated).total_seconds() > stale_after:
                        stale.append((session_hash, entry))
                if not stale:
                    continue
                for _session_hash, entry in stale:
                    entry["state"] = ActivityState.INTERRUPTED.value
                    entry["updated_at"] = reference.isoformat()
                data["last_events"][runtime_key] = {
                    "state": ActivityState.INTERRUPTED.value,
                    "model": stale[-1][1]["model"],
                    "at": reference.isoformat(),
                }
                changed.append(AgentRuntime(runtime_key))
            if changed:
                self._write(data)
                self._data = data
        return changed

    def clear(self) -> None:
        """Reset the store to empty (used by tests and removal paths)."""
        with self._locked():
            data = self._empty()
            self._write(data)
            self._data = data


def _latest_model(sessions: dict[str, dict[str, Any]]) -> str:
    """Model label of the most recently updated session ("" when unknown)."""
    if not sessions:
        return ""
    latest = max(sessions.values(), key=lambda entry: entry["updated_at"])
    model = latest.get("model", "")
    return model if isinstance(model, str) else ""


def derive_runtime_activity(
    data: dict[str, Any],
    now: datetime | None = None,
    terminal_window: float = TERMINAL_WINDOW_SECONDS,
) -> dict[AgentRuntime, RuntimeActivity]:
    """Pure display derivation per runtime (GTK-free, deterministic).

    A runtime is visible when it has running sessions (spinner) or a
    terminal event within ``terminal_window``. The most recent terminal
    event wins; running sessions dominate. The model label is the latest
    sanitized one. Run with ``now`` injected for deterministic tests.
    """
    reference = now or datetime.now(UTC)
    result: dict[AgentRuntime, RuntimeActivity] = {}
    for runtime in AgentRuntime:
        sessions = data.get("sessions", {}).get(runtime.value, {})
        running = {
            session_hash: entry
            for session_hash, entry in sessions.items()
            if entry["state"] == ActivityState.RUNNING.value
        }
        if running:
            latest = max(running.values(), key=lambda entry: entry["updated_at"])
            result[runtime] = RuntimeActivity(
                runtime=runtime,
                state=ActivityState.RUNNING,
                active_count=len(running),
                model=latest.get("model", ""),
                at=datetime.fromisoformat(latest["updated_at"]),
                visible=True,
            )
            continue
        candidates: list[tuple[datetime, ActivityState, str]] = []
        last_event = data.get("last_events", {}).get(runtime.value)
        if last_event is not None:
            at = datetime.fromisoformat(last_event["at"])
            candidates.append((at, ActivityState(last_event["state"]), last_event.get("model", "")))
        for entry in sessions.values():
            if entry["state"] == ActivityState.RUNNING.value:
                continue
            at = datetime.fromisoformat(entry["updated_at"])
            candidates.append((at, ActivityState(entry["state"]), entry.get("model", "")))
        if candidates:
            at, state, model = max(candidates, key=lambda item: item[0])
            age = (reference - at).total_seconds()
            if age <= terminal_window:
                result[runtime] = RuntimeActivity(
                    runtime=runtime,
                    state=state,
                    active_count=0,
                    model=model,
                    at=at,
                    visible=True,
                )
                continue
        result[runtime] = RuntimeActivity(
            runtime=runtime,
            state=None,
            active_count=0,
            model="",
            at=None,
            visible=False,
        )
    return result
