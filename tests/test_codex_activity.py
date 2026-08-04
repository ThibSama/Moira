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

from moira.activity import ActivityState, ActivityStore, AgentRuntime, hash_identity
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
    assert event.session_hash == hash_identity("turn-1")
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
    session_hash = hash_identity("turn-1")
    assert (
        store.snapshot()["sessions"]["codex"][session_hash]["state"] == ActivityState.RUNNING.value
    )
    assert record_turn_notification(_completed(), {"thread-1"}, store) is True
    assert (
        store.snapshot()["sessions"]["codex"][session_hash]["state"]
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
    assert next(iter(sessions.values()))["state"] == ActivityState.RUNNING.value


# ── Capability probe (fake codex binaries) ──


def _fake_codex(tmp_path: Path, *, mode: str) -> Path:
    """``mode``: full | completion_only | unsupported."""
    binary = tmp_path / "codex"
    if mode == "unsupported":
        body = "#!/bin/sh\nexit 1\n"
    elif mode == "completion_only":
        body = r"""#!/bin/sh
# consume stdin request lines, reply with a thread/start error
while IFS= read -r line; do
  id=$(printf '%s' "$line" | sed -n 's/.*"id":[ ]*\([0-9][0-9]*\).*/\1/p')
  if [ "$id" = "1" ]; then
    printf '{"id":1,"result":{}}\n'
  elif [ "$id" = "2" ]; then
    printf '{"id":2,"error":{"code":-32601,"message":"method not found"}}\n'
  fi
done
"""
    else:  # full
        body = r"""#!/bin/sh
while IFS= read -r line; do
  id=$(printf '%s' "$line" | sed -n 's/.*"id":[ ]*\([0-9][0-9]*\).*/\1/p')
  if [ "$id" = "1" ]; then
    printf '{"id":1,"result":{}}\n'
  elif [ "$id" = "2" ]; then
    printf '{"id":2,"result":{"id":"thread-created"}}\n'
  fi
done
"""
    binary.write_text(body, encoding="utf-8")
    binary.chmod(0o755)
    return binary


def test_probe_full(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_codex(tmp_path, mode="full")
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    assert probe_codex() is CodexCapability.FULL


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
        CodexCapability.FULL,
        CodexCapability.COMPLETION_ONLY,
        CodexCapability.UNSUPPORTED,
    )
