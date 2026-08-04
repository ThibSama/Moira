from datetime import UTC, datetime, timedelta

from moira.alerts import evaluate_alerts, merge_with_stale
from moira.models import QuotaReading, QuotaStatus, Service
from moira.persistence import Settings

NOW = datetime(2026, 8, 1, 12, tzinfo=UTC)
RESET = NOW + timedelta(days=5)


def base_key(key: str) -> str:
    """Strip the per-channel suffix from a dedup key."""
    return key.rsplit(":", 1)[0]


def reading(
    pct: float, reset: datetime = RESET, status: QuotaStatus = QuotaStatus.AVAILABLE
) -> QuotaReading:
    return QuotaReading(Service.CLAUDE, "Weekly", pct, reset, NOW, "fixture", status)


def test_failed_refresh_retains_success_as_stale() -> None:
    failure = QuotaReading(
        Service.CLAUDE,
        "Weekly",
        None,
        None,
        NOW,
        "fixture",
        QuotaStatus.PARSE_ERROR,
        "changed format",
    )
    merged = merge_with_stale([reading(40)], [failure])
    assert merged[0].percentage == 40
    assert merged[0].status is QuotaStatus.STALE
    assert merged[0].detail == "changed format"


def test_threshold_crossing_and_deduplication() -> None:
    settings = Settings(thresholds=[50, 75], ntfy_enabled=True)
    alerts = evaluate_alerts([reading(49)], [reading(76)], settings, set())
    assert [base_key(alert.key).rsplit(":", 1)[-1] for alert in alerts] == ["50", "75"]
    sent = {alert.key for alert in alerts}
    assert evaluate_alerts([reading(49)], [reading(76)], settings, sent) == []


def test_no_alert_without_crossing() -> None:
    assert (
        evaluate_alerts(
            [reading(80)], [reading(81)], Settings(thresholds=[75], ntfy_enabled=True), set()
        )
        == []
    )


def test_reset_and_error_alert_deduplication() -> None:
    settings = Settings(thresholds=[], reset_alerts=True, error_alerts=True, ntfy_enabled=True)
    reset_alerts = evaluate_alerts(
        [reading(10)], [reading(2, RESET + timedelta(days=7))], settings, set()
    )
    assert len(reset_alerts) == 1 and reset_alerts[0].key.startswith("reset:")
    error = QuotaReading(
        Service.CLAUDE, "Weekly", None, None, NOW, "fixture", QuotaStatus.PARSE_ERROR, "format"
    )
    first = evaluate_alerts([], [error], settings, set())
    assert len(first) == 1
    assert evaluate_alerts([], [error], settings, {first[0].key}) == []
