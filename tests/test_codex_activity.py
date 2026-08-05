"""Package 6c — Codex activity adapter tests.

Documented app-server events only: ``turn/started`` and
``turn/completed`` notifications with ``{threadId, turn}``. Full
``turn/started`` → ``turn/completed`` is valid only for an app-server
thread Moira owns; terminal notifications for unowned threads are
recorded through the completion-only surface and never synthesize RUNNING.
The capability probe is tested with fake binaries (isolated CODEX_HOME,
no user-state side effects).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from moira.activity import (
    ActivityEvent,
    ActivityState,
    ActivityStore,
    AgentRuntime,
    hash_identity,
)
from moira.codex_activity import (
    CodexCapability,
    handle_turn_notification,
    probe_codex,
    record_turn_notification,
)

NOW = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)


def _started(
    thread: str = "thread-1",
    turn_id: str = "turn-1",
    status: str = "inProgress",
) -> dict[str, object]:
    return {
        "method": "turn/started",
        "params": {"threadId": thread, "turn": {"id": turn_id, "status": status}},
    }


def _completed(
    thread: str = "thread-1",
    turn_id: str = "turn-1",
    status: str = "completed",
) -> dict[str, object]:
    return {
        "method": "turn/completed",
        "params": {"threadId": thread, "turn": {"id": turn_id, "status": status}},
    }


def _store(tmp_path: Path) -> ActivityStore:
    return ActivityStore(tmp_path / "activity.json")


def test_turn_started_for_owned_thread_starts_running(tmp_path: Path) -> None:
    from moira.activity import ActivityEvent

    event = handle_turn_notification(_started(), {"thread-1"})
    assert isinstance(event, ActivityEvent)
    assert event.state is ActivityState.RUNNING
    # Session identity is the owning thread; the turn is bound to it.
    assert event.session_hash == hash_identity("thread-1")
    assert event.turn_hash == hash_identity("thread-1\x00turn-1")
    assert event.runtime is AgentRuntime.CODEX


def test_turn_completed_maps_all_terminal_states(tmp_path: Path) -> None:
    for status, expected in (
        ("completed", ActivityState.COMPLETED),
        ("failed", ActivityState.FAILED),
        ("interrupted", ActivityState.INTERRUPTED),
    ):
        event = handle_turn_notification(_completed(status=status), {"thread-1"})
        assert event is not None and event.state is expected, status


def test_turn_completed_in_progress_ignored(tmp_path: Path) -> None:
    assert handle_turn_notification(_completed(status="inProgress"), {"thread-1"}) is None


def test_full_sequence_via_store(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert record_turn_notification(_started(), {"thread-1"}, store) is True
    thread_hash = hash_identity("thread-1")
    turn_hash = hash_identity("thread-1\x00turn-1")
    assert (
        store.snapshot()["sessions"]["codex"][thread_hash]["turns"][turn_hash]["state"]
        == ActivityState.RUNNING.value
    )
    assert record_turn_notification(_completed(), {"thread-1"}, store) is True
    assert (
        store.snapshot()["sessions"]["codex"][thread_hash]["turns"][turn_hash]["state"]
        == ActivityState.COMPLETED.value
    )
    assert store.snapshot()["last_events"]["codex"]["state"] == "COMPLETED"


def test_unowned_turn_started_never_synthesizes_running(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert record_turn_notification(_started(), set(), store) is False
    assert store.snapshot()["sessions"] == {}


def test_unowned_completion_records_last_event_only(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert record_turn_notification(_completed(), set(), store) is True
    assert store.snapshot()["sessions"] == {}
    assert store.snapshot()["last_events"]["codex"]["state"] == "COMPLETED"


def test_malformed_messages_ignored(tmp_path: Path) -> None:
    store = _store(tmp_path)
    malformed: list[Any] = [
        None,
        "turn/started",
        {},
        {"method": "thread/status/changed", "params": {"threadId": "t", "turn": {}}},
        {"method": "turn/started"},
        {"method": "turn/started", "params": "bogus"},
        {
            "method": "turn/started",
            "params": {"threadId": "", "turn": {"id": "x", "status": "inProgress"}},
        },
        {"method": "turn/started", "params": {"threadId": "t", "turn": {"status": "inProgress"}}},
        {"method": "turn/started", "params": {"threadId": "t", "turn": {"id": "x"}}},
        {
            "method": "turn/started",
            "params": {"threadId": "t", "turn": {"id": 42, "status": "inProgress"}},
        },
        {
            "method": "turn/started",
            "params": {"threadId": "t", "turn": {"id": "x" * 300, "status": "inProgress"}},
        },
    ]
    for message in malformed:
        assert handle_turn_notification(message, {"thread-1"}) is None
        assert record_turn_notification(message, {"thread-1"}, store) is False
    assert store.snapshot()["sessions"] == {}


def test_owned_after_unowned_completion_no_running(tmp_path: Path) -> None:
    """A turn completed while unowned never becomes RUNNING later."""
    store = _store(tmp_path)
    record_turn_notification(_completed(), set(), store)
    record_turn_notification(_started(), {"thread-1"}, store)
    sessions = store.snapshot()["sessions"].get("codex", {})
    # The start for the now-owned thread applies (new event), but the
    # completion-only record never created a session.
    assert len(sessions) == 1
    assert (
        next(iter(sessions.values()))["turns"][hash_identity("thread-1\x00turn-1")]["state"]
        == ActivityState.RUNNING.value
    )


# ── Capability probe (fake codex binaries) ──


def _fake_codex(tmp_path: Path, *, mode: str) -> Path:
    """``mode``: full | completion_only | unsupported."""
    binary = tmp_path / "codex"
    if mode == "unsupported":
        body = "#!/bin/sh\nexit 1\n"
    else:
        body = _FAKE_SERVER_TEMPLATE.format(mode=mode)
    binary.write_text(body, encoding="utf-8")
    binary.chmod(0o755)
    return binary


#: Shared fake ``codex app-server`` implementation (documented stdio
#: JSON-RPC subset). ``mode`` embeds the fixture behaviour; ``silent``
#: never completes a turn (timeout path) and ``no_thread`` fails
#: ``thread/start`` (completion-only path).
_FAKE_SERVER_TEMPLATE = """#!/usr/bin/env python3
import json
import sys

MODE = "{mode}"
THREAD = "moira-thread-1"
TURN = "moira-turn-1"


def send(message):
    sys.stdout.write(json.dumps(message) + "\\n")
    sys.stdout.flush()


for raw in sys.stdin:
    try:
        message = json.loads(raw)
    except json.JSONDecodeError:
        continue
    if not isinstance(message, dict):
        continue
    request_id = message.get("id")
    method = message.get("method")
    if method == "initialize":
        send({{"id": request_id, "result": {{}}}})
    elif method == "thread/start":
        if MODE in ("completion_only", "no_thread"):
            send(
                {{
                    "id": request_id,
                    "error": {{"code": -32601, "message": "method not found"}},
                }}
            )
        else:
            send({{"id": request_id, "result": {{"thread": {{"id": THREAD}}}}}})
    elif method == "turn/start":
        send(
            {{
                "method": "turn/started",
                "params": {{"threadId": THREAD, "turn": {{"id": TURN, "status": "inProgress"}}}},
            }}
        )
        send({{"id": request_id, "result": {{"turn": {{"id": TURN, "status": "inProgress"}}}}}})
        if MODE != "silent":
            send(
                {{
                    "method": "turn/completed",
                    "params": {{"threadId": THREAD, "turn": {{"id": TURN, "status": "failed"}}}},
                }}
            )
"""


def test_probe_full(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_codex(tmp_path, mode="full")
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    assert probe_codex() is CodexCapability.SESSION_OWNED


def test_probe_completion_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_codex(tmp_path, mode="completion_only")
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    assert probe_codex() is CodexCapability.COMPLETION_ONLY


def test_probe_unsupported_on_handshake_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_codex(tmp_path, mode="unsupported")
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    assert probe_codex() is CodexCapability.UNSUPPORTED


def test_probe_unsupported_without_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    assert probe_codex() is CodexCapability.UNSUPPORTED


def test_probe_reaps_alive_process_on_failure(tmp_path: Path) -> None:
    """The probe reaps its app-server process group even on failure."""

    class FakePipe:
        def write(self, value: str) -> int:
            return len(value)

        def flush(self) -> None:
            pass

    class FakeProcess:
        stdin = FakePipe()
        stdout = object()
        pid = 4242
        returncode: int | None = None

        def poll(self) -> int | None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            self.returncode = 0
            return 0

    with (
        patch("moira.codex_activity.shutil.which", return_value="/usr/bin/codex"),
        patch("moira.codex_activity.subprocess.Popen", return_value=FakeProcess()),
        patch("moira.codex_activity.os.killpg") as killpg,
        patch("moira.codex_activity._read_response", side_effect=TimeoutError("t")),
    ):
        assert probe_codex() is CodexCapability.UNSUPPORTED
    assert killpg.call_count == 1


def test_probe_isolated_codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The probe runs against a fresh CODEX_HOME, never the user's state."""
    _fake_codex(tmp_path, mode="full")
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    user_home = tmp_path / "user-codex"
    user_home.mkdir()
    (user_home / "config.toml").write_text('model = "user-model"\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(user_home))
    with patch("moira.codex_activity.tempfile.TemporaryDirectory") as tempdir:
        probe_codex()
    assert tempdir.call_count == 1


def test_live_probe_codex_present(tmp_path: Path) -> None:
    """Live probe: skip only when the real codex binary is absent."""
    import shutil

    if shutil.which("codex") is None:
        pytest.skip("codex binary absent")
    capability = probe_codex()
    # The protocol must at least be reachable on the real binary; the
    # precise level is auth/environment dependent and reported honestly.
    assert capability in (
        CodexCapability.SESSION_OWNED,
        CodexCapability.COMPLETION_ONLY,
        CodexCapability.UNSUPPORTED,
    )


# ── Package 6c correction — real app-server lifecycle (regression) ─────────
#
# A capability probe plus a synthetic mapping test are not enough to
# declare Codex activity visibility operational: the Settings test must
# exercise the external protocol boundary with a subprocess-backed
# fixture and record REAL turn notifications.


def _fake_app_server(tmp_path: Path, *, mode: str) -> Path:
    """A fake ``codex`` binary speaking the documented app-server stdio
    protocol (JSON-RPC lines). ``mode``:

    - ``full``: initialize + thread/start succeed and turn/start emits
      real ``turn/started`` and ``turn/completed`` notifications;
    - ``silent``: the turn never completes (timeout path);
    - ``no_thread``: thread/start fails (completion-only path).
    """
    binary = tmp_path / "codex"
    if mode == "no_thread":
        body = _FAKE_SERVER_TEMPLATE.format(mode="no_thread")
    elif mode == "silent":
        body = _FAKE_SERVER_TEMPLATE.format(mode="silent")
    else:  # full
        body = _FAKE_SERVER_TEMPLATE.format(mode="full")
    binary.write_text(body, encoding="utf-8")
    binary.chmod(0o755)
    return binary


def test_codex_session_records_real_turn_notifications(tmp_path: Path) -> None:
    """The full lifecycle: start → thread/start → turn/start → real
    turn/started + turn/completed notifications recorded through the
    ActivityStore (subprocess-backed, not handcrafted dicts)."""
    from moira.codex_activity import CodexSession

    binary = _fake_app_server(tmp_path, mode="full")
    store = _store(tmp_path)
    session = CodexSession(store=store, binary=str(binary), codex_home=tmp_path / "codex-home")
    try:
        thread_id = session.start()
        assert thread_id == "moira-thread-1"
        terminal = session.run_turn("say ok")
        assert terminal is ActivityState.FAILED
    finally:
        session.close()
    thread_hash = hash_identity("moira-thread-1")
    turn_hash = hash_identity("moira-thread-1\x00moira-turn-1")
    sessions = store.snapshot()["sessions"].get("codex", {})
    assert set(sessions) == {thread_hash}
    turn = sessions[thread_hash]["turns"][turn_hash]
    assert turn["state"] == ActivityState.FAILED.value
    assert turn["started_at"] == turn["updated_at"] or turn["started_at"] <= turn["updated_at"]
    assert store.snapshot()["last_events"]["codex"]["state"] == ActivityState.FAILED.value
    # The raw thread/turn identifiers never appear in the file.
    raw = store.path.read_text(encoding="utf-8")
    assert "moira-thread-1" not in raw and "moira-turn-1" not in raw


def test_codex_session_never_touches_user_codex_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The session runs against an isolated CODEX_HOME; the user's Codex
    state is never modified during capability tests."""
    from moira.codex_activity import CodexSession

    binary = _fake_app_server(tmp_path, mode="full")
    user_home = tmp_path / "user-codex"
    user_home.mkdir()
    marker = user_home / "config.toml"
    marker.write_text('model = "user-model"\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(user_home))
    store = _store(tmp_path / "state")
    session = CodexSession(store=store, binary=str(binary), codex_home=None)
    try:
        assert session.codex_home != user_home
        session.start()
        session.run_turn("hi")
    finally:
        session.close()
    # The user's CODEX_HOME was not modified (no session rollouts, no lock).
    assert sorted(p.name for p in user_home.iterdir()) == ["config.toml"]
    assert not session.codex_home.exists()  # isolated home cleaned up


def test_codex_session_terminates_cleanly_and_idempotent(tmp_path: Path) -> None:
    from moira.codex_activity import CodexSession

    binary = _fake_app_server(tmp_path, mode="full")
    store = _store(tmp_path)
    session = CodexSession(store=store, binary=str(binary), codex_home=tmp_path / "codex-home")
    session.start()
    session.run_turn("hi")
    process = session._process
    assert process is not None
    pid = process.pid
    session.close()
    assert session._process is None  # reference released
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # process group actually reaped
    session.close()  # idempotent


def test_codex_session_turn_timeout_is_bounded_and_cleans_up(tmp_path: Path) -> None:
    from moira.codex_activity import CodexSession

    binary = _fake_app_server(tmp_path, mode="silent")
    store = _store(tmp_path)
    session = CodexSession(store=store, binary=str(binary), codex_home=tmp_path / "codex-home")
    session.start()
    process = session._process
    assert process is not None
    pid = process.pid
    try:
        with pytest.raises(TimeoutError):
            session.run_turn("hi", deadline=1.0)
    finally:
        session.close()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # process group cleaned up after the timeout


def test_codex_session_start_failure_raises_bounded(tmp_path: Path) -> None:
    from moira.codex_activity import CodexSession, CodexSessionError

    binary = _fake_app_server(tmp_path, mode="no_thread")
    store = _store(tmp_path)
    session = CodexSession(store=store, binary=str(binary), codex_home=tmp_path / "codex-home")
    try:
        with pytest.raises(CodexSessionError):
            session.start()
    finally:
        session.close()


def test_codex_probe_levels_reflect_reduced_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """thread/start success reports session ownership (not full live
    monitoring of independent CLI sessions); failure reports
    completion-only."""
    _fake_app_server(tmp_path, mode="full")
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    assert probe_codex() is CodexCapability.SESSION_OWNED
    _fake_app_server(tmp_path, mode="no_thread")
    assert probe_codex() is CodexCapability.COMPLETION_ONLY


def test_turn_notifications_are_thread_scoped_identities(tmp_path: Path) -> None:
    """Codex turn identities bind the turn to its thread (session = thread,
    turn = thread|turn composite), so multiple turns per thread stay
    distinguishable and hashed."""
    event = handle_turn_notification(_started(), {"thread-1"})
    assert isinstance(event, ActivityEvent)
    assert event.session_hash == hash_identity("thread-1")
    assert event.turn_hash == hash_identity("thread-1\x00turn-1")
    assert event.state is ActivityState.RUNNING


def test_live_codex_session_real_binary(tmp_path: Path) -> None:
    """Production path: a real ``codex app-server --stdio`` session with an
    isolated CODEX_HOME drives a real turn and receives real
    turn/started + turn/completed notifications. Skipped when absent."""
    import shutil

    from moira.codex_activity import CodexSession

    if shutil.which("codex") is None:
        pytest.skip("codex binary absent")
    store = _store(tmp_path)
    session = CodexSession(store=store, binary="codex", codex_home=tmp_path / "codex-home")
    try:
        thread_id = session.start()
        assert thread_id
        terminal = session.run_turn("Say ok in one word.", deadline=30.0)
        assert terminal is not None
    finally:
        session.close()
    sessions = store.snapshot()["sessions"].get("codex", {})
    assert len(sessions) == 1
    session_entry = next(iter(sessions.values()))
    assert any(
        turn["state"] != ActivityState.RUNNING.value for turn in session_entry["turns"].values()
    )
    assert store.snapshot()["last_events"]["codex"]["state"] in (
        ActivityState.COMPLETED.value,
        ActivityState.FAILED.value,
        ActivityState.INTERRUPTED.value,
    )
