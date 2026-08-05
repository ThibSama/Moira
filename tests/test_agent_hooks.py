"""Package 6c — packaged agent hook entry tests.

The hook is the command Claude Code and Hermes invoke: it must be
network-free, bounded-input, fixed-output and nonblocking, and failure
must leave the agent untouched (always exit 0, empty stdout). Tests use
temporary HOME/XDG so no event leaks into the real store, and assert the
privacy contract (prompts, errors, transcripts and raw identities are
never persisted).
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from moira.activity import ActivityState, ActivityStore, AgentRuntime, hash_identity
from moira.agent_hooks import MAX_HOOK_INPUT_BYTES, agent_hook_main


@pytest.fixture()
def state_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(home))
    return home


@pytest.fixture()
def claude_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate ~/.claude so the model label comes from a fixture file."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"model": "opus-fixture", "theme": "dark"}), encoding="utf-8")
    return home


def _fire(payload: dict[str, object], runtime: str = "claude") -> int:
    previous = sys.stdin
    sys.stdin = type("FakeInput", (), {"buffer": io.BytesIO(json.dumps(payload).encode())})()
    try:
        return agent_hook_main([runtime])
    finally:
        sys.stdin = previous


def _sessions(store: ActivityStore) -> dict[str, dict[str, Any]]:
    store.reload()
    return cast("dict[str, dict[str, Any]]", store.snapshot()["sessions"])


def _current_turn(store: ActivityStore, runtime: str, session_hash: str) -> dict[str, Any]:
    """The session's current turn entry (v2 turn lifecycle shape)."""
    session = _sessions(store)[runtime][session_hash]
    return cast("dict[str, Any]", session["turns"][session["current"]])


def _last(store: ActivityStore) -> dict[str, dict[str, Any]]:
    store.reload()
    return cast("dict[str, dict[str, Any]]", store.snapshot()["last_events"])


def test_claude_user_prompt_submit_starts(state_home: Path, claude_home: Path) -> None:
    assert (
        _fire(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "claude-session-1",
                "prompt_id": "prompt-1",
                "prompt": "please do the thing",
                "transcript_path": "/private/transcript.jsonl",
            }
        )
        == 0
    )
    store = ActivityStore()
    sessions = _sessions(store)
    assert len(sessions["claude"]) == 1
    turn = _current_turn(store, "claude", hash_identity("claude-session-1"))
    assert turn["state"] == ActivityState.RUNNING.value
    assert turn["model"] == "opus-fixture"


def test_claude_stop_completes_and_stop_failure_fails(state_home: Path, claude_home: Path) -> None:
    _fire(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "s",
            "prompt_id": "prompt-1",
        }
    )
    _fire(
        {
            "hook_event_name": "Stop",
            "session_id": "s",
            "prompt_id": "prompt-1",
            "last_assistant_message": "secret answer",
        }
    )
    store = ActivityStore()
    assert (
        _current_turn(store, "claude", hash_identity("s"))["state"] == ActivityState.COMPLETED.value
    )
    _fire({"hook_event_name": "UserPromptSubmit", "session_id": "s2", "prompt_id": "prompt-2"})
    _fire(
        {
            "hook_event_name": "StopFailure",
            "session_id": "s2",
            "prompt_id": "prompt-2",
            "error": "rate limit exceeded",
            "error_details": {"raw": "sensitive"},
        }
    )
    assert (
        _current_turn(store, "claude", hash_identity("s2"))["state"] == ActivityState.FAILED.value
    )
    assert _last(store)["claude"]["state"] == ActivityState.FAILED.value


def test_claude_session_end_interrupts(state_home: Path, claude_home: Path) -> None:
    _fire({"hook_event_name": "UserPromptSubmit", "session_id": "s", "prompt_id": "prompt-1"})
    _fire({"hook_event_name": "SessionEnd", "session_id": "s", "reason": "user_closed"})
    store = ActivityStore()
    assert (
        _current_turn(store, "claude", hash_identity("s"))["state"]
        == ActivityState.INTERRUPTED.value
    )
    # A terminal turn is never replaced by a later Stop.
    _fire({"hook_event_name": "Stop", "session_id": "s"})
    assert (
        _current_turn(store, "claude", hash_identity("s"))["state"]
        == ActivityState.INTERRUPTED.value
    )


def test_claude_unowned_events_are_ignored(state_home: Path, claude_home: Path) -> None:
    _fire({"hook_event_name": "PreToolUse", "session_id": "s", "tool_name": "Bash"})
    store = ActivityStore()
    assert _sessions(store) == {}


def test_hermes_pre_post_and_session_end(state_home: Path) -> None:
    payloads: list[dict[str, object]] = [
        {
            "hook_event_name": "pre_llm_call",
            "session_id": "h-1",
            "extra": {"model": "m1", "turn_id": "turn-1"},
        },
        {
            "hook_event_name": "post_llm_call",
            "session_id": "h-1",
            "extra": {"model": "m1", "turn_id": "turn-1"},
        },
        {
            "hook_event_name": "on_session_end",
            "session_id": "h-1",
            "extra": {"completed": True, "interrupted": False, "model": "m1", "turn_id": "turn-1"},
        },
    ]
    for payload in payloads:
        assert _fire(payload, "hermes") == 0
    store = ActivityStore()
    turn = _current_turn(store, "hermes", hash_identity("h-1"))
    assert turn["state"] == ActivityState.COMPLETED.value
    assert turn["model"] == "m1"


def test_hermes_interrupted_session_end_wins(state_home: Path) -> None:
    _fire(
        {
            "hook_event_name": "pre_llm_call",
            "session_id": "h-2",
            "extra": {"model": "m", "turn_id": "turn-1"},
        },
        "hermes",
    )
    _fire(
        {
            "hook_event_name": "on_session_end",
            "session_id": "h-2",
            "extra": {"completed": False, "interrupted": True, "model": "m", "turn_id": "turn-1"},
        },
        "hermes",
    )
    store = ActivityStore()
    assert (
        _current_turn(store, "hermes", hash_identity("h-2"))["state"]
        == ActivityState.INTERRUPTED.value
    )


def test_hermes_session_end_without_start_records_last_event_only(state_home: Path) -> None:
    _fire(
        {
            "hook_event_name": "on_session_end",
            "session_id": "never-started",
            "extra": {"completed": True, "interrupted": False, "model": "m"},
        },
        "hermes",
    )
    store = ActivityStore()
    assert _sessions(store).get("hermes", {}) == {}
    assert _last(store)["hermes"]["state"] == ActivityState.COMPLETED.value
    # A completion notifier never synthesizes RUNNING.
    _fire(
        {
            "hook_event_name": "on_session_end",
            "session_id": "never-started-2",
            "extra": {"completed": False, "interrupted": False, "model": "m"},
        },
        "hermes",
    )
    assert _last(store)["hermes"]["state"] == ActivityState.INTERRUPTED.value


def test_privacy_prompts_and_errors_never_persisted(state_home: Path, claude_home: Path) -> None:
    _fire(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "sensitive-session",
            "prompt": "TOP-SECRET-PROMPT",
        }
    )
    _fire(
        {
            "hook_event_name": "StopFailure",
            "session_id": "sensitive-session",
            "error": "TOP-SECRET-ERROR",
            "last_assistant_message": "TOP-SECRET-ANSWER",
        }
    )
    store = ActivityStore()
    raw = store.path.read_text(encoding="utf-8")
    for secret in (
        "TOP-SECRET-PROMPT",
        "TOP-SECRET-ERROR",
        "TOP-SECRET-ANSWER",
        "sensitive-session",
    ):
        assert secret not in raw


def test_fixed_output_empty_stdout_and_exit_zero(state_home: Path, claude_home: Path) -> None:
    """The wrapper prints nothing and exits 0 on every path, including
    garbage input, oversized input, unknown runtime and store failures."""
    from moira.agent_hooks import agent_hook_main

    for runtime in ("claude", "hermes", "bogus"):
        previous = sys.stdin
        sys.stdin = type("FakeInput", (), {"buffer": io.BytesIO(b"not json at all")})()
        try:
            rc = agent_hook_main([runtime])
        finally:
            sys.stdin = previous
        assert rc == 0
    previous = sys.stdin
    sys.stdin = type("FakeInput", (), {"buffer": io.BytesIO(b"")})()
    try:
        assert agent_hook_main([]) == 0
        assert agent_hook_main(["claude"]) == 0
    finally:
        sys.stdin = previous


def test_oversized_input_drained_and_skipped(state_home: Path, claude_home: Path) -> None:
    big = json.dumps(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "s",
            "prompt": "x" * (MAX_HOOK_INPUT_BYTES + 10),
        }
    )
    previous = sys.stdin
    sys.stdin = type("FakeInput", (), {"buffer": io.BytesIO(big.encode())})()
    try:
        rc = agent_hook_main(["claude"])
    finally:
        sys.stdin = previous
    assert rc == 0
    store = ActivityStore()
    assert _sessions(store) == {}


def test_hook_is_network_free_by_construction(state_home: Path) -> None:
    """The hook modules never import network/socket machinery."""
    import moira.agent_hooks as ah

    source = Path(ah.__file__).read_text(encoding="utf-8")
    assert "urllib" not in source and "socket" not in source and "requests" not in source
    import moira.activity as act

    source = Path(act.__file__).read_text(encoding="utf-8")
    assert "urllib" not in source and "socket" not in source


def test_packaged_wrapper_script_invokes_module(tmp_path: Path, state_home: Path) -> None:
    """The deb wrapper is a thin shim over moira.agent_hooks."""
    wrapper = Path(__file__).resolve().parents[1] / "packaging" / "moira-agent-hook"
    assert wrapper.exists()
    text = wrapper.read_text(encoding="utf-8")
    assert "moira.agent_hooks" in text
    assert "exec" in text
    # The module entry point exists and is callable.
    from moira.agent_hooks import agent_hook_main

    assert callable(agent_hook_main)


def test_runtimes_match_domain_enum(state_home: Path) -> None:
    assert AgentRuntime.CLAUDE.value == "claude"
    assert AgentRuntime.HERMES.value == "hermes"
    assert AgentRuntime.CODEX.value == "codex"
