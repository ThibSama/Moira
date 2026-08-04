"""Package 5: per-channel alert dedup (NTFY + native), legacy unprefixed keys,
and typed per-service rules."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from moira.alerts import CHANNEL_NATIVE, CHANNEL_NTFY, evaluate_alerts
from moira.models import QuotaReading, QuotaStatus, Service
from moira.persistence import ProviderRules, Settings

NOW = datetime(2026, 8, 2, 12, tzinfo=UTC)
RESET = NOW + timedelta(days=5)


def reading(
    service: Service = Service.CLAUDE,
    label: str = "Weekly",
    pct: float | None = 100,
    reset: datetime | None = RESET,
    status: QuotaStatus = QuotaStatus.AVAILABLE,
) -> QuotaReading:
    if (
        status in {QuotaStatus.AVAILABLE, QuotaStatus.STALE}
        and pct is not None
        and reset is not None
    ):
        return QuotaReading(service, label, pct, reset, NOW, "fixture", status)
    return QuotaReading(service, label, pct, reset, NOW, "fixture", status, "detail")


def base_key(key: str) -> str:
    """Strip the per-channel suffix from a dedup key."""
    return key.rsplit(":", 1)[0]


# ── Per-channel emission ──


def test_both_channels_enabled_emit_two_alerts_per_event() -> None:
    settings = Settings(ntfy_enabled=True, native_notifications=True, thresholds=[])
    alerts = evaluate_alerts([reading(pct=80)], [reading(pct=100)], settings, set())
    assert len(alerts) == 2
    assert {a.channel for a in alerts} == {CHANNEL_NTFY, CHANNEL_NATIVE}
    ntfy_key = next(a.key for a in alerts if a.channel == CHANNEL_NTFY)
    native_key = next(a.key for a in alerts if a.channel == CHANNEL_NATIVE)
    assert ntfy_key.endswith(f":{CHANNEL_NTFY}")
    assert native_key.endswith(f":{CHANNEL_NATIVE}")
    assert base_key(ntfy_key) == base_key(native_key)


def test_single_channel_emit_one_alert() -> None:
    settings = Settings(ntfy_enabled=True, native_notifications=False, thresholds=[])
    alerts = evaluate_alerts([reading(pct=80)], [reading(pct=100)], settings, set())
    assert [a.channel for a in alerts] == [CHANNEL_NTFY]
    settings = Settings(ntfy_enabled=False, native_notifications=True, thresholds=[])
    alerts = evaluate_alerts([reading(pct=80)], [reading(pct=100)], settings, set())
    assert [a.channel for a in alerts] == [CHANNEL_NATIVE]


def test_no_enabled_channel_produces_no_alerts() -> None:
    settings = Settings()
    assert evaluate_alerts([reading(pct=80)], [reading(pct=100)], settings, set()) == []


def test_one_channel_success_does_not_repeat_because_other_failed() -> None:
    """The core dedup contract: a channel that already delivered its key must
    not repeat on the next evaluation just because the other channel failed."""
    settings = Settings(ntfy_enabled=True, native_notifications=True)
    first = evaluate_alerts([], [reading(pct=100)], settings, set())
    ntfy_key = next(a.key for a in first if a.channel == CHANNEL_NTFY)
    native_key = next(a.key for a in first if a.channel == CHANNEL_NATIVE)
    # NTFY succeeded (its key persisted), native failed (its key not persisted).
    sent = {ntfy_key}
    remaining = evaluate_alerts([reading(pct=100)], [reading(pct=100)], settings, sent)
    assert [a.channel for a in remaining] == [CHANNEL_NATIVE]
    assert remaining[0].key == native_key
    # Both delivered → nothing remains.
    sent = {ntfy_key, native_key}
    assert evaluate_alerts([reading(pct=100)], [reading(pct=100)], settings, sent) == []


def test_legacy_unprefixed_key_suppresses_both_channels() -> None:
    """Legacy keys (no channel suffix) count as delivered on both channels."""
    settings = Settings(ntfy_enabled=True, native_notifications=True)
    alerts = evaluate_alerts([], [reading(pct=100)], settings, set())
    legacy = base_key(alerts[0].key)
    assert evaluate_alerts([reading(pct=100)], [reading(pct=100)], settings, {legacy}) == []


def test_threshold_dedup_is_per_channel() -> None:
    settings = Settings(ntfy_enabled=True, native_notifications=True, thresholds=[50])
    first = evaluate_alerts([reading(pct=49)], [reading(pct=76)], settings, set())
    assert len(first) == 2
    keys = {a.key for a in first}
    second = evaluate_alerts([reading(pct=49)], [reading(pct=76)], settings, keys)
    assert second == []


# ── Per-service rules ──


def test_per_service_thresholds_apply_independently() -> None:
    settings = Settings(
        ntfy_enabled=True,
        rules={
            "claude": ProviderRules([50, 75], True, True),
            "codex": ProviderRules([90], True, True),
        },
    )
    claude_alerts = evaluate_alerts([reading(pct=49)], [reading(pct=76)], settings, set())
    thresholds = {base_key(a.key).rsplit(":", 1)[-1] for a in claude_alerts}
    assert thresholds == {"50", "75"}
    codex_alerts = evaluate_alerts(
        [reading(service=Service.CODEX, pct=49)],
        [reading(service=Service.CODEX, pct=76)],
        settings,
        set(),
    )
    # 76 does not cross the codex 90 threshold.
    assert codex_alerts == []


def test_per_service_reset_alerts() -> None:
    settings = Settings(
        ntfy_enabled=True,
        rules={
            "claude": ProviderRules([50], False, True),
            "codex": ProviderRules([50], True, True),
        },
    )
    new_reset = RESET + timedelta(days=7)
    claude_alerts = evaluate_alerts(
        [reading(pct=10)],
        [reading(pct=2, reset=new_reset)],
        settings,
        set(),
    )
    # Claude reset alerts disabled → no reset event.
    assert not any(a.key.startswith("reset:") for a in claude_alerts)
    codex_alerts = evaluate_alerts(
        [reading(service=Service.CODEX, pct=10)],
        [reading(service=Service.CODEX, pct=2, reset=new_reset)],
        settings,
        set(),
    )
    assert any(a.key.startswith("reset:") for a in codex_alerts)


def test_per_service_error_alerts() -> None:
    settings = Settings(
        ntfy_enabled=True,
        rules={
            "claude": ProviderRules([50], True, True),
            "codex": ProviderRules([50], True, False),
        },
    )
    error = QuotaReading(
        Service.CODEX, "Weekly", None, None, NOW, "fixture", QuotaStatus.PARSE_ERROR, "format"
    )
    # Codex error alerts disabled → no error event.
    assert evaluate_alerts([], [error], settings, set()) == []
    settings = Settings(
        ntfy_enabled=True,
        rules={
            "claude": ProviderRules([50], True, True),
            "codex": ProviderRules([50], True, True),
        },
    )
    alerts = evaluate_alerts([], [error], settings, set())
    assert len(alerts) == 1
    assert alerts[0].key.startswith("error:")


def test_exhaustion_independent_of_per_service_thresholds() -> None:
    settings = Settings(
        ntfy_enabled=True,
        rules={"claude": ProviderRules([], True, True), "codex": ProviderRules([], True, True)},
    )
    alerts = evaluate_alerts([reading(pct=50)], [reading(pct=100)], settings, set())
    assert len(alerts) == 1
    assert alerts[0].key.startswith("exhausted:")
