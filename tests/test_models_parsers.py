from datetime import UTC, datetime
from pathlib import Path

import pytest

from moira.models import QuotaReading, QuotaStatus, Service
from moira.parsers import ParseError, parse_claude_usage, parse_codex_rate_limits, parse_timestamp

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
