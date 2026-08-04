"""Package 4: exact statistics and activity indicators — deterministic fixtures and tests.

Covers: empty/one/multiple days, explicitly reported zero-token days,
missing-day gaps, duplicate rejection at the pure boundary, shuffled-input
determinism, large int64 values, documented half-up rounding, earliest-day
peak tie rule, bucket exclusion, range/service isolation, Claude
UNSUPPORTED, exact-plus-temporary-note coexistence, EN/FR indicator text
and account-wide summary separation.

All arithmetic is integer/Decimal — no floats, no locale, no timezone,
no input-order dependence.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from datetime import time as time_type
from decimal import Decimal

import pytest

from moira.history import HistoryStatus, TokenObservation
from moira.history_view import (
    DailyTokenStats,
    HistoryViewResult,
    build_codex_summary_text,
    build_daily_token_stats_text,
    prepare_history_view,
)
from moira.i18n import _FRENCH
from moira.models import CodexSummary, Service, TokenAvailabilityRecord

NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)
DAY1 = NOW - timedelta(days=2)  # 2026-07-31
DAY2 = NOW - timedelta(days=1)  # 2026-08-01
DAY3 = NOW  # 2026-08-02


def _token_obs(
    day: datetime,
    *,
    service: Service = Service.CODEX,
    observed: datetime | None = None,
    source: str = "codex-app-server:account/usage/read",
    status: HistoryStatus = HistoryStatus.AVAILABLE_EXACT,
    tokens: int | None = 500,
    period_kind: str = "day",
) -> TokenObservation:
    period_start = datetime.combine(day.date(), time_type.min, tzinfo=UTC)
    return TokenObservation(
        service=service,
        period_start=period_start,
        period_kind=period_kind,
        observed_at=observed or day,
        source=source,
        status=status,
        tokens=tokens,
    )


def _view(
    token_obs: list[TokenObservation],
    *,
    range_label: str = "30d",
    filter_label: str = "All",
    avail: list[TokenAvailabilityRecord] | None = None,
) -> HistoryViewResult:
    return prepare_history_view(
        [],
        range_label=range_label,
        filter_label=filter_label,
        token_observations=token_obs,
        token_availability_records=avail or [],
    )


def _stats(view: HistoryViewResult) -> tuple[DailyTokenStats, ...]:
    return view.daily_token_stats


# ── Criterion 1/8: empty input and single-day validity ──────────────────────


def test_empty_input_produces_no_indicators() -> None:
    """Zero exact days → no indicator entries, never a zero card."""
    view = _view([])
    assert _stats(view) == ()
    assert view.token_summaries == ()


def test_single_exact_day_is_valid() -> None:
    """One exact day is valid: total, days=1, avg=total, peak=that day, share 100%."""
    view = _view([_token_obs(DAY3, tokens=1000)])
    stats = _stats(view)
    assert len(stats) == 1
    s = stats[0]
    assert s.service is Service.CODEX
    assert s.reported_days == 1
    assert s.total_tokens == 1000
    assert s.average_per_reported_day == 1000
    assert s.peak_day == DAY3.date().isoformat()
    assert s.peak_tokens == 1000
    assert s.peak_share_percent == Decimal("100.0")
    assert s.has_data


# ── Criterion 2: multiple days, total/average/peak/share ────────────────────


def test_multiple_days_statistics() -> None:
    """Three reported days produce exact total, average, peak and share."""
    view = _view(
        [
            _token_obs(DAY1, tokens=1000),
            _token_obs(DAY2, tokens=2000),
            _token_obs(DAY3, tokens=3000),
        ]
    )
    s = _stats(view)[0]
    assert s.reported_days == 3
    assert s.total_tokens == 6000
    assert s.average_per_reported_day == 2000
    assert s.peak_day == DAY3.date().isoformat()
    assert s.peak_tokens == 3000
    assert s.peak_share_percent == Decimal("50.0")


# ── Criterion 3: explicit zeros and missing-day gaps ─────────────────────────


def test_explicitly_reported_zero_token_days_count() -> None:
    """A reported zero-token day is part of the denominator and total."""
    view = _view(
        [
            _token_obs(DAY2, tokens=0),
            _token_obs(DAY3, tokens=500),
        ]
    )
    s = _stats(view)[0]
    assert s.reported_days == 2
    assert s.total_tokens == 500
    assert s.average_per_reported_day == 250
    assert s.peak_day == DAY3.date().isoformat()
    assert s.peak_tokens == 500
    assert s.peak_share_percent == Decimal("100.0")


def test_missing_day_gaps_never_filled_with_zero() -> None:
    """Gaps between reported days do not become zero days: the denominator is
    the number of reported exact days only."""
    view = _view(
        [
            _token_obs(NOW - timedelta(days=30), tokens=1000),
            _token_obs(NOW - timedelta(days=26), tokens=2000),
        ]
    )
    s = _stats(view)[0]
    assert s.reported_days == 2  # not 5
    assert s.total_tokens == 3000
    assert s.average_per_reported_day == 1500  # not 3000/5


# ── Criterion 4: determinism, int64, rounding ────────────────────────────────


def test_shuffled_input_deterministic() -> None:
    """Input order never changes the derived statistics."""
    sorted_obs = [
        _token_obs(DAY1, tokens=1000, observed=NOW),
        _token_obs(DAY2, tokens=2000, observed=NOW + timedelta(minutes=1)),
        _token_obs(DAY3, tokens=3000, observed=NOW + timedelta(minutes=2)),
    ]
    shuffled = list(reversed(sorted_obs))
    a = _stats(_view(sorted_obs))[0]
    b = _stats(_view(shuffled))[0]
    assert a == b
    assert a.total_tokens == 6000
    assert a.average_per_reported_day == 2000


def test_large_int64_values_exact() -> None:
    """Sums and averages near the signed-int64 bound stay exact (int math)."""
    max64 = 2**63 - 1
    view = _view(
        [
            _token_obs(DAY1, tokens=max64),
            _token_obs(DAY2, tokens=max64 - 1),
        ]
    )
    s = _stats(view)[0]
    assert s.total_tokens == max64 + (max64 - 1)
    assert s.reported_days == 2
    # (2*max64 - 1) / 2 = max64 - 0.5 → half-up → max64
    assert s.average_per_reported_day == max64
    assert s.peak_share_percent == Decimal("50.0")


def test_average_rounding_half_up() -> None:
    """Average per reported day rounds half-up to the nearest integer."""
    # 10 / 3 = 3.333… → 3
    view = _view(
        [_token_obs(DAY1, tokens=4), _token_obs(DAY2, tokens=3), _token_obs(DAY3, tokens=3)]
    )
    assert _stats(view)[0].average_per_reported_day == 3
    # 11 / 3 = 3.666… → 4
    view = _view(
        [_token_obs(DAY1, tokens=5), _token_obs(DAY2, tokens=3), _token_obs(DAY3, tokens=3)]
    )
    assert _stats(view)[0].average_per_reported_day == 4
    # 10 / 4 = 2.5 → half-up → 3
    view = _view(
        [
            _token_obs(DAY1, tokens=4),
            _token_obs(DAY2, tokens=3),
            _token_obs(DAY3, tokens=3),
            _token_obs(NOW - timedelta(days=3), tokens=0),
        ]
    )
    assert _stats(view)[0].average_per_reported_day == 3


def test_peak_share_rounding_half_up() -> None:
    """Peak share rounds half-up to one decimal place."""
    # Sixteen reported days of 1 token: peak 1 of total 16 → 6.25% → 6.3
    view = _view([_token_obs(NOW - timedelta(days=i), tokens=1) for i in range(16)])
    assert _stats(view)[0].peak_share_percent == Decimal("6.3")
    # peak 15 of total 16 → 93.75% → 93.8
    view = _view([_token_obs(DAY1, tokens=15), _token_obs(DAY2, tokens=1)])
    assert _stats(view)[0].peak_share_percent == Decimal("93.8")
    # Seven reported days of 1 token: peak 1 of total 7 → 14.2857… → 14.3
    view = _view([_token_obs(NOW - timedelta(days=i), tokens=1) for i in range(7)])
    assert _stats(view)[0].peak_share_percent == Decimal("14.3")


def test_peak_tie_earliest_day_wins() -> None:
    """A peak tie resolves to the earliest reported day (stable documented rule)."""
    view = _view(
        [
            _token_obs(DAY1, tokens=2000),
            _token_obs(DAY2, tokens=2000),
            _token_obs(DAY3, tokens=1000),
        ]
    )
    s = _stats(view)[0]
    assert s.peak_tokens == 2000
    assert s.peak_day == DAY1.date().isoformat()
    assert s.peak_share_percent == Decimal("40.0")


# ── Criterion 5: duplicate rejection at the pure boundary ────────────────────


def test_duplicate_daily_input_rejected() -> None:
    """A duplicate (service, day) daily pair fails closed instead of double-counting."""
    with pytest.raises(ValueError, match="duplicate daily token observation"):
        _view(
            [
                _token_obs(DAY3, tokens=500),
                _token_obs(DAY3, tokens=500),
            ]
        )


def test_duplicate_check_is_per_service_per_day() -> None:
    """The same day for different services is not a duplicate; bucket rows on
    the same day are not part of the daily identity and never raise. An exact
    Claude row is ignored by the capability gate — never rendered or summed."""
    view = _view(
        [
            _token_obs(DAY3, tokens=500),  # codex daily
            _token_obs(DAY3, tokens=100, period_kind="bucket"),  # codex bucket
            _token_obs(DAY3, tokens=0, service=Service.CLAUDE),  # exact claude (impossible)
        ]
    )
    # Only Codex daily rows are supported token data.
    services = {s.service for s in _stats(view)}
    assert services == {Service.CODEX}
    codex = next(s for s in _stats(view) if s.service is Service.CODEX)
    assert codex.total_tokens == 500  # the bucket row never contributes
    assert codex.reported_days == 1  # the exact Claude row never contributes


def test_duplicate_rejection_happens_before_any_rendering() -> None:
    """The rejection is at the pure aggregation boundary: even a non-empty
    quota input does not bypass it."""
    from moira.history import QuotaObservation

    quota = [
        QuotaObservation(
            service=Service.CODEX,
            quota_label="Weekly",
            percentage=50.0,
            reset_at=NOW + timedelta(days=5),
            observed_at=NOW,
            source="codex-app-server",
        )
    ]
    with pytest.raises(ValueError, match="duplicate daily token observation"):
        prepare_history_view(
            quota,
            range_label="30d",
            filter_label="All",
            token_observations=[_token_obs(DAY3, tokens=500), _token_obs(DAY3, tokens=500)],
        )


# ── Criterion 6: bucket exclusion ────────────────────────────────────────────


def test_bucket_events_excluded_from_daily_indicators() -> None:
    """Migrated bucket events stay in their existing summary but never
    contribute to daily averages or peak indicators."""
    view = _view(
        [
            _token_obs(DAY3, tokens=500),  # daily
            _token_obs(DAY3, tokens=4000, period_kind="bucket"),  # migrated
        ]
    )
    s = _stats(view)[0]
    assert s.total_tokens == 500  # bucket 4000 excluded
    assert s.reported_days == 1
    assert s.peak_tokens == 500
    # The existing per-kind summary still carries the bucket total.
    kinds = {ts.period_kind for ts in view.token_summaries}
    assert kinds == {"day", "bucket"}
    bucket_summary = next(ts for ts in view.token_summaries if ts.period_kind == "bucket")
    assert bucket_summary.total_tokens == 4000


# ── Criteria 7/9: range/service isolation, Claude unsupported, separation ────


def test_range_isolation() -> None:
    """Indicators derive only from the rows of the selected range."""
    day_obs = [_token_obs(DAY1, tokens=1000), _token_obs(DAY3, tokens=3000)]
    narrow = _view([day_obs[1]], range_label="24h")
    wide = _view(day_obs, range_label="30d")
    assert narrow.daily_token_stats[0].total_tokens == 3000
    assert wide.daily_token_stats[0].total_tokens == 4000
    # The range label travels with the view for rendering.
    assert wide.range_label == "30d"


def test_service_filter_isolation() -> None:
    """Filtering to a service changes indicators exactly as it changes rows."""
    codex_rows = [_token_obs(DAY1, tokens=1000), _token_obs(DAY2, tokens=2000)]
    all_view = _view(codex_rows, filter_label="All")
    codex_view = _view(codex_rows, filter_label="Codex")
    assert all_view.daily_token_stats == codex_view.daily_token_stats
    # A Codex-only filter receives only Codex rows upstream; the builder
    # produces exactly one entry, sorted by service.
    assert len(codex_view.daily_token_stats) == 1
    assert codex_view.daily_token_stats[0].service is Service.CODEX


def test_claude_unsupported_shows_no_token_indicators() -> None:
    """Claude remains UNSUPPORTED: no daily indicators and no token summary."""
    claude_obs = [
        _token_obs(
            DAY3,
            service=Service.CLAUDE,
            source="claude-statusline",
            status=HistoryStatus.UNSUPPORTED,
            tokens=None,
        )
    ]
    view = _view(claude_obs)
    assert view.daily_token_stats == ()
    assert view.token_summaries == ()


def test_exact_plus_temporary_note_coexists() -> None:
    """Exact daily indicators coexist with a TEMPORARILY_UNAVAILABLE note."""
    avail = TokenAvailabilityRecord(
        service=Service.CODEX,
        observed_at=NOW + timedelta(minutes=1),
        source="codex-app-server",
        status=HistoryStatus.TEMPORARILY_UNAVAILABLE,
    )
    view = _view(
        [_token_obs(DAY1, tokens=1000), _token_obs(DAY3, tokens=3000)],
        avail=[avail],
    )
    assert len(view.daily_token_stats) == 1
    assert view.daily_token_stats[0].total_tokens == 4000
    assert len(view.token_availability) == 1
    assert view.token_availability[0].status is HistoryStatus.TEMPORARILY_UNAVAILABLE


def test_account_summary_stays_separate_and_account_wide() -> None:
    """The official summary is a separate field, explicitly account-wide, and
    never relabeled as selected-range data."""
    summary = CodexSummary(
        service=Service.CODEX,
        source="codex-app-server:account/usage/read",
        observed_at=NOW,
        lifetime_tokens=100000,
        peak_daily_tokens=9000,
        current_streak_days=3,
        longest_streak_days=14,
        longest_running_turn_sec=1500,
    )
    view = prepare_history_view(
        [],
        range_label="30d",
        filter_label="All",
        codex_summaries=[summary],
        token_observations=[_token_obs(DAY3, tokens=500)],
    )
    assert len(view.codex_summaries) == 1
    assert view.codex_summaries[0].lifetime_tokens == 100000
    # The summary is not folded into daily indicators.
    assert view.daily_token_stats[0].total_tokens == 500
    text = build_codex_summary_text(summary, lambda s: s)
    assert "account-wide" in text
    assert "Lifetime: 100,000" in text


# ── Criterion 1/10: text builders, EN/FR, rendering inputs ──────────────────


def test_build_daily_stats_text_english() -> None:
    """The indicator line is a pure function with the service and range in the label."""
    view = _view(
        [
            _token_obs(DAY1, tokens=1000),
            _token_obs(DAY2, tokens=2000),
            _token_obs(DAY3, tokens=3000),
        ]
    )
    text = build_daily_token_stats_text(view.daily_token_stats[0], view.range_label, lambda s: s)
    assert "Codex · 30d" in text
    assert "Total: 6,000" in text
    assert "Reported days: 3" in text
    assert "Avg/day: 2,000" in text
    assert "Peak: 2026-08-02 (3,000)" in text
    assert "Peak share: 50.0%" in text


def _fr(s: str) -> str:
    """French catalog lookup with English fallback (typed translator)."""
    return _FRENCH.get(s, s)


def test_build_daily_stats_text_french() -> None:
    """French translations exist and the pure builder renders them."""
    view = _view([_token_obs(DAY3, tokens=1000)])
    text = build_daily_token_stats_text(view.daily_token_stats[0], "30d", _fr)
    assert "Total" in text  # "Total" is identical in French
    assert "Jours rapportés" in text
    assert "Moy./jour" in text
    assert "Pic" in text
    assert "Part du pic" in text
    assert "100.0%" in text


def test_french_catalog_has_package4_keys() -> None:
    """Every Package 4 visible string has a French translation."""
    for key in ("Reported days", "Avg/day", "Peak", "Peak share", "account-wide"):
        assert _FRENCH.get(key) is not None, f"missing French translation for {key!r}"


def test_daily_stats_frozen_and_immutable() -> None:
    """DailyTokenStats is frozen with tuple-safe fields."""
    from dataclasses import FrozenInstanceError

    view = _view([_token_obs(DAY3, tokens=1000)])
    s = view.daily_token_stats[0]
    with pytest.raises(FrozenInstanceError):
        s.total_tokens = 999  # type: ignore[misc]
    assert isinstance(view.daily_token_stats, tuple)


def test_zero_token_peak_share_is_none() -> None:
    """All-zero reported days: peak share is None (no division by zero)."""
    view = _view(
        [
            _token_obs(DAY1, tokens=0),
            _token_obs(DAY2, tokens=0),
        ]
    )
    s = view.daily_token_stats[0]
    assert s.total_tokens == 0
    assert s.reported_days == 2
    assert s.average_per_reported_day == 0
    assert s.peak_day == DAY1.date().isoformat()  # earliest reported day
    assert s.peak_tokens == 0
    assert s.peak_share_percent is None
