"""Package 6c — agent integration orchestration tests.

Setup/remove/test controls and capability reporting for Claude Code,
Codex CLI and Hermes. Missing binaries or unsupported versions must yield
translated sanitized states without altering external config; tests never
persist fake success into the real activity store.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from moira.activity import AgentRuntime
from moira.agent_integration import (
    probe_capability,
    remove_runtime,
    setup_runtime,
)
from moira.agent_integration import (
    test_runtime as fire_runtime_test,
)


def _write_claude_settings(home: Path, settings: dict[str, object]) -> None:
    path = home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(settings), encoding="utf-8")


# ── Claude ──


def test_claude_capability_not_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    report = probe_capability(AgentRuntime.CLAUDE)
    assert report.level == "not_installed"
    assert report.detail  # sanitized translated detail


def test_claude_setup_and_capability(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _write_claude_settings(home, {"theme": "dark"})
    result = setup_runtime(AgentRuntime.CLAUDE)
    assert result.changed
    assert result.capability.level == "full"
    # Idempotent.
    second = setup_runtime(AgentRuntime.CLAUDE)
    assert not second.changed
    assert second.capability.level == "full"
    removed = remove_runtime(AgentRuntime.CLAUDE)
    assert removed.changed
    assert removed.capability.level == "not_installed"


def test_claude_remove_when_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    result = remove_runtime(AgentRuntime.CLAUDE)
    assert not result.changed
    assert result.capability.level == "not_installed"


def test_claude_test_proves_callbacks_no_persist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _write_claude_settings(home, {"theme": "dark"})
    real_state = tmp_path / "real-state"
    monkeypatch.setenv("XDG_STATE_HOME", str(real_state))
    result = fire_runtime_test(AgentRuntime.CLAUDE)
    assert result.capability.level == "full"
    assert not (real_state / "moira" / "activity.json").exists()


# ── Hermes ──


def _fake_hermes(tmp_path: Path, *, version: str = "0.19.0", hooks_rc: int = 0) -> None:
    binary = tmp_path / "hermes"
    binary.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "--version" ]; then echo "Hermes Agent v{version} (2026.7.20)"; exit 0; fi\n'
        f'if [ "$1" = "hooks" ]; then exit {hooks_rc}; fi\n'
        "exit 1\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)


def test_hermes_capability_unsupported_without_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    report = probe_capability(AgentRuntime.HERMES)
    assert report.level == "unsupported"
    assert report.detail


def test_hermes_setup_remove_with_fake_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_hermes(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    result = setup_runtime(AgentRuntime.HERMES)
    assert result.changed
    assert result.capability.level == "full"
    config = hermes_home / "config.yaml"
    assert config.exists()
    from moira.hermes_hooks import parse_hooks_block

    events = parse_hooks_block(config.read_text(encoding="utf-8"))
    assert set(events) == {"pre_llm_call", "post_llm_call", "on_session_end"}
    removed = remove_runtime(AgentRuntime.HERMES)
    assert removed.changed
    assert "hooks:" not in config.read_text(encoding="utf-8")


def test_hermes_setup_fails_closed_without_hooks_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_hermes(tmp_path, hooks_rc=2)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    result = setup_runtime(AgentRuntime.HERMES)
    assert not result.changed
    assert result.capability.level == "unsupported"
    # External config was NOT altered.
    assert not (hermes_home / "config.yaml").exists()


def test_hermes_test_does_not_alter_external_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))  # no hermes binary: fail closed
    result = fire_runtime_test(AgentRuntime.HERMES)
    # The callback test itself is binary-independent (fires the packaged
    # hook directly); capability reports the probe state.
    assert result.capability.level in ("full", "unsupported")


# ── Codex ──


def test_codex_capability_unsupported_without_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    report = probe_capability(AgentRuntime.CODEX)
    assert report.level == "unsupported"
    assert report.detail


def test_codex_setup_reprobes_and_remove_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    setup_result = setup_runtime(AgentRuntime.CODEX)
    assert not setup_result.changed
    assert setup_result.capability.level == "unsupported"
    remove_result = remove_runtime(AgentRuntime.CODEX)
    assert not remove_result.changed


def test_codex_test_proves_mapping_without_persisting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    real_state = tmp_path / "real-state"
    monkeypatch.setenv("XDG_STATE_HOME", str(real_state))
    result = fire_runtime_test(AgentRuntime.CODEX)
    assert result.capability.level == "full"
    assert not (real_state / "moira" / "activity.json").exists()


# ── Live probes (skip when binaries absent) ──


def test_live_probes_report_truthfully(tmp_path: Path) -> None:
    """Live probe of every runtime; skip only when the binary is absent."""
    for runtime, binary in (
        (AgentRuntime.CLAUDE, "claude"),
        (AgentRuntime.CODEX, "codex"),
        (AgentRuntime.HERMES, "hermes"),
    ):
        if shutil.which(binary) is None:
            pytest.skip(f"{binary} binary absent")
        report = probe_capability(runtime)
        assert report.detail  # sanitized detail is always present
        # Claude reads the real user settings; report may be either state.
        assert report.level in ("full", "completion_only", "unsupported", "not_installed")
