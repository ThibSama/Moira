"""Deterministic tests for alert exhaustion/recovery events, deduplication,
and 100% threshold suppression."""

from datetime import UTC, datetime, timedelta

from moira.alerts import evaluate_alerts
from moira.models import QuotaReading, QuotaStatus, Service
from moira.persistence import Settings

NOW = datetime(2026, 8, 2, 12, tzinfo=UTC)
RESET = NOW + timedelta(days=5)
NEW_RESET = RESET + timedelta(days=7)


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


# ── Exhaustion alert ──


def test_exhaustion_alert_fires_at_100_pct() -> None:
    settings = Settings(thresholds=[50, 75, 90, 100])
    alerts = evaluate_alerts([reading(pct=80)], [reading(pct=100)], settings, set())
    exh = [a for a in alerts if a.key.startswith("exhausted:")]
    assert len(exh) == 1
    assert "claude" in exh[0].key


def test_exhaustion_alert_deduplicated() -> None:
    settings = Settings(thresholds=[])
    first = evaluate_alerts([], [reading(pct=100)], settings, set())
    assert len(first) == 1
    second = evaluate_alerts([reading(pct=100)], [reading(pct=100)], settings, {first[0].key})
    assert len(second) == 0


def test_no_exhaustion_alert_below_100() -> None:
    settings = Settings(thresholds=[])
    alerts = evaluate_alerts([reading(pct=80)], [reading(pct=99)], settings, set())
    exh = [a for a in alerts if a.key.startswith("exhausted:")]
    assert len(exh) == 0


def test_stale_100_does_not_fire_exhaustion() -> None:
    settings = Settings(thresholds=[])
    stale = reading(pct=100, status=QuotaStatus.STALE)
    alerts = evaluate_alerts([], [stale], settings, set())
    exh = [a for a in alerts if a.key.startswith("exhausted:")]
    assert len(exh) == 0


# ── 100% threshold suppression ──


def test_100_pct_threshold_suppressed_when_exhaustion_fires() -> None:
    """A duplicate generic 100% alert must be suppressed."""
    settings = Settings(thresholds=[50, 75, 90, 100])
    alerts = evaluate_alerts([reading(pct=80)], [reading(pct=100)], settings, set())
    # Should have exhaustion, but NOT a threshold:100 alert
    thresholds = [a for a in alerts if a.key.startswith("threshold:")]
    assert all("100" not in a.key.rsplit(":", 1)[-1] for a in thresholds)
    exh = [a for a in alerts if a.key.startswith("exhausted:")]
    assert len(exh) == 1


def test_sub_100_threshold_still_fires() -> None:
    settings = Settings(thresholds=[50, 75, 90])
    alerts = evaluate_alerts([reading(pct=49)], [reading(pct=76)], settings, set())
    thresholds = [a for a in alerts if a.key.startswith("threshold:")]
    assert len(thresholds) == 2  # 50 and 75


# ── Recovery alert ──


def test_recovery_alert_fires_when_exhaustion_clears() -> None:
    settings = Settings(thresholds=[])
    alerts = evaluate_alerts(
        [reading(pct=100, reset=RESET)],
        [reading(pct=50, reset=NEW_RESET)],
        settings,
        set(),
    )
    rec = [a for a in alerts if a.key.startswith("recovered:")]
    assert len(rec) == 1


def test_recovery_alert_deduplicated() -> None:
    settings = Settings(thresholds=[])
    sent: set[str] = set()
    first = evaluate_alerts(
        [reading(pct=100, reset=RESET)],
        [reading(pct=50, reset=NEW_RESET)],
        settings,
        sent,
    )
    sent.update(a.key for a in first)
    assert len(first) == 1
    second = evaluate_alerts(
        [reading(pct=100, reset=RESET)],
        [reading(pct=50, reset=NEW_RESET)],
        settings,
        sent,
    )
    assert len(second) == 0


def test_no_recovery_when_not_previously_exhausted() -> None:
    settings = Settings(thresholds=[])
    alerts = evaluate_alerts(
        [reading(pct=80)],
        [reading(pct=50, reset=NEW_RESET)],
        settings,
        set(),
    )
    rec = [a for a in alerts if a.key.startswith("recovered:")]
    assert len(rec) == 0


# ── Codex exhaustion ──


def test_codex_exhaustion_alert() -> None:
    settings = Settings(thresholds=[])
    alerts = evaluate_alerts(
        [],
        [reading(service=Service.CODEX, pct=100)],
        settings,
        set(),
    )
    exh = [a for a in alerts if a.key.startswith("exhausted:")]
    assert len(exh) == 1
    assert "codex" in exh[0].key


# ── Independence from thresholds ──


def test_exhaustion_independent_from_thresholds() -> None:
    """Exhaustion fires even with empty thresholds list."""
    settings = Settings(thresholds=[])
    alerts = evaluate_alerts([reading(pct=50)], [reading(pct=100)], settings, set())
    exh = [a for a in alerts if a.key.startswith("exhausted:")]
    assert len(exh) == 1


# ── Reset alert not fired for exhausted → recovered window ──


def test_reset_suppressed_during_exhaustion_recovery() -> None:
    """A reset alert should not fire when transitioning from exhausted to recovered,
    since the recovery event already covers that."""
    settings = Settings(thresholds=[], reset_alerts=True)
    alerts = evaluate_alerts(
        [reading(pct=100, reset=RESET)],
        [reading(pct=50, reset=NEW_RESET)],
        settings,
        set(),
    )
    resets = [a for a in alerts if a.key.startswith("reset:")]
    assert len(resets) == 0
