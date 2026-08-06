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

Setup/remove touch only Moira-owned entries (exact command match),
preserve every unrelated hook, use backup + atomic replace + restrictive
permissions (0600), recover after interruption (a stale temporary file
can never corrupt the atomic file) and are idempotent. A malformed
existing ``hooks.json`` is NEVER clobbered — setup fails closed.

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


def _is_moira_group(group: Any, command: str) -> bool:
    return (
        isinstance(group, dict)
        and isinstance(group.get("hooks"), list)
        and any(
            isinstance(hook, dict)
            and hook.get("type") == "command"
            and hook.get("command") == command
            for hook in group["hooks"]
        )
    )


def read_hooks_file(path: Path | None = None) -> dict[str, Any]:
    """Read and validate ``hooks.json`` (fail closed).

    Returns ``{"description": ..., "hooks": {...}}`` (the canonical
    ``HooksFile`` shape). A missing file yields the empty shape; a
    malformed, oversized or non-object file raises ``CodexHookError`` —
    the user's file is never clobbered.
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
    hooks = raw.get("hooks", {})
    if not isinstance(hooks, dict):
        raise CodexHookError("Codex hooks.json hooks must be an object")
    return {"description": raw.get("description"), "hooks": hooks}


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


def _with_moira_groups(data: dict[str, Any], command: str) -> dict[str, Any]:
    hooks = data["hooks"]
    merged = {event: list(groups) for event, groups in hooks.items()}
    for event in CODEX_HOOK_EVENTS:
        groups = [g for g in merged.get(event, []) if not _is_moira_group(g, command)]
        groups.append(_moira_group(command))
        merged[event] = groups
    return {"description": data.get("description"), "hooks": merged}


def setup(path: Path | None = None, command: str = MOIRA_HOOK_COMMAND) -> bool:
    """Install Moira-owned hook entries (idempotent, atomic, backed up).

    Returns True when the file changed. Preserves every unrelated entry
    and the ``description``. Raises ``CodexHookError`` on a malformed
    existing file (fail closed — never clobbered). A stale temporary
    file from an interrupted write can never corrupt the atomic file.
    """
    target = path or hooks_path()
    current = read_hooks_file(target)
    if all(
        any(_is_moira_group(g, command) for g in current["hooks"].get(event, []))
        for event in CODEX_HOOK_EVENTS
    ):
        return False  # already installed — nothing to change
    merged = _with_moira_groups(current, command)
    if not merged.get("description"):
        merged["description"] = "Moira agent activity observer hooks"
    _backup(target)
    _atomic_json(target, merged)
    return True


def remove(path: Path | None = None, command: str = MOIRA_HOOK_COMMAND) -> bool:
    """Remove ONLY Moira-owned hook entries (idempotent, atomic, backed up).

    Unrelated entries are preserved; an event left empty is dropped; a
    file that held nothing but Moira entries is deleted (restoring the
    pre-setup state). Returns True when something changed.
    """
    target = path or hooks_path()
    if not target.exists():
        return False
    current = read_hooks_file(target)
    hooks = current["hooks"]
    kept: dict[str, list[Any]] = {}
    for event, groups in hooks.items():
        remaining = [g for g in groups if not _is_moira_group(g, command)]
        if remaining:
            kept[event] = remaining
    if kept == hooks and all(
        not any(_is_moira_group(g, command) for g in groups) for groups in hooks.values()
    ):
        return False  # no Moira entry present
    _backup(target)
    if not kept:
        try:
            target.unlink()
        except OSError:
            pass
        return True
    _atomic_json(target, {"description": current.get("description"), "hooks": kept})
    return True


def hooks_installed(path: Path | None = None, command: str = MOIRA_HOOK_COMMAND) -> bool:
    """True when every owned event carries a Moira group in the file."""
    try:
        current = read_hooks_file(path)
    except CodexHookError:
        return False
    return all(
        any(_is_moira_group(g, command) for g in current["hooks"].get(event, []))
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
