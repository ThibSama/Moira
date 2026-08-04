"""Package 6c — agent activity domain tests.

Deterministic clocks and temporary HOME/XDG. Covers the four states,
fail-closed validation (malformed, naive, future-skewed, oversized,
unknown), idempotent replays, late-start rejection, terminal ordering,
concurrency aggregation, the watchdog (never to success), the five-minute
terminal window, store atomicity/locking/corruption tolerance and the
privacy contract (raw identities, prompts and paths never persisted).
"""

from __future__ import annotations

import json
import stat
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from moira.activity import (
    MAX_FUTURE_SKEW_SECONDS,
    MAX_MODEL_LABEL_LEN,
    MAX_SESSIONS_PER_RUNTIME,
    TERMINAL_WINDOW_SECONDS,
    ActivityEvent,
    ActivityOutcome,
    ActivityState,
    ActivityStore,
    AgentRuntime,
    LastActivityEvent,
    derive_runtime_activity,
    hash_identity,
    sanitize_model,
    validate_identity,
)

NOW = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)


def _hash(raw: str) -> str:
    return hash_identity(raw)


def _event(
    runtime: AgentRuntime = AgentRuntime.CLAUDE,
    state: ActivityState = ActivityState.RUNNING,
    session: str = "sess-1",
    model: str = "opus",
    at: datetime = NOW,
) -> ActivityEvent:
    return ActivityEvent(runtime, state, _hash(session), model, at)


def _store(tmp_path: Path) -> ActivityStore:
    return ActivityStore(tmp_path / "activity.json")


def test_states_are_exactly_the_four_contract_states() -> None:
    assert {s.value for s in ActivityState} == {
        "RUNNING",
        "COMPLETED",
        "FAILED",
        "INTERRUPTED",
    }


def test_unknown_runtime_and_state_are_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.record(
            ActivityEvent(AgentRuntime("bogus"), ActivityState.RUNNING, _hash("s"), "", NOW)
        )
    with pytest.raises(ValueError):
        store.record(
            ActivityEvent(AgentRuntime.CLAUDE, ActivityState("BOGUS"), _hash("s"), "", NOW)
        )


def test_malformed_session_hash_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.record(
            ActivityEvent(AgentRuntime.CLAUDE, ActivityState.RUNNING, "not-a-hash", "", NOW)
        )


def test_naive_timestamp_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    naive = datetime(2026, 8, 4, 12, 0, 0)  # no tzinfo
    with pytest.raises(ValueError):
        store.record(
            ActivityEvent(AgentRuntime.CLAUDE, ActivityState.RUNNING, _hash("s"), "", naive)
        )


def test_future_skewed_timestamp_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    future = NOW + timedelta(seconds=MAX_FUTURE_SKEW_SECONDS + 1)
    with pytest.raises(ValueError):
        store.record(_event(at=future), now=NOW)


def test_oversized_identity_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        validate_identity("x" * 257)
    assert len(validate_identity("ok")) == 64


def test_oversized_model_label_is_truncated() -> None:
    long_model = "model-" + "x" * 200
    cleaned = sanitize_model(long_model)
    assert len(cleaned) == MAX_MODEL_LABEL_LEN
    assert sanitize_model("") == ""
    assert sanitize_model(42) == ""
    assert sanitize_model("  spaced  out  ") == "spaced out"


def test_start_then_terminal_sequence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.record(_event(state=ActivityState.RUNNING), now=NOW) is ActivityOutcome.ACCEPTED
    assert (
        store.record(_event(state=ActivityState.COMPLETED, at=NOW + timedelta(seconds=1)), now=NOW)
        is ActivityOutcome.ACCEPTED
    )
    session = store.snapshot()["sessions"]["claude"][_hash("sess-1")]
    assert session["state"] == "COMPLETED"
    assert session["started_at"] != session["updated_at"]
    assert store.snapshot()["last_events"]["claude"]["state"] == "COMPLETED"


def test_replays_are_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.record(_event(state=ActivityState.RUNNING), now=NOW) is ActivityOutcome.ACCEPTED
    replay = store.record(_event(state=ActivityState.RUNNING), now=NOW)
    assert replay is ActivityOutcome.REPLAYED
    session = store.snapshot()["sessions"]["claude"][_hash("sess-1")]
    assert session["state"] == "RUNNING"
    # Identical terminal replay stays idempotent.
    assert (
        store.record(_event(state=ActivityState.COMPLETED, at=NOW + timedelta(seconds=1)), now=NOW)
        is ActivityOutcome.ACCEPTED
    )
    assert (
        store.record(_event(state=ActivityState.COMPLETED, at=NOW + timedelta(seconds=1)), now=NOW)
        is ActivityOutcome.REPLAYED
    )


def test_late_start_cannot_replace_terminal_event(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record(_event(state=ActivityState.RUNNING, at=NOW), now=NOW)
    store.record(_event(state=ActivityState.COMPLETED, at=NOW + timedelta(seconds=1)), now=NOW)
    late = store.record(_event(state=ActivityState.RUNNING, at=NOW + timedelta(seconds=2)), now=NOW)
    assert late is ActivityOutcome.REJECTED
    assert store.snapshot()["sessions"]["claude"][_hash("sess-1")]["state"] == "COMPLETED"


def test_out_of_order_terminal_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record(_event(state=ActivityState.RUNNING), now=NOW)
    store.record(_event(state=ActivityState.COMPLETED, at=NOW + timedelta(seconds=1)), now=NOW)
    assert (
        store.record(_event(state=ActivityState.FAILED, at=NOW + timedelta(seconds=2)), now=NOW)
        is ActivityOutcome.REJECTED
    )
    # FAILED after INTERRUPTED also rejected.
    store2 = _store(tmp_path / "b.json")
    store2.record(_event(state=ActivityState.RUNNING), now=NOW)
    store2.record(_event(state=ActivityState.INTERRUPTED, at=NOW + timedelta(seconds=1)), now=NOW)
    assert (
        store2.record(_event(state=ActivityState.FAILED, at=NOW + timedelta(seconds=2)), now=NOW)
        is ActivityOutcome.REJECTED
    )


def test_terminal_for_unknown_session_rejected_but_last_event_ok(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert (
        store.record(_event(state=ActivityState.COMPLETED, session="never-started"), now=NOW)
        is ActivityOutcome.REJECTED
    )
    assert store.snapshot()["sessions"] == {}
    # The completion-notifier surface records the terminal last event.
    assert (
        store.record_last(
            LastActivityEvent(AgentRuntime.CODEX, ActivityState.COMPLETED, "", NOW),
            now=NOW,
        )
        is ActivityOutcome.ACCEPTED
    )
    assert store.snapshot()["last_events"]["codex"]["state"] == "COMPLETED"


def test_record_last_never_accepts_running(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.record_last(
            LastActivityEvent(AgentRuntime.CODEX, ActivityState.RUNNING, "", NOW), now=NOW
        )


def test_concurrent_sessions_aggregate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record(_event(session="a", at=NOW), now=NOW)
    store.record(_event(session="b", at=NOW + timedelta(seconds=1)), now=NOW)
    store.record(_event(session="c", at=NOW + timedelta(seconds=2)), now=NOW)
    view = derive_runtime_activity(store.snapshot(), now=NOW + timedelta(seconds=3))
    claude = view[AgentRuntime.CLAUDE]
    assert claude.visible and claude.state is ActivityState.RUNNING
    assert claude.active_count == 3
    assert claude.model == "opus"


def test_derive_prefers_most_recent_terminal(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record(_event(session="a", state=ActivityState.RUNNING, at=NOW), now=NOW)
    store.record(
        _event(session="a", state=ActivityState.COMPLETED, at=NOW + timedelta(seconds=1)), now=NOW
    )
    store.record(
        _event(session="b", state=ActivityState.RUNNING, at=NOW + timedelta(seconds=2)), now=NOW
    )
    store.record(
        _event(session="b", state=ActivityState.FAILED, at=NOW + timedelta(seconds=3)), now=NOW
    )
    view = derive_runtime_activity(store.snapshot(), now=NOW + timedelta(seconds=4))
    claude = view[AgentRuntime.CLAUDE]
    assert claude.state is ActivityState.FAILED
    assert claude.visible


def test_terminal_window_exactly_five_minutes_then_hidden(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record(_event(state=ActivityState.RUNNING, at=NOW), now=NOW)
    ended = NOW + timedelta(seconds=10)
    store.record(_event(state=ActivityState.COMPLETED, at=ended), now=NOW)
    inside = derive_runtime_activity(
        store.snapshot(), now=ended + timedelta(seconds=TERMINAL_WINDOW_SECONDS)
    )
    assert inside[AgentRuntime.CLAUDE].visible
    assert inside[AgentRuntime.CLAUDE].state is ActivityState.COMPLETED
    after = derive_runtime_activity(
        store.snapshot(), now=ended + timedelta(seconds=TERMINAL_WINDOW_SECONDS + 1)
    )
    assert not after[AgentRuntime.CLAUDE].visible


def test_watchdog_expires_stale_to_interrupted_never_success(tmp_path: Path) -> None:
    store = _store(tmp_path)
    started = NOW - timedelta(seconds=3600)
    store.record(_event(state=ActivityState.RUNNING, at=started), now=started)
    changed = store.expire_stale(NOW, stale_after=1800)
    assert changed == [AgentRuntime.CLAUDE]
    session = store.snapshot()["sessions"]["claude"][_hash("sess-1")]
    assert session["state"] == "INTERRUPTED"
    assert store.snapshot()["last_events"]["claude"]["state"] == "INTERRUPTED"
    # A stale RUNNING is never turned into COMPLETED or FAILED.
    store2 = _store(tmp_path / "c.json")
    store2.record(_event(state=ActivityState.RUNNING, at=started), now=started)
    store2.expire_stale(NOW, stale_after=1800)
    assert store2.snapshot()["sessions"]["claude"][_hash("sess-1")]["state"] == "INTERRUPTED"


def test_watchdog_leaves_fresh_running_alone(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record(_event(state=ActivityState.RUNNING, at=NOW - timedelta(seconds=600)), now=NOW)
    assert store.expire_stale(NOW, stale_after=1800) == []


def test_store_is_atomic_0600_with_lock_file(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record(_event(state=ActivityState.RUNNING), now=NOW)
    mode = stat.S_IMODE(store.path.stat().st_mode)
    assert mode == 0o600
    lock_mode = stat.S_IMODE((tmp_path / "activity.json.lock").stat().st_mode)
    assert lock_mode == 0o600
    assert (tmp_path / "activity.json.lock").exists()
    # No leftover temp files.
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".activity.json.")]
    assert leftovers == []


def test_concurrent_writers_do_not_lose_updates(tmp_path: Path) -> None:
    store = _store(tmp_path)
    errors: list[Exception] = []

    def worker(index: int) -> None:
        try:
            for step in range(5):
                session = f"w{index}-{step}"
                store.record(
                    ActivityEvent(
                        AgentRuntime.CLAUDE,
                        ActivityState.RUNNING,
                        _hash(session),
                        "m",
                        NOW + timedelta(seconds=index),
                    ),
                    now=NOW,
                )
        except Exception as exc:  # pragma: no cover - defensive
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    store.reload()
    sessions = store.snapshot()["sessions"]["claude"]
    assert len(sessions) == 20


def test_corrupt_file_falls_back_to_empty(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record(_event(state=ActivityState.RUNNING), now=NOW)
    store.path.write_text("{not json", encoding="utf-8")
    store.reload()
    assert store.snapshot()["sessions"] == {}


def test_deleted_file_tolerated(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record(_event(state=ActivityState.RUNNING), now=NOW)
    store.path.unlink()
    store.reload()
    assert store.snapshot()["sessions"] == {}
    # A subsequent record recreates the file.
    assert store.record(_event(session="new"), now=NOW) is ActivityOutcome.ACCEPTED
    assert store.path.exists()


def test_oversized_file_falls_back_to_empty(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("x" * (1024 * 1024 + 1), encoding="utf-8")
    store.reload()
    assert store.snapshot()["sessions"] == {}


def test_invalid_last_event_running_in_file_falls_back_to_empty(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record(_event(state=ActivityState.RUNNING), now=NOW)
    data = json.loads(store.path.read_text(encoding="utf-8"))
    data["last_events"]["claude"] = {"state": "RUNNING", "model": "", "at": NOW.isoformat()}
    store.path.write_text(json.dumps(data), encoding="utf-8")
    store.reload()
    assert store.snapshot()["sessions"] == {}


def test_privacy_contract_raw_identities_never_persisted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record(_event(state=ActivityState.RUNNING, session="privacy-session-id"), now=NOW)
    store.record(
        _event(
            state=ActivityState.COMPLETED,
            session="privacy-session-id",
            at=NOW + timedelta(seconds=1),
        ),
        now=NOW,
    )
    raw = store.path.read_text(encoding="utf-8")
    assert "privacy-session-id" not in raw
    assert _hash("privacy-session-id") in raw
    # Only the four allowed keys per session entry.
    session = store.snapshot()["sessions"]["claude"][_hash("privacy-session-id")]
    assert set(session) == {"state", "model", "started_at", "updated_at"}


def test_sessions_pruned_to_bound(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for index in range(MAX_SESSIONS_PER_RUNTIME + 50):
        store.record(
            _event(session=f"s{index:04d}", at=NOW + timedelta(seconds=index)),
            now=NOW,
        )
        store.record(
            _event(
                session=f"s{index:04d}",
                state=ActivityState.COMPLETED,
                at=NOW + timedelta(seconds=index + 1),
            ),
            now=NOW,
        )
    assert len(store.snapshot()["sessions"]["claude"]) <= MAX_SESSIONS_PER_RUNTIME


def test_clear_resets_store(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record(_event(state=ActivityState.RUNNING), now=NOW)
    store.clear()
    assert store.snapshot()["sessions"] == {}
    assert store.snapshot()["last_events"] == {}


def test_file_mode_0600_after_recreate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record(_event(state=ActivityState.RUNNING), now=NOW)
    store.path.chmod(0o644)
    store.record(_event(session="b"), now=NOW)
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def test_hash_is_stable_and_hex(tmp_path: Path) -> None:
    first = hash_identity("session-xyz")
    assert first == hash_identity("session-xyz")
    assert len(first) == 64
    int(first, 16)
    assert hash_identity("a") != hash_identity("b")


def test_env_xdg_state_home_is_honoured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    from moira.activity import activity_path

    assert activity_path() == tmp_path / "state" / "moira" / "activity.json"
