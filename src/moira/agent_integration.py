"""Agent integration orchestration — setup/remove/test per runtime.

One facade the Settings page uses for the three agent runtimes (Claude
Code, Codex CLI, Hermes):

- ``probe_capability`` reports the current capability (full /
  completion-only / unsupported / not installed) with a sanitized detail;
- ``setup_runtime`` installs only Moira-owned entries (Claude Code hooks
  + status line, Hermes shell hooks) and never touches anything Moira
  does not own; Codex has nothing to install — setup re-probes the
  documented app-server protocol;
- ``remove_runtime`` removes only owned entries;
- ``test_runtime`` proves the callbacks fire using a throwaway
  ``XDG_STATE_HOME`` so fake events never persist into the real activity
  store and quota-alert deduplication is never altered.

Every failure returns a fixed translated outcome; raw exceptions never
reach the UI.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .activity import ActivityState, ActivityStore, AgentRuntime
from .agent_hooks import agent_hook_main
from .claude_integration import remove as remove_claude
from .claude_integration import setup as setup_claude
from .codex_activity import CodexCapability, probe_codex, record_turn_notification
from .hermes_hooks import remove as remove_hermes
from .hermes_hooks import setup as setup_hermes
from .hermes_hooks import test_hooks as test_hermes_hooks
from .i18n import tr

_ = tr


@dataclass(frozen=True, slots=True)
class CapabilityReport:
    level: str  # "full" | "completion_only" | "unsupported" | "not_installed"
    detail: str  # sanitized, translated


@dataclass(frozen=True, slots=True)
class IntegrationResult:
    changed: bool
    capability: CapabilityReport


def _claude_installed() -> bool:
    from .claude_integration import (
        CLAUDE_HOOK_COMMAND,
        CLAUDE_HOOK_EVENTS,
        _hook_command,
        settings_path,
    )

    try:
        settings = json.loads(settings_path().read_text(encoding="utf-8"))
        if not isinstance(settings, dict):
            return False
        hooks = settings.get("hooks")
        if not isinstance(hooks, dict):
            return False
        for event in CLAUDE_HOOK_EVENTS:
            entries = hooks.get(event)
            if not isinstance(entries, list) or not any(
                _hook_command(entry) == CLAUDE_HOOK_COMMAND for entry in entries
            ):
                return False
        return True
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def probe_capability(runtime: AgentRuntime) -> CapabilityReport:
    """Report the current capability of one runtime (sanitized detail)."""
    if runtime is AgentRuntime.CLAUDE:
        if _claude_installed():
            return CapabilityReport("full", _("Claude Code hooks installed."))
        return CapabilityReport("not_installed", _("Claude Code integration is not installed."))
    if runtime is AgentRuntime.HERMES:
        from .hermes_hooks import probe_hermes

        probe = probe_hermes()
        if not probe.supported:
            return CapabilityReport("unsupported", _("Hermes is unavailable: ") + _(probe.reason))
        return CapabilityReport(
            "full", _("Hermes ") + probe.version + " — " + _("shell hooks available.")
        )
    # Codex
    capability = probe_codex()
    if capability is CodexCapability.FULL:
        return CapabilityReport(
            "full",
            _("Codex app-server session ownership available."),
        )
    if capability is CodexCapability.COMPLETION_ONLY:
        return CapabilityReport(
            "completion_only",
            _("Codex completions only — session ownership unavailable."),
        )
    return CapabilityReport("unsupported", _("Codex activity is unsupported."))


def setup_runtime(runtime: AgentRuntime) -> IntegrationResult:
    """Install Moira-owned integration pieces for one runtime."""
    if runtime is AgentRuntime.CLAUDE:
        try:
            changed = setup_claude()
        except Exception:
            return IntegrationResult(False, probe_capability(runtime))
        return IntegrationResult(changed, probe_capability(runtime))
    if runtime is AgentRuntime.HERMES:
        from .hermes_hooks import probe_hermes

        probe = probe_hermes()
        if not probe.supported:
            return IntegrationResult(
                False,
                CapabilityReport("unsupported", _("Hermes is unavailable: ") + _(probe.reason)),
            )
        try:
            changed = setup_hermes()
        except Exception:
            return IntegrationResult(False, probe_capability(runtime))
        return IntegrationResult(changed, probe_capability(runtime))
    # Codex: nothing to install — re-probe the documented protocol.
    return IntegrationResult(False, probe_capability(runtime))


def remove_runtime(runtime: AgentRuntime) -> IntegrationResult:
    """Remove only the Moira-owned integration pieces for one runtime."""
    if runtime is AgentRuntime.CLAUDE:
        try:
            changed = remove_claude()
        except Exception:
            return IntegrationResult(False, probe_capability(runtime))
        return IntegrationResult(changed, probe_capability(runtime))
    if runtime is AgentRuntime.HERMES:
        try:
            changed = remove_hermes()
        except Exception:
            return IntegrationResult(False, probe_capability(runtime))
        return IntegrationResult(changed, probe_capability(runtime))
    # Codex: nothing owned to remove.
    return IntegrationResult(False, probe_capability(runtime))


def _fire_payload(payload: dict[str, Any], runtime: str) -> None:
    """Invoke the packaged hook exactly as the agent would (stdin JSON)."""
    import io
    import sys

    previous_stdin = sys.stdin
    sys.stdin = type("FakeInput", (), {"buffer": io.BytesIO(json.dumps(payload).encode())})()
    try:
        agent_hook_main([runtime])
    finally:
        sys.stdin = previous_stdin


def _claude_test() -> bool:
    """Prove the four Claude hook callbacks fire with documented payloads."""
    with tempfile.TemporaryDirectory() as temp:
        previous = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = temp
        try:
            store = ActivityStore()
            _fire_payload(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "moira-test-session",
                    "prompt": "must never be stored",
                },
                "claude",
            )
            store.reload()
            sessions = store.snapshot()["sessions"].get("claude", {})
            if (
                len(sessions) != 1
                or next(iter(sessions.values()))["state"] != ActivityState.RUNNING.value
            ):
                return False
            _fire_payload({"hook_event_name": "Stop", "session_id": "moira-test-session"}, "claude")
            store.reload()
            sessions = store.snapshot()["sessions"].get("claude", {})
            if next(iter(sessions.values()))["state"] != ActivityState.COMPLETED.value:
                return False
            # A second session exercises the failure path (a terminal
            # session is never replaced by a later StopFailure).
            _fire_payload(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "moira-test-session-2",
                    "prompt": "must never be stored",
                },
                "claude",
            )
            _fire_payload(
                {"hook_event_name": "StopFailure", "session_id": "moira-test-session-2"}, "claude"
            )
            store.reload()
            last = store.snapshot()["last_events"].get("claude", {})
            if last.get("state") != ActivityState.FAILED.value:
                return False
        finally:
            if previous is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = previous
    return True


def _codex_test() -> bool:
    """Prove the documented turn-notification mapping with a fake stream."""
    with tempfile.TemporaryDirectory() as temp:
        store = ActivityStore(Path(temp) / "activity.json")
        owned = {"owned-thread"}
        record_turn_notification(
            {
                "method": "turn/started",
                "params": {
                    "threadId": "owned-thread",
                    "turn": {"id": "turn-1", "status": "inProgress"},
                },
            },
            owned,
            store,
        )
        sessions = store.snapshot()["sessions"].get("codex", {})
        if (
            len(sessions) != 1
            or next(iter(sessions.values()))["state"] != ActivityState.RUNNING.value
        ):
            return False
        record_turn_notification(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "owned-thread",
                    "turn": {"id": "turn-1", "status": "completed"},
                },
            },
            owned,
            store,
        )
        sessions = store.snapshot()["sessions"].get("codex", {})
        if next(iter(sessions.values()))["state"] != ActivityState.COMPLETED.value:
            return False
    return True


def test_runtime(runtime: AgentRuntime) -> IntegrationResult:
    """Prove the callbacks fire; never persists fake success."""
    if runtime is AgentRuntime.CLAUDE:
        ok = _claude_test()
    elif runtime is AgentRuntime.HERMES:
        ok = test_hermes_hooks()
    else:
        ok = _codex_test()
    if ok:
        return IntegrationResult(False, CapabilityReport("full", _("Callbacks verified.")))
    return IntegrationResult(
        False, CapabilityReport("unsupported", _("Callback verification failed."))
    )
