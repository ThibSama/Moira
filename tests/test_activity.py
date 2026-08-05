"""Package 6c/6d — agent activity domain tests.

Deterministic clocks and temporary HOME/XDG. Covers the four states,
fail-closed validation (malformed, naive, future-skewed, oversized,
unknown), idempotent replays, late-start rejection, terminal ordering,
the independent turn lifecycle (provider turn ids and derived ``seq:N``),
concurrency aggregation, the watchdog (never to success), the five-minute
terminal window, store atomicity/locking/corruption tolerance, the
additive v1 → v2 migration and the privacy contract (raw identities,
prompts and paths never persisted).
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
    turn: str | None = None,
) -> ActivityEvent:
    """Build an event; ``turn`` is the provider turn identity (hashed).

    ``None`` exercises the derived-identity fallback (no provider turn id).
    """
    return ActivityEvent(
        runtime, state, _hash(session), model, at, None if turn is None else _hash(turn)
    )


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
    turn = session["turns"][session["current"]]
    assert turn["state"] == "COMPLETED"
    assert turn["started_at"] != turn["updated_at"]
    assert store.snapshot()["last_events"]["claude"]["state"] == "COMPLETED"


def test_replays_are_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.record(_event(state=ActivityState.RUNNING), now=NOW) is ActivityOutcome.ACCEPTED
    replay = store.record(_event(state=ActivityState.RUNNING), now=NOW)
    assert replay is ActivityOutcome.REPLAYED
    session = store.snapshot()["sessions"]["claude"][_hash("sess-1")]
    assert session["turns"][session["current"]]["state"] == "RUNNING"
    # Identical terminal replay stays idempotent.
    assert (
        store.record(_event(state=ActivityState.COMPLETED, at=NOW + timedelta(seconds=1)), now=NOW)
        is ActivityOutcome.ACCEPTED
    )
    assert (
        store.record(_event(state=ActivityState.COMPLETED, at=NOW + timedelta(seconds=1)), now=NOW)
        is ActivityOutcome.REPLAYED
    )


def test_late_start_opens_a_new_derived_turn(tmp_path: Path) -> None:
    """Without a provider turn identity the lifecycle decides: a RUNNING
    event after a terminal turn opens the next derived turn — a new turn
    in the same session must display RUNNING. The completed turn itself is
    never reopened: its record keeps its terminal state."""
    store = _store(tmp_path)
    store.record(_event(state=ActivityState.RUNNING, at=NOW), now=NOW)
    store.record(_event(state=ActivityState.COMPLETED, at=NOW + timedelta(seconds=1)), now=NOW)
    late = store.record(_event(state=ActivityState.RUNNING, at=NOW + timedelta(seconds=2)), now=NOW)
    assert late is ActivityOutcome.ACCEPTED  # a new derived turn, not a reopen
    session = store.snapshot()["sessions"]["claude"][_hash("sess-1")]
    turns = session["turns"]
    assert len(turns) == 2
    assert any(entry["state"] == ActivityState.COMPLETED.value for entry in turns.values())
    assert any(entry["state"] == ActivityState.RUNNING.value for entry in turns.values())


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
    assert session["turns"][session["current"]]["state"] == "INTERRUPTED"
    assert store.snapshot()["last_events"]["claude"]["state"] == "INTERRUPTED"
    # A stale RUNNING is never turned into COMPLETED or FAILED.
    store2 = _store(tmp_path / "c.json")
    store2.record(_event(state=ActivityState.RUNNING, at=started), now=started)
    store2.expire_stale(NOW, stale_after=1800)
    assert (
        store2.snapshot()["sessions"]["claude"][_hash("sess-1")]["turns"][
            store2.snapshot()["sessions"]["claude"][_hash("sess-1")]["current"]
        ]["state"]
        == "INTERRUPTED"
    )


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
    # Session and turn entries carry only the four allowed state keys.
    session = store.snapshot()["sessions"]["claude"][_hash("privacy-session-id")]
    assert set(session) == {"turns", "current", "next_seq"}
    turn = session["turns"][session["current"]]
    assert set(turn) == {"state", "model", "started_at", "updated_at"}


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


# ── Package 6c correction — turn lifecycle (regression) ────────────────────
#
# Claude ``UserPromptSubmit``/``Stop``/``StopFailure`` and Hermes
# ``pre_llm_call``/``post_llm_call`` are turn-level events that repeat
# under one provider session. Each turn transitions independently
# RUNNING → COMPLETED/FAILED/INTERRUPTED; a new turn in the same session
# displays RUNNING; a later failure replaces an earlier success as the
# most recent terminal state; duplicate delivery stays idempotent; a late
# event from a previous turn never overwrites a newer turn.


def test_two_successful_turns_same_session(tmp_path: Path) -> None:
    """Claude: two successful turns using the same session_id."""
    store = _store(tmp_path)
    s = _hash("sess-1")
    assert store.record(_event(turn="t1", at=NOW), now=NOW) is ActivityOutcome.ACCEPTED
    assert (
        store.record(
            _event(state=ActivityState.COMPLETED, turn="t1", at=NOW + timedelta(seconds=1)), now=NOW
        )
        is ActivityOutcome.ACCEPTED
    )
    # A new turn in the same provider session must display RUNNING.
    assert (
        store.record(_event(turn="t2", at=NOW + timedelta(seconds=2)), now=NOW)
        is ActivityOutcome.ACCEPTED
    )
    session = store.snapshot()["sessions"]["claude"][s]
    assert session["turns"][_hash("t2")]["state"] == ActivityState.RUNNING.value
    view = derive_runtime_activity(store.snapshot(), now=NOW + timedelta(seconds=3))
    assert view[AgentRuntime.CLAUDE].state is ActivityState.RUNNING
    assert (
        store.record(
            _event(state=ActivityState.COMPLETED, turn="t2", at=NOW + timedelta(seconds=3)), now=NOW
        )
        is ActivityOutcome.ACCEPTED
    )
    session = store.snapshot()["sessions"]["claude"][s]
    assert session["turns"][_hash("t1")]["state"] == ActivityState.COMPLETED.value
    assert session["turns"][_hash("t2")]["state"] == ActivityState.COMPLETED.value


def test_derived_two_turns_same_session(tmp_path: Path) -> None:
    """Two turns under one session when the payload carries no turn id."""
    store = _store(tmp_path)
    assert store.record(_event(at=NOW), now=NOW) is ActivityOutcome.ACCEPTED
    assert (
        store.record(_event(state=ActivityState.COMPLETED, at=NOW + timedelta(seconds=1)), now=NOW)
        is ActivityOutcome.ACCEPTED
    )
    assert store.record(_event(at=NOW + timedelta(seconds=2)), now=NOW) is ActivityOutcome.ACCEPTED
    turns = store.snapshot()["sessions"]["claude"][_hash("sess-1")]["turns"]
    assert len(turns) == 2
    assert {entry["state"] for entry in turns.values()} == {
        ActivityState.COMPLETED.value,
        ActivityState.RUNNING.value,
    }


def test_successful_then_failing_second_turn(tmp_path: Path) -> None:
    """Claude: a later failure replaces an earlier success as the most
    recent terminal state."""
    store = _store(tmp_path)
    store.record(_event(turn="t1", at=NOW), now=NOW)
    store.record(
        _event(state=ActivityState.COMPLETED, turn="t1", at=NOW + timedelta(seconds=1)), now=NOW
    )
    store.record(_event(turn="t2", at=NOW + timedelta(seconds=2)), now=NOW)
    assert (
        store.record(
            _event(state=ActivityState.FAILED, turn="t2", at=NOW + timedelta(seconds=3)), now=NOW
        )
        is ActivityOutcome.ACCEPTED
    )
    assert store.snapshot()["last_events"]["claude"]["state"] == ActivityState.FAILED.value
    view = derive_runtime_activity(store.snapshot(), now=NOW + timedelta(seconds=4))
    assert view[AgentRuntime.CLAUDE].state is ActivityState.FAILED
    assert view[AgentRuntime.CLAUDE].visible


def test_hermes_two_pre_post_cycles_same_session(tmp_path: Path) -> None:
    """Hermes: two pre_llm_call/post_llm_call cycles, one session."""
    store = _store(tmp_path)
    runtime = AgentRuntime.HERMES
    s = _hash("h-1")
    store.record(_event(runtime=runtime, session="h-1", turn="t1", at=NOW), now=NOW)
    store.record(
        _event(
            runtime=runtime,
            session="h-1",
            state=ActivityState.COMPLETED,
            turn="t1",
            at=NOW + timedelta(seconds=1),
        ),
        now=NOW,
    )
    store.record(
        _event(runtime=runtime, session="h-1", turn="t2", at=NOW + timedelta(seconds=2)), now=NOW
    )
    assert store.snapshot()["sessions"]["hermes"][s]["turns"][_hash("t2")]["state"] == (
        ActivityState.RUNNING.value
    )
    store.record(
        _event(
            runtime=runtime,
            session="h-1",
            state=ActivityState.COMPLETED,
            turn="t2",
            at=NOW + timedelta(seconds=3),
        ),
        now=NOW,
    )
    assert store.snapshot()["last_events"]["hermes"]["state"] == ActivityState.COMPLETED.value


def test_hermes_success_then_interrupted_second_turn(tmp_path: Path) -> None:
    """Hermes: completed first turn, then an interrupted second turn
    (the documented payload: post_llm_call is absent on interruption,
    on_session_end carries interrupted=True)."""
    store = _store(tmp_path)
    runtime = AgentRuntime.HERMES
    store.record(_event(runtime=runtime, turn="t1", at=NOW), now=NOW)
    store.record(
        _event(
            runtime=runtime, state=ActivityState.COMPLETED, turn="t1", at=NOW + timedelta(seconds=1)
        ),
        now=NOW,
    )
    store.record(_event(runtime=runtime, turn="t2", at=NOW + timedelta(seconds=2)), now=NOW)
    store.record(
        _event(
            runtime=runtime,
            state=ActivityState.INTERRUPTED,
            turn="t2",
            at=NOW + timedelta(seconds=3),
        ),
        now=NOW,
    )
    assert store.snapshot()["last_events"]["hermes"]["state"] == ActivityState.INTERRUPTED.value
    view = derive_runtime_activity(store.snapshot(), now=NOW + timedelta(seconds=4))
    assert view[runtime].state is ActivityState.INTERRUPTED


def test_duplicate_start_and_duplicate_terminal_idempotent(tmp_path: Path) -> None:
    """Duplicate delivery of the same start and the same terminal event."""
    store = _store(tmp_path)
    assert store.record(_event(turn="t1", at=NOW), now=NOW) is ActivityOutcome.ACCEPTED
    assert store.record(_event(turn="t1", at=NOW), now=NOW) is ActivityOutcome.REPLAYED
    assert (
        store.record(
            _event(state=ActivityState.COMPLETED, turn="t1", at=NOW + timedelta(seconds=1)), now=NOW
        )
        is ActivityOutcome.ACCEPTED
    )
    assert (
        store.record(
            _event(state=ActivityState.COMPLETED, turn="t1", at=NOW + timedelta(seconds=1)), now=NOW
        )
        is ActivityOutcome.REPLAYED
    )
    session = store.snapshot()["sessions"]["claude"][_hash("sess-1")]
    assert len(session["turns"]) == 1
    assert session["turns"][_hash("t1")]["state"] == ActivityState.COMPLETED.value


def test_late_start_cannot_reopen_completed_turn(tmp_path: Path) -> None:
    """A replayed start for an already-terminal turn is rejected; only a
    genuinely new turn identity opens a new turn."""
    store = _store(tmp_path)
    store.record(_event(turn="t1", at=NOW), now=NOW)
    store.record(
        _event(state=ActivityState.COMPLETED, turn="t1", at=NOW + timedelta(seconds=1)), now=NOW
    )
    late = store.record(_event(turn="t1", at=NOW + timedelta(seconds=2)), now=NOW)
    assert late is ActivityOutcome.REJECTED
    assert (
        store.snapshot()["sessions"]["claude"][_hash("sess-1")]["turns"][_hash("t1")]["state"]
        == ActivityState.COMPLETED.value
    )
    # A genuinely new turn in the same session is accepted.
    assert (
        store.record(_event(turn="t2", at=NOW + timedelta(seconds=3)), now=NOW)
        is ActivityOutcome.ACCEPTED
    )


def test_late_terminal_from_previous_turn_does_not_overwrite(tmp_path: Path) -> None:
    """Claude: a late terminal from turn 1 arriving after turn 2 started
    must not overwrite turn 2 (newer running work stays visible)."""
    store = _store(tmp_path)
    s = _hash("sess-1")
    store.record(_event(turn="t1", at=NOW), now=NOW)
    store.record(_event(turn="t2", at=NOW + timedelta(seconds=1)), now=NOW)
    # Turn 1's Stop arrives late, while turn 2 is running.
    assert (
        store.record(
            _event(state=ActivityState.COMPLETED, turn="t1", at=NOW + timedelta(seconds=2)), now=NOW
        )
        is ActivityOutcome.ACCEPTED
    )
    session = store.snapshot()["sessions"]["claude"][s]
    assert session["turns"][_hash("t1")]["state"] == ActivityState.COMPLETED.value
    assert session["turns"][_hash("t2")]["state"] == ActivityState.RUNNING.value
    view = derive_runtime_activity(store.snapshot(), now=NOW + timedelta(seconds=3))
    assert view[AgentRuntime.CLAUDE].state is ActivityState.RUNNING
    # The late terminal never became the runtime's most recent terminal event.
    assert (
        "last_events" not in store.snapshot()
        or "claude" not in store.snapshot().get("last_events", {})
        or store.snapshot()["last_events"]["claude"]["state"] != ActivityState.COMPLETED.value
    )


def test_out_of_order_terminal_for_same_turn_rejected(tmp_path: Path) -> None:
    """A second, different terminal state for the same turn is rejected."""
    store = _store(tmp_path)
    store.record(_event(turn="t1", at=NOW), now=NOW)
    store.record(
        _event(state=ActivityState.COMPLETED, turn="t1", at=NOW + timedelta(seconds=1)), now=NOW
    )
    assert (
        store.record(
            _event(state=ActivityState.FAILED, turn="t1", at=NOW + timedelta(seconds=2)), now=NOW
        )
        is ActivityOutcome.REJECTED
    )


def test_concurrent_sessions_interleaved_turns(tmp_path: Path) -> None:
    """Multiple concurrent sessions with interleaved turn events remain
    distinguishable; active count stays correct."""
    store = _store(tmp_path)
    store.record(_event(session="a", turn="t1", at=NOW), now=NOW)
    store.record(_event(session="b", turn="t1", at=NOW + timedelta(seconds=1)), now=NOW)
    store.record(
        _event(
            session="a", state=ActivityState.COMPLETED, turn="t1", at=NOW + timedelta(seconds=2)
        ),
        now=NOW,
    )
    # Session "a" opens a second turn while session "b" is still running.
    store.record(_event(session="a", turn="t2", at=NOW + timedelta(seconds=3)), now=NOW)
    view = derive_runtime_activity(store.snapshot(), now=NOW + timedelta(seconds=4))
    claude = view[AgentRuntime.CLAUDE]
    assert claude.state is ActivityState.RUNNING
    assert claude.active_count == 2
    store.record(
        _event(session="a", state=ActivityState.FAILED, turn="t2", at=NOW + timedelta(seconds=4)),
        now=NOW,
    )
    view = derive_runtime_activity(store.snapshot(), now=NOW + timedelta(seconds=5))
    assert view[AgentRuntime.CLAUDE].state is ActivityState.RUNNING  # session b still running
    assert view[AgentRuntime.CLAUDE].active_count == 1


def test_persistence_reload_between_start_and_terminal(tmp_path: Path) -> None:
    """A fresh store instance (hook process) sees a started turn and can
    close it: the file is the single source of truth."""
    path = tmp_path / "activity.json"
    first = ActivityStore(path)
    first.record(_event(session="s1", turn="t1", at=NOW), now=NOW)
    second = ActivityStore(path)
    assert (
        second.record(
            _event(
                session="s1",
                state=ActivityState.COMPLETED,
                turn="t1",
                at=NOW + timedelta(seconds=1),
            ),
            now=NOW,
        )
        is ActivityOutcome.ACCEPTED
    )
    session = second.snapshot()["sessions"]["claude"][_hash("s1")]
    assert session["turns"][_hash("t1")]["state"] == ActivityState.COMPLETED.value
    first.reload()
    assert (
        first.snapshot()["sessions"]["claude"][_hash("s1")]["turns"][_hash("t1")]["state"]
        == ActivityState.COMPLETED.value
    )


def test_no_sensitive_payload_field_appears_in_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hook wrapper consumes full provider payloads but the persisted
    store contains no prompt, response, transcript, error or raw identity."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    from moira.agent_hooks import agent_hook_main

    def fire(payload: dict[str, object], runtime: str) -> None:
        import io
        import sys

        previous = sys.stdin
        sys.stdin = type("FakeInput", (), {"buffer": io.BytesIO(json.dumps(payload).encode())})()
        try:
            assert agent_hook_main([runtime]) == 0
        finally:
            sys.stdin = previous

    secrets = [
        "RAW-SESSION-SECRET",
        "RAW-PROMPT-SECRET",
        "RAW-ANSWER-SECRET",
        "RAW-ERROR-SECRET",
        "RAW-TURN-SECRET",
    ]
    fire(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "RAW-SESSION-SECRET",
            "prompt_id": "RAW-TURN-SECRET",
            "prompt": "RAW-PROMPT-SECRET",
            "session_title": "RAW-PROMPT-SECRET",
            "transcript_path": "/private/RAW-PROMPT-SECRET.jsonl",
        },
        "claude",
    )
    fire(
        {
            "hook_event_name": "StopFailure",
            "session_id": "RAW-SESSION-SECRET",
            "prompt_id": "RAW-TURN-SECRET",
            "error": "RAW-ERROR-SECRET",
            "error_details": {"raw": "RAW-ERROR-SECRET"},
            "last_assistant_message": "RAW-ANSWER-SECRET",
        },
        "claude",
    )
    fire(
        {
            "hook_event_name": "pre_llm_call",
            "session_id": "RAW-SESSION-SECRET",
            "extra": {
                "turn_id": "RAW-TURN-SECRET",
                "user_message": "RAW-PROMPT-SECRET",
                "conversation_history": [{"role": "user", "content": "RAW-PROMPT-SECRET"}],
                "model": "m",
            },
        },
        "hermes",
    )
    raw = (tmp_path / "state" / "moira" / "activity.json").read_text(encoding="utf-8")
    for secret in secrets:
        assert secret not in raw
    # Only the hashed identities appear: session hash and the composite
    # session|turn hash (both SHA-256 digests of the raw values).
    from moira.activity import composite_identity

    turn_digest = composite_identity("RAW-SESSION-SECRET", "RAW-TURN-SECRET")
    assert turn_digest is not None and turn_digest in raw
    assert _hash("RAW-SESSION-SECRET") in raw


# ── Package 6d — activity v1 → v2 migration ────────────────────────────────
#
# A v1 file (flat per-session lifecycle, written by Package 6c) is migrated
# additively on load: every session record is preserved exactly as one
# legacy turn with the derived ``seq:1`` identity; nothing is fabricated
# (a v1 RUNNING record stays a single RUNNING turn), and malformed v1
# shapes fail closed to an empty store — never a partial migration.


def _v1_store_file(tmp_path: Path) -> Path:
    """Write a populated v1 activity file and return its path."""
    path = tmp_path / "activity.json"
    sessions: dict[str, dict[str, object]] = {}
    for session_id, state in (("legacy-ok", "COMPLETED"), ("legacy-run", "RUNNING")):
        sessions[_hash(session_id)] = {
            "state": state,
            "model": "opus",
            "started_at": "2026-08-03T10:00:00+00:00",
            "updated_at": "2026-08-03T10:05:00+00:00",
        }
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "sessions": {"claude": sessions},
                "last_events": {
                    "claude": {
                        "state": "COMPLETED",
                        "model": "opus",
                        "at": "2026-08-03T10:05:00+00:00",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_v1_file_migrates_to_one_legacy_turn_per_session(tmp_path: Path) -> None:
    """A v1 session becomes one v2 session holding exactly one legacy turn
    (``seq:1``), preserving state, model and timestamps; last events and
    the version are migrated too."""
    store = ActivityStore(_v1_store_file(tmp_path))
    data = store.snapshot()
    assert data["version"] == 2
    sessions = data["sessions"]["claude"]
    assert set(sessions) == {_hash("legacy-ok"), _hash("legacy-run")}
    for session_id, expected_state in (("legacy-ok", "COMPLETED"), ("legacy-run", "RUNNING")):
        session = sessions[_hash(session_id)]
        assert session["current"] is not None
        assert session["next_seq"] == 2
        assert len(session["turns"]) == 1  # exactly one legacy turn, never more
        turn = session["turns"][session["current"]]
        assert turn["state"] == expected_state
        assert turn["model"] == "opus"
        assert turn["started_at"] == "2026-08-03T10:00:00+00:00"
        assert turn["updated_at"] == "2026-08-03T10:05:00+00:00"
        # The legacy identity is the deterministic derived seq:1 composite.
        assert session["current"] == hash_identity(f"{_hash(session_id)}\x00seq:1")
    last = data["last_events"]["claude"]
    assert last["state"] == "COMPLETED" and last["at"] == "2026-08-03T10:05:00+00:00"


def test_v1_migration_never_fabricates_running_history(tmp_path: Path) -> None:
    """A v1 RUNNING record stays one RUNNING turn — migration never
    invents terminal history for it."""
    store = ActivityStore(_v1_store_file(tmp_path))
    session = store.snapshot()["sessions"]["claude"][_hash("legacy-run")]
    assert len(session["turns"]) == 1
    assert next(iter(session["turns"].values()))["state"] == "RUNNING"


def test_v1_migration_then_new_turn_appends_legacy_turn_untouched(tmp_path: Path) -> None:
    """After migration, a new turn in the same session appends next to the
    legacy turn — the v1 record is preserved, not replaced."""
    path = _v1_store_file(tmp_path)
    store = ActivityStore(path)
    legacy = _hash("legacy-ok")
    assert (
        store.record(_event(session="legacy-ok", turn="new-turn", at=NOW), now=NOW)
        is ActivityOutcome.ACCEPTED
    )
    session = store.snapshot()["sessions"]["claude"][legacy]
    assert len(session["turns"]) == 2
    legacy_turn = session["turns"][hash_identity(f"{legacy}\x00seq:1")]
    assert legacy_turn["state"] == "COMPLETED"
    assert legacy_turn["updated_at"] == "2026-08-03T10:05:00+00:00"
    assert session["turns"][_hash("new-turn")]["state"] == ActivityState.RUNNING.value
    # The migrated file is persisted back as v2.
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["version"] == 2


def test_v1_migration_derived_sequence_continues_at_seq_2(tmp_path: Path) -> None:
    """A derived (no provider turn id) event after migration continues the
    legacy session's sequence at ``seq:2`` — no collision with ``seq:1``."""
    store = ActivityStore(_v1_store_file(tmp_path))
    assert store.record(_event(session="legacy-ok", at=NOW), now=NOW) is ActivityOutcome.ACCEPTED
    session = store.snapshot()["sessions"]["claude"][_hash("legacy-ok")]
    assert len(session["turns"]) == 2
    assert session["next_seq"] == 3
    assert hash_identity(f"{_hash('legacy-ok')}\x00seq:2") in session["turns"]


def test_v1_migration_malformed_shapes_fail_closed(tmp_path: Path) -> None:
    """A v1 file outside the documented shape fails closed to an empty
    store — never a partial migration."""
    session_id = _hash("sess")
    cases = [
        # last_events missing entirely.
        {"version": 1, "sessions": {"claude": {session_id: {"state": "COMPLETED"}}}},
        # last_events carries RUNNING (invalid in v1 as in v2).
        {
            "version": 1,
            "sessions": {"claude": {}},
            "last_events": {
                "claude": {"state": "RUNNING", "model": "m", "at": "2026-08-03T10:00:00+00:00"}
            },
        },
        # Unknown runtime key.
        {
            "version": 1,
            "sessions": {"slack": {}},
            "last_events": {},
        },
    ]
    for index, payload in enumerate(cases):
        path = tmp_path / f"bad-{index}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        store = ActivityStore(path)
        assert store.snapshot()["sessions"] == {}
        assert store.snapshot()["last_events"] == {}
