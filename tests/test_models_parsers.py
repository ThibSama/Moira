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


# ── Codex usage parser tests (official contract: summary + dailyUsageBuckets) ──


def test_codex_usage_valid_single_day() -> None:
    """Parses dailyUsageBuckets[{startDate,tokens}] with summary."""
    payload = {
        "result": {
            "summary": {"lifetime": 50000, "peak": 3000, "streak": 7, "longestTurn": 1500},
            "dailyUsageBuckets": [
                {"startDate": "2026-08-04", "tokens": 3900},
            ],
        }
    }
    readings, summary = parse_codex_usage(payload, NOW)
    assert len(readings) == 1
    r = readings[0]
    assert isinstance(r, TokenReading)
    assert r.service == Service.CODEX
    assert r.day.isoformat() == "2026-08-04"
    assert r.tokens == 3900
    assert r.status == "available_exact"
    assert r.source == "codex-app-server:account/usage/read"
    assert summary is not None
    assert summary["lifetime"] == 50000
    assert summary["peak"] == 3000
    assert summary["streak"] == 7
    assert summary["longestTurn"] == 1500


def test_codex_usage_minimal_bucket_only() -> None:
    """A single bucket with just tokens, no summary."""
    payload = {
        "result": {
            "dailyUsageBuckets": [
                {"startDate": "2026-08-04", "tokens": 1500},
            ],
        }
    }
    readings, summary = parse_codex_usage(payload, NOW)
    assert len(readings) == 1
    assert readings[0].tokens == 1500
    assert summary is None


def test_codex_usage_summary_only_null_buckets() -> None:
    """Null dailyUsageBuckets with a summary returns empty readings."""
    payload = {
        "result": {
            "summary": {"lifetime": 10000},
            "dailyUsageBuckets": None,
        }
    }
    readings, summary = parse_codex_usage(payload, NOW)
    assert len(readings) == 0
    assert summary is not None
    assert summary["lifetime"] == 10000


def test_codex_usage_null_summary_fields() -> None:
    """Summary fields can be null (None becomes None in the dict)."""
    payload = {
        "result": {
            "summary": {"lifetime": None, "peak": 500, "streak": None, "longestTurn": None},
            "dailyUsageBuckets": [
                {"startDate": "2026-08-04", "tokens": 100},
            ],
        }
    }
    readings, summary = parse_codex_usage(payload, NOW)
    assert len(readings) == 1
    assert summary is not None
    assert summary["lifetime"] is None
    assert summary["peak"] == 500
    assert summary["streak"] is None


def test_codex_usage_multiple_days() -> None:
    """Multiple daily buckets are parsed in order."""
    payload = {
        "result": {
            "dailyUsageBuckets": [
                {"startDate": "2026-08-04", "tokens": 100},
                {"startDate": "2026-08-05", "tokens": 200},
            ],
        }
    }
    readings, _ = parse_codex_usage(payload, NOW)
    assert len(readings) == 2
    assert readings[0].day.isoformat() == "2026-08-04"
    assert readings[0].tokens == 100
    assert readings[1].day.isoformat() == "2026-08-05"
    assert readings[1].tokens == 200


def test_codex_usage_duplicate_dates_fails() -> None:
    """Duplicate startDate values are rejected."""
    payload = {
        "result": {
            "dailyUsageBuckets": [
                {"startDate": "2026-08-04", "tokens": 100},
                {"startDate": "2026-08-04", "tokens": 200},
            ],
        }
    }
    with pytest.raises(ParseError, match="duplicate date"):
        parse_codex_usage(payload, NOW)


def test_codex_usage_wrong_date_format_fails() -> None:
    """Invalid startDate format is rejected."""
    payload = {
        "result": {
            "dailyUsageBuckets": [
                {"startDate": "not-a-date", "tokens": 100},
            ],
        }
    }
    with pytest.raises(ParseError, match="startDate format invalid"):
        parse_codex_usage(payload, NOW)


def test_codex_usage_missing_date_field() -> None:
    """Missing startDate is rejected."""
    payload = {"result": {"dailyUsageBuckets": [{"tokens": 100}]}}
    with pytest.raises(ParseError):
        parse_codex_usage(payload, NOW)


def test_codex_usage_missing_tokens_field() -> None:
    """Missing tokens field is rejected."""
    payload = {"result": {"dailyUsageBuckets": [{"startDate": "2026-08-04"}]}}
    with pytest.raises(ParseError, match="tokens field missing"):
        parse_codex_usage(payload, NOW)


@pytest.mark.parametrize(
    "bad_field",
    [
        {"tokens": True},
        {"tokens": 3.5},
        {"tokens": "100"},
        {"tokens": -1},
        {"tokens": float("inf")},
        {"tokens": None},
    ],
)
def test_codex_usage_bad_token_types_fail_closed(bad_field: dict[str, object]) -> None:
    """Bad token types (boolean, float, string, negative, inf, null) are rejected."""
    entry: dict[str, object] = {"startDate": "2026-08-04"}
    entry.update(bad_field)
    payload = {"result": {"dailyUsageBuckets": [entry]}}
    with pytest.raises(ParseError):
        parse_codex_usage(payload, NOW)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"result": None},
    ],
)
def test_codex_usage_empty_or_missing(payload: dict[str, object]) -> None:
    """Missing or null result raises ParseError. Null/empty buckets is valid."""
    with pytest.raises(ParseError):
        parse_codex_usage(payload, NOW)


def test_codex_usage_null_buckets_valid() -> None:
    """Null dailyUsageBuckets with empty result is valid (no data, no error)."""
    readings, summary = parse_codex_usage({"result": {}}, NOW)
    assert len(readings) == 0
    assert summary is None


def test_codex_usage_empty_buckets_valid() -> None:
    """Empty dailyUsageBuckets list is valid (no data, no error)."""
    readings, summary = parse_codex_usage({"result": {"dailyUsageBuckets": []}}, NOW)
    assert len(readings) == 0
    assert summary is None


def test_codex_usage_non_list_buckets_raises() -> None:
    """dailyUsageBuckets that isn't a list and isn't None raises."""
    payload = {"result": {"dailyUsageBuckets": "not-a-list"}}
    with pytest.raises(ParseError, match="not a list"):
        parse_codex_usage(payload, NOW)


def test_codex_usage_non_dict_bucket_entry() -> None:
    """A non-dict entry in buckets is rejected."""
    payload = {"result": {"dailyUsageBuckets": ["not-a-dict"]}}
    with pytest.raises(ParseError):
        parse_codex_usage(payload, NOW)


def test_codex_usage_malformed_summary_still_parses_buckets() -> None:
    """Malformed summary does not prevent daily bucket parsing."""
    payload = {
        "result": {
            "summary": {"lifetime": "not-an-int"},
            "dailyUsageBuckets": [
                {"startDate": "2026-08-04", "tokens": 100},
            ],
        }
    }
    readings, summary = parse_codex_usage(payload, NOW)
    assert len(readings) == 1
    assert readings[0].tokens == 100
    # Summary parse failed → None
    assert summary is None


# ── TokenReading validation tests ──


def test_token_reading_valid() -> None:
    """TokenReading with available_exact and tokens."""
    from datetime import date as date_type

    r = TokenReading(
        service=Service.CODEX,
        day=date_type(2026, 8, 4),
        retrieved_at=NOW,
        source="test",
        status="available_exact",
        tokens=100,
    )
    assert r.tokens == 100
    assert r.available is True


def test_token_reading_boolean_rejected() -> None:
    """Boolean token values are rejected."""
    from datetime import date as date_type

    with pytest.raises(ValueError):
        TokenReading(
            service=Service.CODEX,
            day=date_type(2026, 8, 4),
            retrieved_at=NOW,
            source="test",
            status="available_exact",
            tokens=True,
        )


def test_token_reading_negative_rejected() -> None:
    """Negative token values are rejected."""
    from datetime import date as date_type

    with pytest.raises(ValueError):
        TokenReading(
            service=Service.CODEX,
            day=date_type(2026, 8, 4),
            retrieved_at=NOW,
            source="test",
            status="available_exact",
            tokens=-1,
        )


def test_token_reading_non_available_with_counts() -> None:
    """Non-available statuses must not carry counts."""
    from datetime import date as date_type

    with pytest.raises(ValueError):
        TokenReading(
            service=Service.CODEX,
            day=date_type(2026, 8, 4),
            retrieved_at=NOW,
            source="test",
            status="unsupported",
            tokens=100,
        )


def test_token_reading_available_exact_no_tokens() -> None:
    """AVAILABLE_EXACT requires tokens to be set."""
    from datetime import date as date_type

    with pytest.raises(ValueError, match="require tokens"):
        TokenReading(
            service=Service.CODEX,
            day=date_type(2026, 8, 4),
            retrieved_at=NOW,
            source="test",
            status="available_exact",
        )


def test_token_reading_unavailable_no_counts() -> None:
    """UNAVAILABLE reading is valid with no counts."""
    from datetime import date as date_type

    r = TokenReading(
        service=Service.CODEX,
        day=date_type(2026, 8, 4),
        retrieved_at=NOW,
        source="test",
        status="temporarily_unavailable",
    )
    assert r.tokens is None
    assert r.available is False


def test_token_reading_with_summary_fields() -> None:
    """TokenReading carries optional summary fields."""
    from datetime import date as date_type

    r = TokenReading(
        service=Service.CODEX,
        day=date_type(2026, 8, 4),
        retrieved_at=NOW,
        source="test",
        status="available_exact",
        tokens=5000,
        summary_lifetime=100000,
        summary_peak=3000,
        summary_streak=12,
        summary_longest_turn=2500,
    )
    assert r.tokens == 5000
    assert r.summary_lifetime == 100000
    assert r.summary_peak == 3000
    assert r.summary_streak == 12
    assert r.summary_longest_turn == 2500


def test_token_reading_summary_fields_non_negative() -> None:
    """Summary fields must be non-negative integers."""
    from datetime import date as date_type

    with pytest.raises(ValueError):
        TokenReading(
            service=Service.CODEX,
            day=date_type(2026, 8, 4),
            retrieved_at=NOW,
            source="test",
            status="available_exact",
            tokens=100,
            summary_lifetime=-1,
        )
