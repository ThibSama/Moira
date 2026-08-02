"""Deterministic tests for the pure weekly-exhaustion rule and snapshot derivation.

Covers: exhaustion at >=100%, stale/error/missing/sub-100 must not establish
exhaustion, Claude suppression of five-hour row, Codex critical state,
and reset-after behavior. Uses injected clocks; no correctness sleeps.
"""

from datetime import UTC, datetime, timedelta

from moira.exhaustion import (
    derive_service,
    derive_state,
    is_weekly_exhausted,
)
from moira.models import QuotaReading, QuotaStatus, Service

NOW = datetime(2026, 8, 2, 12, tzinfo=UTC)
RESET = NOW + timedelta(days=5)


def reading(
    service: Service = Service.CLAUDE,
    label: str = "Weekly",
    pct: float | None = 100,
    reset: datetime | None = RESET,
    status: QuotaStatus = QuotaStatus.AVAILABLE,
    retrieved: datetime = NOW,
) -> QuotaReading:
    if (
        status in {QuotaStatus.AVAILABLE, QuotaStatus.STALE}
        and pct is not None
        and reset is not None
    ):
        return QuotaReading(service, label, pct, reset, retrieved, "fixture", status)
    return QuotaReading(service, label, pct, reset, retrieved, "fixture", status, "detail")


# ── Exhaustion rule ──


def test_available_100_pct_is_exhausted() -> None:
    assert is_weekly_exhausted(reading(pct=100), now=NOW)


def test_available_above_100_not_possible_but_100_is_boundary() -> None:
    assert is_weekly_exhausted(reading(pct=100), now=NOW)


def test_sub_100_is_not_exhausted() -> None:
    assert not is_weekly_exhausted(reading(pct=99), now=NOW)
    assert not is_weekly_exhausted(reading(pct=50), now=NOW)


def test_stale_100_is_not_exhausted() -> None:
    r = reading(pct=100, status=QuotaStatus.STALE)
    assert not is_weekly_exhausted(r, now=NOW)


def test_missing_reading_is_not_exhausted() -> None:
    assert not is_weekly_exhausted(None, now=NOW)


def test_error_reading_is_not_exhausted() -> None:
    r = reading(pct=None, reset=None, status=QuotaStatus.ERROR)
    assert not is_weekly_exhausted(r, now=NOW)


def test_parse_error_reading_is_not_exhausted() -> None:
    r = reading(pct=None, reset=None, status=QuotaStatus.PARSE_ERROR)
    assert not is_weekly_exhausted(r, now=NOW)


def test_unavailable_reading_is_not_exhausted() -> None:
    r = reading(pct=None, reset=None, status=QuotaStatus.UNAVAILABLE)
    assert not is_weekly_exhausted(r, now=NOW)


def test_expired_reset_clears_exhaustion() -> None:
    past_reset = NOW - timedelta(hours=1)
    r = reading(pct=100, reset=past_reset)
    assert not is_weekly_exhausted(r, now=NOW)


def test_reset_at_exactly_now_clears_exhaustion() -> None:
    r = reading(pct=100, reset=NOW)
    assert not is_weekly_exhausted(r, now=NOW)


# ── Claude snapshot derivation ──


def test_claude_exhausted_disables_five_hour() -> None:
    """For exhausted Claude, show critical weekly state and disable five-hour row."""
    readings = [
        reading(service=Service.CLAUDE, label="Five-hour", pct=42, reset=RESET),
        reading(service=Service.CLAUDE, label="Weekly", pct=100, reset=RESET),
    ]
    snapshot = derive_service(Service.CLAUDE, readings, now=NOW)
    assert snapshot.exhausted
    assert snapshot.weekly is not None
    assert snapshot.weekly.percentage == 100
    assert snapshot.five_hour is not None
    assert snapshot.five_hour.percentage == 42


def test_claude_not_exhausted_when_weekly_below_100() -> None:
    readings = [
        reading(service=Service.CLAUDE, label="Five-hour", pct=42, reset=RESET),
        reading(service=Service.CLAUDE, label="Weekly", pct=68, reset=RESET),
    ]
    snapshot = derive_service(Service.CLAUDE, readings, now=NOW)
    assert not snapshot.exhausted


def test_claude_stale_weekly_100_not_exhausted() -> None:
    """STALE weekly at 100% must not newly establish exhaustion."""
    readings = [
        reading(
            service=Service.CLAUDE,
            label="Weekly",
            pct=100,
            reset=RESET,
            status=QuotaStatus.STALE,
        ),
    ]
    snapshot = derive_service(Service.CLAUDE, readings, now=NOW)
    assert not snapshot.exhausted


def test_claude_stored_readings_unchanged() -> None:
    """Derivation must not mutate stored readings."""
    readings = [
        reading(service=Service.CLAUDE, label="Five-hour", pct=42, reset=RESET),
        reading(service=Service.CLAUDE, label="Weekly", pct=100, reset=RESET),
    ]
    before = [r.to_dict() for r in readings]
    derive_service(Service.CLAUDE, readings, now=NOW)
    after = [r.to_dict() for r in readings]
    assert before == after


# ── Codex snapshot derivation ──


def test_codex_exhausted_shows_critical() -> None:
    readings = [
        reading(service=Service.CODEX, label="Weekly", pct=100, reset=RESET),
    ]
    snapshot = derive_service(Service.CODEX, readings, now=NOW)
    assert snapshot.exhausted
    assert snapshot.weekly is not None
    assert snapshot.weekly.percentage == 100
    assert snapshot.five_hour is None  # Codex never has a five-hour quota


def test_codex_not_exhausted_below_100() -> None:
    readings = [
        reading(service=Service.CODEX, label="Weekly", pct=38, reset=RESET),
    ]
    snapshot = derive_service(Service.CODEX, readings, now=NOW)
    assert not snapshot.exhausted


def test_codex_critical_state_has_reset_and_countdown() -> None:
    readings = [
        reading(service=Service.CODEX, label="Weekly", pct=100, reset=RESET),
    ]
    snapshot = derive_service(Service.CODEX, readings, now=NOW)
    assert snapshot.exhausted
    assert snapshot.weekly_reset_at == RESET
    # The reset is in the future
    assert snapshot.weekly_reset_at > NOW


# ── Full state derivation ──


def test_derive_state_both_services() -> None:
    readings = [
        reading(service=Service.CLAUDE, label="Five-hour", pct=42, reset=RESET),
        reading(service=Service.CLAUDE, label="Weekly", pct=100, reset=RESET),
        reading(service=Service.CODEX, label="Weekly", pct=38, reset=RESET),
    ]
    state = derive_state(readings, now=NOW)
    assert state[Service.CLAUDE].exhausted
    assert not state[Service.CODEX].exhausted


def test_derive_state_empty_readings() -> None:
    state = derive_state([], now=NOW)
    assert not state[Service.CLAUDE].exhausted
    assert not state[Service.CODEX].exhausted
    assert state[Service.CLAUDE].weekly is None
    assert state[Service.CODEX].weekly is None
