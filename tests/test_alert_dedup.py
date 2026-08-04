"""Deterministic tests for alert exhaustion/recovery events, deduplication,
and 100% threshold suppression."""

from datetime import UTC, datetime, timedelta

from moira.alerts import evaluate_alerts
from moira.models import QuotaReading, QuotaStatus, Service
from moira.persistence import Settings

NOW = datetime(2026, 8, 2, 12, tzinfo=UTC)
RESET = NOW + timedelta(days=5)
NEW_RESET = RESET + timedelta(days=7)
# Real post-reset chronology: evaluation time is after the old reset
OLD_RESET = NOW - timedelta(hours=1)
AFTER_OLD_RESET = OLD_RESET + timedelta(hours=1)
NEW_RESET_POST = OLD_RESET + timedelta(days=5)


def base_key(key: str) -> str:
    """Strip the per-channel suffix from a dedup key."""
    return key.rsplit(":", 1)[0]


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
    settings = Settings(thresholds=[50, 75, 90, 100], ntfy_enabled=True)
    alerts = evaluate_alerts([reading(pct=80)], [reading(pct=100)], settings, set())
    exh = [a for a in alerts if a.key.startswith("exhausted:")]
    assert len(exh) == 1
    assert "claude" in exh[0].key


def test_exhaustion_alert_deduplicated() -> None:
    settings = Settings(thresholds=[], ntfy_enabled=True)
    first = evaluate_alerts([], [reading(pct=100)], settings, set())
    assert len(first) == 1
    second = evaluate_alerts([reading(pct=100)], [reading(pct=100)], settings, {first[0].key})
    assert len(second) == 0


def test_no_exhaustion_alert_below_100() -> None:
    settings = Settings(thresholds=[], ntfy_enabled=True)
    alerts = evaluate_alerts([reading(pct=80)], [reading(pct=99)], settings, set())
    exh = [a for a in alerts if a.key.startswith("exhausted:")]
    assert len(exh) == 0


def test_stale_100_does_not_fire_exhaustion() -> None:
    settings = Settings(thresholds=[], ntfy_enabled=True)
    stale = reading(pct=100, status=QuotaStatus.STALE)
    alerts = evaluate_alerts([], [stale], settings, set())
    exh = [a for a in alerts if a.key.startswith("exhausted:")]
    assert len(exh) == 0


# ── 100% threshold suppression ──


def test_100_pct_threshold_suppressed_when_exhaustion_fires() -> None:
    """A duplicate generic 100% alert must be suppressed."""
    settings = Settings(thresholds=[50, 75, 90, 100], ntfy_enabled=True)
    alerts = evaluate_alerts([reading(pct=80)], [reading(pct=100)], settings, set())
    # Should have exhaustion, but NOT a threshold:100 alert
    thresholds = [a for a in alerts if a.key.startswith("threshold:")]
    assert all("100" not in base_key(a.key).rsplit(":", 1)[-1] for a in thresholds)
    exh = [a for a in alerts if a.key.startswith("exhausted:")]
    assert len(exh) == 1


def test_sub_100_threshold_still_fires() -> None:
    settings = Settings(thresholds=[50, 75, 90], ntfy_enabled=True)
    alerts = evaluate_alerts([reading(pct=49)], [reading(pct=76)], settings, set())
    thresholds = [a for a in alerts if a.key.startswith("threshold:")]
    assert len(thresholds) == 2  # 50 and 75


# ── Recovery alert ──


def test_recovery_alert_fires_when_exhaustion_clears() -> None:
    settings = Settings(thresholds=[], ntfy_enabled=True)
    alerts = evaluate_alerts(
        [reading(pct=100, reset=RESET)],
        [reading(pct=50, reset=NEW_RESET)],
        settings,
        set(),
    )
    rec = [a for a in alerts if a.key.startswith("recovered:")]
    assert len(rec) == 1


def test_recovery_alert_deduplicated() -> None:
    settings = Settings(thresholds=[], ntfy_enabled=True)
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
    settings = Settings(thresholds=[], ntfy_enabled=True)
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
    settings = Settings(thresholds=[], ntfy_enabled=True)
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
    settings = Settings(thresholds=[], ntfy_enabled=True)
    alerts = evaluate_alerts([reading(pct=50)], [reading(pct=100)], settings, set())
    exh = [a for a in alerts if a.key.startswith("exhausted:")]
    assert len(exh) == 1


# ── Reset alert not fired for exhausted → recovered window ──


def test_reset_suppressed_during_exhaustion_recovery() -> None:
    """A reset alert should not fire when transitioning from exhausted to recovered,
    since the recovery event already covers that."""
    settings = Settings(thresholds=[], reset_alerts=True, ntfy_enabled=True)
    alerts = evaluate_alerts(
        [reading(pct=100, reset=RESET)],
        [reading(pct=50, reset=NEW_RESET)],
        settings,
        set(),
    )
    resets = [a for a in alerts if a.key.startswith("reset:")]
    assert len(resets) == 0


# ── 0.2.1 regression: expired 100% reading produces no exhaustion ──


def test_expired_100_pct_produces_no_exhaustion_alert() -> None:
    """An AVAILABLE weekly reading at 100% whose reset_at has passed must not
    produce an exhaustion alert."""
    past_reset = NOW - timedelta(hours=1)
    settings = Settings(thresholds=[], ntfy_enabled=True)
    alerts = evaluate_alerts(
        [],
        [reading(pct=100, reset=past_reset)],
        settings,
        set(),
        now=NOW,
    )
    exh = [a for a in alerts if a.key.startswith("exhausted:")]
    assert len(exh) == 0


def test_expired_100_pct_produces_no_recovery_alert() -> None:
    """An expired 100% reading must not produce a recovery alert either."""
    past_reset = NOW - timedelta(hours=1)
    settings = Settings(thresholds=[], ntfy_enabled=True)
    alerts = evaluate_alerts(
        [reading(pct=100, reset=past_reset)],
        [reading(pct=100, reset=past_reset)],
        settings,
        set(),
        now=NOW,
    )
    rec = [a for a in alerts if a.key.startswith("recovered:")]
    assert len(rec) == 0


# ── 0.2.1 regression: UI and alerts use equivalent exhaustion semantics ──


def test_ui_and_alerts_equivalent_for_exhausted_reading() -> None:
    """is_weekly_exhausted (UI) and evaluate_alerts agree on a current exhausted reading."""
    from moira.exhaustion import is_weekly_exhausted

    settings = Settings(thresholds=[], ntfy_enabled=True)
    r = reading(pct=100, reset=RESET)
    alerts = evaluate_alerts([], [r], settings, set(), now=NOW)
    exh_alerts = [a for a in alerts if a.key.startswith("exhausted:")]
    # The UI rule and the alert rule must agree
    assert is_weekly_exhausted(r, now=NOW) is True
    assert len(exh_alerts) == 1


def test_ui_and_alerts_equivalent_for_expired_reading() -> None:
    """Both UI and alerts agree that an expired 100% reading is not exhausted."""
    from moira.exhaustion import is_weekly_exhausted

    past_reset = NOW - timedelta(hours=1)
    r = reading(pct=100, reset=past_reset)
    settings = Settings(thresholds=[], ntfy_enabled=True)
    alerts = evaluate_alerts([], [r], settings, set(), now=NOW)
    exh_alerts = [a for a in alerts if a.key.startswith("exhausted:")]
    assert is_weekly_exhausted(r, now=NOW) is False
    assert len(exh_alerts) == 0


# ── 0.2.1 regression: threshold 100 suppressed, lower thresholds fire ──


def test_threshold_100_suppressed_while_90_fires() -> None:
    """When exhaustion fires at 100%, the generic 100% threshold is suppressed
    but a crossed lower threshold (e.g. 90) governed normally.

    Since reading goes from 80→100 and exhaustion fires, the 100% threshold
    would fire. The 100% threshold should NOT appear. The 90% threshold should
    appear if crossed.
    """
    settings = Settings(thresholds=[90, 100], ntfy_enabled=True)
    alerts = evaluate_alerts(
        [reading(pct=80)],
        [reading(pct=100)],
        settings,
        set(),
        now=NOW,
    )
    exh = [a for a in alerts if a.key.startswith("exhausted:")]
    assert len(exh) == 1

    threshold_alerts = [a for a in alerts if a.key.startswith("threshold:")]
    # Only the 90% threshold should fire (crossed from 80→100), NOT the 100% one.
    threshold_values = {base_key(a.key).rsplit(":", 1)[-1] for a in threshold_alerts}
    assert "90" in threshold_values
    assert "100" not in threshold_values


def test_lower_thresholds_fire_normally_at_non_100_crossing() -> None:
    """Lower threshold crossings not involving 100% are governed normally."""
    settings = Settings(thresholds=[50, 75, 90, 100], ntfy_enabled=True)
    alerts = evaluate_alerts(
        [reading(pct=49)],
        [reading(pct=95)],
        settings,
        set(),
        now=NOW,
    )
    threshold_alerts = [a for a in alerts if a.key.startswith("threshold:")]
    threshold_values = {base_key(a.key).rsplit(":", 1)[-1] for a in threshold_alerts}
    assert threshold_values == {"50", "75", "90"}
    assert "100" not in threshold_values


# ── 0.2.1 regression: recovery after new window or sub-100 fresh reading ──


def test_recovery_after_new_window_deduplicated() -> None:
    """Recovery when a new weekly window appears after the old reset has passed.
    Uses realistic chronology: evaluation time is after the old reset_at."""
    settings = Settings(thresholds=[], ntfy_enabled=True)
    sent: set[str] = set()
    first = evaluate_alerts(
        [reading(pct=100, reset=OLD_RESET)],
        [reading(pct=50, reset=NEW_RESET_POST)],
        settings,
        sent,
        now=AFTER_OLD_RESET,
    )
    sent.update(a.key for a in first)
    assert len(first) == 1
    second = evaluate_alerts(
        [reading(pct=100, reset=OLD_RESET)],
        [reading(pct=50, reset=NEW_RESET_POST)],
        settings,
        sent,
        now=AFTER_OLD_RESET,
    )
    assert len(second) == 0


def test_recovery_after_sub_100_fresh_reading_deduplicated() -> None:
    """Recovery when the reading drops below 100% in the same reset window
    remains deduplicated (per-reading identity)."""
    settings = Settings(thresholds=[], ntfy_enabled=True)
    sent: set[str] = set()
    first = evaluate_alerts(
        [reading(pct=100)],
        [reading(pct=50)],
        settings,
        sent,
        now=NOW,
    )
    sent.update(a.key for a in first)
    assert len(first) == 1
    second = evaluate_alerts(
        [reading(pct=100)],
        [reading(pct=50)],
        settings,
        sent,
        now=NOW,
    )
    assert len(second) == 0


# ── 0.2.1 regression: five-hour 100% does not trigger exhaustion ──


def test_five_hour_100_pct_does_not_fire_exhaustion() -> None:
    """A five-hour reading at 100% must not produce a weekly exhaustion alert."""
    settings = Settings(thresholds=[], ntfy_enabled=True)
    alerts = evaluate_alerts(
        [],
        [reading(label="Five-hour", pct=100)],
        settings,
        set(),
        now=NOW,
    )
    exh = [a for a in alerts if a.key.startswith("exhausted:")]
    assert len(exh) == 0


# ── 0.2.2 regression: realistic post-reset recovery ──


def test_real_post_reset_recovery_emits_one_event() -> None:
    """Real chronology: the old weekly window reset 1 hour ago. The previous
    reading was AVAILABLE weekly at 100% with that expired reset_at. The
    current reading is AVAILABLE weekly at 50% with a new later reset.
    Exactly one recovery event is emitted."""
    settings = Settings(thresholds=[], ntfy_enabled=True)
    alerts = evaluate_alerts(
        [reading(pct=100, reset=OLD_RESET)],
        [reading(pct=50, reset=NEW_RESET_POST)],
        settings,
        set(),
        now=AFTER_OLD_RESET,
    )
    rec = [a for a in alerts if a.key.startswith("recovered:")]
    assert len(rec) == 1
    # No exhaustion or reset alert for this transition
    exh = [a for a in alerts if a.key.startswith("exhausted:")]
    assert len(exh) == 0
    resets = [a for a in alerts if a.key.startswith("reset:")]
    assert len(resets) == 0


def test_real_post_reset_recovery_deduplicated() -> None:
    """The realistic post-reset recovery event is deduplicated per window."""
    settings = Settings(thresholds=[], ntfy_enabled=True)
    sent: set[str] = set()
    first = evaluate_alerts(
        [reading(pct=100, reset=OLD_RESET)],
        [reading(pct=50, reset=NEW_RESET_POST)],
        settings,
        sent,
        now=AFTER_OLD_RESET,
    )
    sent.update(a.key for a in first)
    assert len(first) == 1
    second = evaluate_alerts(
        [reading(pct=100, reset=OLD_RESET)],
        [reading(pct=50, reset=NEW_RESET_POST)],
        settings,
        sent,
        now=AFTER_OLD_RESET,
    )
    assert len(second) == 0


def test_expired_100_prior_with_new_100_current_no_recovery() -> None:
    """If the prior was exhausted and expired, but the current is still 100%
    with a new reset, no recovery fires (no sub-100 fresh reading)."""
    settings = Settings(thresholds=[], ntfy_enabled=True)
    alerts = evaluate_alerts(
        [reading(pct=100, reset=OLD_RESET)],
        [reading(pct=100, reset=NEW_RESET_POST)],
        settings,
        set(),
        now=AFTER_OLD_RESET,
    )
    rec = [a for a in alerts if a.key.startswith("recovered:")]
    assert len(rec) == 0


# ── 0.2.2: was_weekly_exhausted domain rule ──


def test_was_weekly_exhausted_available_100() -> None:
    from moira.exhaustion import was_weekly_exhausted

    assert was_weekly_exhausted(reading(pct=100, reset=RESET), now=NOW) is True


def test_was_weekly_exhausted_expired_100() -> None:
    """An expired 100% reading WAS exhausted at observation time."""
    from moira.exhaustion import was_weekly_exhausted

    past_reset = NOW - timedelta(hours=1)
    assert was_weekly_exhausted(reading(pct=100, reset=past_reset), now=NOW) is True


def test_was_weekly_exhausted_sub_100() -> None:
    from moira.exhaustion import was_weekly_exhausted

    assert was_weekly_exhausted(reading(pct=99), now=NOW) is False


def test_was_weekly_exhausted_stale_100() -> None:
    from moira.exhaustion import was_weekly_exhausted

    assert was_weekly_exhausted(reading(pct=100, status=QuotaStatus.STALE), now=NOW) is False


def test_was_weekly_exhausted_none() -> None:
    from moira.exhaustion import was_weekly_exhausted

    assert was_weekly_exhausted(None, now=NOW) is False


def test_was_weekly_exhausted_five_hour() -> None:
    """Five-hour readings must not be considered prior weekly exhaustion."""
    from moira.exhaustion import was_weekly_exhausted

    assert was_weekly_exhausted(reading(label="Five-hour", pct=100), now=NOW) is False


def test_was_weekly_exhausted_error_reading() -> None:
    from moira.exhaustion import was_weekly_exhausted

    r = reading(pct=None, reset=None, status=QuotaStatus.ERROR)
    assert was_weekly_exhausted(r, now=NOW) is False


def test_was_weekly_exhausted_unavailable_reading() -> None:
    from moira.exhaustion import was_weekly_exhausted

    r = reading(pct=None, reset=None, status=QuotaStatus.UNAVAILABLE)
    assert was_weekly_exhausted(r, now=NOW) is False


def test_was_weekly_exhausted_parse_error_reading() -> None:
    from moira.exhaustion import was_weekly_exhausted

    r = reading(pct=None, reset=None, status=QuotaStatus.PARSE_ERROR)
    assert was_weekly_exhausted(r, now=NOW) is False


def test_was_weekly_exhausted_codex_service() -> None:
    """Does not reject based on service — Codex prior exhaustion is valid."""
    from moira.exhaustion import was_weekly_exhausted

    r = reading(service=Service.CODEX, pct=100, reset=RESET)
    assert was_weekly_exhausted(r, now=NOW) is True


# ── 0.2.2: no duplicate recovery and reset for same transition ──


def test_no_recovery_and_reset_for_same_transition() -> None:
    """When a recovery fires, a reset alert must not also fire for the same transition."""
    settings = Settings(thresholds=[], reset_alerts=True, ntfy_enabled=True)
    alerts = evaluate_alerts(
        [reading(pct=100, reset=OLD_RESET)],
        [reading(pct=50, reset=NEW_RESET_POST)],
        settings,
        set(),
        now=AFTER_OLD_RESET,
    )
    rec = [a for a in alerts if a.key.startswith("recovered:")]
    resets = [a for a in alerts if a.key.startswith("reset:")]
    assert len(rec) == 1
    assert len(resets) == 0
