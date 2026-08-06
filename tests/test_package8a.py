"""Package 8a — ordinary Codex CLI activity into the Agent activity panel.

FEATURE (not a correction): Moira observes ordinary Codex CLI/TUI
sessions through the documented user-level lifecycle hooks
(SessionStart / UserPromptSubmit / Stop) installed in the user-level
``$CODEX_HOME/hooks.json`` file, decodes the official bounded payload
schema, hashes every identity through the privacy boundary and feeds
the existing ``ActivityStore`` — no duplicate store, hook binary,
watcher or dashboard.

Verified contract (Codex CLI 0.146.0, official sources):
- feature flag ``hooks`` (stable; ``codex features list``);
- ``$CODEX_HOME/hooks.json`` (user-level, JSON) with per-event
  ``MatcherGroup`` entries ``{matcher, hooks: [{type: "command",
  command}]}`` (codex-rs/config/src/hook_config.rs);
- payload delivered on the hook command's STDIN per the official
  generated input schemas (codex-rs/hooks/schema/generated):
  SessionStart {hook_event_name, session_id, model, cwd, ...},
  UserPromptSubmit {…, turn_id, prompt, …},
  Stop {…, turn_id, stop_hook_active, last_assistant_message, …};
- trust is Codex-owned (the hook runs only after the user approves the
  trust prompt); Moira never writes Codex trust state and never bypasses
  it. The hook writes a Moira-owned verification marker ONLY when Codex
  actually executed it, which is what lifts the capability from
  ``awaiting_trust`` to ``full``.

Privacy: prompts, responses, cwd/project paths, transcript paths,
repository names, raw payloads/errors, accounts and secrets are never
stored — only SHA-256 digests of session/turn identities and the
sanitized ``model`` payload scalar.

RED on f39d9b6: ``moira.codex_hooks`` does not exist yet; the codex
runtime is unknown to the packaged hook; capability never reports
``full``/``awaiting_trust``.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import gi  # type: ignore[import-untyped]

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Secret", "1")
import pytest  # noqa: E402

import moira.codex_hooks as ch  # noqa: E402
from moira.activity import (  # noqa: E402
    WATCHDOG_STALE_SECONDS,
    ActivityState,
    ActivityStore,
    AgentRuntime,
)
from moira.agent_hooks import agent_hook_main  # noqa: E402
from moira.agent_integration import probe_capability, remove_runtime, setup_runtime  # noqa: E402
from moira.codex_activity import CodexCapability  # noqa: E402


@pytest.fixture
def state_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    return state


@pytest.fixture
def codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "codex-home"
    home.mkdir(parents=True, exist_ok=True)  # a real CODEX_HOME exists in production
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


def _fire(payload: dict[str, Any]) -> None:
    """Invoke the packaged hook exactly as Codex would (stdin JSON)."""
    previous = sys.stdin
    sys.stdin = type("FakeInput", (), {"buffer": io.BytesIO(json.dumps(payload).encode())})()
    try:
        agent_hook_main(["codex"])
    finally:
        sys.stdin = previous


def _prompt(session: str, turn: str, model: str = "gpt-5.2-codex") -> dict[str, Any]:
    return {
        "hook_event_name": "UserPromptSubmit",
        "session_id": session,
        "turn_id": turn,
        "model": model,
        "cwd": "/private/project",
        "permission_mode": "default",
        "prompt": "must never be stored",
        "transcript_path": "/private/project/.codex/history.jsonl",
    }


def _stop(session: str, turn: str, model: str = "gpt-5.2-codex") -> dict[str, Any]:
    return {
        "hook_event_name": "Stop",
        "session_id": session,
        "turn_id": turn,
        "model": model,
        "cwd": "/private/project",
        "permission_mode": "default",
        "stop_hook_active": True,
        "last_assistant_message": "must never be stored",
        "transcript_path": "/private/project/.codex/history.jsonl",
    }


def _session_start(session: str, model: str = "gpt-5.2-codex") -> dict[str, Any]:
    return {
        "hook_event_name": "SessionStart",
        "session_id": session,
        "model": model,
        "cwd": "/private/project",
        "permission_mode": "default",
        "source": "startup",
        "transcript_path": "/private/project/.codex/history.jsonl",
    }


def _turns(store: ActivityStore) -> list[dict[str, Any]]:
    sessions = store.snapshot()["sessions"].get(AgentRuntime.CODEX.value, {})
    return [turn for session in sessions.values() for turn in session["turns"].values()]


def _raw(store: ActivityStore) -> str:
    return store.path.read_text(encoding="utf-8")


def _marker(state_home: Path) -> Path:
    return state_home / "moira" / "codex-hooks-verified.json"


# ── Lifecycle mapping (requirement 3) ───────────────────────────────────────


def test_user_prompt_submit_starts_running_with_exact_model(state_home: Path) -> None:
    store = ActivityStore()
    _fire(_prompt("sess-1", "turn-1", model="gpt-5.2-codex-mini"))
    store.reload()
    turns = _turns(store)
    assert len(turns) == 1
    assert turns[0]["state"] == ActivityState.RUNNING.value
    assert turns[0]["model"] == "gpt-5.2-codex-mini"  # exact documented payload field
    assert len(store.sessions_for(AgentRuntime.CODEX)) == 1  # its session count


def test_stop_with_turn_id_terminates_exact_turn(state_home: Path) -> None:
    store = ActivityStore()
    _fire(_prompt("sess-1", "turn-1"))
    _fire(_prompt("sess-1", "turn-2"))
    _fire(_stop("sess-1", "turn-1"))  # late stop for the FIRST turn
    store.reload()
    by_state = {turn["state"] for turn in _turns(store)}
    assert by_state == {ActivityState.COMPLETED.value, ActivityState.RUNNING.value}
    session = next(iter(store.snapshot()["sessions"]["codex"].values()))
    named = [t for h, t in session["turns"].items() if h != session["current"]]
    assert named and named[0]["state"] == ActivityState.COMPLETED.value


def test_stop_without_turn_id_lifecycle(state_home: Path) -> None:
    """Without a documented turn id, Stop closes only the newest
    compatible running turn; with nothing running it is rejected."""
    store = ActivityStore()
    _fire(_prompt("sess-1", "turn-1"))
    _fire(_prompt("sess-1", "turn-2"))
    _fire({"hook_event_name": "Stop", "session_id": "sess-1", "model": "gpt-5.2-codex"})
    store.reload()
    by_state = {turn["state"] for turn in _turns(store)}
    assert by_state == {ActivityState.COMPLETED.value, ActivityState.RUNNING.value}
    assert _turns(store)[0]["state"] == ActivityState.RUNNING.value  # newest closed
    before = _raw(store)
    _fire({"hook_event_name": "Stop", "session_id": "sess-1", "model": "gpt-5.2-codex"})
    store.reload()
    assert _raw(store) == before  # nothing running left: rejected, no fabrication


def test_session_start_never_fabricates_activity(state_home: Path) -> None:
    store = ActivityStore()
    _fire(_session_start("sess-1"))
    store.reload()
    assert _turns(store) == []  # metadata validated, zero fabricated activity
    assert _marker(state_home).exists()  # but it proves Codex executed the hook


def test_unknown_events_are_ignored(state_home: Path) -> None:
    store = ActivityStore()
    for name in ("PreToolUse", "PostToolUse", "PermissionRequest", "PreCompact", "SubagentStart"):
        _fire({"hook_event_name": name, "session_id": "sess-1", "model": "m"})
    store.reload()
    assert _turns(store) == []
    assert not _marker(state_home).exists()  # Moira owns only its three lifecycle events


@pytest.mark.parametrize(
    "payload",
    [
        "not json",  # raw non-JSON stdin
        ["list"],  # non-object
        {"hook_event_name": "UserPromptSubmit", "model": "m"},  # no session_id
        {"hook_event_name": "UserPromptSubmit", "session_id": {"x": 1}, "model": "m"},  # non-scalar
        {  # noqa: E501 oversized turn id
            "hook_event_name": "Stop",
            "session_id": "s",
            "turn_id": "t" * 1000,
            "model": "m",
        },
    ],
)
def test_malformed_or_oversized_payloads_ignored(state_home: Path, payload: Any) -> None:
    store = ActivityStore()
    previous = sys.stdin
    sys.stdin = type("FakeInput", (), {"buffer": io.BytesIO(
        (json.dumps(payload) if not isinstance(payload, str) else payload).encode()
    )})()
    try:
        agent_hook_main(["codex"])
    finally:
        sys.stdin = previous
    store.reload()
    assert _turns(store) == []
    assert not _marker(state_home).exists()


# ── Privacy (requirement 2) ────────────────────────────────────────────────


def test_privacy_never_stores_paths_prompts_or_raw(state_home: Path) -> None:
    store = ActivityStore()
    prompt = _prompt("sess-1", "turn-1")
    prompt.update({
        "prompt": "SECRET-PROMPT",
        "cwd": "/home/secret-user/project",
        "transcript_path": "/home/secret-user/.codex/h.jsonl",
        "unknown_extra": {"nested": "secret"},
    })
    _fire(prompt)
    stop = _stop("sess-1", "turn-1")
    stop["last_assistant_message"] = "SECRET-RESPONSE"
    _fire(stop)
    store.reload()
    raw = _raw(store)
    for forbidden in (
        "SECRET-PROMPT",
        "SECRET-RESPONSE",
        "secret-user",
        "/home/",
        "h.jsonl",
        "unknown_extra",
        "sess-1",  # raw session id never stored, only its digest
        "turn-1",  # raw turn id never stored
        "hook_event_name",
        "transcript_path",
        "permission_mode",
    ):
        assert forbidden not in raw, forbidden
    marker = _marker(state_home)
    if marker.exists():
        marker_raw = marker.read_text(encoding="utf-8")
        assert "sess-1" not in marker_raw and "turn-1" not in marker_raw


# ── Replays and late events (requirement 3) ────────────────────────────────


def test_replay_and_late_start_rules(state_home: Path) -> None:
    store = ActivityStore()
    _fire(_prompt("sess-1", "turn-1"))
    _fire(_prompt("sess-1", "turn-1"))  # replay while running
    _fire(_stop("sess-1", "turn-1"))
    _fire(_stop("sess-1", "turn-1"))  # replay of the terminal
    store.reload()
    assert len(_turns(store)) == 1
    assert _turns(store)[0]["state"] == ActivityState.COMPLETED.value
    before = _raw(store)
    _fire(_prompt("sess-1", "turn-1"))  # a stale late start cannot reopen it
    store.reload()
    assert _raw(store) == before
    assert _turns(store)[0]["state"] == ActivityState.COMPLETED.value


# ── Watchdog (requirement 5) ────────────────────────────────────────────────


def test_watchdog_expires_missing_stop_to_interrupted(state_home: Path) -> None:
    store = ActivityStore()
    _fire(_prompt("sess-1", "turn-1"))
    store.reload()
    changed = store.expire_stale(
        now=datetime.now(UTC) + timedelta(seconds=WATCHDOG_STALE_SECONDS + 60)
    )
    assert AgentRuntime.CODEX in changed
    store.reload()
    assert _turns(store)[0]["state"] == ActivityState.INTERRUPTED.value  # never success
    last = store.last_event_for(AgentRuntime.CODEX)
    assert last is not None and last["state"] == ActivityState.INTERRUPTED.value


# ── Model display (requirement 6) ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        (None, "blank"),  # absent → blank, never the configured default
        ("", "blank"),  # empty string → blank
        (42, "reject"),  # non-scalar → the whole event is rejected
    ],
)
def test_model_display_rules(state_home: Path, model: Any, expected: str) -> None:
    store = ActivityStore()
    payload = _prompt("sess-1", "turn-1")
    if model is None:
        payload.pop("model", None)
    else:
        payload["model"] = model
    _fire(payload)
    store.reload()
    if expected == "blank":
        assert _turns(store)[0]["model"] == ""
    else:
        assert _turns(store) == []  # documented scalars only
        assert not _marker(state_home).exists()


# ── Concurrent sessions ────────────────────────────────────────────────────


def test_concurrent_sessions_stay_separate(state_home: Path) -> None:
    store = ActivityStore()
    _fire(_prompt("sess-a", "a1"))
    _fire(_prompt("sess-b", "b1"))
    _fire(_stop("sess-a", "a1"))
    _fire(_prompt("sess-b", "b2"))
    store.reload()
    sessions = store.sessions_for(AgentRuntime.CODEX)
    assert len(sessions) == 2  # both sessions have their own count
    per_session = sorted(
        (len(session["turns"]), any(t["state"] == "RUNNING" for t in session["turns"].values()))
        for session in sessions.values()
    )
    assert per_session == [(1, False), (2, True)]


# ── Setup / remove / merge / idempotence (requirement 7) ───────────────────


def test_setup_creates_owned_hooks_json_and_idempotent(codex_home: Path) -> None:
    assert ch.setup() is True
    path = ch.hooks_path()
    assert path == codex_home / "hooks.json"  # CODEX_HOME env, never ~/.codex
    assert not (Path.home() / ".codex" / "hooks.json").exists()  # user home untouched
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data["hooks"]) == {"SessionStart", "UserPromptSubmit", "Stop"}
    for event in ("SessionStart", "UserPromptSubmit", "Stop"):
        assert data["hooks"][event] == [
            {"matcher": "*", "hooks": [{"type": "command", "command": ch.MOIRA_HOOK_COMMAND}]}
        ]
    assert data["description"]
    first = path.read_bytes()
    assert ch.setup() is False  # idempotent
    assert path.read_bytes() == first  # byte-identical


def test_setup_preserves_unrelated_entries(codex_home: Path) -> None:
    path = ch.hooks_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "description": "user hooks",
        "hooks": {
            "PreToolUse": [
                {"matcher": "*", "hooks": [{"type": "command", "command": "/usr/bin/other"}]}
            ],
        },
    }))
    assert ch.setup() is True
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["description"] == "user hooks"  # preserved
    assert data["hooks"]["PreToolUse"] == [  # unrelated entry preserved byte-for-byte
        {"matcher": "*", "hooks": [{"type": "command", "command": "/usr/bin/other"}]}
    ]
    assert ch.hooks_installed()


def test_setup_fails_closed_on_invalid_existing_file(codex_home: Path) -> None:
    path = ch.hooks_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json")
    before = path.read_bytes()
    with pytest.raises(ch.CodexHookError):
        ch.setup()  # never clobber a malformed user-owned file
    assert path.read_bytes() == before


def test_remove_phases(codex_home: Path) -> None:
    """Remove deletes the file when Moira owns everything, preserves
    unrelated entries otherwise, and is idempotent."""
    ch.setup()
    assert ch.remove() is True
    assert not ch.hooks_path().exists()  # restores the pre-setup state
    assert ch.remove() is False
    path = ch.hooks_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "hooks": {
            "PreToolUse": [
                {"matcher": "*", "hooks": [{"type": "command", "command": "/usr/bin/other"}]}
            ],
            "UserPromptSubmit": [
                {"matcher": "*", "hooks": [{"type": "command", "command": ch.MOIRA_HOOK_COMMAND}]}
            ],
        },
    }))
    assert ch.remove() is True
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "UserPromptSubmit" not in data["hooks"]  # empty Moira event dropped
    assert data["hooks"]["PreToolUse"] == [  # unrelated preserved
        {"matcher": "*", "hooks": [{"type": "command", "command": "/usr/bin/other"}]}
    ]
    assert ch.remove() is False  # idempotent


def test_backup_and_atomic_replace_with_stale_tmp(codex_home: Path) -> None:
    path = ch.hooks_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"description": "old", "hooks": {}}))
    stale = path.with_name(".hooks.json.tmp")  # leftover from an interrupted write
    stale.write_text("garbage")
    ch.setup()
    assert not stale.exists()  # the atomic write cleaned the stale temp (self-healing)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["hooks"]["UserPromptSubmit"]  # the real file is the new atomic one
    assert path.with_name("hooks.json.moira-backup").exists()  # backup before replace


# ── Capability mapping (requirement 10) ────────────────────────────────────


@pytest.mark.parametrize("marker", [False, True])
def test_capability_hooks_levels(
    state_home: Path, codex_home: Path, marker: bool
) -> None:
    ch.setup()
    if marker:
        ch.write_verified_marker()
    report = probe_capability(AgentRuntime.CODEX)
    assert report.level == ("full" if marker else "awaiting_trust")


def test_capability_session_owned_fallback(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "moira.agent_integration.probe_codex", lambda *a, **k: CodexCapability.SESSION_OWNED
    )
    report = probe_capability(AgentRuntime.CODEX)
    assert report.level == "session_owned"  # app-server only, no CLI hooks


def test_feature_disabled_probe_and_setup(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A disabled hooks feature is probed unsupported, and setup enables
    it through Codex's OWN ``features enable`` surface (ownership-scoped)."""
    fake = _fake_codex(monkeypatch, codex_home, hooks_state="false")
    report = probe_capability(AgentRuntime.CODEX)
    assert report.level == "unsupported"
    assert not fake["enable_called"]
    result = setup_runtime(AgentRuntime.CODEX)
    assert fake["enable_called"] is True  # `codex features enable hooks`
    assert ch.hooks_installed()
    assert result.capability.level == "awaiting_trust"  # installed but trust still pending


def test_capability_not_installed_without_binary(
    monkeypatch: pytest.MonkeyPatch, codex_home: Path
) -> None:
    monkeypatch.setattr(shutil, "which", lambda *a, **k: None)
    report = probe_capability(AgentRuntime.CODEX)
    assert report.level == "not_installed"


def test_remove_clears_marker_and_falls_back(
    codex_home: Path, state_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ch.setup()
    ch.write_verified_marker()
    monkeypatch.setattr(
        "moira.agent_integration.probe_codex", lambda *a, **k: CodexCapability.SESSION_OWNED
    )
    result = remove_runtime(AgentRuntime.CODEX)
    assert result.changed is True
    assert not ch.hooks_installed()
    assert not ch.marker_exists()  # Moira-owned marker removed
    assert result.capability.level == "session_owned"


# ── Dedup with the app-server path (requirement 8) ─────────────────────────


def test_hook_and_app_server_do_not_double_count_same_turn(state_home: Path) -> None:
    """The store is the deterministic dedup point: the CLI hook and the
    app-server adapter hash the SAME session (session_id == threadId
    identity space) and the SAME composite turn identity, so one
    validated turn is ONE record whichever path observes it. In
    production the paths observe disjoint session sets (Moira-owned
    threads vs the user's own TUI sessions); when the same identity
    arrives on both, the named-turn logic merges it."""
    store = ActivityStore()
    _fire(_prompt("thread-1", "turn-1"))
    store.reload()
    assert len(_turns(store)) == 1
    from moira.codex_activity import handle_turn_notification, record_turn_notification

    started = handle_turn_notification(
        {
            "method": "turn/started",
            "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "inProgress"}},
        },
        {"thread-1"},
    )
    assert started is not None and started.state is ActivityState.RUNNING
    assert record_turn_notification(
        {
            "method": "turn/started",
            "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "inProgress"}},
        },
        {"thread-1"},
        store,
    )
    store.reload()
    assert len(_turns(store)) == 1  # the SAME turn — no double count
    assert record_turn_notification(
        {
            "method": "turn/completed",
            "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}},
        },
        {"thread-1"},
        store,
    )
    store.reload()
    assert _turns(store)[0]["state"] == ActivityState.COMPLETED.value
    assert len(_turns(store)) == 1  # one lifecycle for one turn


# ── Mixed groups: handler-level ownership (Package 8b) ─────────────────────


def test_mixed_group_setup_preserves_sibling_command(codex_home: Path) -> None:
    """RED on 4927880: a group carrying the Moira command next to an
    unrelated command was discarded wholesale. Ownership is per HANDLER:
    the sibling survives, the owned handler stays in place, and no
    duplicate Moira group is appended."""
    path = ch.hooks_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "hooks": {
            "UserPromptSubmit": [
                {"matcher": "*", "hooks": [
                    {"type": "command", "command": "/usr/bin/other"},
                    {"type": "command", "command": ch.MOIRA_HOOK_COMMAND},
                ]},
            ],
        },
    }))
    assert ch.setup() is True
    data = json.loads(path.read_text(encoding="utf-8"))
    groups = data["hooks"]["UserPromptSubmit"]
    assert len(groups) == 1  # no duplicate Moira group appended
    commands = [h["command"] for h in groups[0]["hooks"]]
    assert commands == ["/usr/bin/other", ch.MOIRA_HOOK_COMMAND]


def test_mixed_group_removal_preserves_sibling_and_matcher(codex_home: Path) -> None:
    """RED on 4927880: removal discarded the whole mixed group. Only the
    exact Moira handler is removed; the sibling and the matcher survive."""
    path = ch.hooks_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "hooks": {
            "UserPromptSubmit": [
                {"matcher": "*.py", "hooks": [
                    {"type": "command", "command": ch.MOIRA_HOOK_COMMAND},
                    {"type": "command", "command": "/usr/bin/other"},
                ]},
            ],
        },
    }))
    assert ch.remove() is True
    data = json.loads(path.read_text(encoding="utf-8"))
    groups = data["hooks"]["UserPromptSubmit"]
    assert len(groups) == 1
    assert groups[0]["matcher"] == "*.py"  # matcher preserved
    assert groups[0]["hooks"] == [{"type": "command", "command": "/usr/bin/other"}]


def test_duplicate_owned_handlers_collapse_on_setup(codex_home: Path) -> None:
    """Duplicate exact Moira handlers are collapsed to one; unrelated
    handlers in the same group survive and setup becomes stable."""
    path = ch.hooks_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "hooks": {
            "Stop": [
                {"matcher": "*", "hooks": [
                    {"type": "command", "command": ch.MOIRA_HOOK_COMMAND},
                    {"type": "command", "command": ch.MOIRA_HOOK_COMMAND},
                    {"type": "command", "command": "/usr/bin/other"},
                ]},
            ],
        },
    }))
    assert ch.setup() is True  # the duplicates are collapsed
    data = json.loads(path.read_text(encoding="utf-8"))
    groups = data["hooks"]["Stop"]
    assert len(groups) == 1
    commands = [h["command"] for h in groups[0]["hooks"]]
    assert commands == [ch.MOIRA_HOOK_COMMAND, "/usr/bin/other"]
    assert ch.setup() is False  # stable afterwards


@pytest.mark.parametrize(
    "file_hooks",
    [
        {"UserPromptSubmit": "broken"},  # event value is a plain string
        {"UserPromptSubmit": {"hooks": []}},  # event value is a dict
    ],
)
def test_parseable_string_or_dict_event_value_fails_closed(
    codex_home: Path, file_hooks: dict[str, Any]
) -> None:
    """RED on 4927880: a parseable-but-schema-invalid event value was
    iterated and rewritten (a string even became a list of characters).
    The complete mutable shape is validated before ANY write:
    CodexHookError, original file byte-identical."""
    path = ch.hooks_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": file_hooks}))
    before = path.read_bytes()
    with pytest.raises(ch.CodexHookError):
        ch.setup()
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "file_hooks",
    [
        {"UserPromptSubmit": ["not-a-group"]},  # group is not an object
        {"UserPromptSubmit": [{"matcher": "*"}]},  # group without list-valued hooks
        {"UserPromptSubmit": [{"hooks": ["not-a-handler"]}]},  # handler not an object
        {"UserPromptSubmit": [{"hooks": [{"type": 42, "command": "x"}]}]},  # non-string type
        {"UserPromptSubmit": [{"hooks": [{"type": "command"}]}]},  # command without command
    ],
)
def test_malformed_group_hooks_handler_shape_fails_closed(
    codex_home: Path, file_hooks: dict[str, Any]
) -> None:
    """Every unsafe or malformed shape raises CodexHookError before any
    backup or write; the original file stays byte-identical."""
    path = ch.hooks_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": file_hooks}))
    before = path.read_bytes()
    with pytest.raises(ch.CodexHookError):
        ch.setup()
    assert path.read_bytes() == before


def test_clean_setup_remove_cycle_idempotent(codex_home: Path) -> None:
    """Ordinary clean setup/remove stays idempotent and re-installable."""
    path = ch.hooks_path()
    assert ch.setup() is True
    first = path.read_bytes()
    assert ch.setup() is False
    assert path.read_bytes() == first
    assert ch.remove() is True
    assert not path.exists()
    assert ch.remove() is False
    assert ch.setup() is True  # re-install works after removal
    assert ch.remove() is True
    assert not path.exists()


# ── Real installed-Codex probe (may SKIP) ──────────────────────────────────


def test_real_installed_codex_feature_state(codex_home: Path) -> None:
    if shutil.which("codex") is None:
        pytest.skip("codex CLI not installed")
    assert ch.feature_state() == "enabled"  # verified on 0.146.0: hooks stable + true


def _fake_codex(
    monkeypatch: pytest.MonkeyPatch,
    codex_home: Path,
    hooks_state: str = "true",
) -> dict[str, Any]:
    """A fake ``codex`` binary whose only behavior is the documented
    ``features list``/``features enable hooks`` surface."""
    bin_dir = codex_home.parent / "fakebin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    marker = bin_dir / "enable-marker"
    script = bin_dir / "codex"
    script.write_text(
        "#!/bin/sh\n"
        f"state='{hooks_state}'\n"
        "if [ -f \"$FAKE_ENABLE_MARKER\" ]; then state=true; fi\n"
        'if [ "$1" = features ] && [ "$2" = list ]; then\n'
        '  printf "hooks                                stable             %s\\n" "$state"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = features ] && [ "$2" = enable ] && [ "$3" = hooks ]; then\n'
        "  : > \"$FAKE_ENABLE_MARKER\"\n"
        "  exit 0\n"
        "fi\n"
        "exit 1\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("FAKE_ENABLE_MARKER", str(marker))
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    fake = {"enable_called": False, "marker": marker}
    original_run = subprocess.run

    def _run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if (
            isinstance(args, list)
            and len(args) >= 3
            and args[0].endswith("codex")
            and args[1] == "features"
        ):
            fake["enable_called"] = fake["enable_called"] or (args[2] == "enable")
        return original_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _run)
    return fake
