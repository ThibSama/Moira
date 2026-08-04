from __future__ import annotations

import math
import re
from datetime import UTC, date, datetime
from typing import Any

from .models import QuotaReading, QuotaStatus, Service, TokenReading

ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
PERCENT_RE = r"(?P<pct>\d{1,3}(?:\.\d+)?)\s*%"
ISO_RE = r"(?P<iso>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2}))"


class ParseError(ValueError):
    pass


def clean_terminal(text: str) -> str:
    text = ANSI_RE.sub("", text).replace("\r", "\n")
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def parse_timestamp(value: str) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    if re.fullmatch(r"\d{10}(?:\.\d+)?", normalized):
        return datetime.fromtimestamp(float(normalized), UTC)
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ParseError("unsupported reset timestamp") from exc
    if parsed.tzinfo is None:
        raise ParseError("reset timestamp has no timezone")
    return parsed


def _find_window(text: str, labels: tuple[str, ...]) -> tuple[float, datetime]:
    clean = clean_terminal(text)
    label_expr = "|".join(re.escape(label) for label in labels)
    reset_expression = r"(?:reset(?:s|ting)?(?:\s+at)?[: ]+)"
    patterns = (
        rf"(?is)(?:{label_expr}).{{0,180}}?{PERCENT_RE}.{{0,240}}?"
        rf"{reset_expression}({ISO_RE})",
        rf"(?is)(?:{label_expr}).{{0,180}}?{PERCENT_RE}.{{0,240}}?({ISO_RE})",
    )
    for pattern in patterns:
        match = re.search(pattern, clean)
        if match:
            pct = float(match.group("pct"))
            if not 0 <= pct <= 100:
                raise ParseError("percentage outside 0..100")
            return pct, parse_timestamp(match.group("iso"))
    raise ParseError("required quota window not found")


def parse_claude_usage(text: str, retrieved_at: datetime) -> list[QuotaReading]:
    five_pct, five_reset = _find_window(
        text,
        ("current session", "five hour", "5 hour", "five-hour", "5-hour"),
    )
    week_pct, week_reset = _find_window(
        text,
        (
            "current week (all models)",
            "current week",
            "seven day",
            "7 day",
            "seven-day",
            "7-day",
            "weekly",
            "week",
        ),
    )
    source = "claude-cli:/usage"
    return [
        QuotaReading(
            Service.CLAUDE,
            "Five-hour",
            five_pct,
            five_reset,
            retrieved_at,
            source,
            QuotaStatus.AVAILABLE,
        ),
        QuotaReading(
            Service.CLAUDE,
            "Weekly",
            week_pct,
            week_reset,
            retrieved_at,
            source,
            QuotaStatus.AVAILABLE,
        ),
    ]


def parse_codex_status(text: str, retrieved_at: datetime) -> list[QuotaReading]:
    pct, reset = _find_window(text, ("weekly", "week", "7 day", "seven day"))
    return [
        QuotaReading(
            Service.CODEX,
            "Weekly",
            pct,
            reset,
            retrieved_at,
            "codex-cli:/status",
            QuotaStatus.AVAILABLE,
        )
    ]


def parse_codex_rate_limits(payload: dict[str, Any], retrieved_at: datetime) -> list[QuotaReading]:
    """Parse Codex app-server's documented account/rateLimits/read response."""
    result = payload.get("result")
    if not isinstance(result, dict):
        raise ParseError("Codex rate-limit result missing")
    snapshots = result.get("rateLimits")
    if not isinstance(snapshots, list):
        snapshots = [snapshots] if isinstance(snapshots, dict) else []
    candidates: list[dict[str, Any]] = []
    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            continue
        for name in ("primary", "secondary"):
            window = snapshot.get(name)
            if isinstance(window, dict) and window.get("windowDurationMins") is not None:
                candidates.append(window)
    weekly = [
        item
        for item in candidates
        if isinstance(item.get("windowDurationMins"), int)
        and not isinstance(item["windowDurationMins"], bool)
        and item["windowDurationMins"] == 7 * 24 * 60
    ]
    if not weekly:
        raise ParseError("Codex weekly quota window missing")
    window = min(weekly, key=lambda item: int(item["windowDurationMins"]))
    try:
        used_value = window["usedPercent"]
        reset_value = window["resetsAt"]
        if (
            isinstance(used_value, bool)
            or not isinstance(used_value, (int, float))
            or isinstance(reset_value, bool)
            or not isinstance(reset_value, (int, float))
        ):
            raise ValueError
        used = float(used_value)
        if not math.isfinite(used) or not math.isfinite(float(reset_value)) or reset_value <= 0:
            raise ValueError
        reset = datetime.fromtimestamp(float(reset_value), UTC)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ParseError("Codex weekly quota fields malformed") from exc
    if not 0 <= used <= 100:
        raise ParseError("percentage outside 0..100")
    return [
        QuotaReading(
            Service.CODEX,
            "Weekly",
            used,
            reset,
            retrieved_at,
            "codex-app-server:account/rateLimits/read",
            QuotaStatus.AVAILABLE,
        )
    ]


def _validate_usage_token_field(value: Any, name: str) -> int | None:
    """Validate a single token field from structured usage data.

    Returns the integer value, or raises ParseError.
    None is accepted (field absent). Booleans, floats, strings, negatives,
    and overflow values all fail closed.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ParseError(f"Codex usage {name} is boolean (expected integer)")
    if not isinstance(value, (int, float)):
        raise ParseError(f"Codex usage {name} has wrong type {type(value).__name__}")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ParseError(f"Codex usage {name} is not a finite number")
        if value != math.floor(value):
            raise ParseError(f"Codex usage {name} must be a whole number")
        value = int(value)
    if value < 0:
        raise ParseError(f"Codex usage {name} is negative")
    return int(value)


def parse_codex_usage(payload: dict[str, Any], retrieved_at: datetime) -> list[TokenReading]:
    """Parse Codex app-server's documented account/usage/read response.

    Returns one TokenReading per daily bucket. Only structured fields are
    parsed — no terminal scraping or private-endpoint access.
    Fails closed on malformed, contradictory, or absent data.
    """
    result = payload.get("result")
    if not isinstance(result, dict):
        raise ParseError("Codex usage result missing")
    usage = result.get("usage")
    if not isinstance(usage, list):
        raise ParseError("Codex usage array missing or not a list")
    if not usage:
        raise ParseError("Codex usage array is empty")

    source = "codex-app-server:account/usage/read"
    readings: list[TokenReading] = []
    seen_days: set[date] = set()

    for entry in usage:
        if not isinstance(entry, dict):
            raise ParseError("Codex usage entry is not a dict")

        # Parse and validate the day field
        day_raw = entry.get("day")
        if not isinstance(day_raw, str):
            raise ParseError("Codex usage day field missing or wrong type")
        try:
            day = date.fromisoformat(day_raw)
        except (ValueError, TypeError) as exc:
            raise ParseError("Codex usage day format invalid") from exc
        if day in seen_days:
            raise ParseError("Codex usage contains duplicate days")
        seen_days.add(day)

        # Parse token fields with fail-closed validation
        input_tokens = _validate_usage_token_field(entry.get("inputTokens"), "inputTokens")
        cached_input_tokens = _validate_usage_token_field(
            entry.get("cachedInputTokens"), "cachedInputTokens"
        )
        output_tokens = _validate_usage_token_field(entry.get("outputTokens"), "outputTokens")
        reasoning_output_tokens = _validate_usage_token_field(
            entry.get("reasoningOutputTokens"), "reasoningOutputTokens"
        )
        total_tokens = _validate_usage_token_field(entry.get("totalTokens"), "totalTokens")

        # Enforce total invariant when both total and breakdown are present
        has_breakdown = any(
            v is not None
            for v in (input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens)
        )
        if total_tokens is not None:
            if has_breakdown:
                computed: int = 0
                for v in (
                    input_tokens,
                    cached_input_tokens,
                    output_tokens,
                    reasoning_output_tokens,
                ):
                    if v is not None:
                        computed += v
                if computed != total_tokens:
                    raise ParseError(
                        f"Codex usage total ({total_tokens}) does not match breakdown sum "
                        f"({computed}) for day {day_raw}"
                    )
        elif has_breakdown:
            # Breakdown fields present but no total — can't verify, fail closed
            raise ParseError(
                f"Codex usage has breakdown fields but no total_tokens for day {day_raw}"
            )

        if total_tokens is None and not has_breakdown:
            raise ParseError(f"Codex usage entry for {day_raw} has no token fields")

        readings.append(
            TokenReading(
                service=Service.CODEX,
                day=day,
                retrieved_at=retrieved_at,
                source=source,
                status=QuotaStatus.AVAILABLE,
                input_tokens=input_tokens,
                cached_input_tokens=cached_input_tokens,
                output_tokens=output_tokens,
                reasoning_output_tokens=reasoning_output_tokens,
                total_tokens=total_tokens,
            )
        )

    return readings
