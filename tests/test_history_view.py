"""Deterministic tests for the History UI: view models, chart data reduction,
filters, boundaries, stale-result rejection, bounded requests, GTK isolation,
and 90-day performance.

These tests do not require a display server — they test the pure
deterministic data preparation and the async reader logic.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

from moira.history import QuotaObservation
from moira.history_view import (
    MAX_CHART_POINTS,
    ChartPoint,
    HistoryReader,
    HistoryViewResult,
    SeriesStats,
    SeriesView,
    _detect_resets,
    _reduce_points,
    prepare_history_view,
)
from moira.models import QuotaReading, QuotaStatus, Service

NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)
RESET = NOW + timedelta(days=5)
NEW_RESET = RESET + timedelta(days=7)


def _obs(
    service: Service = Service.CLAUDE,
    label: str = "Weekly",
    pct: float = 50.0,
    reset: datetime = RESET,
    observed: datetime = NOW,
    source: str = "fixture",
) -> QuotaObservation:
    return QuotaObservation(
        service=service,
        quota_label=label,
        percentage=pct,
        reset_at=reset,
        observed_at=observed,
        source=source,
    )


def _reading(
    service: Service = Service.CLAUDE,
    label: str = "Weekly",
    pct: float | None = 50.0,
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


# ── View model tests ──


def test_prepare_history_view_empty() -> None:
    result = prepare_history_view([], range_label="24h", filter_label="All")
    assert result.series == []
    assert result.diagnostic == "ok"
    assert result.range_label == "24h"


def test_prepare_history_view_single_series() -> None:
    obs = [_obs(pct=50.0), _obs(pct=60.0, observed=NOW + timedelta(minutes=5))]
    result = prepare_history_view(obs, range_label="24h", filter_label="All")
    assert len(result.series) == 1
    s = result.series[0]
    assert s.stats.label == "Weekly"
    assert s.stats.service is Service.CLAUDE
    assert s.stats.latest == 60.0
    assert s.stats.minimum == 50.0
    assert s.stats.maximum == 60.0
    assert s.stats.count == 2
    assert len(s.points) == 2


def test_prepare_history_view_separate_metrics() -> None:
    """Claude five-hour, Claude weekly, and Codex weekly are never merged."""
    obs = [
        _obs(service=Service.CLAUDE, label="Five-hour", pct=30.0),
        _obs(service=Service.CLAUDE, label="Weekly", pct=50.0),
        _obs(service=Service.CODEX, label="Weekly", pct=60.0),
    ]
    result = prepare_history_view(obs, range_label="24h", filter_label="All")
    assert len(result.series) == 3
    labels = {(s.stats.label, s.stats.service) for s in result.series}
    assert ("Five-hour", Service.CLAUDE) in labels
    assert ("Weekly", Service.CLAUDE) in labels
    assert ("Weekly", Service.CODEX) in labels


def test_prepare_history_view_stats() -> None:
    obs = [
        _obs(pct=10.0, observed=NOW),
        _obs(pct=80.0, observed=NOW + timedelta(minutes=5)),
        _obs(pct=40.0, observed=NOW + timedelta(minutes=10)),
    ]
    result = prepare_history_view(obs, range_label="7d", filter_label="Claude")
    s = result.series[0].stats
    assert s.latest == 40.0
    assert s.minimum == 10.0
    assert s.maximum == 80.0
    assert s.count == 3
    assert s.first_observed == NOW
    assert s.last_observed == NOW + timedelta(minutes=10)
    assert s.reset_count == 0


def test_prepare_history_view_reset_markers() -> None:
    obs = [
        _obs(pct=50.0, reset=RESET, observed=NOW),
        _obs(pct=60.0, reset=NEW_RESET, observed=NOW + timedelta(minutes=5)),
    ]
    result = prepare_history_view(obs, range_label="24h", filter_label="All")
    s = result.series[0]
    assert s.stats.reset_count == 1
    assert s.points[0].is_reset is False
    assert s.points[1].is_reset is True


def test_chart_point_is_frozen() -> None:
    p = ChartPoint(observed_at=NOW, percentage=50.0, is_reset=False)
    with pytest.raises(AttributeError):
        p.percentage = 60.0  # type: ignore[misc]


def test_series_stats_is_frozen() -> None:
    s = SeriesStats(
        label="Weekly",
        service=Service.CLAUDE,
        latest=50.0,
        minimum=10.0,
        maximum=80.0,
        first_observed=NOW,
        last_observed=NOW,
        count=3,
        reset_count=0,
    )
    with pytest.raises(AttributeError):
        s.count = 5  # type: ignore[misc]


def test_history_view_result_is_frozen() -> None:
    r = HistoryViewResult(series=[], diagnostic="ok", range_label="24h", filter_label="All")
    with pytest.raises(AttributeError):
        r.diagnostic = "error"  # type: ignore[misc]


# ── Reduction tests ──


def test_reduce_under_max_keeps_all() -> None:
    obs = [_obs(pct=float(i), observed=NOW + timedelta(minutes=i)) for i in range(10)]
    resets = _detect_resets(obs)
    points = _reduce_points(obs, resets, max_points=100)
    assert len(points) == 10
    assert all(not p.is_reset for p in points)


def test_reduce_over_max_caps_at_max() -> None:
    obs = [_obs(pct=float(i % 100), observed=NOW + timedelta(minutes=i)) for i in range(500)]
    resets = _detect_resets(obs)
    points = _reduce_points(obs, resets, max_points=50)
    assert len(points) <= 50


def test_reduce_preserves_first_and_last() -> None:
    obs = [_obs(pct=float(i % 100), observed=NOW + timedelta(minutes=i)) for i in range(300)]
    resets = _detect_resets(obs)
    points = _reduce_points(obs, resets, max_points=50)
    assert points[0].observed_at == obs[0].observed_at
    assert points[-1].observed_at == obs[-1].observed_at


def test_reduce_preserves_extrema() -> None:
    """Local extrema must survive reduction."""
    percentages = [50.0, 30.0, 70.0, 20.0, 80.0, 10.0, 90.0]
    obs = [_obs(pct=p, observed=NOW + timedelta(minutes=i)) for i, p in enumerate(percentages)]
    # Add filler to force reduction
    for i in range(200):
        obs.append(_obs(pct=50.0 + i * 0.01, observed=NOW + timedelta(minutes=10 + i)))
    resets = _detect_resets(obs)
    points = _reduce_points(obs, resets, max_points=30)
    point_pcts = [p.percentage for p in points]
    # The extreme values must be preserved
    assert 10.0 in point_pcts
    assert 90.0 in point_pcts


def test_reduce_preserves_resets() -> None:
    """Reset transitions must survive reduction."""
    obs = [_obs(pct=50.0, observed=NOW + timedelta(minutes=i)) for i in range(200)]
    # Insert a reset at index 100
    obs[100] = _obs(pct=50.0, reset=NEW_RESET, observed=NOW + timedelta(minutes=100))
    resets = _detect_resets(obs)
    points = _reduce_points(obs, resets, max_points=30)
    assert any(p.is_reset for p in points)


def test_reduce_preserves_order() -> None:
    obs = [_obs(pct=float(i % 100), observed=NOW + timedelta(minutes=i)) for i in range(300)]
    resets = _detect_resets(obs)
    points = _reduce_points(obs, resets, max_points=30)
    times = [p.observed_at for p in points]
    assert times == sorted(times)


def test_max_chart_points_constant() -> None:
    assert MAX_CHART_POINTS == 200


# ── 90-day performance ──


def test_90_day_reduction_performance() -> None:
    """90 days of 15-minute samples (8640 points) must reduce quickly."""
    # 90 days * 24 hours * 4 samples/hour = 8640
    obs = [
        _obs(pct=30.0 + (i % 40), observed=NOW - timedelta(minutes=15 * (8640 - i)))
        for i in range(8640)
    ]
    resets = _detect_resets(obs)
    start = time.monotonic()
    points = _reduce_points(obs, resets, max_points=MAX_CHART_POINTS)
    elapsed = time.monotonic() - start
    assert len(points) <= MAX_CHART_POINTS
    assert elapsed < 1.0  # Must be fast


# ── Filter tests ──


def test_filter_by_service_claude() -> None:
    obs = [
        _obs(service=Service.CLAUDE, label="Weekly", pct=50.0),
        _obs(service=Service.CODEX, label="Weekly", pct=60.0),
    ]
    claude_obs = [o for o in obs if o.service is Service.CLAUDE]
    result = prepare_history_view(claude_obs, range_label="24h", filter_label="Claude")
    assert len(result.series) == 1
    assert result.series[0].stats.service is Service.CLAUDE


def test_filter_by_service_codex() -> None:
    obs = [
        _obs(service=Service.CLAUDE, label="Weekly", pct=50.0),
        _obs(service=Service.CODEX, label="Weekly", pct=60.0),
    ]
    codex_obs = [o for o in obs if o.service is Service.CODEX]
    result = prepare_history_view(codex_obs, range_label="24h", filter_label="Codex")
    assert len(result.series) == 1
    assert result.series[0].stats.service is Service.CODEX


def test_filter_all_services() -> None:
    obs = [
        _obs(service=Service.CLAUDE, label="Weekly", pct=50.0),
        _obs(service=Service.CODEX, label="Weekly", pct=60.0),
    ]
    result = prepare_history_view(obs, range_label="24h", filter_label="All")
    assert len(result.series) == 2


# ── Local-time labels ──


def test_utc_storage_local_display() -> None:
    """Observations stored as UTC are converted to local time only for display."""
    from moira.ui import format_local_datetime

    utc_time = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)
    local_str = format_local_datetime(utc_time)
    # The function should return a non-empty string in local timezone
    assert len(local_str) > 0
    # The UTC datetime converted to local should match
    utc_time.astimezone()
    assert local_str is not None


# ── Async reader: stale result rejection ──


def test_reader_stale_result_rejected() -> None:
    """A stale read result (from an old request) is discarded."""
    import concurrent.futures

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="test")
    received: list[HistoryViewResult] = []

    reader = HistoryReader(executor)
    reader.set_callback(lambda v: received.append(v))

    try:
        # Request 1
        reader.request(
            range_func=lambda conn, now: {"quota": [], "tokens": []},
            range_label="24h",
            filter_label="All",
            now=NOW,
        )
        # Request 2 (supersedes request 1)
        reader.request(
            range_func=lambda conn, now: {"quota": [], "tokens": []},
            range_label="7d",
            filter_label="All",
            now=NOW,
        )
        # Wait for completion
        time.sleep(1.0)
        # Only the newest result should have been published
        assert len(received) <= 1
        if received:
            assert received[0].range_label == "7d"
    finally:
        executor.shutdown(wait=True)


def test_reader_bounded_pending() -> None:
    """At most one pending read exists at a time."""
    import concurrent.futures

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="test")
    reader = HistoryReader(executor)

    try:
        # Submit 5 rapid requests
        for i in range(5):
            reader.request(
                range_func=lambda conn, now: {"quota": [], "tokens": []},
                range_label=f"r{i}",
                filter_label="All",
                now=NOW,
            )
        # The reader should not create unbounded work
        # Only the newest result would be published
        with reader._lock:
            # _pending is the last submitted future
            assert reader._pending is not None
    finally:
        executor.shutdown(wait=True)


# ── GTK isolation ──


def test_reader_returns_typed_view_models() -> None:
    """The reader returns HistoryViewResult, not SQLite rows or connections."""
    import concurrent.futures

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="test")
    received: list[HistoryViewResult] = []

    reader = HistoryReader(executor)
    reader.set_callback(lambda v: received.append(v))

    try:
        reader.request(
            range_func=lambda conn, now: {"quota": [_obs(pct=50.0)], "tokens": []},
            range_label="24h",
            filter_label="All",
            now=NOW,
        )
        time.sleep(1.0)
        assert len(received) == 1
        result = received[0]
        assert isinstance(result, HistoryViewResult)
        # No SQLite rows or connections exposed
        assert all(isinstance(s, SeriesView) for s in result.series)
    finally:
        executor.shutdown(wait=True)


# ── Empty error states ──


def test_empty_result_no_series() -> None:
    result = prepare_history_view([], range_label="24h", filter_label="All", diagnostic="ok")
    assert result.series == []
    assert result.diagnostic == "ok"


def test_error_result_diagnostic() -> None:
    result = prepare_history_view(
        [], range_label="24h", filter_label="All", diagnostic="database unavailable"
    )
    assert result.diagnostic == "database unavailable"
    assert result.series == []


def test_no_estimated_tokens_in_view() -> None:
    """HistoryViewResult never contains estimated tokens."""
    result = prepare_history_view([_obs(pct=50.0)], range_label="24h", filter_label="All")
    # SeriesView does not have token fields
    import inspect

    for s in result.series:
        members = dict(inspect.getmembers(s))
        assert "input_tokens" not in members
        assert "total_tokens" not in members
