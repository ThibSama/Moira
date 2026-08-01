from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import QuotaReading, QuotaStatus, Service

STATUS_LINE_COMMAND = "/usr/bin/moira-claude-statusline"
CACHE_MAX_AGE_SECONDS = 15 * 60
WINDOWS = (("five_hour", "Five-hour"), ("seven_day", "Weekly"))


class ClaudeIntegrationError(RuntimeError):
    pass


def _config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


def _state_home() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))


def settings_path() -> Path:
    return Path.home() / ".claude/settings.json"


def integration_path() -> Path:
    return _config_home() / "moira/claude-integration.json"


def cache_path() -> Path:
    return _state_home() / "moira/claude-rate-limits.json"


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
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


def _read_object(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ClaudeIntegrationError(f"{description} not found") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ClaudeIntegrationError(f"{description} is not readable JSON") from exc
    if not isinstance(value, dict):
        raise ClaudeIntegrationError(f"{description} must contain a JSON object")
    return value


def _percentage(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("percentage is not numeric")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 100:
        raise ValueError("percentage outside 0..100")
    return result


def _epoch(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("reset epoch is invalid")
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError as exc:
            raise ValueError("reset epoch is invalid") from exc
    else:
        raise ValueError("reset epoch is invalid")
    if not math.isfinite(result) or not 0 < result <= 253_402_300_799:
        raise ValueError("reset epoch is invalid")
    return result


def minimal_cache(payload: dict[str, Any], retrieved_at: float | None = None) -> dict[str, Any]:
    limits = payload.get("rate_limits")
    if not isinstance(limits, dict):
        raise ValueError("rate_limits missing")
    retrieved = float(time.time() if retrieved_at is None else retrieved_at)
    if not math.isfinite(retrieved) or retrieved <= 0:
        raise ValueError("retrieval time is invalid")
    result: dict[str, Any] = {}
    for key, _label in WINDOWS:
        window = limits.get(key)
        if not isinstance(window, dict):
            raise ValueError(f"rate_limits.{key} missing")
        result[key] = {
            "percentage": _percentage(window.get("used_percentage")),
            "reset_epoch": _epoch(window.get("resets_at")),
            "retrieved_at": retrieved,
            "service": Service.CLAUDE.value,
        }
    return result


def update_cache(payload: dict[str, Any], retrieved_at: float | None = None) -> bool:
    try:
        value = minimal_cache(payload, retrieved_at)
    except ValueError:
        return False
    _atomic_json(cache_path(), value)
    return True


def load_cached_readings(now: datetime | None = None) -> list[QuotaReading]:
    current = now or datetime.now(UTC)
    source = "claude-status-line:rate_limits"
    try:
        value = _read_object(cache_path(), "Claude rate-limit cache")
        readings: list[QuotaReading] = []
        for key, label in WINDOWS:
            item = value.get(key)
            if not isinstance(item, dict) or set(item) != {
                "percentage",
                "reset_epoch",
                "retrieved_at",
                "service",
            }:
                raise ValueError("cache fields malformed")
            if item["service"] != Service.CLAUDE.value:
                raise ValueError("cache service malformed")
            percentage = _percentage(item["percentage"])
            reset = datetime.fromtimestamp(_epoch(item["reset_epoch"]), UTC)
            retrieved = datetime.fromtimestamp(_epoch(item["retrieved_at"]), UTC)
            age = (current - retrieved).total_seconds()
            status = QuotaStatus.AVAILABLE if age <= CACHE_MAX_AGE_SECONDS else QuotaStatus.STALE
            detail = (
                "" if status is QuotaStatus.AVAILABLE else "Waiting for a recent Claude response"
            )
            readings.append(
                QuotaReading(
                    Service.CLAUDE,
                    label,
                    percentage,
                    reset,
                    retrieved,
                    source,
                    status,
                    detail,
                )
            )
        return readings
    except ClaudeIntegrationError as exc:
        detail = str(exc)
        status = QuotaStatus.UNAVAILABLE
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        detail = "Claude rate-limit cache is malformed"
        status = QuotaStatus.PARSE_ERROR
    return [
        QuotaReading(
            Service.CLAUDE,
            "Five-hour",
            None,
            None,
            current,
            source,
            status,
            detail,
        )
    ]


def setup(
    claude_settings: Path | None = None,
    metadata: Path | None = None,
    command: str = STATUS_LINE_COMMAND,
) -> bool:
    target = claude_settings or settings_path()
    record_path = metadata or integration_path()
    settings = _read_object(target, "Claude settings")
    current = settings.get("statusLine")
    if isinstance(current, dict) and current.get("command") == command:
        if not record_path.exists():
            raise ClaudeIntegrationError("Moira status line exists without restoration metadata")
        return False
    if current is not None and (
        not isinstance(current, dict)
        or current.get("type") != "command"
        or not isinstance(current.get("command"), str)
        or not current["command"].strip()
    ):
        raise ClaudeIntegrationError("existing Claude status line cannot be safely chained")
    backup = target.with_name(f"{target.name}.moira-backup")
    _atomic_json(backup, settings)
    _atomic_json(record_path, {"original_status_line": current, "moira_command": command})
    replacement = dict(current) if isinstance(current, dict) else {"type": "command"}
    replacement["type"] = "command"
    replacement["command"] = command
    settings["statusLine"] = replacement
    _atomic_json(target, settings)
    return True


def remove(
    claude_settings: Path | None = None,
    metadata: Path | None = None,
    command: str = STATUS_LINE_COMMAND,
) -> bool:
    target = claude_settings or settings_path()
    record_path = metadata or integration_path()
    if not record_path.exists():
        return False
    settings = _read_object(target, "Claude settings")
    record = _read_object(record_path, "Moira Claude integration metadata")
    current = settings.get("statusLine")
    if not isinstance(current, dict) or current.get("command") != command:
        raise ClaudeIntegrationError(
            "Claude status line changed after Moira setup; removal stopped"
        )
    original = record.get("original_status_line")
    if original is None:
        settings.pop("statusLine", None)
    elif isinstance(original, dict):
        settings["statusLine"] = original
    else:
        raise ClaudeIntegrationError("Moira restoration metadata is malformed")
    _atomic_json(
        target.with_name(f"{target.name}.moira-remove-backup"),
        _read_object(target, "Claude settings"),
    )
    _atomic_json(target, settings)
    record_path.unlink()
    return True


def statusline_main() -> int:
    raw = sys.stdin.buffer.read()
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            update_cache(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    try:
        record = _read_object(integration_path(), "Moira Claude integration metadata")
        original = record.get("original_status_line")
        if not isinstance(original, dict):
            return 0
        delegate = original.get("command")
        if not isinstance(delegate, str) or not delegate.strip():
            return 0
        completed = subprocess.run(  # noqa: S602
            delegate,
            shell=True,
            executable="/bin/sh",
            input=raw,
            check=False,
        )
        return int(completed.returncode)
    except (ClaudeIntegrationError, OSError, subprocess.SubprocessError):
        return 1


if __name__ == "__main__":
    raise SystemExit(statusline_main())
