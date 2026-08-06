"""Codex CLI hooks — ordinary CLI/TUI activity into the Agent activity panel.

Package 8a: Moira observes ordinary Codex CLI/TUI sessions through the
documented USER-LEVEL lifecycle hooks installed in
``$CODEX_HOME/hooks.json`` (never project-local hooks). Verified against
Codex CLI 0.146.0 and the official sources:

- feature flag ``hooks`` (stable; ``codex features list`` shows the
  effective state);
- ``hooks.json`` (JSON, ``HooksFile``): ``description`` plus per-event
  arrays of ``MatcherGroup`` ``{matcher, hooks: [{type: "command",
  command}]}`` (codex-rs/config/src/hook_config.rs);
- the payload is written to the hook command's STDIN (codex-rs/hooks
  command_runner) per the official generated input schemas; Moira uses
  exactly ``SessionStart``, ``UserPromptSubmit`` and ``Stop`` and reads
  only the documented scalar fields ``hook_event_name``, ``session_id``,
  ``turn_id`` and ``model`` — ``cwd``, ``transcript_path``, ``prompt``,
  ``last_assistant_message`` and every other field are ignored and never
  stored;
- trust is Codex-owned: the first run prompts the user and the hook runs
  only after approval. Moira NEVER writes Codex trust state and never
  bypasses it (``--dangerously-bypass-hook-trust`` is never used). The
  hook writes a Moira-owned verification marker ONLY when Codex
  actually executed it, which is what lifts the capability from
  ``awaiting_trust`` (reduced) to ``full``.

Setup/remove manage ownership at the INDIVIDUAL handler level (Package
8b): the owned handler is exactly ``{type: "command", command:
MOIRA_HOOK_COMMAND}`` — a mixed ``MatcherGroup`` is preserved handler by
handler, with every sibling, matcher and unrelated group field kept
without semantic alteration; a group is dropped only when its hooks list
becomes empty from a removal, an event only when its group list becomes
empty. Setup leaves an existing valid owned handler in place and
collapses duplicate exact Moira handlers. The complete mutable shape
(root object, optional string/null description, hooks object, event
lists, group objects with list-valued hooks, handler objects with string
type and string command for command handlers) is validated BEFORE any
backup or write; any malformed shape raises ``CodexHookError`` and the
original file stays byte-identical. Writes are backup + atomic replace
at 0600, recover after interruption (a stale temporary file can never
corrupt the atomic file) and are idempotent. A malformed existing
``hooks.json`` is NEVER clobbered — setup fails closed.

No ``pgrep``, ``/proc``, transcript scraping, daemon, polling or global
process monitor: the hook is a pure event observer. Desktop, IDE, cloud
and ``codex exec`` support are NOT claimed unless verified elsewhere.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .agent_hooks import AGENT_HOOK_COMMAND

#: The three documented user-level lifecycle events Moira owns.
CODEX_HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "Stop")

#: The user-level hooks file name (never project-local .codex/hooks.json).
HOOKS_FILE_NAME = "hooks.json"

#: Exact Moira-owned hook command installed in hooks.json (the ownership
#: marker: setup/remove match this string and nothing else).
MOIRA_HOOK_COMMAND = f"{AGENT_HOOK_COMMAND} codex"

#: Bounded hooks.json size: a larger file fails closed.
MAX_HOOKS_FILE_BYTES = 1_048_576

#: Bounded deadline for the ``codex features list`` subprocess.
FEATURES_DEADLINE_SECONDS = 10.0

#: Verification-marker file name under ``$XDG_STATE_HOME/moira``.
VERIFIED_MARKER_NAME = "codex-hooks-verified.json"


class CodexHookError(RuntimeError):
    """Fail-closed outcome for hooks.json management."""


def codex_home() -> Path:
    """Return the Codex config directory (``$CODEX_HOME`` or ~/.codex)."""
    raw = os.environ.get("CODEX_HOME")
    if raw and raw.strip():
        return Path(raw).expanduser()
    return Path.home() / ".codex"


def hooks_path() -> Path:
    """Return the user-level ``hooks.json`` path."""
    return codex_home() / HOOKS_FILE_NAME


def marker_path() -> Path:
    """Return the Moira-owned hook-verification marker path."""
    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return state_home / "moira" / VERIFIED_MARKER_NAME


# ── Hooks file management ──────────────────────────────────────────────────


def _moira_group(command: str) -> dict[str, Any]:
    return {"matcher": "*", "hooks": [{"type": "command", "command": command}]}


def _is_moira_handler(handler: Any, command: str) -> bool:
    """Ownership at the INDIVIDUAL handler level (Package 8b).

    The owned handler is EXACTLY ``{"type": "command", "command":
    MOIRA_HOOK_COMMAND}`` — never a group, never a matcher, never a
    sibling handler. A mixed ``MatcherGroup`` holding the Moira command
    next to unrelated handlers is owned only through its single Moira
    handler; every sibling survives.
    """
    return (
        isinstance(handler, dict)
        and handler.get("type") == "command"
        and handler.get("command") == command
    )


def read_hooks_file(path: Path | None = None) -> dict[str, Any]:
    """Read and validate ``hooks.json`` (fail closed, complete shape).

    Returns ``{"description": ..., "hooks": {...}}`` (the canonical
    ``HooksFile`` shape). A missing file yields the empty shape. The
    COMPLETE mutable shape is validated BEFORE any backup or write
    (Package 8b): the root object, an optional string/null description,
    the ``hooks`` object, every event value a list, every group an
    object with a list-valued ``hooks``, every handler an object with a
    string ``type``, and command handlers a string ``command``. Any
    unsafe or malformed shape raises ``CodexHookError`` and the original
    file is never touched. Supported non-command handlers (prompt,
    agent) and additional fields (matcher, commandWindows, timeout,
    async, statusMessage, …) are preserved untouched.
    """
    target = path or hooks_path()
    try:
        if not target.exists():
            return {"hooks": {}}
        size = target.stat().st_size
        if size > MAX_HOOKS_FILE_BYTES:
            raise CodexHookError("Codex hooks.json exceeds the supported size")
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"hooks": {}}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise CodexHookError("Codex hooks.json is malformed") from exc
    if not isinstance(raw, dict):
        raise CodexHookError("Codex hooks.json must be an object")
    description = raw.get("description")
    if description is not None and not isinstance(description, str):
        raise CodexHookError("Codex hooks.json description must be a string or null")
    hooks = raw.get("hooks", {})
    if not isinstance(hooks, dict):
        raise CodexHookError("Codex hooks.json hooks must be an object")
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            raise CodexHookError(f"Codex hooks.json event {event!r} must be a list")
        for group in groups:
            if not isinstance(group, dict):
                raise CodexHookError("Codex hooks.json hook groups must be objects")
            handlers = group.get("hooks")
            if not isinstance(handlers, list):
                raise CodexHookError("Codex hooks.json hook groups must carry a list-valued hooks")
            for handler in handlers:
                if not isinstance(handler, dict):
                    raise CodexHookError("Codex hooks.json handlers must be objects")
                handler_type = handler.get("type")
                if not isinstance(handler_type, str):
                    raise CodexHookError("Codex hooks.json handler type must be a string")
                if handler_type == "command" and not isinstance(handler.get("command"), str):
                    raise CodexHookError(
                        "Codex hooks.json command handlers must carry a string command"
                    )
    return {"description": description, "hooks": hooks}


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    """Write JSON atomically at mode 0600 (temp + fsync + replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _backup(path: Path) -> None:
    """Snapshot the current file before any replace (restrictive mode)."""
    if path.exists():
        _atomic_json(path.with_name(f"{path.name}.moira-backup"), read_hooks_file(path))


def _merge_owned(data: dict[str, Any], command: str) -> dict[str, Any]:
    """Guarantee the exact Moira handler exactly once per owned event.

    Handler-level merge (Package 8b): an existing valid owned handler is
    LEFT IN PLACE inside its group (siblings, matcher and every group
    field preserved without semantic alteration); duplicate exact Moira
    handlers are collapsed to one; a fresh Moira group is appended only
    when no group carries the exact Moira handler.
    """
    hooks: dict[str, list[Any]] = {}
    for event, groups in data["hooks"].items():
        event_groups: list[Any] = []
        for group in groups:
            owned_seen = False
            kept: list[Any] = []
            for handler in group["hooks"]:
                if _is_moira_handler(handler, command):
                    if owned_seen:
                        continue  # collapse duplicate exact Moira handlers
                    owned_seen = True
                kept.append(handler)
            merged_group = dict(group)
            merged_group["hooks"] = kept
            event_groups.append(merged_group)
        hooks[event] = event_groups
    for event in CODEX_HOOK_EVENTS:
        present = any(
            _is_moira_handler(handler, command)
            for group in hooks.get(event, [])
            for handler in group["hooks"]
        )
        if not present:
            hooks.setdefault(event, []).append(_moira_group(command))
    return {"description": data.get("description"), "hooks": hooks}


def _without_owned(data: dict[str, Any], command: str) -> dict[str, Any]:
    """Remove ONLY the exact Moira handler from every group.

    A group is dropped only when its ``hooks`` list BECOMES empty as a
    result of the removal; an event is dropped only when its group list
    becomes empty. Sibling handlers, matchers, unrelated groups and
    unrelated events survive untouched.
    """
    hooks: dict[str, list[Any]] = {}
    for event, groups in data["hooks"].items():
        event_groups: list[Any] = []
        for group in groups:
            had_owned = any(_is_moira_handler(h, command) for h in group["hooks"])
            kept = [h for h in group["hooks"] if not _is_moira_handler(h, command)]
            if had_owned and not kept:
                continue  # drop the group only when the removal emptied it
            merged_group = dict(group)
            merged_group["hooks"] = kept
            event_groups.append(merged_group)
        if event_groups:
            hooks[event] = event_groups
    return {"description": data.get("description"), "hooks": hooks}


def setup(path: Path | None = None, command: str = MOIRA_HOOK_COMMAND) -> bool:
    """Install Moira-owned hook handlers (idempotent, atomic, backed up).

    Returns True when the file changed. Ownership is per HANDLER: an
    existing valid owned handler stays in place, duplicate exact Moira
    handlers collapse to one, and every unrelated handler, group,
    matcher and field is preserved. Raises ``CodexHookError`` on any
    malformed existing shape (fail closed, never clobbered) — the
    complete shape is validated before any backup or write. A stale
    temporary file from an interrupted write can never corrupt the
    atomic file.
    """
    target = path or hooks_path()
    current = read_hooks_file(target)
    merged = _merge_owned(current, command)
    if merged == current:
        return False  # already installed with exactly one owned handler each
    if not merged.get("description"):
        merged["description"] = "Moira agent activity observer hooks"
    _backup(target)
    _atomic_json(target, merged)
    return True


def remove(path: Path | None = None, command: str = MOIRA_HOOK_COMMAND) -> bool:
    """Remove ONLY the Moira-owned handler entries (idempotent, atomic).

    Unrelated entries are preserved; a group is dropped only when its
    hooks list becomes empty from the removal; an event only when its
    group list becomes empty; a file that held nothing but Moira
    entries is deleted (restoring the pre-setup state). Returns True
    when something changed.
    """
    target = path or hooks_path()
    if not target.exists():
        return False
    current = read_hooks_file(target)
    if not any(
        _is_moira_handler(handler, command)
        for groups in current["hooks"].values()
        for group in groups
        for handler in group["hooks"]
    ):
        return False  # no Moira handler present
    merged = _without_owned(current, command)
    _backup(target)
    if not merged["hooks"]:
        try:
            target.unlink()
        except OSError:
            pass
        return True
    _atomic_json(target, merged)
    return True


def hooks_installed(path: Path | None = None, command: str = MOIRA_HOOK_COMMAND) -> bool:
    """True when every owned event carries the exact Moira handler."""
    try:
        current = read_hooks_file(path)
    except CodexHookError:
        return False
    return all(
        any(
            _is_moira_handler(handler, command)
            for group in current["hooks"].get(event, [])
            for handler in group["hooks"]
        )
        for event in CODEX_HOOK_EVENTS
    )


# ── Verification marker (trust evidence) ───────────────────────────────────


def write_verified_marker() -> None:
    """Record that Codex actually executed the Moira hook (trust granted).

    Written ONLY by the hook process after a valid owned event arrives —
    the marker appears only when Codex ran the hook, so it is the honest
    evidence that lifts ``awaiting_trust`` to ``full``. Privacy-minimal
    (version + timestamp); atomic at 0600; silent failure (the hook is
    an observer and must never fail).
    """
    try:
        from datetime import UTC, datetime

        _atomic_json(
            marker_path(),
            {"version": 1, "at": datetime.now(UTC).isoformat(), "runtime": "codex"},
        )
    except Exception:
        pass  # the agent is never affected


def clear_verified_marker() -> None:
    """Remove the Moira-owned marker (used by the removal path)."""
    try:
        marker_path().unlink()
    except OSError:
        pass


def marker_exists() -> bool:
    try:
        size = marker_path().stat().st_size
        return 0 < size <= MAX_HOOKS_FILE_BYTES
    except OSError:
        return False


# ── Feature flag (ownership-scoped, verified) ──────────────────────────────


def feature_state(binary: str = "codex", timeout: float = FEATURES_DEADLINE_SECONDS) -> str:
    """Report the effective state of the ``hooks`` feature flag.

    Runs the documented ``codex features list`` surface (bounded). The
    installed 0.146.0 reports ``hooks`` as stable and enabled. Returns
    ``"enabled"``, ``"disabled"`` or ``"removed"``; a missing binary,
    failed or unparseable probe fails closed to ``"disabled"``.
    """
    executable = shutil.which(binary)
    if not executable:
        return "disabled"
    try:
        proc = subprocess.run(
            [executable, "features", "list"],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=dict(os.environ),
        )
    except (OSError, subprocess.SubprocessError):
        return "disabled"
    if proc.returncode != 0:
        return "disabled"
    for line in proc.stdout.splitlines():
        columns = line.split()
        if len(columns) >= 3 and columns[0] == "hooks":
            stage, effective = columns[1], columns[2]
            if stage == "removed":
                return "removed"
            return "enabled" if effective == "true" else "disabled"
    return "disabled"  # no hooks row at all: the feature is unknown


def enable_hooks_feature(binary: str = "codex", timeout: float = FEATURES_DEADLINE_SECONDS) -> bool:
    """Enable the supported ``hooks`` feature flag (ownership-scoped).

    Delegates to Codex's OWN documented ``codex features enable hooks``
    surface so the edit touches only the features table — Moira never
    hand-edits Codex's config.toml and never touches trust state.
    """
    executable = shutil.which(binary)
    if not executable:
        return False
    try:
        proc = subprocess.run(
            [executable, "features", "enable", "hooks"],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=dict(os.environ),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0
