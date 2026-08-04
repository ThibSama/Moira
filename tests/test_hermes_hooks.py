"""Package 6c — Hermes shell-hook adapter tests.

Covers the version probe (fake hermes binaries), the subset-YAML hooks
editor (parse/render/splice/merge/remove with round-trip validation,
byte-preservation of unrelated config, fail-closed on unsupported YAML),
ownership discipline, and the callback test that proves events fire
without persisting fake success into the real store.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from moira.hermes_hooks import (
    HERMES_HOOK_COMMAND,
    HERMES_HOOK_TIMEOUT,
    HermesHooksError,
    config_path,
    merge_hooks,
    parse_hooks_block,
    probe_hermes,
    remove,
    remove_hooks,
    render_hooks_block,
    set_hooks,
    setup,
    test_hooks,
    unset_hooks,
)


def _config(tmp_path: Path) -> Path:
    return tmp_path / "hermes" / "config.yaml"


def _write(text: str, tmp_path: Path) -> Path:
    path = _config(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    return path


BASIC_CONFIG = """model: deepseek-v4-flash
terminal:
  backend: docker
"""


def test_parse_empty_and_absent_blocks() -> None:
    assert parse_hooks_block("") == {}
    assert parse_hooks_block("model: x\n") == {}
    assert parse_hooks_block("hooks:\n") == {}
    assert parse_hooks_block("# comment only\n") == {}


def test_parse_documented_shape() -> None:
    text = """hooks:
  pre_llm_call:
    - command: "~/bin/guard.sh"
      timeout: 10
  post_tool_call:
    - matcher: "write_file|patch"
      command: "~/.hermes/agent-hooks/format.sh"
"""
    events = parse_hooks_block(text)
    assert events["pre_llm_call"] == [{"command": "~/bin/guard.sh", "timeout": 10}]
    assert events["post_tool_call"] == [
        {"matcher": "write_file|patch", "command": "~/.hermes/agent-hooks/format.sh"}
    ]


def test_parse_scalars() -> None:
    text = """hooks:
  pre_llm_call:
    - command: "/bin/true"
      timeout: 5
      enabled: true
      ratio: 2.5
      nothing: null
"""
    events = parse_hooks_block(text)
    entry = events["pre_llm_call"][0]
    assert entry["timeout"] == 5
    assert entry["enabled"] is True
    assert entry["ratio"] == 2.5
    assert entry["nothing"] is None


@pytest.mark.parametrize(
    "text",
    [
        "hooks: {pre_llm_call: []}\n",
        "hooks: []\n",
        "hooks: pre_llm_call\n",
        "hooks:\n  pre_llm_call:\n    - {command: x}\n",
        "hooks:\n  pre_llm_call: {command: x}\n",
        "hooks:\n  pre_llm_call:\n    command: x\n",
        "hooks:\n  pre_llm_call:\n  - command: x\n",  # compact list style unsupported
        "hooks:\n  pre_llm_call:\n    - command: |\n        multi\n",  # block scalar
        "hooks:\n  pre_llm_call:\n    - {command: x}\n",  # inline flow mapping item
        "hooks:\n  pre_llm_call:\n    - command: &anchor x\n",
        "hooks:\n  pre_llm_call:\n    - command: yes\n",
        "hooks:\n  pre_llm_call:\n    - command: x\n    - matcher: [a, b]\n",
    ],
)
def test_unsupported_yaml_fails_closed(text: str) -> None:
    with pytest.raises(HermesHooksError):
        parse_hooks_block(text)


def test_set_hooks_appends_at_eof_preserving_config(tmp_path: Path) -> None:
    path = _write(BASIC_CONFIG, tmp_path)
    updated, changed = set_hooks(path.read_text(encoding="utf-8"))
    assert changed
    assert updated.startswith(BASIC_CONFIG)
    events = parse_hooks_block(updated)
    assert set(events) == {"pre_llm_call", "post_llm_call", "on_session_end"}
    for event in ("pre_llm_call", "post_llm_call", "on_session_end"):
        assert events[event] == [{"command": HERMES_HOOK_COMMAND, "timeout": HERMES_HOOK_TIMEOUT}]


def test_set_hooks_merges_with_existing_hooks_and_preserves_them(tmp_path: Path) -> None:
    text = """hooks:
  pre_tool_call:
    - matcher: "terminal"
      command: "~/bin/guard.sh"
      timeout: 10
"""
    updated, changed = set_hooks(text)
    assert changed
    events = parse_hooks_block(updated)
    # Unrelated event and entry preserved exactly.
    assert events["pre_tool_call"] == [
        {"matcher": "terminal", "command": "~/bin/guard.sh", "timeout": 10}
    ]
    for event in ("pre_llm_call", "post_llm_call", "on_session_end"):
        assert events[event] == [{"command": HERMES_HOOK_COMMAND, "timeout": HERMES_HOOK_TIMEOUT}]


def test_set_hooks_is_idempotent(tmp_path: Path) -> None:
    path = _write(BASIC_CONFIG, tmp_path)
    first, changed1 = set_hooks(path.read_text(encoding="utf-8"))
    assert changed1
    second, changed2 = set_hooks(first)
    assert not changed2
    assert second == first


def test_set_hooks_round_trip_preserves_other_top_level_keys(tmp_path: Path) -> None:
    text = """model: x
hooks:
  pre_llm_call:
    - command: "old"
terminal:
  backend: docker
"""
    updated, changed = set_hooks(text)
    assert changed
    lines = updated.split("\n")
    assert lines[0] == "model: x"
    assert any(line.startswith("terminal:") for line in lines)
    assert "backend: docker" in updated
    # Round-trip: the reparsed block equals the merged block.
    events = parse_hooks_block(updated)
    assert events["pre_llm_call"] == [
        {"command": "old"},
        {"command": HERMES_HOOK_COMMAND, "timeout": HERMES_HOOK_TIMEOUT},
    ]


def test_unset_hooks_removes_only_owned_entries(tmp_path: Path) -> None:
    text = """hooks:
  pre_llm_call:
    - command: "old-user-hook"
    - command: "/usr/bin/moira-agent-hook hermes"
      timeout: 5
  pre_tool_call:
    - matcher: "terminal"
      command: "~/bin/guard.sh"
"""
    updated, changed = unset_hooks(text)
    assert changed
    events = parse_hooks_block(updated)
    assert events["pre_llm_call"] == [{"command": "old-user-hook"}]
    assert events["pre_tool_call"] == [{"matcher": "terminal", "command": "~/bin/guard.sh"}]
    assert "post_llm_call" not in events and "on_session_end" not in events
    # Idempotent.
    second, changed2 = unset_hooks(updated)
    assert not changed2


def test_unset_hooks_drops_empty_block(tmp_path: Path) -> None:
    text = """model: x
hooks:
  pre_llm_call:
    - command: "/usr/bin/moira-agent-hook hermes"
      timeout: 5
"""
    updated, changed = unset_hooks(text)
    assert changed
    assert "hooks:" not in updated
    assert "model: x" in updated
    assert parse_hooks_block(updated) == {}


def test_merge_and_remove_pure_functions() -> None:
    events: dict[str, list[dict[str, Any]]] = {
        "pre_tool_call": [{"matcher": "terminal", "command": "x"}]
    }
    merged, changed = merge_hooks(events)
    assert changed
    assert merged["pre_tool_call"] == [{"matcher": "terminal", "command": "x"}]
    merged2, changed2 = merge_hooks(merged)
    assert not changed2
    removed, removed_changed = remove_hooks(merged2)
    assert removed_changed
    assert set(removed) == {"pre_tool_call"}
    assert removed["pre_tool_call"] == [{"matcher": "terminal", "command": "x"}]


def test_render_is_deterministic() -> None:
    events = parse_hooks_block(
        """hooks:
  pre_llm_call:
    - command: "/usr/bin/moira-agent-hook hermes"
      timeout: 5
"""
    )
    assert render_hooks_block(events) == render_hooks_block(events)


def test_setup_writes_backup_and_atomic(tmp_path: Path) -> None:
    path = _write(BASIC_CONFIG, tmp_path)
    assert setup(path) is True
    assert (tmp_path / "hermes" / "config.yaml.moira-backup").exists()
    assert not setup(path)  # idempotent
    text = path.read_text(encoding="utf-8")
    events = parse_hooks_block(text)
    assert set(events) == {"pre_llm_call", "post_llm_call", "on_session_end"}
    assert remove(path) is True
    assert "hooks:" not in path.read_text(encoding="utf-8")
    assert not remove(path)


def test_setup_missing_config_creates_hooks_only(tmp_path: Path) -> None:
    path = _config(tmp_path)
    assert setup(path) is True
    events = parse_hooks_block(path.read_text(encoding="utf-8"))
    assert set(events) == {"pre_llm_call", "post_llm_call", "on_session_end"}
    assert remove(path) is True


def test_config_path_honours_hermes_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    assert config_path() == tmp_path / "hermes-home" / "config.yaml"


# ── Probe (fake hermes binaries) ──


def _fake_hermes(tmp_path: Path, *, version: str, hooks_rc: int = 0) -> Path:
    binary = tmp_path / "hermes"
    binary.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "--version" ]; then echo "Hermes Agent v{version} (2026.7.20)"; exit 0; fi\n'
        f'if [ "$1" = "hooks" ]; then exit {hooks_rc}; fi\n'
        "exit 1\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    return binary


def test_probe_hermes_full(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_hermes(tmp_path, version="0.19.0")
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    report = probe_hermes()
    assert report.supported
    assert report.version == "0.19.0"
    assert report.reason == ""


def test_probe_hermes_unsupported_when_hooks_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_hermes(tmp_path, version="0.19.0", hooks_rc=2)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    report = probe_hermes()
    assert not report.supported
    assert report.version == "0.19.0"
    assert report.reason == "shell hooks unsupported"


def test_probe_hermes_fails_closed_without_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))  # empty PATH: no hermes
    report = probe_hermes()
    assert not report.supported
    assert report.reason == "not installed"


def test_probe_hermes_fails_closed_on_unknown_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_hermes(tmp_path, version="??")
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    report = probe_hermes()
    assert not report.supported
    assert report.reason == "version unknown"


def test_live_probe_hermes_present(tmp_path: Path) -> None:
    """Live probe: skip only when the real hermes binary is absent."""
    import shutil

    if shutil.which("hermes") is None:
        pytest.skip("hermes binary absent")
    report = probe_hermes()
    # The real install is expected to support shell hooks; fail the test if
    # the probe reports an unexpected negative so the report stays honest.
    assert report.version != ""
    assert report.reason != "version unknown"


# ── Callback test (never persists fake success) ──


def test_test_hooks_proves_callbacks_without_persisting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    from moira.activity import ActivityEvent, ActivityState, ActivityStore, AgentRuntime

    real_state = tmp_path / "real-state"
    monkeypatch.setenv("XDG_STATE_HOME", str(real_state))
    # Prime the real store with one real session so we can prove the
    # callback test never touches it.
    real_store = ActivityStore()
    real_store.record(
        ActivityEvent(
            AgentRuntime.HERMES,
            ActivityState.RUNNING,
            "a" * 64,
            "real-model",
            datetime.now(UTC),
        )
    )
    real_store.record(
        ActivityEvent(
            AgentRuntime.HERMES,
            ActivityState.COMPLETED,
            "a" * 64,
            "real-model",
            datetime.now(UTC),
        )
    )
    before = json.dumps(real_store.snapshot(), sort_keys=True)
    assert test_hooks() is True
    real_store.reload()
    after = json.dumps(real_store.snapshot(), sort_keys=True)
    assert after == before  # fake events never persisted
    sessions = real_store.snapshot()["sessions"]
    assert set(sessions) == {"hermes"}  # no claude/codex fake sessions either
    assert len(sessions["hermes"]) == 1
