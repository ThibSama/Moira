"""Package 6c — Claude Code hook ownership tests.

Moira's Claude setup merges owned lifecycle hooks (UserPromptSubmit,
Stop, StopFailure, SessionEnd) into ``~/.claude/settings.json``:
preserve unrelated hooks/statusLine and settings exactly, back up
atomically, install idempotently, remove only owned entries, refuse
ambiguous ownership, and roll back cleanly when a write fails.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from moira.claude_integration import (
    ClaudeIntegrationError,
    remove,
    setup,
)

OWNED_EVENTS = ("UserPromptSubmit", "Stop", "StopFailure", "SessionEnd")
MOIRA_HOOK = "/usr/bin/moira-agent-hook claude"


def _owned_entry() -> dict[str, object]:
    return {"matcher": "", "hooks": [{"type": "command", "command": MOIRA_HOOK}]}


def _settings_path(tmp_path: Path) -> Path:
    return tmp_path / "settings.json"


def _metadata(tmp_path: Path) -> Path:
    return tmp_path / "integration.json"


def test_setup_installs_all_four_owned_hooks(tmp_path: Path) -> None:
    path = _settings_path(tmp_path)
    path.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    assert setup(path, _metadata(tmp_path), "/installed/statusline")
    installed = json.loads(path.read_text(encoding="utf-8"))
    hooks = installed["hooks"]
    assert set(hooks) == set(OWNED_EVENTS)
    for event in OWNED_EVENTS:
        assert hooks[event] == [_owned_entry()]
    assert installed["statusLine"]["command"] == "/installed/statusline"
    assert installed["theme"] == "dark"
    # Backup contains the pristine settings (no hooks).
    backup = json.loads((tmp_path / "settings.json.moira-backup").read_text(encoding="utf-8"))
    assert backup == {"theme": "dark"}


def test_setup_merges_with_existing_hooks_and_statusline(tmp_path: Path) -> None:
    path = _settings_path(tmp_path)
    original: dict[str, Any] = {
        "theme": "dark",
        "statusLine": {"type": "command", "command": "/existing/status-line", "padding": 2},
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "rtk hook claude"}]}
            ],
            "Stop": [
                {"matcher": "", "hooks": [{"type": "command", "command": "~/.local/bin/ntfy.sh"}]}
            ],
        },
    }
    path.write_text(json.dumps(original), encoding="utf-8")
    assert setup(path, _metadata(tmp_path), "/installed/statusline")
    installed = json.loads(path.read_text(encoding="utf-8"))
    # Unrelated hooks preserved exactly.
    assert installed["hooks"]["PreToolUse"] == original["hooks"]["PreToolUse"]
    # Existing Stop hook preserved; Moira's Stop appended.
    assert installed["hooks"]["Stop"] == [
        original["hooks"]["Stop"][0],
        _owned_entry(),
    ]
    # All four owned events present with the Moira entry.
    for event in OWNED_EVENTS:
        assert MOIRA_HOOK in [entry["hooks"][0]["command"] for entry in installed["hooks"][event]]
    # Unrelated statusLine settings preserved; the command is chained.
    assert installed["statusLine"]["padding"] == 2
    assert installed["statusLine"]["command"] == "/installed/statusline"
    # Removal restores the exact original statusLine AND keeps unrelated hooks.
    assert remove(path, _metadata(tmp_path), "/installed/statusline")
    restored = json.loads(path.read_text(encoding="utf-8"))
    assert restored["statusLine"] == original["statusLine"]
    assert restored["hooks"] == original["hooks"]
    assert restored["theme"] == "dark"


def test_setup_is_idempotent(tmp_path: Path) -> None:
    path = _settings_path(tmp_path)
    path.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    assert setup(path, _metadata(tmp_path))
    assert not setup(path, _metadata(tmp_path))
    installed = json.loads(path.read_text(encoding="utf-8"))
    assert len(installed["hooks"]["Stop"]) == 1  # no duplicates


def test_remove_is_idempotent_and_removes_only_owned(tmp_path: Path) -> None:
    path = _settings_path(tmp_path)
    path.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    assert setup(path, _metadata(tmp_path))
    # A foreign hook appears after setup (unrelated change).
    installed = json.loads(path.read_text(encoding="utf-8"))
    installed["hooks"]["UserPromptSubmit"].append(
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "other-tool"}]}
    )
    path.write_text(json.dumps(installed), encoding="utf-8")
    assert remove(path, _metadata(tmp_path))
    restored = json.loads(path.read_text(encoding="utf-8"))
    # Only Moira's entry was removed; the foreign one survives.
    assert restored["hooks"]["UserPromptSubmit"] == [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "other-tool"}]}
    ]
    assert "hooks" not in restored or "Stop" not in restored["hooks"]
    assert not remove(path, _metadata(tmp_path))


def test_remove_drops_hooks_key_when_only_owned_entries_exist(tmp_path: Path) -> None:
    path = _settings_path(tmp_path)
    path.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    assert setup(path, _metadata(tmp_path))
    assert remove(path, _metadata(tmp_path))
    restored = json.loads(path.read_text(encoding="utf-8"))
    assert "hooks" not in restored
    assert "statusLine" not in restored


def test_ambiguous_ownership_refuses_setup(tmp_path: Path) -> None:
    path = _settings_path(tmp_path)
    # A Moira-command entry outside the owned shape: matcher scoped.
    path.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {"matcher": "Bash", "hooks": [{"type": "command", "command": MOIRA_HOOK}]}
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ClaudeIntegrationError):
        setup(path, _metadata(tmp_path), "/installed/statusline")
    # Nothing was written.
    assert json.loads(path.read_text(encoding="utf-8"))["hooks"]["Stop"][0]["matcher"] == "Bash"


def test_ambiguous_ownership_refuses_removal(tmp_path: Path) -> None:
    path = _settings_path(tmp_path)
    path.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    assert setup(path, _metadata(tmp_path))
    # Corrupt Moira's owned entry after setup.
    installed = json.loads(path.read_text(encoding="utf-8"))
    installed["hooks"]["Stop"] = [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": MOIRA_HOOK}]}
    ]
    path.write_text(json.dumps(installed), encoding="utf-8")
    with pytest.raises(ClaudeIntegrationError):
        remove(path, _metadata(tmp_path), "/installed/statusline")
    # The settings file was not modified by the refused removal.
    assert json.loads(path.read_text(encoding="utf-8"))["hooks"]["Stop"][0]["matcher"] == "Bash"


def test_legacy_record_upgraded_with_hooks(tmp_path: Path) -> None:
    """A pre-6c installation (record without hooks) gets hooks merged."""
    path = _settings_path(tmp_path)
    metadata = _metadata(tmp_path)
    path.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    assert setup(path, metadata, "/installed/statusline")
    # Simulate the old record format.
    metadata.write_text(
        json.dumps({"original_status_line": None, "moira_command": "/installed/statusline"}),
        encoding="utf-8",
    )
    assert setup(path, metadata, "/installed/statusline")
    installed = json.loads(path.read_text(encoding="utf-8"))
    assert "hooks" in installed
    record = json.loads(metadata.read_text(encoding="utf-8"))
    assert set(record["hooks"]) == set(OWNED_EVENTS)


def test_statusline_changed_after_setup_refuses_removal(tmp_path: Path) -> None:
    path = _settings_path(tmp_path)
    path.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    assert setup(path, _metadata(tmp_path), "/installed/statusline")
    installed = json.loads(path.read_text(encoding="utf-8"))
    installed["statusLine"]["command"] = "/someone/else"
    path.write_text(json.dumps(installed), encoding="utf-8")
    with pytest.raises(ClaudeIntegrationError):
        remove(path, _metadata(tmp_path), "/installed/statusline")


def test_non_dict_hooks_refuses_merge(tmp_path: Path) -> None:
    path = _settings_path(tmp_path)
    path.write_text(json.dumps({"hooks": "not-an-object"}), encoding="utf-8")
    with pytest.raises(ClaudeIntegrationError):
        setup(path, _metadata(tmp_path), "/installed/statusline")


def test_setup_rolls_back_on_write_failure(tmp_path: Path) -> None:
    """A failed atomic write leaves the settings file untouched."""
    path = _settings_path(tmp_path)
    original = {"theme": "dark"}
    path.write_text(json.dumps(original), encoding="utf-8")
    with patch("moira.claude_integration._atomic_json", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            setup(path, _metadata(tmp_path), "/installed/statusline")
    assert json.loads(path.read_text(encoding="utf-8")) == original


def test_backup_and_remove_backup_are_atomic_json(tmp_path: Path) -> None:
    path = _settings_path(tmp_path)
    path.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    assert setup(path, _metadata(tmp_path), "/installed/statusline")
    backup = tmp_path / "settings.json.moira-backup"
    assert json.loads(backup.read_text(encoding="utf-8")) == {"theme": "dark"}
    installed = json.loads(path.read_text(encoding="utf-8"))
    installed["model"] = "opus"
    path.write_text(json.dumps(installed), encoding="utf-8")
    assert remove(path, _metadata(tmp_path), "/installed/statusline")
    remove_backup = tmp_path / "settings.json.moira-remove-backup"
    assert json.loads(remove_backup.read_text(encoding="utf-8"))["model"] == "opus"
    restored = json.loads(path.read_text(encoding="utf-8"))
    assert restored["model"] == "opus"
    assert "statusLine" not in restored


def test_owned_entry_shape_is_exact() -> None:
    from moira.claude_integration import _is_owned_hook_entry

    entry = _owned_entry()
    assert _is_owned_hook_entry(entry, MOIRA_HOOK)
    mutations: list[dict[str, object]] = [
        {"matcher": "Bash"},
        {"hooks": []},
        {"hooks": [{"type": "command", "command": "other"}]},
        {
            "hooks": [
                {"type": "command", "command": MOIRA_HOOK},
                {"type": "command", "command": "x"},
            ]
        },
    ]
    for mutation in mutations:
        altered = dict(entry)
        altered.update(mutation)
        assert not _is_owned_hook_entry(altered, MOIRA_HOOK)
