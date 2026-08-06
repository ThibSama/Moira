"""Packaged agent hook entry — the command Claude Code, Codex CLI and Hermes invoke.

Usage: ``moira-agent-hook <runtime>`` where ``<runtime>`` is ``claude``,
``codex`` or ``hermes``. Reads a bounded JSON payload from stdin,
extracts only the privacy-minimal identity/model fields, records the
typed activity event and exits 0 with EMPTY stdout — the fixed observer
output. Any failure (bad JSON, oversized input, store error, unknown
event) is silent and still exits 0: hooks are network-free,
bounded-input, fixed-output and nonblocking, and failure leaves the
agent untouched.

Never stored: prompts, responses, transcripts, paths, raw payloads,
error/stop details, accounts or secrets. Claude Code hook inputs carry
``prompt``, ``last_assistant_message``, ``error`` and ``error_details``;
Codex hook inputs carry ``cwd``, ``transcript_path``, ``prompt``,
``last_assistant_message``, ``permission_mode`` and ``source`` — all of
them are ignored here (Codex is the user-level lifecycle contract of
Package 8a: SessionStart/UserPromptSubmit/Stop from
``$CODEX_HOME/hooks.json``; only the documented scalars
``hook_event_name``, ``session_id``, ``turn_id`` and ``model`` are read).

The wrapper honours ``XDG_STATE_HOME`` for the activity store, which lets
tests and the settings-page test action redirect writes to a throwaway
directory so fake events never persist into the real store.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .activity import (
    MAX_RAW_IDENTITY_LEN,
    ActivityEvent,
    ActivityOutcome,
    ActivityState,
    ActivityStore,
    AgentRuntime,
    LastActivityEvent,
    composite_identity,
    sanitize_model,
    validate_identity,
)

#: The packaged hook binary installed by the .deb; the runtime is the
#: first argument (``claude`` or ``hermes``). Reused by the Claude Code
#: and Hermes integration setups to build their owned hook commands.
AGENT_HOOK_COMMAND = "/usr/bin/moira-agent-hook"

#: Bounded stdin buffer: payloads larger than this are drained (not stored)
#: and the event is skipped.
MAX_HOOK_INPUT_BYTES = 256 * 1024

#: Bounded settings read for the model label (never the whole file).
MAX_SETTINGS_READ_BYTES = 64 * 1024

#: Claude Code hook events Moira owns and their state mapping.
CLAUDE_EVENT_STATES: dict[str, ActivityState] = {
    "UserPromptSubmit": ActivityState.RUNNING,
    "Stop": ActivityState.COMPLETED,
    "StopFailure": ActivityState.FAILED,
    "SessionEnd": ActivityState.INTERRUPTED,
}

#: Codex CLI user-level lifecycle events Moira observes (Package 8a).
#: SessionStart is validated for the trust marker but never fabricates
#: activity; UserPromptSubmit starts a turn; Stop is the documented
#: neutral terminal event (Codex documents no failure outcome in the
#: Stop payload — failures are only ever mapped from an explicit
#: documented outcome; a missing Stop expires through the watchdog to
#: INTERRUPTED, never to success).
CODEX_OBSERVED_EVENTS = ("SessionStart", "UserPromptSubmit", "Stop")
CODEX_EVENT_STATES: dict[str, ActivityState] = {
    "UserPromptSubmit": ActivityState.RUNNING,
    "Stop": ActivityState.COMPLETED,
}

#: Hermes shell-hook events Moira observes.
HERMES_EVENTS = ("pre_llm_call", "post_llm_call", "on_session_end")


def _read_bounded_stdin() -> str | None:
    """Read bounded JSON text from stdin; drain the rest without storing.

    Returns None when the input exceeds the bound (oversized) so the event
    is skipped, or when reading fails.
    """
    try:
        raw = sys.stdin.buffer.read(MAX_HOOK_INPUT_BYTES + 1)
    except (OSError, ValueError):
        return None
    if len(raw) > MAX_HOOK_INPUT_BYTES:
        # Drain the remainder (bounded memory, no EPIPE) and skip.
        try:
            while sys.stdin.buffer.read(MAX_HOOK_INPUT_BYTES):
                pass
        except (OSError, ValueError):
            pass
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _read_claude_model() -> str:
    """Best-effort sanitized model label from ``~/.claude/settings.json``.

    Reads only the first bounded bytes; any failure yields an empty label.
    """
    try:
        path = Path.home() / ".claude" / "settings.json"
        with path.open("r", encoding="utf-8") as handle:
            chunk = handle.read(MAX_SETTINGS_READ_BYTES)
        settings = json.loads(chunk)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return ""
    if not isinstance(settings, dict):
        return ""
    return settings.get("model", "") if isinstance(settings.get("model"), str) else ""


def _session_hash(payload: dict[str, Any]) -> str | None:
    raw = payload.get("session_id")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return validate_identity(raw)
    except ValueError:
        return None


def _turn_hash(payload: dict[str, Any], session_raw: str | None) -> str | None:
    """Hashed provider turn identity bound to its session, or None.

    Claude Code supplies ``prompt_id`` (a base-field UUID correlating a
    user prompt with all subsequent events until the next prompt); Hermes
    supplies ``extra.turn_id``. Missing or oversized identifiers fail
    closed to None and the store derives an identity from the lifecycle.
    """
    if session_raw is None:
        return None
    raw = payload.get("prompt_id")
    if isinstance(raw, str) and raw.strip():
        return composite_identity(session_raw, raw)
    extra = payload.get("extra")
    if isinstance(extra, dict):
        raw = extra.get("turn_id")
        if isinstance(raw, str) and raw.strip():
            return composite_identity(session_raw, raw)
    return None


def _record_claude(payload: dict[str, Any], store: ActivityStore) -> None:
    event_name = payload.get("hook_event_name")
    if not isinstance(event_name, str):
        return
    state = CLAUDE_EVENT_STATES.get(event_name)
    if state is None:
        return  # an event Moira does not own — observer ignores it
    session_raw = payload.get("session_id")
    session_hash = _session_hash(payload)
    if session_hash is None:
        return
    store.record(
        ActivityEvent(
            runtime=AgentRuntime.CLAUDE,
            state=state,
            session_hash=session_hash,
            turn_hash=_turn_hash(payload, session_raw if isinstance(session_raw, str) else None),
            model=_read_claude_model(),
            at=datetime.now(UTC),
        )
    )


def _record_hermes(payload: dict[str, Any], store: ActivityStore) -> None:
    event_name = payload.get("hook_event_name")
    if not isinstance(event_name, str) or event_name not in HERMES_EVENTS:
        return
    extra = payload.get("extra")
    model = ""
    if isinstance(extra, dict):
        raw_model = extra.get("model")
        if isinstance(raw_model, str):
            model = raw_model
    session_raw = payload.get("session_id")
    session_hash = _session_hash(payload)
    if session_hash is None:
        return
    turn_hash = _turn_hash(payload, session_raw if isinstance(session_raw, str) else None)
    now = datetime.now(UTC)
    if event_name == "pre_llm_call":
        store.record(
            ActivityEvent(
                runtime=AgentRuntime.HERMES,
                state=ActivityState.RUNNING,
                session_hash=session_hash,
                turn_hash=turn_hash,
                model=model,
                at=now,
            )
        )
        return
    if event_name == "post_llm_call":
        store.record(
            ActivityEvent(
                runtime=AgentRuntime.HERMES,
                state=ActivityState.COMPLETED,
                session_hash=session_hash,
                turn_hash=turn_hash,
                model=model,
                at=now,
            )
        )
        return
    # on_session_end — a session-bound terminal signal for the session's
    # current turn when known, otherwise a runtime-scoped completion
    # notifier. Interrupted wins over completed; an unknown end defaults
    # to interrupted, never to success.
    interrupted = isinstance(extra, dict) and extra.get("interrupted") is True
    completed = isinstance(extra, dict) and extra.get("completed") is True
    state = ActivityState.INTERRUPTED if (interrupted or not completed) else ActivityState.COMPLETED
    outcome = store.record(
        ActivityEvent(
            runtime=AgentRuntime.HERMES,
            state=state,
            session_hash=session_hash,
            turn_hash=turn_hash,
            model=model,
            at=now,
        )
    )
    if outcome is ActivityOutcome.REJECTED:
        store.record_last(
            LastActivityEvent(
                runtime=AgentRuntime.HERMES,
                state=state,
                model=model,
                at=now,
            )
        )


def _record_codex(payload: dict[str, Any], store: ActivityStore) -> None:
    """Record one Codex CLI lifecycle event (Package 8a, bounded decode).

    Accepts ONLY the documented user-level events and the documented
    scalar fields ``hook_event_name``, ``session_id``, ``turn_id`` and
    ``model``; every other field (cwd, transcript_path, prompt,
    last_assistant_message, permission_mode, source, agent_type, …) is
    ignored and never stored. SessionStart validates the session and
    proves Codex executed the hook (trust marker) but NEVER fabricates
    activity, model, tokens or outcome. UserPromptSubmit starts one
    RUNNING turn; Stop terminates the exact documented turn when a
    ``turn_id`` exists, otherwise the store closes only the newest
    compatible running turn in that validated session.
    """
    event_name = payload.get("hook_event_name")
    if not isinstance(event_name, str) or event_name not in CODEX_OBSERVED_EVENTS:
        return  # an event Moira does not own — observer ignores it
    session_raw = payload.get("session_id")
    if not isinstance(session_raw, str) or not session_raw.strip():
        return
    try:
        session_hash = validate_identity(session_raw)
    except ValueError:
        return
    # Documented scalars only (fail closed): a present-but-non-string or
    # oversized turn id rejects the whole event — the derived fallback is
    # reserved for a genuinely ABSENT turn id (requirement 3).
    turn_raw = payload.get("turn_id")
    turn_hash = None
    if turn_raw is not None:
        if not isinstance(turn_raw, str) or len(turn_raw) > MAX_RAW_IDENTITY_LEN:
            return
        if turn_raw.strip():
            turn_hash = composite_identity(session_raw, turn_raw)
    model_raw = payload.get("model")
    if model_raw is not None and not isinstance(model_raw, str):
        return  # a non-scalar model is malformed — never recorded
    model = sanitize_model(model_raw if isinstance(model_raw, str) else "")
    from .codex_hooks import write_verified_marker

    write_verified_marker()  # Codex really executed the hook → trust evidence
    state = CODEX_EVENT_STATES.get(event_name)
    if state is None:
        return  # SessionStart: metadata validated, zero fabricated activity
    store.record(
        ActivityEvent(
            runtime=AgentRuntime.CODEX,
            state=state,
            session_hash=session_hash,
            turn_hash=turn_hash,
            model=model,
            at=datetime.now(UTC),
        )
    )


def agent_hook_main(argv: list[str] | None = None) -> int:
    """Run the packaged hook for one runtime. Always exits 0, empty stdout."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return 0
    runtime = args[0]
    if runtime not in ("claude", "codex", "hermes"):
        return 0
    payload_text = _read_bounded_stdin()
    if payload_text is None:
        return 0
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError:
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        store = ActivityStore()
        if runtime == "claude":
            _record_claude(payload, store)
        elif runtime == "codex":
            _record_codex(payload, store)
        else:
            _record_hermes(payload, store)
    except Exception:
        # Failure leaves the agent untouched: still exit 0, still no output.
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(agent_hook_main())
