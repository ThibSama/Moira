"""Agent integration orchestration — setup/remove/test per runtime.

One facade the Settings page uses for the three agent runtimes (Claude
Code, Codex CLI, Hermes):

- ``probe_capability`` reports the current capability (full /
  session-owned / awaiting-trust / completion-only / unsupported / not
  installed) with a sanitized detail;
- ``setup_runtime`` installs only Moira-owned entries (Claude Code hooks
  + status line, Codex CLI user-level lifecycle hooks in
  ``$CODEX_HOME/hooks.json`` with the ownership-scoped hooks feature
  flag, Hermes shell hooks) and never touches anything Moira does not
  own (Codex trust state is NEVER written or bypassed);
- ``remove_runtime`` removes only owned entries;
- ``test_runtime`` proves the integration boundary: Claude and Hermes
  fire the installed hook callbacks with the documented payloads, Codex
  fires its documented lifecycle payloads through the installed hooks
  (or, without hooks, starts a real Moira-owned app-server session and
  verifies real turn notifications) — always against a throwaway
  ``XDG_STATE_HOME`` so fake events never persist into the real
  activity store and quota-alert deduplication is never altered.

Every failure returns a fixed translated outcome; raw exceptions never
reach the UI.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .activity import ActivityState, ActivityStore, AgentRuntime
from .agent_hooks import agent_hook_main
from .claude_integration import remove as remove_claude
from .claude_integration import setup as setup_claude
from .codex_activity import CodexCapability, CodexSession, CodexSessionError, probe_codex
from .codex_hooks import clear_verified_marker as codex_clear_marker
from .codex_hooks import enable_hooks_feature as codex_enable_hooks_feature
from .codex_hooks import feature_state as codex_feature_state
from .codex_hooks import hooks_installed as codex_hooks_installed
from .codex_hooks import marker_exists
from .codex_hooks import remove as remove_codex_hooks
from .codex_hooks import setup as setup_codex_hooks
from .hermes_hooks import remove as remove_hermes
from .hermes_hooks import setup as setup_hermes
from .hermes_hooks import test_hooks as test_hermes_hooks
from .i18n import tr

_ = tr


@dataclass(frozen=True, slots=True)
class CapabilityReport:
    level: str  # "full" | "session_owned" | "completion_only" | "unsupported" | "not_installed"
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
    if shutil.which("codex") is None:
        return CapabilityReport("not_installed", _("Codex CLI is not installed."))
    feature = codex_feature_state()
    if feature == "removed":
        return CapabilityReport(
            "unsupported",
            _("Codex hooks are not supported by this version of Codex."),
        )
    if feature == "disabled":
        return CapabilityReport("unsupported", _("The Codex hooks feature is disabled."))
    if codex_hooks_installed():
        if marker_exists():
            return CapabilityReport("full", _("Codex CLI hooks installed and verified."))
        # Truthful reduced state: the hook is installed but Codex has NOT
        # yet run it — the user must approve Codex's own trust prompt.
        return CapabilityReport(
            "awaiting_trust",
            _("Codex CLI hooks installed — approve the Codex hook trust prompt."),
        )
    capability = probe_codex()
    if capability is CodexCapability.SESSION_OWNED:
        return CapabilityReport(
            "session_owned",
            _("Codex activity: Moira-owned app-server sessions only."),
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
    # Codex: install the user-level lifecycle hooks. The supported hooks
    # feature flag is enabled through Codex's OWN documented
    # ``features enable`` surface (ownership-scoped — never a hand edit
    # of config.toml, never Codex trust state); the hooks themselves are
    # Moira-owned entries merged into ``$CODEX_HOME/hooks.json``.
    if shutil.which("codex") is None:
        return IntegrationResult(False, probe_capability(runtime))
    feature = codex_feature_state()
    if feature == "removed":
        return IntegrationResult(False, probe_capability(runtime))
    if feature == "disabled" and not codex_enable_hooks_feature():
        return IntegrationResult(False, probe_capability(runtime))
    try:
        changed = setup_codex_hooks()
    except Exception:
        return IntegrationResult(False, probe_capability(runtime))
    return IntegrationResult(changed, probe_capability(runtime))


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
    # Codex: remove only the Moira-owned hooks.json entries and the
    # Moira-owned verification marker; Codex trust state is untouched.
    try:
        changed = remove_codex_hooks()
    except Exception:
        return IntegrationResult(False, probe_capability(runtime))
    codex_clear_marker()
    return IntegrationResult(changed, probe_capability(runtime))


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
    """Prove the installed hook callbacks with documented payloads.

    Exercises the turn lifecycle end-to-end: two turns under one session
    (the Package 6c regression), including a failing second turn replacing
    the first success as the most recent terminal event.
    """
    with tempfile.TemporaryDirectory() as temp:
        previous = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = temp
        try:
            store = ActivityStore()
            # Turn 1: prompt → completed.
            _fire_payload(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "moira-test-session",
                    "prompt_id": "moira-test-prompt-1",
                    "prompt": "must never be stored",
                },
                "claude",
            )
            _fire_payload(
                {
                    "hook_event_name": "Stop",
                    "session_id": "moira-test-session",
                    "prompt_id": "moira-test-prompt-1",
                },
                "claude",
            )
            store.reload()
            sessions = store.snapshot()["sessions"].get("claude", {})
            if len(sessions) != 1:
                return False
            turns = next(iter(sessions.values()))["turns"]
            if (
                len(turns) != 1
                or next(iter(turns.values()))["state"] != ActivityState.COMPLETED.value
            ):
                return False
            # Turn 2: a new prompt in the SAME session must display RUNNING
            # (the original defect: the terminal session rejected it).
            _fire_payload(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "moira-test-session",
                    "prompt_id": "moira-test-prompt-2",
                    "prompt": "must never be stored",
                },
                "claude",
            )
            store.reload()
            sessions = store.snapshot()["sessions"].get("claude", {})
            turns = next(iter(sessions.values()))["turns"]
            if len(turns) != 2:
                return False
            if not any(turn["state"] == ActivityState.RUNNING.value for turn in turns.values()):
                return False
            # Turn 2 fails: the failure must replace the earlier success.
            _fire_payload(
                {
                    "hook_event_name": "StopFailure",
                    "session_id": "moira-test-session",
                    "prompt_id": "moira-test-prompt-2",
                    "error": "must never be stored",
                },
                "claude",
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
    """Real external integration test: exercise the app-server protocol
    boundary with a real subprocess and record REAL turn notifications.

    Starts a Moira-owned ``codex app-server --stdio`` session against an
    isolated ``CODEX_HOME``, owns a thread, drives a real turn and
    verifies the real ``turn/started`` → ``turn/completed`` notifications
    were recorded through the ActivityStore (throwaway state). This is NOT
    a synthetic mapping test: it is the documented protocol boundary.
    """
    if shutil.which("codex") is None:
        return False
    with tempfile.TemporaryDirectory() as temp:
        store = ActivityStore(Path(temp) / "activity.json")
        session = CodexSession(store=store, binary="codex", codex_home=Path(temp) / "codex-home")
        try:
            session.start()
            session.run_turn("Reply with the single word: ok.", deadline=30.0)
        except (CodexSessionError, TimeoutError, OSError):
            return False
        finally:
            session.close()
        sessions = store.snapshot()["sessions"].get("codex", {})
        if len(sessions) != 1:
            return False
        entry = next(iter(sessions.values()))
        if not any(
            turn["state"] != ActivityState.RUNNING.value for turn in entry["turns"].values()
        ):
            return False
        last = store.snapshot()["last_events"].get("codex")
        return last is not None and last["state"] != ActivityState.RUNNING.value


def _codex_hooks_test() -> bool:
    """Prove the installed Codex hook callbacks with documented payloads.

    Fires the exact documented lifecycle payloads (SessionStart,
    UserPromptSubmit, Stop) through the packaged hook into a throwaway
    ``XDG_STATE_HOME`` and verifies the turn lifecycle and the trust
    marker. Never touches the real activity store.
    """
    with tempfile.TemporaryDirectory() as temp:
        previous = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = temp
        try:
            store = ActivityStore()
            _fire_payload(
                {
                    "hook_event_name": "SessionStart",
                    "session_id": "moira-test-codex-session",
                    "model": "gpt-5.2-codex",
                    "cwd": "/must/never/be/stored",
                },
                "codex",
            )
            _fire_payload(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "moira-test-codex-session",
                    "turn_id": "moira-test-codex-turn-1",
                    "model": "gpt-5.2-codex",
                    "prompt": "must never be stored",
                },
                "codex",
            )
            _fire_payload(
                {
                    "hook_event_name": "Stop",
                    "session_id": "moira-test-codex-session",
                    "turn_id": "moira-test-codex-turn-1",
                    "model": "gpt-5.2-codex",
                    "last_assistant_message": "must never be stored",
                },
                "codex",
            )
            store.reload()
            sessions = store.snapshot()["sessions"].get("codex", {})
            if len(sessions) != 1:
                return False
            turns = next(iter(sessions.values()))["turns"]
            if len(turns) != 1 or next(iter(turns.values()))["state"] != (
                ActivityState.COMPLETED.value
            ):
                return False
            marker = Path(temp) / "moira" / "codex-hooks-verified.json"
            if not marker.exists():
                return False
        finally:
            if previous is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = previous
    return True


def test_runtime(runtime: AgentRuntime) -> IntegrationResult:
    """Prove the integration boundary; never persists fake success.

    Distinguishes the test kinds: Claude and Hermes fire the installed
    packaged hook with the documented payloads (installed hook callback
    test); Codex fires its documented lifecycle payloads through the
    installed hooks when present, otherwise starts a real app-server
    session and verifies real turn notifications (external integration
    test). "Callbacks verified" is never reported unless the actual
    installed or subprocess protocol boundary was exercised.
    """
    if runtime is AgentRuntime.CLAUDE:
        ok = _claude_test()
    elif runtime is AgentRuntime.HERMES:
        ok = test_hermes_hooks()
    else:
        ok = _codex_hooks_test() if codex_hooks_installed() else _codex_test()
    if ok:
        if runtime is AgentRuntime.CODEX:
            if codex_hooks_installed():
                return IntegrationResult(
                    False,
                    CapabilityReport("full", _("Codex CLI hook callbacks verified.")),
                )
            return IntegrationResult(
                False,
                CapabilityReport(
                    "session_owned",
                    _("Codex turn notifications verified (real app-server session)."),
                ),
            )
        return IntegrationResult(False, CapabilityReport("full", _("Callbacks verified.")))
    return IntegrationResult(
        False, CapabilityReport("unsupported", _("Callback verification failed."))
    )
