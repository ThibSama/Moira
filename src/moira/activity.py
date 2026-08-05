"""Agent activity domain — privacy-minimal activity visibility.

Independent from quotas. Persists only runtime, sanitized provider/model
labels, hashed session/turn identity and UTC state timestamps in
``$XDG_STATE_HOME/moira/activity.json`` (mode 0600, atomic writes with
process-safe ``fcntl`` locking). Prompts, responses, transcripts, paths,
raw payloads/errors, accounts and secrets are never stored.

States: RUNNING, COMPLETED, FAILED, INTERRUPTED. Incoming events are
validated fail-closed (malformed, naive, future-skewed, oversized and
unknown events are rejected). Replays are idempotent.

Turn lifecycle: Claude ``UserPromptSubmit``/``Stop``/``StopFailure`` and
Hermes ``pre_llm_call``/``post_llm_call`` are turn-level events that
repeat under one provider session. Each turn transitions independently
RUNNING → COMPLETED/FAILED/INTERRUPTED, keyed by a hashed turn identity:

- when the provider supplies a real turn identifier (Claude ``prompt_id``
  in every hook event, Hermes ``turn_id`` in every shell-hook event,
  Codex ``turn.id`` per thread), the turn identity is the hashed composite
  of the session identity and the turn identifier — late or reordered
  events resolve to the exact turn they belong to;
- otherwise the store derives a deterministic privacy-safe turn identity
  from the lifecycle (a per-session ordinal, ``seq:N``), so legitimate
  replays never create duplicate turns.

A new turn in the same provider session displays RUNNING; a later failure
replaces an earlier success as the most recent terminal state; an old or
late event from a previous turn never overwrites a newer turn; a late
start can never reopen a completed turn; terminal records never become
RUNNING again.

A completion notifier may record a runtime-scoped last event
(``record_last``) but can never synthesize RUNNING. The watchdog expires
missing terminal events to INTERRUPTED — never to success.

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

#: Maximum turn records kept per session (oldest terminal turns pruned
#: first; a running turn is never pruned).
MAX_TURNS_PER_SESSION = 50

#: Terminal state shown for exactly five minutes after the last turn ends.
TERMINAL_WINDOW_SECONDS = 300

#: A running turn with no update for this long is expired by the watchdog
#: to INTERRUPTED (never to success).
WATCHDOG_STALE_SECONDS = 1800

#: Persisted activity-file schema version (v2 = turn lifecycle; v1 files
#: migrate additively on load).
ACTIVITY_VERSION = 2

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

    ``session_hash`` is the SHA-256 hex digest of the provider session
    identity; ``turn_hash`` is the SHA-256 hex digest of the turn identity
    (the provider turn identifier bound to its session, or the derived
    ``seq:N`` identity) — raw identities never enter the store. ``None``
    for ``turn_hash`` means the event carries no turn identity and the
    store derives one from the lifecycle. ``model`` is already sanitized
    (bounded printable label, empty when unknown). ``at`` is a
    timezone-aware UTC timestamp.
    """

    runtime: AgentRuntime
    state: ActivityState
    session_hash: str
    model: str
    at: datetime
    turn_hash: str | None = None


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


def composite_identity(*parts: str) -> str | None:
    """Hash a composite raw identity (e.g. session + turn identifier).

    Each part must be a non-empty bounded string; any invalid part yields
    None (fail closed, silent). The composite is privacy-safe: only the
    digest is ever persisted.
    """
    for part in parts:
        if not isinstance(part, str) or not part.strip():
            return None
        if len(part) > MAX_RAW_IDENTITY_LEN:
            return None
    return hash_identity("\x00".join(parts))


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


def _parse_aware(value: object, label: str) -> datetime:
    """Parse a persisted ISO timestamp, requiring timezone awareness."""
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} is not an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must be timezone-aware")
    return parsed.astimezone(UTC)


def _turn_entry(
    state: ActivityState, model: str, started_at: datetime, updated_at: datetime
) -> dict[str, str]:
    return {
        "state": state.value,
        "model": model,
        "started_at": started_at.astimezone(UTC).isoformat(),
        "updated_at": updated_at.astimezone(UTC).isoformat(),
    }


def activity_path() -> Path:
    """Return the activity store path under ``$XDG_STATE_HOME``."""
    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return state_home / "moira" / "activity.json"


def _new_session() -> dict[str, Any]:
    return {"turns": {}, "current": None, "next_seq": 1}


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
        """Strictly validate a persisted store dict (fail closed).

        Accepts version 1 (legacy flat per-session lifecycle, migrated
        additively) and version 2 (turn lifecycle); anything else fails
        closed to an empty store.
        """
        if not isinstance(raw, dict):
            raise ValueError("activity file has an unsupported shape")
        version = raw.get("version")
        if version == 1:
            return self._migrate_v1(raw)
        if version != ACTIVITY_VERSION:
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
            for session_hash, session in entries.items():
                if not isinstance(session, dict):
                    raise ValueError("activity session entry must be an object")
                _validate_session_hash(session_hash)
                turns_raw = session.get("turns")
                current = session.get("current")
                next_seq = session.get("next_seq")
                if (
                    not isinstance(turns_raw, dict)
                    or not isinstance(current, str)
                    or not isinstance(next_seq, int)
                    or isinstance(next_seq, bool)
                    or next_seq < 1
                ):
                    raise ValueError("activity session entry has an unsupported shape")
                turns: dict[str, dict[str, Any]] = {}
                for turn_hash, entry in turns_raw.items():
                    if not isinstance(entry, dict):
                        raise ValueError("activity turn entry must be an object")
                    _validate_session_hash(turn_hash)
                    state = _validate_state(entry.get("state"))
                    model = _validate_model(entry.get("model"))
                    started_at = _parse_aware(entry.get("started_at"), "turn started_at")
                    updated_at = _parse_aware(entry.get("updated_at"), "turn updated_at")
                    turns[turn_hash] = _turn_entry(state, model, started_at, updated_at)
                if not turns:
                    raise ValueError("activity session must contain at least one turn")
                if current not in turns:
                    raise ValueError("activity session current turn is missing")
                coerced[runtime.value][session_hash] = {
                    "turns": turns,
                    "current": current,
                    "next_seq": next_seq,
                }
        coerced_last: dict[str, dict[str, str]] = {}
        for runtime_key, entry in last_events.items():
            runtime = _validate_runtime(runtime_key)
            if not isinstance(entry, dict):
                raise ValueError("activity last event must be an object")
            state = _validate_state(entry.get("state"))
            if state.terminal is False:
                raise ValueError("a last event must never be RUNNING")
            at = _parse_aware(entry.get("at"), "last event at")
            coerced_last[runtime.value] = {
                "state": state.value,
                "model": _validate_model(entry.get("model")),
                "at": at.isoformat(),
            }
        return {
            "version": ACTIVITY_VERSION,
            "sessions": coerced,
            "last_events": coerced_last,
        }

    def _migrate_v1(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Migrate a v1 store (flat session lifecycle) to v2 additively.

        Each v1 session entry becomes a session holding one turn with the
        derived ``seq:1`` identity; states, model labels and timestamps are
        preserved exactly. Never raises past the shared validators.
        """
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
                _validate_session_hash(session_hash)
                state = _validate_state(entry.get("state"))
                model = _validate_model(entry.get("model"))
                started_at = _parse_aware(entry.get("started_at"), "session started_at")
                updated_at = _parse_aware(entry.get("updated_at"), "session updated_at")
                turn_hash = hash_identity(f"{session_hash}\x00seq:1")
                coerced[runtime.value][session_hash] = {
                    "turns": {turn_hash: _turn_entry(state, model, started_at, updated_at)},
                    "current": turn_hash,
                    "next_seq": 2,
                }
        coerced_last: dict[str, dict[str, str]] = {}
        for runtime_key, entry in last_events.items():
            runtime = _validate_runtime(runtime_key)
            if not isinstance(entry, dict):
                raise ValueError("activity last event must be an object")
            state = _validate_state(entry.get("state"))
            if state.terminal is False:
                raise ValueError("a last event must never be RUNNING")
            at = _parse_aware(entry.get("at"), "last event at")
            coerced_last[runtime.value] = {
                "state": state.value,
                "model": _validate_model(entry.get("model")),
                "at": at.isoformat(),
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
        """Bound stored turns per session and sessions per runtime.

        Oldest terminal turns are pruned first; a running turn is never
        pruned. Sessions are bounded per runtime, dropping the oldest
        terminal-only sessions; sessions with a running turn are never
        pruned.
        """
        for sessions in data["sessions"].values():
            for session in sessions.values():
                turns = session["turns"]
                if len(turns) > MAX_TURNS_PER_SESSION:
                    terminal = [
                        (turn_hash, entry)
                        for turn_hash, entry in turns.items()
                        if entry["state"] != ActivityState.RUNNING.value
                    ]
                    terminal.sort(key=lambda item: item[1]["updated_at"])
                    overflow = len(turns) - MAX_TURNS_PER_SESSION
                    for turn_hash, _entry in terminal[:overflow]:
                        del turns[turn_hash]
                    if session.get("current") not in turns:
                        # Defensive (current is the newest turn and is never
                        # pruned): repoint at the newest remaining turn.
                        if turns:
                            session["current"] = max(
                                turns, key=lambda turn_hash: turns[turn_hash]["updated_at"]
                            )
                        else:
                            session["current"] = None
            if len(sessions) <= MAX_SESSIONS_PER_RUNTIME:
                continue

            def newest(session: dict[str, Any]) -> str:
                return max((turn["updated_at"] for turn in session["turns"].values()), default="")

            terminal_sessions = [
                (session_hash, session)
                for session_hash, session in sessions.items()
                if not any(
                    turn["state"] == ActivityState.RUNNING.value
                    for turn in session["turns"].values()
                )
            ]
            terminal_sessions.sort(key=lambda item: newest(item[1]))
            overflow = len(sessions) - MAX_SESSIONS_PER_RUNTIME
            for session_hash, _session in terminal_sessions[:overflow]:
                del sessions[session_hash]

    # ── Turn application ──────────────────────────────────────────────────

    def _create_turn(
        self,
        sessions: dict[str, dict[str, Any]],
        session_hash: str,
        session: dict[str, Any] | None,
        state: ActivityState,
        model: str,
        at: datetime,
        turn_hash: str | None = None,
    ) -> dict[str, Any]:
        """Create a turn (derived ``seq:N`` identity when none is given)."""
        if session is None:
            session = _new_session()
            sessions[session_hash] = session
        if turn_hash is None:
            ordinal = int(session["next_seq"])
            session["next_seq"] = ordinal + 1
            turn_hash = hash_identity(f"{session_hash}\x00seq:{ordinal}")
        session["turns"][turn_hash] = _turn_entry(state, model, at, at)
        session["current"] = turn_hash
        return session

    def _apply_derived(
        self,
        sessions: dict[str, dict[str, Any]],
        session_hash: str,
        session: dict[str, Any] | None,
        state: ActivityState,
        model: str,
        at: datetime,
    ) -> tuple[dict[str, Any] | None, ActivityOutcome, str | None]:
        """Apply an event without a provider turn identity.

        The lifecycle determines the turn: a RUNNING event while the
        session's current turn is running is an idempotent replay; a
        RUNNING event after a terminal turn opens the next derived turn
        (``seq:N+1``); a terminal event closes the current running turn; a
        terminal event with no running turn replays the identical terminal
        state or is rejected (never reopens a completed turn).
        """
        if session is None:
            if state is ActivityState.RUNNING:
                return (
                    self._create_turn(sessions, session_hash, None, state, model, at),
                    (ActivityOutcome.ACCEPTED),
                    None,
                )
            return None, ActivityOutcome.REJECTED, None
        current = session.get("current")
        current_turn = session["turns"].get(current) if isinstance(current, str) else None
        if state is ActivityState.RUNNING:
            if current_turn is not None and current_turn["state"] == ActivityState.RUNNING.value:
                return session, ActivityOutcome.REPLAYED, None
            return (
                self._create_turn(sessions, session_hash, session, state, model, at),
                ActivityOutcome.ACCEPTED,
                None,
            )
        if current_turn is not None and current_turn["state"] == ActivityState.RUNNING.value:
            current_turn["state"] = state.value
            current_turn["model"] = model
            current_turn["updated_at"] = at.astimezone(UTC).isoformat()
            return session, ActivityOutcome.ACCEPTED, current
        if current_turn is not None and current_turn["state"] == state.value:
            return session, ActivityOutcome.REPLAYED, None
        return session, ActivityOutcome.REJECTED, None

    def _apply_named(
        self,
        sessions: dict[str, dict[str, Any]],
        session_hash: str,
        session: dict[str, Any] | None,
        turn_hash: str,
        state: ActivityState,
        model: str,
        at: datetime,
    ) -> tuple[dict[str, Any] | None, ActivityOutcome, str | None]:
        """Apply an event carrying a real provider turn identity.

        The turn identity resolves the exact turn regardless of delivery
        order: a late start can never reopen a completed turn, and a late
        terminal applies to its own turn — never to a newer one.
        """
        if session is None:
            if state is ActivityState.RUNNING:
                return (
                    self._create_turn(sessions, session_hash, None, state, model, at, turn_hash),
                    ActivityOutcome.ACCEPTED,
                    None,
                )
            return None, ActivityOutcome.REJECTED, None
        existing = session["turns"].get(turn_hash)
        if state is ActivityState.RUNNING:
            if existing is None:
                return (
                    self._create_turn(sessions, session_hash, session, state, model, at, turn_hash),
                    ActivityOutcome.ACCEPTED,
                    None,
                )
            if existing["state"] == ActivityState.RUNNING.value:
                return session, ActivityOutcome.REPLAYED, None
            return session, ActivityOutcome.REJECTED, None
        if existing is None:
            return session, ActivityOutcome.REJECTED, None
        if existing["state"] != ActivityState.RUNNING.value:
            if existing["state"] == state.value:
                return session, ActivityOutcome.REPLAYED, None
            return session, ActivityOutcome.REJECTED, None
        existing["state"] = state.value
        existing["model"] = model
        existing["updated_at"] = at.astimezone(UTC).isoformat()
        return session, ActivityOutcome.ACCEPTED, turn_hash

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
        """Apply one turn-scoped event (locked, read-merge-write).

        Rules (fail closed, never partial):
        - RUNNING with a provider turn identity creates that turn (or
          replays it while it is running); a RUNNING event for an
          already-terminal turn is rejected (a late start can never reopen
          a completed turn).
        - RUNNING without a turn identity replays the session's running
          turn or opens the next derived turn after a terminal one.
        - A terminal event transitions the matching running turn; a
          terminal event for an already-terminal turn with the same state
          is a replay, with a different state it is rejected (out-of-order
          terminal). A terminal event for an unknown turn/session is
          rejected here — the completion-notifier surface is
          ``record_last``.
        - Any accepted terminal event that closes the session's current
          turn updates the runtime's sanitized last event (last events
          never carry RUNNING); a late terminal for a previous turn never
          overwrites a newer turn's display state.
        """
        reference = now or datetime.now(UTC)
        runtime = _validate_runtime(event.runtime)
        state = _validate_state(event.state)
        session_hash = _validate_session_hash(event.session_hash)
        turn_hash = None if event.turn_hash is None else _validate_session_hash(event.turn_hash)
        model = _validate_model(event.model)
        at = _validate_timestamp(event.at, reference, "event timestamp")
        with self._locked():
            data = self._read()
            sessions = data["sessions"].setdefault(runtime.value, {})
            session = sessions.get(session_hash)
            if turn_hash is None:
                session, outcome, applied = self._apply_derived(
                    sessions, session_hash, session, state, model, at
                )
            else:
                session, outcome, applied = self._apply_named(
                    sessions, session_hash, session, turn_hash, state, model, at
                )
            if outcome is ActivityOutcome.REJECTED:
                return outcome  # rejected events never touch the file
            if (
                outcome is ActivityOutcome.ACCEPTED
                and state.terminal
                and applied is not None
                and session is not None
                and session.get("current") == applied
            ):
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
        """Watchdog: expire running turns with no recent update.

        Missing terminal events expire to INTERRUPTED — never to success.
        Returns the runtimes whose state changed (persisted atomically).
        """
        reference = now or datetime.now(UTC)
        changed: list[AgentRuntime] = []
        with self._locked():
            data = self._read()
            for runtime_key, sessions in data["sessions"].items():
                expired: list[tuple[str, dict[str, Any]]] = []
                for session in sessions.values():
                    for turn_hash, turn in session["turns"].items():
                        if turn["state"] != ActivityState.RUNNING.value:
                            continue
                        updated = datetime.fromisoformat(turn["updated_at"])
                        if (reference - updated).total_seconds() > stale_after:
                            turn["state"] = ActivityState.INTERRUPTED.value
                            turn["updated_at"] = reference.isoformat()
                            expired.append((turn_hash, turn))
                if not expired:
                    continue
                data["last_events"][runtime_key] = {
                    "state": ActivityState.INTERRUPTED.value,
                    "model": expired[-1][1]["model"],
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


def derive_runtime_activity(
    data: dict[str, Any],
    now: datetime | None = None,
    terminal_window: float = TERMINAL_WINDOW_SECONDS,
) -> dict[AgentRuntime, RuntimeActivity]:
    """Pure display derivation per runtime (GTK-free, deterministic).

    A runtime is visible when it has running sessions (spinner) or a
    terminal event within ``terminal_window``. Running sessions dominate:
    older terminal events can never hide newer running work. The most
    recent terminal event wins; the model label is the latest sanitized
    one. Run with ``now`` injected for deterministic tests.
    """
    reference = now or datetime.now(UTC)
    result: dict[AgentRuntime, RuntimeActivity] = {}
    for runtime in AgentRuntime:
        sessions = data.get("sessions", {}).get(runtime.value, {})
        running = {
            session_hash: session
            for session_hash, session in sessions.items()
            if any(
                turn["state"] == ActivityState.RUNNING.value for turn in session["turns"].values()
            )
        }
        if running:
            latest: dict[str, Any] | None = None
            for session in running.values():
                for turn in session["turns"].values():
                    if turn["state"] != ActivityState.RUNNING.value:
                        continue
                    if latest is None or turn["updated_at"] > latest["updated_at"]:
                        latest = turn
            assert latest is not None  # a running session always holds a running turn
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
        for session in sessions.values():
            for turn in session["turns"].values():
                if turn["state"] == ActivityState.RUNNING.value:
                    continue
                at = datetime.fromisoformat(turn["updated_at"])
                candidates.append((at, ActivityState(turn["state"]), turn.get("model", "")))
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
