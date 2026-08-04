from datetime import UTC, datetime
from pathlib import Path

import pytest

from moira.models import (
    CodexSummary,
    HistoryStatus,
    QuotaReading,
    QuotaStatus,
    Service,
    TokenReading,
)
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


# ── Codex usage parser tests (official contract: required summary + dailyUsageBuckets) ──


def test_codex_usage_valid_single_day() -> None:
    """Parses dailyUsageBuckets[{startDate,tokens}] with the required summary."""
    payload = {
        "result": {
            "summary": {
                "lifetimeTokens": 50000,
                "peakDailyTokens": 3000,
                "currentStreakDays": 7,
                "longestStreakDays": 12,
                "longestRunningTurnSec": 1500,
            },
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
    assert r.status is HistoryStatus.AVAILABLE_EXACT
    assert r.source == "codex-app-server:account/usage/read"
    assert summary is not None
    assert summary.lifetime_tokens == 50000
    assert summary.peak_daily_tokens == 3000
    assert summary.current_streak_days == 7
    assert summary.longest_streak_days == 12
    assert summary.longest_running_turn_sec == 1500


def test_codex_usage_unknown_additive_fields_ignored() -> None:
    """Unknown additive summary/bucket fields are ignored."""
    payload = {
        "result": {
            "summary": {
                "lifetimeTokens": 100,
                "someFutureField": "x",
            },
            "dailyUsageBuckets": [
                {"startDate": "2026-08-04", "tokens": 1500, "future": {"nested": 1}},
            ],
        }
    }
    readings, summary = parse_codex_usage(payload, NOW)
    assert len(readings) == 1
    assert readings[0].tokens == 1500
    assert summary.lifetime_tokens == 100
    assert summary.peak_daily_tokens is None


def test_codex_usage_required_summary_missing_fails() -> None:
    """A payload without the required summary object is INVALID (ParseError)."""
    payload = {
        "result": {
            "dailyUsageBuckets": [
                {"startDate": "2026-08-04", "tokens": 1500},
            ],
        }
    }
    with pytest.raises(ParseError, match="summary"):
        parse_codex_usage(payload, NOW)


def test_codex_usage_required_summary_null_fails() -> None:
    """A null summary object is INVALID (ParseError)."""
    payload: dict[str, object] = {"result": {"summary": None, "dailyUsageBuckets": []}}
    with pytest.raises(ParseError, match="summary"):
        parse_codex_usage(payload, NOW)


def test_codex_usage_summary_only_null_buckets() -> None:
    """Null dailyUsageBuckets with the required summary returns empty readings."""
    payload = {
        "result": {
            "summary": {"lifetimeTokens": 10000},
            "dailyUsageBuckets": None,
        }
    }
    readings, summary = parse_codex_usage(payload, NOW)
    assert len(readings) == 0
    assert summary is not None
    assert summary.lifetime_tokens == 10000


def test_codex_usage_null_summary_fields() -> None:
    """Summary fields can be null (None fields in the typed record)."""
    payload = {
        "result": {
            "summary": {
                "lifetimeTokens": None,
                "peakDailyTokens": 500,
                "currentStreakDays": None,
                "longestStreakDays": None,
                "longestRunningTurnSec": None,
            },
            "dailyUsageBuckets": [
                {"startDate": "2026-08-04", "tokens": 100},
            ],
        }
    }
    readings, summary = parse_codex_usage(payload, NOW)
    assert len(readings) == 1
    assert summary is not None
    assert summary.lifetime_tokens is None
    assert summary.peak_daily_tokens == 500
    assert summary.current_streak_days is None


def test_codex_usage_multiple_days() -> None:
    """Multiple daily buckets are parsed in order."""
    payload = {
        "result": {
            "summary": {"lifetimeTokens": 1},
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
            "summary": {"lifetimeTokens": 1},
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
            "summary": {"lifetimeTokens": 1},
            "dailyUsageBuckets": [
                {"startDate": "not-a-date", "tokens": 100},
            ],
        }
    }
    with pytest.raises(ParseError, match="startDate format invalid"):
        parse_codex_usage(payload, NOW)


def test_codex_usage_missing_date_field() -> None:
    """Missing startDate is rejected."""
    payload = {
        "result": {
            "summary": {"lifetimeTokens": 1},
            "dailyUsageBuckets": [{"tokens": 100}],
        }
    }
    with pytest.raises(ParseError):
        parse_codex_usage(payload, NOW)


def test_codex_usage_missing_tokens_field() -> None:
    """Missing tokens field is rejected."""
    payload = {
        "result": {
            "summary": {"lifetimeTokens": 1},
            "dailyUsageBuckets": [{"startDate": "2026-08-04"}],
        }
    }
    with pytest.raises(ParseError, match="tokens field missing"):
        parse_codex_usage(payload, NOW)


@pytest.mark.parametrize(
    "bad_field",
    [
        {"tokens": True},
        {"tokens": 3.5},
        {"tokens": 3.0},  # integral floats are rejected too
        {"tokens": "100"},
        {"tokens": -1},
        {"tokens": float("inf")},
        {"tokens": None},
        {"tokens": 2**63},  # above signed int64
    ],
)
def test_codex_usage_bad_token_types_fail_closed(bad_field: dict[str, object]) -> None:
    """Bad token types (boolean, every float, string, negative, inf, null,
    int64 overflow) are rejected."""
    entry: dict[str, object] = {"startDate": "2026-08-04"}
    entry.update(bad_field)
    payload = {
        "result": {
            "summary": {"lifetimeTokens": 1},
            "dailyUsageBuckets": [entry],
        }
    }
    with pytest.raises(ParseError):
        parse_codex_usage(payload, NOW)


def test_codex_usage_int64_max_accepted() -> None:
    """INT64_MAX is the upper bound and is accepted."""
    payload = {
        "result": {
            "summary": {"lifetimeTokens": 2**63 - 1},
            "dailyUsageBuckets": [{"startDate": "2026-08-04", "tokens": 2**63 - 1}],
        }
    }
    readings, summary = parse_codex_usage(payload, NOW)
    assert readings[0].tokens == 2**63 - 1
    assert summary.lifetime_tokens == 2**63 - 1


@pytest.mark.parametrize(
    "bad_summary",
    [
        {"lifetimeTokens": True},
        {"lifetimeTokens": 1.5},
        {"lifetimeTokens": 1.0},  # integral floats rejected in summary too
        {"lifetimeTokens": "100"},
        {"lifetimeTokens": -1},
        {"lifetimeTokens": 2**63},
        {"peakDailyTokens": 2**63},
        {"currentStreakDays": -2},
        {"longestStreakDays": True},
        {"longestRunningTurnSec": 9.9},
    ],
)
def test_codex_usage_malformed_summary_fails_closed(bad_summary: dict[str, object]) -> None:
    """Malformed required summary fields make the whole usage parse INVALID."""
    payload = {
        "result": {
            "summary": {"lifetimeTokens": 1, **bad_summary},
            "dailyUsageBuckets": [
                {"startDate": "2026-08-04", "tokens": 100},
            ],
        }
    }
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
    """Missing or null result raises ParseError."""
    with pytest.raises(ParseError):
        parse_codex_usage(payload, NOW)


def test_codex_usage_null_buckets_with_summary_valid() -> None:
    """Null dailyUsageBuckets with the required summary is valid."""
    readings, summary = parse_codex_usage(
        {"result": {"summary": {"lifetimeTokens": 10}, "dailyUsageBuckets": None}}, NOW
    )
    assert len(readings) == 0
    assert summary is not None
    assert summary.lifetime_tokens == 10


def test_codex_usage_empty_buckets_with_summary_valid() -> None:
    """Empty dailyUsageBuckets list with the required summary is valid."""
    readings, summary = parse_codex_usage(
        {"result": {"summary": {"lifetimeTokens": 10}, "dailyUsageBuckets": []}}, NOW
    )
    assert len(readings) == 0
    assert summary is not None


def test_codex_usage_non_list_buckets_raises() -> None:
    """dailyUsageBuckets that isn't a list and isn't None raises."""
    payload = {"result": {"summary": {"lifetimeTokens": 1}, "dailyUsageBuckets": "not-a-list"}}
    with pytest.raises(ParseError, match="not a list"):
        parse_codex_usage(payload, NOW)


def test_codex_usage_non_dict_bucket_entry() -> None:
    """A non-dict entry in buckets is rejected."""
    payload = {
        "result": {
            "summary": {"lifetimeTokens": 1},
            "dailyUsageBuckets": ["not-a-dict"],
        }
    }
    with pytest.raises(ParseError):
        parse_codex_usage(payload, NOW)


# ── TokenReading validation tests ──


def test_token_reading_valid() -> None:
    """TokenReading with available_exact and tokens."""
    from datetime import date as date_type

    r = TokenReading(
        service=Service.CODEX,
        day=date_type(2026, 8, 4),
        retrieved_at=NOW,
        source="test",
        status=HistoryStatus.AVAILABLE_EXACT,
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
            status=HistoryStatus.AVAILABLE_EXACT,
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
            status=HistoryStatus.AVAILABLE_EXACT,
            tokens=-1,
        )


def test_token_reading_overflow_rejected() -> None:
    """Values above signed int64 are rejected."""
    from datetime import date as date_type

    with pytest.raises(ValueError, match="int64"):
        TokenReading(
            service=Service.CODEX,
            day=date_type(2026, 8, 4),
            retrieved_at=NOW,
            source="test",
            status=HistoryStatus.AVAILABLE_EXACT,
            tokens=2**63,
        )


def test_token_reading_int64_max_accepted() -> None:
    """INT64_MAX is accepted as the upper bound."""
    from datetime import date as date_type

    r = TokenReading(
        service=Service.CODEX,
        day=date_type(2026, 8, 4),
        retrieved_at=NOW,
        source="test",
        status=HistoryStatus.AVAILABLE_EXACT,
        tokens=2**63 - 1,
    )
    assert r.tokens == 2**63 - 1


def test_token_reading_arbitrary_status_string_rejected() -> None:
    """A free-form status string is rejected — status is a typed enum."""
    from datetime import date as date_type

    with pytest.raises(ValueError, match="HistoryStatus"):
        TokenReading(
            service=Service.CODEX,
            day=date_type(2026, 8, 4),
            retrieved_at=NOW,
            source="test",
            status="available_exact",  # type: ignore[arg-type]
            tokens=100,
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
            status=HistoryStatus.UNSUPPORTED,
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
            status=HistoryStatus.AVAILABLE_EXACT,
        )


def test_token_reading_unavailable_no_counts() -> None:
    """UNAVAILABLE reading is valid with no counts."""
    from datetime import date as date_type

    r = TokenReading(
        service=Service.CODEX,
        day=date_type(2026, 8, 4),
        retrieved_at=NOW,
        source="test",
        status=HistoryStatus.TEMPORARILY_UNAVAILABLE,
    )
    assert r.tokens is None
    assert r.available is False


# ── CodexSummary model tests (frozen, typed, int64-bound) ──


def _summary(**overrides: object) -> CodexSummary:
    values: dict[str, object] = {
        "service": Service.CODEX,
        "source": "codex-app-server:account/usage/read",
        "observed_at": NOW,
        "lifetime_tokens": 100000,
        "peak_daily_tokens": 3000,
        "current_streak_days": 12,
        "longest_streak_days": 30,
        "longest_running_turn_sec": 2500,
    }
    values.update(overrides)
    return CodexSummary(**values)  # type: ignore[arg-type]


def test_codex_summary_valid() -> None:
    s = _summary()
    assert s.lifetime_tokens == 100000
    assert s.peak_daily_tokens == 3000
    assert s.current_streak_days == 12
    assert s.longest_streak_days == 30
    assert s.longest_running_turn_sec == 2500


def test_codex_summary_nullable_fields() -> None:
    s = _summary(lifetime_tokens=None, current_streak_days=None)
    assert s.lifetime_tokens is None
    assert s.peak_daily_tokens == 3000
    assert s.current_streak_days is None


def test_codex_summary_is_frozen_and_deeply_immutable() -> None:
    s = _summary()
    with pytest.raises(AttributeError):
        s.lifetime_tokens = 1  # type: ignore[misc]
    with pytest.raises(AttributeError):
        s.source = "other"  # type: ignore[misc]


def test_codex_summary_rejects_boolean() -> None:
    with pytest.raises(ValueError):
        _summary(lifetime_tokens=True)


def test_codex_summary_rejects_float() -> None:
    with pytest.raises(ValueError):
        _summary(lifetime_tokens=100.0)


def test_codex_summary_rejects_negative() -> None:
    with pytest.raises(ValueError):
        _summary(lifetime_tokens=-1)


def test_codex_summary_rejects_overflow() -> None:
    with pytest.raises(ValueError, match="int64"):
        _summary(peak_daily_tokens=2**63)


def test_codex_summary_accepts_int64_max() -> None:
    s = _summary(longest_running_turn_sec=2**63 - 1)
    assert s.longest_running_turn_sec == 2**63 - 1


def test_codex_summary_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _summary(observed_at=datetime(2026, 8, 1, 12))


def test_codex_summary_to_dict_roundtrip() -> None:
    s = _summary()
    restored = CodexSummary.from_dict(s.to_dict())
    assert restored == s
