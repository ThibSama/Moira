"""Hermes shell-hook adapter — version probe and owned hooks-block editor.

Moira observes Hermes through the documented shell-hook mechanism:
``pre_llm_call`` (start), ``post_llm_call`` (complete) and
``on_session_end`` (interrupt/complete) entries in the ``hooks:`` block of
``$HERMES_HOME/config.yaml`` (``~/.hermes`` by default), each pointing at
the packaged ``/usr/bin/moira-agent-hook hermes`` command.

Because Moira has no YAML dependency, the ``hooks:`` block is edited with a
strict subset editor: it parses ONLY the top-level ``hooks:`` block (event
mappings of list-of-mappings with scalar fields), merges/removes only the
Moira-owned command entries, preserves every other byte of the file, and
fails closed whenever the block contains anything outside the subset. The
result is round-trip re-parsed before the file is written atomically.

The probe runs ``hermes --version`` (documented version surface) and
``hermes hooks list``; when the CLI is absent or lacks shell-hook support
the capability fails closed to UNSUPPORTED with a translated reason.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .activity import ActivityState, ActivityStore
from .agent_hooks import AGENT_HOOK_COMMAND

#: Moira-owned Hermes hook command (identical for all three events).
HERMES_HOOK_COMMAND = f"{AGENT_HOOK_COMMAND} hermes"

#: Hermes hook events Moira observes, in canonical order.
HERMES_EVENTS = ("pre_llm_call", "post_llm_call", "on_session_end")

#: Hook entry timeout: bounds the subprocess so hooks stay nonblocking.
HERMES_HOOK_TIMEOUT = 5

#: Bounded subprocess timeouts for the live probes (seconds).
PROBE_TIMEOUT = 10

_VERSION_RE = re.compile(r"Hermes Agent v(\d+)\.(\d+)\.(\d+)")

#: YAML scalars with special meaning that are never accepted as plain
#: strings by the subset parser (fail closed instead).
_SPECIAL_SCALARS = {"true", "false", "null", "~", "yes", "no", "on", "off"}


class HermesHooksError(RuntimeError):
    """Fail-closed outcome for the Hermes hooks editor/probe."""


@dataclass(frozen=True, slots=True)
class HermesCapability:
    supported: bool
    version: str
    reason: str = ""


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def config_path() -> Path:
    return hermes_home() / "config.yaml"


def probe_hermes() -> HermesCapability:
    """Probe the installed Hermes version and shell-hook support.

    Fail closed: an absent binary, an unparseable version or a CLI without
    the ``hooks`` subcommand maps to UNSUPPORTED with a sanitized reason.
    """
    binary = shutil.which("hermes")
    if not binary:
        return HermesCapability(False, "", "not installed")
    try:
        version_out = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return HermesCapability(False, "", "version probe failed")
    match = _VERSION_RE.search(version_out or "")
    if not match:
        return HermesCapability(False, "", "version unknown")
    version = ".".join(match.groups())
    try:
        hooks_out = subprocess.run(
            [binary, "hooks", "list"],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return HermesCapability(False, version, "hooks probe failed")
    if hooks_out.returncode != 0:
        return HermesCapability(False, version, "shell hooks unsupported")
    return HermesCapability(True, version, "")


# ── Minimal YAML subset for the top-level hooks: block ────────────────────


@dataclass(frozen=True, slots=True)
class _BlockRegion:
    start: int  # line index of the `hooks:` key (inclusive)
    end: int  # line index just past the block (exclusive)


def _split_lines(text: str) -> list[str]:
    return text.split("\n")


def _is_blank(line: str) -> bool:
    return not line.strip()


def _is_comment(line: str) -> bool:
    return line.lstrip().startswith("#")


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip())


def _find_hooks_region(lines: list[str]) -> _BlockRegion | None:
    """Locate the top-level ``hooks:`` block (column-0 key, anywhere)."""
    for index, line in enumerate(lines):
        if _is_blank(line) or _is_comment(line):
            continue
        if _indent_of(line) != 0:
            continue
        stripped = line.strip()
        if stripped == "hooks:":
            return _BlockRegion(index, _block_end(lines, index))
        if stripped.startswith("hooks:"):
            raise HermesHooksError("hooks block uses unsupported inline or flow style")
    return None


def _block_end(lines: list[str], start: int) -> int:
    """First line after ``start`` that ends the block (next top-level key
    or a column-0 comment belonging to the next section)."""
    for index in range(start + 1, len(lines)):
        if _is_blank(lines[index]):
            continue
        if _indent_of(lines[index]) != 0:
            continue
        if _is_comment(lines[index]):
            return index
        stripped = lines[index].strip()
        if stripped in {"---", "..."} or stripped.startswith("%"):
            return index
        return index
    return len(lines)


def _parse_scalar(text: str) -> Any:
    """Parse one YAML scalar (strings, ints, floats, bools, null)."""
    value = text.strip()
    if value == "" or value in {"~", "null", "Null", "NULL"}:
        return None
    if value.startswith('"') and value.endswith('"') and len(value) >= 2:
        body = value[1:-1]
        return body.replace('\\"', '"').replace("\\\\", "\\")
    if value.startswith("'") and value.endswith("'") and len(value) >= 2:
        return value[1:-1].replace("''", "'")
    if value in {"true", "True", "TRUE"}:
        return True
    if value in {"false", "False", "FALSE"}:
        return False
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if re.fullmatch(r"-?\d+\.\d+", value):
        return float(value)
    if value[0] in "{[":  # flow collections — unsupported in the subset
        raise HermesHooksError("hooks block uses unsupported flow collection")
    if any(ch in value for ch in "&*!|>:"):
        raise HermesHooksError("hooks block uses unsupported YAML feature")
    if value.lower() in _SPECIAL_SCALARS:
        raise HermesHooksError("hooks block uses an ambiguous YAML scalar")
    return value


def _split_field(line: str) -> tuple[str, str] | None:
    """Split ``key: value`` at the first top-level colon, stripping a
    trailing comment that sits outside quotes."""
    quote: str | None = None
    for index, char in enumerate(line):
        if quote is not None:
            if char == quote and (index == 0 or line[index - 1] != "\\"):
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
            continue
        if char == ":":
            key = line[:index].strip()
            value = line[index + 1 :].strip()
            out: list[str] = []
            q: str | None = None
            for c in value:
                if q is not None:
                    out.append(c)
                    if c == q and (not out or out[-2] != "\\"):
                        q = None
                    continue
                if c in {'"', "'"}:
                    q = c
                    out.append(c)
                elif c == "#" and (not out or out[-1].isspace()):
                    break
                else:
                    out.append(c)
            return key, "".join(out).strip()
    return None


def _parse_hooks_block_text(block_lines: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Strictly parse the hooks-block body into events -> entries.

    Supported subset: a mapping of event names to lists of mappings whose
    fields are scalar values. Anything else raises HermesHooksError.
    """
    events: dict[str, list[dict[str, Any]]] = {}
    content = [line for line in block_lines if not _is_blank(line) and not _is_comment(line)]
    if not content:
        return events
    event_indent = _indent_of(content[0])
    if event_indent <= 0:
        raise HermesHooksError("hooks block entries must be indented")
    index = 0
    while index < len(content):
        line = content[index]
        indent = _indent_of(line)
        if indent != event_indent:
            raise HermesHooksError("hooks block has inconsistent indentation")
        stripped = line.strip()
        if stripped.startswith("- "):
            raise HermesHooksError("hooks block is not an event mapping")
        field = _split_field(stripped)
        if field is None:
            raise HermesHooksError("hooks block entry is not a mapping key")
        event_name, inline = field
        if not event_name:
            raise HermesHooksError("hooks block has an empty event name")
        if inline not in ("", "[]", "{}"):
            raise HermesHooksError("hooks event must map to a list")
        index += 1
        entries: list[dict[str, Any]] = []
        while index < len(content) and _indent_of(content[index]) > event_indent:
            item_line = content[index]
            item_indent = _indent_of(item_line)
            if item_indent != event_indent + 2:
                raise HermesHooksError("hooks entries must be indented consistently")
            stripped_item = item_line.strip()
            if not stripped_item.startswith("- "):
                raise HermesHooksError("hooks event body must be a list")
            item_body = stripped_item[2:].strip()
            if item_body.startswith("{") or item_body.startswith("["):
                raise HermesHooksError("hooks block uses unsupported flow collection")
            index += 1
            entry: dict[str, Any] = {}
            inline_field = _split_field(item_body)
            if inline_field is not None and inline_field[1] != "":
                entry[inline_field[0]] = _parse_scalar(inline_field[1])
            while index < len(content) and _indent_of(content[index]) == item_indent + 2:
                field_line = content[index]
                field_split = _split_field(field_line.strip())
                if field_split is None:
                    raise HermesHooksError("hooks entry field is malformed")
                entry[field_split[0]] = _parse_scalar(field_split[1])
                index += 1
            entries.append(entry)
        events[event_name] = entries
    return events


def parse_hooks_block(text: str) -> dict[str, list[dict[str, Any]]]:
    """Parse the top-level hooks block from a config file's text."""
    lines = _split_lines(text)
    region = _find_hooks_region(lines)
    if region is None:
        return {}
    return _parse_hooks_block_text(lines[region.start + 1 : region.end])


def _render_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_hooks_block(events: dict[str, list[dict[str, Any]]]) -> str:
    """Deterministic serialization of a hooks block (docs-style layout)."""
    lines = ["hooks:"]
    for event, entries in events.items():
        if not entries:
            continue
        lines.append(f"  {event}:")
        for entry in entries:
            items = list(entry.items())
            if not items:
                lines.append("    - null")
                continue
            lines.append(f"    - {items[0][0]}: {_render_scalar(items[0][1])}")
            for key, value in items[1:]:
                lines.append(f"      {key}: {_render_scalar(value)}")
    return "\n".join(lines)


def _own_entry() -> dict[str, Any]:
    return {"command": HERMES_HOOK_COMMAND, "timeout": HERMES_HOOK_TIMEOUT}


def merge_hooks(
    events: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, list[dict[str, Any]]], bool]:
    """Merge the Moira-owned Hermes hook entries (idempotent, additive).

    Other entries are preserved exactly; an existing entry with the Moira
    command is left untouched (already installed).
    """
    changed = False
    merged: dict[str, list[dict[str, Any]]] = {}
    for event in HERMES_EVENTS:
        entries = list(events.get(event, []))
        owned = [entry for entry in entries if entry.get("command") == HERMES_HOOK_COMMAND]
        if not owned:
            entries.append(_own_entry())
            changed = True
        merged[event] = entries
    for event, entries in events.items():
        if event not in HERMES_EVENTS:
            merged[event] = list(entries)
    return merged, changed


def remove_hooks(
    events: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, list[dict[str, Any]]], bool]:
    """Remove ONLY the Moira-owned Hermes hook entries.

    Unrelated events and entries are preserved; event keys emptied by the
    removal are dropped. Never touches anything Moira does not own.
    """
    changed = False
    merged: dict[str, list[dict[str, Any]]] = {}
    for event, entries in events.items():
        kept = [entry for entry in entries if entry.get("command") != HERMES_HOOK_COMMAND]
        if len(kept) != len(entries):
            changed = True
        if kept or event not in HERMES_EVENTS:
            merged[event] = kept
    return merged, changed


def _splice(text: str, region: _BlockRegion | None, rendered: str) -> str:
    """Replace the hooks block (or append it), preserving every other byte."""
    lines = _split_lines(text)
    if region is None:
        if text and not text.endswith("\n"):
            text += "\n"
        return text + rendered + "\n"
    return "\n".join(lines[: region.start] + rendered.split("\n") + lines[region.end :])


def set_hooks(text: str) -> tuple[str, bool]:
    """Return the config text with the Moira hooks merged (or an error).

    Round-trip validates the result before returning. ``changed`` is True
    when the text differs.
    """
    events = parse_hooks_block(text)
    region = _find_hooks_region(_split_lines(text))
    merged, changed = merge_hooks(events)
    if not changed:
        return text, False
    result = _splice(text, region, render_hooks_block(merged))
    if parse_hooks_block(result) != merged:
        raise HermesHooksError("hooks block round-trip validation failed")
    return result, True


def unset_hooks(text: str) -> tuple[str, bool]:
    """Return the config text with Moira-owned entries removed."""
    events = parse_hooks_block(text)
    merged, changed = remove_hooks(events)
    if not changed:
        return text, False
    region = _find_hooks_region(_split_lines(text))
    assert region is not None  # events were non-empty, so the block exists
    if not merged:
        lines = _split_lines(text)
        result = "\n".join(lines[: region.start] + lines[region.end :])
    else:
        result = _splice(text, region, render_hooks_block(merged))
    if parse_hooks_block(result) != merged:
        raise HermesHooksError("hooks block round-trip validation failed")
    return result, True


# ── Setup / remove / test orchestration ───────────────────────────────────


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(text)
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
    try:
        backup = path.with_name(path.name + ".moira-backup")
        if not backup.exists():
            _atomic_write(backup, path.read_text(encoding="utf-8"))
    except OSError:
        pass


def _read_config_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise HermesHooksError("cannot read the Hermes configuration") from exc


def setup(target: Path | None = None) -> bool:
    """Install the Moira-owned Hermes hook entries (idempotent)."""
    path = target or config_path()
    text = _read_config_text(path)
    updated, changed = set_hooks(text)
    if changed:
        _backup(path)
        _atomic_write(path, updated)
    return changed


def remove(target: Path | None = None) -> bool:
    """Remove only the Moira-owned Hermes hook entries."""
    path = target or config_path()
    text = _read_config_text(path)
    updated, changed = unset_hooks(text)
    if changed:
        _atomic_write(path, updated)
    return changed


def _fire_hook_payload(payload: dict[str, object]) -> None:
    """Invoke the packaged hook exactly as Hermes would (stdin JSON)."""
    from .agent_hooks import agent_hook_main

    previous_stdin = sys.stdin
    sys.stdin = type("FakeInput", (), {"buffer": io.BytesIO(json.dumps(payload).encode())})()
    try:
        agent_hook_main(["hermes"])
    finally:
        sys.stdin = previous_stdin


def test_hooks() -> bool:
    """Prove the configured callbacks fire with the documented payloads.

    Runs the packaged hook command against a throwaway XDG_STATE_HOME so
    no fake event persists into the real activity store. Verifies the full
    sequence: pre_llm_call starts a session, post_llm_call completes it,
    and on_session_end records the interrupted last event — a terminal
    session is never replaced.
    """
    with tempfile.TemporaryDirectory() as temp:
        state_home = Path(temp)
        previous = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = str(state_home)
        try:
            store = ActivityStore()
            _fire_hook_payload(
                {
                    "hook_event_name": "pre_llm_call",
                    "session_id": "moira-test-session",
                    "extra": {"model": "test-model", "is_first_turn": True},
                }
            )
            store.reload()
            started = store.snapshot()["sessions"].get("hermes", {})
            if (
                len(started) != 1
                or next(iter(started.values()))["state"] != ActivityState.RUNNING.value
            ):
                return False
            _fire_hook_payload(
                {
                    "hook_event_name": "post_llm_call",
                    "session_id": "moira-test-session",
                    "extra": {"model": "test-model"},
                }
            )
            store.reload()
            completed = store.snapshot()["sessions"].get("hermes", {})
            if (
                len(completed) != 1
                or next(iter(completed.values()))["state"] != ActivityState.COMPLETED.value
            ):
                return False
            _fire_hook_payload(
                {
                    "hook_event_name": "on_session_end",
                    "session_id": "moira-test-session",
                    "extra": {"model": "test-model", "completed": True, "interrupted": True},
                }
            )
            store.reload()
            last_event = store.snapshot()["last_events"].get("hermes")
            if last_event is None or last_event["state"] != ActivityState.INTERRUPTED.value:
                return False
        finally:
            if previous is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = previous
    return True
