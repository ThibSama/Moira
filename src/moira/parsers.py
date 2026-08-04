from __future__ import annotations

import math
import re
from datetime import UTC, date, datetime
from typing import Any

from .models import (
    INT64_MAX,
    CodexSummary,
    HistoryStatus,
    QuotaReading,
    QuotaStatus,
    Service,
    TokenReading,
)

ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
PERCENT_RE = r"(?P<pct>\d{1,3}(?:\.\d+)?)\s*%"
ISO_RE = r"(?P<iso>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2}))"

# Canonical source string for the Codex usage surface. Stable across
# releases so daily event identity never depends on source wording.
USAGE_SOURCE = "codex-app-server:account/usage/read"


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


def _validate_single_token(value: Any, name: str) -> int | None:
    """Validate a single integer token field from the usage surface.

    Returns the integer, or None if the field is absent (nullable int64).
    Booleans, every float (including integral floats), strings, negatives,
    and values above signed int64 all fail closed.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ParseError(f"Codex usage {name} is boolean (expected integer)")
    if not isinstance(value, int):
        raise ParseError(f"Codex usage {name} has wrong type {type(value).__name__}")
    if value < 0:
        raise ParseError(f"Codex usage {name} is negative")
    if value > INT64_MAX:
        raise ParseError(f"Codex usage {name} exceeds signed int64")
    return value


# Official generated-schema mapping for the required summary object.
# All five fields are nullable int64. Unknown additive fields are ignored.
_CODEX_SUMMARY_FIELDS: tuple[tuple[str, str], ...] = (
    ("lifetimeTokens", "lifetime_tokens"),
    ("peakDailyTokens", "peak_daily_tokens"),
    ("currentStreakDays", "current_streak_days"),
    ("longestStreakDays", "longest_streak_days"),
    ("longestRunningTurnSec", "longest_running_turn_sec"),
)


def _parse_codex_summary(
    summary: dict[str, Any], source: str, observed_at: datetime
) -> CodexSummary:
    """Parse the required summary object from account/usage/read.

    Mirrors the generated schema: the summary is a required object with five
    nullable int64 fields. Missing keys → None (nullable). Booleans, floats,
    strings, negatives, and int64 overflow → ParseError. Unknown additive
    fields are ignored.
    """
    kwargs: dict[str, int | None] = {}
    for official, attr in _CODEX_SUMMARY_FIELDS:
        raw = summary.get(official)
        kwargs[attr] = _validate_single_token(raw, f"summary.{official}")
    return CodexSummary(
        service=Service.CODEX,
        source=source,
        observed_at=observed_at,
        lifetime_tokens=kwargs["lifetime_tokens"],
        peak_daily_tokens=kwargs["peak_daily_tokens"],
        current_streak_days=kwargs["current_streak_days"],
        longest_streak_days=kwargs["longest_streak_days"],
        longest_running_turn_sec=kwargs["longest_running_turn_sec"],
    )


def parse_codex_usage(
    payload: dict[str, Any], retrieved_at: datetime
) -> tuple[list[TokenReading], CodexSummary]:
    """Parse Codex app-server's documented account/usage/read response.

    Mirrors the generated protocol schema: ``summary`` is a REQUIRED object
    with the five official nullable int64 fields (``lifetimeTokens``,
    ``peakDailyTokens``, ``currentStreakDays``, ``longestStreakDays``,
    ``longestRunningTurnSec``); ``dailyUsageBuckets`` may be null and holds
    ``[{startDate, tokens}]`` entries. Unknown additive fields are ignored.

    Each bucket becomes one TokenReading with a single ``tokens`` field —
    no input/output/cache/reasoning breakdown. The summary is returned as
    one typed ``CodexSummary`` record, separate from the daily readings.

    Returns (daily_readings, summary). Fails closed on malformed,
    contradictory, or absent data: a missing/malformed required summary or
    malformed buckets raises ParseError → the caller produces an INVALID
    token state while quotas survive. Transport/provider failures are
    handled by the caller.
    """
    result = payload.get("result")
    if not isinstance(result, dict):
        raise ParseError("Codex usage result missing")

    # ── Parse summary (REQUIRED) ──
    summary_raw = result.get("summary")
    if not isinstance(summary_raw, dict):
        raise ParseError("Codex usage summary missing or not an object")
    summary = _parse_codex_summary(summary_raw, USAGE_SOURCE, retrieved_at)

    # ── Parse dailyUsageBuckets (nullable) ──
    buckets = result.get("dailyUsageBuckets")
    if buckets is None:
        # Null buckets → no daily data, but the summary still exists
        return ([], summary)
    if not isinstance(buckets, list):
        raise ParseError("Codex usage dailyUsageBuckets is not a list")
    if not buckets:
        return ([], summary)

    source = USAGE_SOURCE
    readings: list[TokenReading] = []
    seen_days: set[date] = set()

    for i, entry in enumerate(buckets):
        if not isinstance(entry, dict):
            raise ParseError(f"Codex usage bucket[{i}] is not a dict")

        # Parse and validate startDate
        date_raw = entry.get("startDate")
        if not isinstance(date_raw, str):
            raise ParseError(f"Codex usage bucket[{i}] startDate missing or wrong type")
        try:
            day = date.fromisoformat(date_raw)
        except (ValueError, TypeError) as exc:
            raise ParseError(f"Codex usage bucket[{i}] startDate format invalid") from exc
        if day in seen_days:
            raise ParseError(f"Codex usage contains duplicate date {date_raw}")
        seen_days.add(day)

        # Parse and validate tokens
        tokens = _validate_single_token(entry.get("tokens"), f"bucket[{i}].tokens")
        if tokens is None:
            raise ParseError(f"Codex usage bucket[{i}] tokens field missing")

        readings.append(
            TokenReading(
                service=Service.CODEX,
                day=day,
                retrieved_at=retrieved_at,
                source=source,
                status=HistoryStatus.AVAILABLE_EXACT,
                tokens=tokens,
            )
        )

    return (readings, summary)
