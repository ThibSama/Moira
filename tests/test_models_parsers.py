from datetime import UTC, datetime
from pathlib import Path

import pytest

from moira.models import QuotaReading, QuotaStatus, Service, TokenReading
from moira.parsers import (
    ParseError,
    parse_claude_usage,
    parse_codex_rate_limits,
    parse_codex_usage,
    parse_timestamp,
)

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 8, 1, 12, tzinfo=UTC)


def test_claude_valid() -> None:
    readings = parse_claude_usage((FIXTURES / "claude-valid.txt").read_text(), NOW)
    assert [(item.quota_label, item.percentage) for item in readings] == [
        ("Five-hour", 42),
        ("Weekly", 68),
    ]
    assert readings[0].reset_at == datetime(2026, 8, 1, 16, 30, tzinfo=UTC)


def test_claude_installed_cli_labels() -> None:
    output = (
        "Current session 12% used — resets at 2026-08-01T18:30:00+02:00\n"
        "Current week (all models) 34% used — resets at 2026-08-06T09:15:00+02:00"
    )
    readings = parse_claude_usage(output, NOW)
    assert [item.percentage for item in readings] == [12, 34]


@pytest.mark.parametrize("name", ["claude-missing.txt", "claude-malformed.txt"])
def test_claude_missing_and_malformed_are_errors(name: str) -> None:
    with pytest.raises(ParseError):
        parse_claude_usage((FIXTURES / name).read_text(), NOW)


def test_codex_structured_weekly_only() -> None:
    payload = {
        "result": {
            "rateLimits": [
                {
                    "primary": {
                        "usedPercent": 10,
                        "windowDurationMins": 300,
                        "resetsAt": 1785600000,
                    },
                    "secondary": {
                        "usedPercent": 37,
                        "windowDurationMins": 10080,
                        "resetsAt": 1786204800,
                    },
                }
            ]
        }
    }
    readings = parse_codex_rate_limits(payload, NOW)
    assert len(readings) == 1
    assert readings[0].quota_label == "Weekly"
    assert readings[0].percentage == 37


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"result": {"rateLimits": []}},
        {
            "result": {
                "rateLimits": [
                    {
                        "secondary": {
                            "usedPercent": "bad",
                            "windowDurationMins": 10080,
                            "resetsAt": 1,
                        }
                    }
                ]
            }
        },
        {
            "result": {
                "rateLimits": [
                    {
                        "secondary": {
                            "usedPercent": 10,
                            "windowDurationMins": 10081,
                            "resetsAt": 1786204800,
                        }
                    }
                ]
            }
        },
        {
            "result": {
                "rateLimits": [
                    {
                        "secondary": {
                            "usedPercent": "10",
                            "windowDurationMins": 10080,
                            "resetsAt": 1786204800,
                        }
                    }
                ]
            }
        },
    ],
)
def test_codex_missing_or_malformed(payload: dict[str, object]) -> None:
    with pytest.raises(ParseError):
        parse_codex_rate_limits(payload, NOW)


@pytest.mark.parametrize("percentage", [-0.01, 100.01])
def test_percentage_validation(percentage: float) -> None:
    with pytest.raises(ValueError):
        QuotaReading(
            Service.CLAUDE, "Weekly", percentage, NOW, NOW, "fixture", QuotaStatus.AVAILABLE
        )


def test_reset_time_conversion() -> None:
    assert parse_timestamp("2026-08-01T18:30:00+02:00").astimezone(UTC).hour == 16
    assert parse_timestamp("1785600000").tzinfo is UTC
    with pytest.raises(ParseError):
        parse_timestamp("2026-08-01 18:30")


# ── Codex usage parser tests ──


def test_codex_usage_valid_single_day() -> None:
    payload = {
        "result": {
            "usage": [
                {
                    "day": "2026-08-04",
                    "inputTokens": 1500,
                    "cachedInputTokens": 300,
                    "outputTokens": 2000,
                    "reasoningOutputTokens": 100,
                    "totalTokens": 3900,
                }
            ]
        }
    }
    readings = parse_codex_usage(payload, NOW)
    assert len(readings) == 1
    r = readings[0]
    assert isinstance(r, TokenReading)
    assert r.service == Service.CODEX
    assert r.day.isoformat() == "2026-08-04"
    assert r.input_tokens == 1500
    assert r.cached_input_tokens == 300
    assert r.output_tokens == 2000
    assert r.reasoning_output_tokens == 100
    assert r.total_tokens == 3900
    assert r.status is QuotaStatus.AVAILABLE
    assert r.source == "codex-app-server:account/usage/read"


def test_codex_usage_total_invariant_enforced() -> None:
    """Total must match breakdown sum when both are present."""
    payload = {
        "result": {
            "usage": [
                {
                    "day": "2026-08-04",
                    "inputTokens": 100,
                    "outputTokens": 200,
                    "totalTokens": 999,  # wrong: should be 300
                }
            ]
        }
    }
    with pytest.raises(ParseError, match="does not match breakdown sum"):
        parse_codex_usage(payload, NOW)


def test_codex_usage_total_without_breakdown_ok() -> None:
    """Total alone (no breakdown) is valid — no invariant check needed."""
    payload = {
        "result": {
            "usage": [
                {
                    "day": "2026-08-04",
                    "totalTokens": 3900,
                }
            ]
        }
    }
    readings = parse_codex_usage(payload, NOW)
    assert len(readings) == 1
    assert readings[0].total_tokens == 3900
    assert readings[0].input_tokens is None


def test_codex_usage_breakdown_without_total_raises() -> None:
    """Breakdown fields without total_tokens is invalid."""
    payload = {
        "result": {
            "usage": [
                {
                    "day": "2026-08-04",
                    "inputTokens": 100,
                    "outputTokens": 200,
                }
            ]
        }
    }
    with pytest.raises(ParseError):
        parse_codex_usage(payload, NOW)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"result": {}},
        {"result": {"usage": []}},
        {"result": {"usage": "not-a-list"}},
        {"result": {"usage": [{"day": "2026-08-04"}]}},  # no token fields
    ],
)
def test_codex_usage_empty_or_missing(payload: dict[str, object]) -> None:
    with pytest.raises(ParseError):
        parse_codex_usage(payload, NOW)


@pytest.mark.parametrize(
    "bad_field",
    [
        {"inputTokens": True},
        {"inputTokens": 3.5},
        {"inputTokens": "100"},
        {"inputTokens": -1},
        {"totalTokens": True},
        {"totalTokens": -5},
        {"totalTokens": float("inf")},
    ],
)
def test_codex_usage_bad_field_types_fail_closed(bad_field: dict[str, object]) -> None:
    entry: dict[str, object] = {"day": "2026-08-04"}
    entry.update(bad_field)
    payload = {"result": {"usage": [entry]}}
    with pytest.raises(ParseError):
        parse_codex_usage(payload, NOW)


def test_codex_usage_duplicate_days_fails() -> None:
    payload = {
        "result": {
            "usage": [
                {"day": "2026-08-04", "totalTokens": 100},
                {"day": "2026-08-04", "totalTokens": 200},
            ]
        }
    }
    with pytest.raises(ParseError, match="duplicate days"):
        parse_codex_usage(payload, NOW)


def test_codex_usage_multiple_days() -> None:
    payload = {
        "result": {
            "usage": [
                {"day": "2026-08-04", "inputTokens": 100, "totalTokens": 100},
                {"day": "2026-08-05", "inputTokens": 200, "totalTokens": 200},
            ]
        }
    }
    readings = parse_codex_usage(payload, NOW)
    assert len(readings) == 2
    assert readings[0].day.isoformat() == "2026-08-04"
    assert readings[1].day.isoformat() == "2026-08-05"


def test_codex_usage_wrong_day_format_fails() -> None:
    payload = {
        "result": {
            "usage": [
                {"day": "not-a-date", "totalTokens": 100},
            ]
        }
    }
    with pytest.raises(ParseError, match="day format invalid"):
        parse_codex_usage(payload, NOW)


def test_codex_usage_missing_day_field() -> None:
    payload = {"result": {"usage": [{"totalTokens": 100}]}}
    with pytest.raises(ParseError):
        parse_codex_usage(payload, NOW)


def test_token_reading_validation() -> None:
    """TokenReading validates fields at construction."""
    from datetime import date as date_type

    # Valid
    r = TokenReading(
        service=Service.CODEX,
        day=date_type(2026, 8, 4),
        retrieved_at=NOW,
        source="test",
        status=QuotaStatus.AVAILABLE,
        input_tokens=100,
        total_tokens=100,
    )
    assert r.input_tokens == 100

    # Boolean rejected
    with pytest.raises(ValueError):
        TokenReading(
            service=Service.CODEX,
            day=date_type(2026, 8, 4),
            retrieved_at=NOW,
            source="test",
            status=QuotaStatus.AVAILABLE,
            input_tokens=True,  # type: ignore[arg-type]
            total_tokens=100,
        )

    # Negative rejected
    with pytest.raises(ValueError):
        TokenReading(
            service=Service.CODEX,
            day=date_type(2026, 8, 4),
            retrieved_at=NOW,
            source="test",
            status=QuotaStatus.AVAILABLE,
            input_tokens=-1,
            total_tokens=100,
        )

    # Non-available with counts rejected
    with pytest.raises(ValueError):
        TokenReading(
            service=Service.CODEX,
            day=date_type(2026, 8, 4),
            retrieved_at=NOW,
            source="test",
            status=QuotaStatus.UNAVAILABLE,
            input_tokens=100,
        )
