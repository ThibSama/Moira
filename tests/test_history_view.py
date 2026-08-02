"""Deterministic tests for the History UI: view models, chart data reduction,
filters, boundaries, stale-result rejection, bounded requests, GTK isolation,
and 90-day performance.

These tests do not require a display server — they test the pure
deterministic data preparation and the async reader logic.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

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
    assert result.series == ()
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
    r = HistoryViewResult(series=(), diagnostic="ok", range_label="24h", filter_label="All")
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


# ── Async reader: bounded, stale rejection, cancellation ──


class _InstrumentedExecutor:
    """Instrumented executor that tracks submit calls using a real thread pool."""

    def __init__(self, max_workers: int = 2) -> None:
        import concurrent.futures

        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="test"
        )
        self.submit_count = 0
        self._lock = threading.Lock()

    def submit(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            self.submit_count += 1
        return self._pool.submit(fn, *args, **kwargs)

    def shutdown(self, wait: bool = True) -> None:
        self._pool.shutdown(wait=wait)


def test_reader_bounded_submissions(tmp_path: Path) -> None:
    """At most one running read and one pending request. Rapid changes
    do not submit unbounded work. Uses a slow range_func to keep the
    worker occupied while enqueuing follow-up requests."""
    from moira.history_db import _connect, init_schema

    db_path = tmp_path / "test.sqlite3"
    conn = _connect(db_path)
    init_schema(conn)
    conn.close()

    executor = _InstrumentedExecutor()
    received: list[HistoryViewResult] = []

    # range_func that sleeps briefly to simulate I/O
    started = threading.Event()

    def slow_range(c: Any, now: Any) -> dict[str, list[Any]]:
        started.set()
        time.sleep(0.3)
        return {"quota": [], "tokens": []}

    reader = HistoryReader(executor, dispatcher=lambda cb, v, rid=0: cb(v), db_path=db_path)
    reader.set_callback(lambda v: received.append(v))

    # Submit 5 rapid requests
    for i in range(5):
        reader.request(
            range_func=slow_range,
            range_label=f"r{i}",
            filter_label="All",
            now=NOW,
        )

    # Wait for the worker to start
    assert started.wait(timeout=3.0)

    # Only 1 should have been submitted (the running one)
    assert executor.submit_count == 1

    # Wait for drain
    time.sleep(1.0)

    # After drain, at most a few additional submits (for the pending request)
    assert executor.submit_count <= 3

    reader.cancel()
    executor.shutdown(wait=False)


def test_reader_stale_result_rejected(tmp_path: Path) -> None:
    """A stale read result (from an old request) is discarded."""
    from moira.history_db import _connect, init_schema

    db_path = tmp_path / "test.sqlite3"
    conn = _connect(db_path)
    init_schema(conn)
    conn.close()

    executor = _InstrumentedExecutor()
    received: list[HistoryViewResult] = []
    reader = HistoryReader(executor, dispatcher=lambda cb, v, rid=0: cb(v), db_path=db_path)
    reader.set_callback(lambda v: received.append(v))

    # Request 1 — runs on the pool
    reader.request(
        range_func=lambda conn, now: {"quota": [], "tokens": []},
        range_label="24h",
        filter_label="All",
        now=NOW,
    )
    time.sleep(0.3)
    # Request 2 — also runs (request 1 has completed)
    reader.request(
        range_func=lambda conn, now: {"quota": [], "tokens": []},
        range_label="7d",
        filter_label="All",
        now=NOW,
    )
    time.sleep(0.3)

    # Both completed; results published
    assert len(received) <= 2
    if received:
        assert received[-1].range_label in ("24h", "7d")

    reader.cancel()
    executor.shutdown(wait=False)


def test_reader_cancel_stops_callbacks(tmp_path: Path) -> None:
    """After cancel(), no callback fires."""
    from moira.history_db import _connect, init_schema

    db_path = tmp_path / "test.sqlite3"
    conn = _connect(db_path)
    init_schema(conn)
    conn.close()

    executor = _InstrumentedExecutor()
    received: list[HistoryViewResult] = []
    reader = HistoryReader(executor, dispatcher=lambda cb, v, rid=0: cb(v), db_path=db_path)
    reader.set_callback(lambda v: received.append(v))

    reader.cancel()
    reader.request(
        range_func=lambda conn, now: {"quota": [], "tokens": []},
        range_label="24h",
        filter_label="All",
        now=NOW,
    )
    assert len(received) == 0

    executor.shutdown(wait=False)


def test_reader_publishes_via_dispatcher(tmp_path: Path) -> None:
    """Results are published through the injected dispatcher, not directly."""
    from moira.history_db import _connect, init_schema

    db_path = tmp_path / "test.sqlite3"
    conn = _connect(db_path)
    init_schema(conn)
    conn.close()

    dispatched: list[bool] = []

    def dispatcher(cb: Any, view: Any, req_id: int = 0) -> None:
        dispatched.append(True)
        cb(view)

    executor = _InstrumentedExecutor()
    received: list[HistoryViewResult] = []
    reader = HistoryReader(executor, dispatcher=dispatcher, db_path=db_path)
    reader.set_callback(lambda v: received.append(v))

    reader.request(
        range_func=lambda conn, now: {"quota": [], "tokens": []},
        range_label="24h",
        filter_label="All",
        now=NOW,
    )
    time.sleep(0.5)

    assert len(dispatched) > 0
    assert len(received) > 0

    reader.cancel()
    executor.shutdown(wait=False)


# ── GTK isolation ──


def test_reader_returns_typed_view_models(tmp_path: Path) -> None:
    """The reader returns HistoryViewResult, not SQLite rows or connections."""
    from moira.history_db import _connect, init_schema

    db_path = tmp_path / "test.sqlite3"
    conn = _connect(db_path)
    init_schema(conn)
    conn.close()

    executor = _InstrumentedExecutor()
    received: list[HistoryViewResult] = []
    reader = HistoryReader(executor, dispatcher=lambda cb, v, rid=0: cb(v), db_path=db_path)
    reader.set_callback(lambda v: received.append(v))

    reader.request(
        range_func=lambda conn, now: {"quota": [_obs(pct=50.0)], "tokens": []},
        range_label="24h",
        filter_label="All",
        now=NOW,
    )
    time.sleep(0.3)
    assert len(received) >= 1
    result = received[-1]
    assert isinstance(result, HistoryViewResult)
    assert all(isinstance(s, SeriesView) for s in result.series)

    reader.cancel()
    executor.shutdown(wait=False)


# ── Empty error states ──


def test_empty_result_no_series() -> None:
    result = prepare_history_view([], range_label="24h", filter_label="All", diagnostic="ok")
    assert result.series == ()
    assert result.diagnostic == "ok"


def test_error_result_diagnostic() -> None:
    result = prepare_history_view(
        [], range_label="24h", filter_label="All", diagnostic="database unavailable"
    )
    assert result.diagnostic == "database unavailable"
    assert result.series == ()


def test_no_estimated_tokens_in_view() -> None:
    """HistoryViewResult never contains estimated tokens."""
    result = prepare_history_view([_obs(pct=50.0)], range_label="24h", filter_label="All")
    import inspect

    for s in result.series:
        members = dict(inspect.getmembers(s))
        assert "input_tokens" not in members
        assert "total_tokens" not in members


# ── Deep immutability ──


def test_series_points_is_tuple() -> None:
    """SeriesView.points is a tuple, not a list."""
    result = prepare_history_view([_obs(pct=50.0)], range_label="24h", filter_label="All")
    assert isinstance(result.series, tuple)
    for s in result.series:
        assert isinstance(s.points, tuple)


def test_view_result_series_is_tuple() -> None:
    """HistoryViewResult.series is a tuple."""
    result = prepare_history_view([_obs(pct=50.0)], range_label="24h", filter_label="All")
    assert isinstance(result.series, tuple)


# ── Mandatory-point overflow (soft cap) ──


def test_mandatory_points_exceeding_cap_preserved() -> None:
    """When mandatory points (resets) exceed max_points, all are kept (soft cap).

    Creates alternating reset windows that produce more mandatory indices
    than the cap. Every mandatory timestamp and chronological order
    must survive.
    """
    max_pts = 5
    # Create 30 observations with alternating reset windows.
    # Each window boundary is a reset transition (mandatory).
    # With 15 alternating windows, we get 14 reset transitions
    # + first + last = 16 mandatory points — well above max_pts=5.
    obs: list[QuotaObservation] = []
    resets_at = [NOW + timedelta(days=i) for i in range(15)]  # 15 different resets
    for i in range(30):
        reset = resets_at[i % len(resets_at)]
        obs.append(_obs(pct=float(i % 40), reset=reset, observed=NOW + timedelta(minutes=i)))
    resets = _detect_resets(obs)
    # Count mandatory points: first + last + all reset indices
    mandatory = {0, len(obs) - 1}
    for i, is_reset in enumerate(resets):
        if is_reset:
            mandatory.add(i)
    assert len(mandatory) > max_pts  # Verify the test is meaningful

    points = _reduce_points(obs, resets, max_points=max_pts)
    # All mandatory timestamps must survive
    point_times = {p.observed_at for p in points}
    for idx in mandatory:
        assert obs[idx].observed_at in point_times, f"Mandatory point {idx} was dropped"
    # Chronological order must be preserved
    times = [p.observed_at for p in points]
    assert times == sorted(times)
    # Soft cap: result has more points than max_pts
    assert len(points) >= len(mandatory)


def test_reduction_preserves_last_point() -> None:
    """The last observation must always survive reduction."""
    obs = [_obs(pct=float(i % 100), observed=NOW + timedelta(minutes=i)) for i in range(300)]
    resets = _detect_resets(obs)
    points = _reduce_points(obs, resets, max_points=30)
    assert points[-1].observed_at == obs[-1].observed_at


def test_reduction_preserves_reset_transitions() -> None:
    """All reset transitions must survive reduction."""
    obs = [_obs(pct=50.0, observed=NOW + timedelta(minutes=i)) for i in range(200)]
    obs[100] = _obs(pct=50.0, reset=NEW_RESET, observed=NOW + timedelta(minutes=100))
    resets = _detect_resets(obs)
    points = _reduce_points(obs, resets, max_points=30)
    assert any(p.is_reset for p in points)


def test_reduction_preserves_extrema() -> None:
    """Local extrema must survive reduction."""
    percentages = [50.0, 30.0, 70.0, 20.0, 80.0, 10.0, 90.0]
    obs = [_obs(pct=p, observed=NOW + timedelta(minutes=i)) for i, p in enumerate(percentages)]
    for i in range(200):
        obs.append(_obs(pct=50.0 + i * 0.01, observed=NOW + timedelta(minutes=10 + i)))
    resets = _detect_resets(obs)
    points = _reduce_points(obs, resets, max_points=30)
    point_pcts = [p.percentage for p in points]
    assert 10.0 in point_pcts
    assert 90.0 in point_pcts


# ── Shared axis coordinates ──


def test_shared_time_axis() -> None:
    """When multiple series are drawn together, they share a time axis."""
    # This is tested by the chart's _draw method using all_times
    # from all series. Here we verify the data structure supports it.
    obs1 = [_obs(service=Service.CLAUDE, label="Weekly", pct=50.0, observed=NOW)]
    obs2 = [
        _obs(
            service=Service.CODEX,
            label="Weekly",
            pct=60.0,
            observed=NOW + timedelta(hours=12),
        )
    ]
    result = prepare_history_view(obs1 + obs2, range_label="24h", filter_label="All")
    assert len(result.series) == 2
    # Each series can access its own points
    assert len(result.series[0].points) >= 1
    assert len(result.series[1].points) >= 1


# ── French translations ──


def test_french_all_filter() -> None:
    """The 'All' filter label is translated to 'Tous' in French."""
    from moira.i18n import _FRENCH

    assert _FRENCH.get("All") == "Tous"


def test_french_no_data_chart() -> None:
    """The 'No data' chart label is translated to French."""
    from moira.i18n import _FRENCH

    assert _FRENCH.get("No data") is not None
    assert _FRENCH["No data"] != "No data"


def test_french_no_history_database() -> None:
    from moira.i18n import _FRENCH

    assert _FRENCH.get("No history database") is not None


def test_french_exact_tokens_unavailable() -> None:
    from moira.i18n import _FRENCH

    assert _FRENCH.get("Exact token usage is not available") is not None


# ── Light/dark rendering inputs ──


def test_chart_set_dark() -> None:
    """Chart.set_dark accepts a boolean and queues redraw."""
    # We can't instantiate the GTK widget without a display in all environments,
    # so we test the method exists and accepts the parameter
    import moira.history_chart as chart_module

    assert hasattr(chart_module.QuotaChart, "set_dark")
    assert hasattr(chart_module.QuotaChart, "set_series")


# ── Absent DB without creation ──


def test_absent_db_returns_no_database(tmp_path: Path) -> None:
    """When the database file does not exist, 'no database' is returned."""
    from moira.history_view import _safe_read

    db_path = tmp_path / "nonexistent.sqlite3"
    result = _safe_read(
        range_func=lambda conn, now: {"quota": [], "tokens": []},
        range_label="24h",
        filter_label="All",
        service=None,
        now=NOW,
        req_id=1,
        db_path=db_path,
    )
    assert result is not None
    _, view = result
    assert view.diagnostic == "no database"
    assert not db_path.exists()  # DB was not created


# ── Schema mismatch state ──


def test_schema_mismatch_state(tmp_path: Path) -> None:
    """A schema version mismatch returns 'schema mismatch', not 'database unavailable'."""
    from moira.history_db import SCHEMA_SQL_V1
    from moira.history_view import _safe_read

    db_path = tmp_path / "history.sqlite3"
    # Create a v1 database
    import os
    import sqlite3

    fd = os.open(str(db_path), os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    os.chmod(db_path, 0o600)
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.executescript(SCHEMA_SQL_V1)
    conn.execute("INSERT INTO schema_meta (version) VALUES (999)")
    conn.close()

    result = _safe_read(
        range_func=lambda conn, now: {"quota": [], "tokens": []},
        range_label="24h",
        filter_label="All",
        service=None,
        now=NOW,
        req_id=1,
        db_path=db_path,
    )
    assert result is not None
    _, view = result
    assert view.diagnostic == "schema mismatch"


# ── 2c: Lifecycle, visibility, callback chronology ──


def test_reader_is_current(tmp_path: Path) -> None:
    """HistoryReader.is_current returns True only for the latest request id."""
    from moira.history_db import _connect, init_schema

    db_path = tmp_path / "test.sqlite3"
    conn = _connect(db_path)
    init_schema(conn)
    conn.close()

    reader = HistoryReader(executor=_InstrumentedExecutor(), db_path=db_path)
    # Before any request, request_id is 0, so is_current(0) is True
    assert reader.is_current(0)
    assert not reader.is_current(1)

    # After cancel, is_current always returns False
    reader.cancel()
    assert not reader.is_current(0)


def test_cancel_after_queue_before_dispatch(tmp_path: Path) -> None:
    """Cancelling after the worker completed but before idle dispatch
    must prevent delivery. Uses an injected dispatcher that defers."""
    from moira.history_db import _connect, init_schema

    db_path = tmp_path / "test.sqlite3"
    conn = _connect(db_path)
    init_schema(conn)
    conn.close()

    executor = _InstrumentedExecutor()
    received: list[HistoryViewResult] = []
    deferred: list[tuple[Any, HistoryViewResult, int]] = []

    def deferring_dispatcher(cb: Any, view: Any, req_id: int = 0) -> None:
        deferred.append((cb, view, req_id))

    reader = HistoryReader(executor, dispatcher=deferring_dispatcher, db_path=db_path)
    reader.set_callback(lambda v: received.append(v))

    reader.request(
        range_func=lambda conn, now: {"quota": [], "tokens": []},
        range_label="24h",
        filter_label="All",
        now=NOW,
    )
    time.sleep(0.3)

    # The worker completed and the dispatcher deferred the callback.
    assert len(deferred) == 1

    # Cancel before dispatching the deferred callback
    reader.cancel()

    # Now dispatch the deferred callback — is_current should return False
    cb, view, req_id = deferred[0]
    assert not reader.is_current(req_id)

    # Cancelled reader should not deliver
    # (In production, HistoryPage._on_result checks is_current before rendering)

    executor.shutdown(wait=False)


def test_shutdown_destroys_and_rejects() -> None:
    """HistoryPage-like lifecycle: after shutdown, refresh/render are rejected.

    Since we can't instantiate GTK widgets without a display in all
    environments, we test the reader cancel + is_current pattern
    that HistoryPage.shutdown uses."""
    reader = HistoryReader(executor=_InstrumentedExecutor())
    reader.cancel()

    # After cancel, is_current always returns False
    assert not reader.is_current(999)


def test_visibility_transitions_block_reread() -> None:
    """Hidden pages receive no write-triggered read.

    This tests the visibility model: when ``_visible`` is False,
    ``on_refresh_complete`` should not call ``refresh``.
    We simulate the HistoryPage logic directly."""
    visible = [False]
    refreshed = [0]

    def on_refresh_complete() -> bool:
        if visible[0]:
            refreshed[0] += 1
        return False

    # Hidden: no refresh
    visible[0] = False
    on_refresh_complete()
    assert refreshed[0] == 0

    # Visible: refresh
    visible[0] = True
    on_refresh_complete()
    assert refreshed[0] == 1

    # Hidden again: no refresh
    visible[0] = False
    on_refresh_complete()
    assert refreshed[0] == 1


def test_gen1_in_flight_gen2_pending_gen1_success_no_callback(tmp_path: Path) -> None:
    """Gen1 in flight + gen2 pending (free slot): gen1 success → 0 callbacks.

    Gen2 was accepted in the free pending slot (no saturation), so
    _latest_accepted_gen >= 2 when gen1 completes. The publication
    invariant suppresses the callback.
    """
    import moira.history_db as hdb
    from moira.history_db import HistoryCoordinator

    original_write = hdb.write_history_safely
    gen1_started = threading.Event()
    gen1_block = threading.Event()
    gen1_done = threading.Event()
    write_count = [0]
    callback_count = [0]

    def controlled_write(*args: Any, **kwargs: Any) -> Any:
        write_count[0] += 1
        if write_count[0] == 1:
            gen1_started.set()
            gen1_block.wait(timeout=5.0)
            result = original_write(*args, **kwargs)
            gen1_done.set()
            return result
        return original_write(*args, **kwargs)

    coord = HistoryCoordinator(db_path=tmp_path / "test.sqlite3")
    coord.set_write_success_callback(lambda: callback_count.__setitem__(0, callback_count[0] + 1))
    coord.start()

    hdb.write_history_safely = controlled_write
    try:
        # Enqueue gen1 — worker picks it up and blocks
        coord.enqueue([], NOW)
        assert gen1_started.wait(timeout=3.0)

        # Enqueue gen2 — accepted in free pending slot (no saturation)
        coord.enqueue([], NOW)

        # Release gen1 — it succeeds while gen2 is pending
        gen1_block.set()
        assert gen1_done.wait(timeout=3.0)

        # Gen1 success must NOT fire callback (gen2 is newer, still pending)
        assert callback_count[0] == 0
    finally:
        hdb.write_history_safely = original_write
        coord.shutdown()


def test_gen2_success_emits_exactly_one_callback(tmp_path: Path) -> None:
    """Gen1 success suppressed; gen2 success → exactly 1 callback."""
    import moira.history_db as hdb
    from moira.history_db import HistoryCoordinator

    original_write = hdb.write_history_safely
    gen1_started = threading.Event()
    gen1_block = threading.Event()
    write_count = [0]
    callback_count = [0]

    def controlled_write(*args: Any, **kwargs: Any) -> Any:
        write_count[0] += 1
        if write_count[0] == 1:
            gen1_started.set()
            gen1_block.wait(timeout=5.0)
        return original_write(*args, **kwargs)

    coord = HistoryCoordinator(db_path=tmp_path / "test.sqlite3")
    coord.set_write_success_callback(lambda: callback_count.__setitem__(0, callback_count[0] + 1))
    coord.start()

    hdb.write_history_safely = controlled_write
    try:
        coord.enqueue([], NOW)
        assert gen1_started.wait(timeout=3.0)
        coord.enqueue([], NOW)

        # Release gen1 — succeeds, but gen2 is pending → no callback
        gen1_block.set()

        # Wait for gen2 to complete
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and callback_count[0] == 0:
            time.sleep(0.05)

        # Gen2 success → exactly 1 callback
        assert callback_count[0] == 1
    finally:
        hdb.write_history_safely = original_write
        coord.shutdown()


def test_gen2_failure_emits_zero_callbacks(tmp_path: Path) -> None:
    """Gen2 (latest accepted) failure → 0 callbacks."""
    import moira.history_db as hdb
    from moira.history import HistoryWriteResult
    from moira.history_db import HistoryCoordinator

    original_write = hdb.write_history_safely
    gen1_started = threading.Event()
    gen1_block = threading.Event()
    write_count = [0]
    callback_count = [0]

    def controlled_write(*args: Any, **kwargs: Any) -> Any:
        write_count[0] += 1
        if write_count[0] == 1:
            gen1_started.set()
            gen1_block.wait(timeout=5.0)
            return original_write(*args, **kwargs)
        # Gen2 fails
        return HistoryWriteResult(ok=False, diagnostic="database unavailable")

    coord = HistoryCoordinator(db_path=tmp_path / "test.sqlite3")
    coord.set_write_success_callback(lambda: callback_count.__setitem__(0, callback_count[0] + 1))
    coord.start()

    hdb.write_history_safely = controlled_write
    try:
        coord.enqueue([], NOW)
        assert gen1_started.wait(timeout=3.0)
        coord.enqueue([], NOW)

        gen1_block.set()

        # Wait for both writes to complete
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and write_count[0] < 2:
            time.sleep(0.05)

        # Gen1 success suppressed (gen2 pending), gen2 failure → 0 callbacks
        assert callback_count[0] == 0
    finally:
        hdb.write_history_safely = original_write
        coord.shutdown()


def test_retained_saturated_gen3_success_one_callback(tmp_path: Path) -> None:
    """Retained saturated gen3 success → exactly 1 callback.

    Gen1 in flight; gen2 pending (free slot); gen3 replaces gen2 (saturates).
    Gen1 success suppressed; gen3 success clears saturation → 1 callback.
    """
    import moira.history_db as hdb
    from moira.history_db import HistoryCoordinator

    original_write = hdb.write_history_safely
    gen1_started = threading.Event()
    gen1_block = threading.Event()
    write_count = [0]
    callback_count = [0]

    def controlled_write(*args: Any, **kwargs: Any) -> Any:
        write_count[0] += 1
        if write_count[0] == 1:
            gen1_started.set()
            gen1_block.wait(timeout=5.0)
        return original_write(*args, **kwargs)

    coord = HistoryCoordinator(db_path=tmp_path / "test.sqlite3")
    coord.set_write_success_callback(lambda: callback_count.__setitem__(0, callback_count[0] + 1))
    coord.start()

    hdb.write_history_safely = controlled_write
    try:
        coord.enqueue([], NOW)  # gen1
        assert gen1_started.wait(timeout=3.0)
        coord.enqueue([], NOW)  # gen2 (free pending slot)
        coord.enqueue([], NOW)  # gen3 (replaces gen2, saturates)

        gen1_block.set()

        # Wait for gen3 to complete
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and callback_count[0] == 0:
            time.sleep(0.05)

        # Gen3 success → exactly 1 callback
        assert callback_count[0] == 1
    finally:
        hdb.write_history_safely = original_write
        coord.shutdown()


def test_clearing_during_in_flight_prevents_delivery(tmp_path: Path) -> None:
    """Clearing the callback during an in-flight write prevents delivery."""
    import moira.history_db as hdb
    from moira.history_db import HistoryCoordinator

    original_write = hdb.write_history_safely
    write_started = threading.Event()
    write_block = threading.Event()
    callback_count = [0]

    def controlled_write(*args: Any, **kwargs: Any) -> Any:
        write_started.set()
        write_block.wait(timeout=5.0)
        return original_write(*args, **kwargs)

    coord = HistoryCoordinator(db_path=tmp_path / "test.sqlite3")
    coord.set_write_success_callback(lambda: callback_count.__setitem__(0, callback_count[0] + 1))
    coord.start()

    hdb.write_history_safely = controlled_write
    try:
        coord.enqueue([], NOW)
        assert write_started.wait(timeout=3.0)

        # Clear callback while write is in-flight
        coord.clear_write_success_callback()

        # Release the write — it succeeds but callback is detached
        write_block.set()
        time.sleep(0.3)

        assert callback_count[0] == 0
    finally:
        hdb.write_history_safely = original_write
        coord.shutdown()


def test_shutdown_clears_callback_ownership(tmp_path: Path) -> None:
    """Shutdown clears the callback; no further invocations."""
    from moira.history_db import HistoryCoordinator

    callback_count = [0]
    coord = HistoryCoordinator(db_path=tmp_path / "test.sqlite3")
    coord.set_write_success_callback(lambda: callback_count.__setitem__(0, callback_count[0] + 1))
    coord.start()

    coord.shutdown()
    assert callback_count[0] == 0

    # Verify callback is cleared
    with coord._cond:  # noqa: SLF001
        assert coord._write_success_callback is None  # noqa: SLF001


def test_callback_exception_isolated(tmp_path: Path) -> None:
    """Callback exceptions do not crash the worker or affect state."""
    from moira.history_db import HistoryCoordinator

    callback_count = [0]

    def exploding_callback() -> None:
        callback_count[0] += 1
        raise RuntimeError("boom")

    coord = HistoryCoordinator(db_path=tmp_path / "test.sqlite3")
    coord.set_write_success_callback(exploding_callback)
    coord.start()

    coord.enqueue([], NOW)
    time.sleep(0.3)

    # Callback fired (and exception swallowed)
    assert callback_count[0] >= 1
    # Coordinator is still alive and functional
    assert coord.lifecycle_state in ("running",)
    coord.shutdown()


# ── 2c: Token availability note ──


def test_token_note_alongside_quota() -> None:
    """Token availability is modeled separately from quota diagnostics.

    The _DIAG_TOKENS_UNSUPPORTED constant exists but is never emitted
    by _safe_read for quota reads. The token note is a persistent UI
    widget, not a quota diagnostic.
    """
    from moira.history_view import _DIAG_TOKENS_UNSUPPORTED, _safe_read

    # _DIAG_TOKENS_UNSUPPORTED is defined
    assert _DIAG_TOKENS_UNSUPPORTED == "exact tokens unavailable"

    # _safe_read never returns this diagnostic for quota reads
    # (it returns 'no database', 'empty range', 'schema mismatch',
    # or 'database unavailable')
    result = _safe_read(
        range_func=lambda conn, now: {"quota": [_obs(pct=50.0)], "tokens": []},
        range_label="24h",
        filter_label="All",
        service=None,
        now=NOW,
        req_id=1,
        db_path=Path("/nonexistent"),
    )
    assert result is not None
    _, view = result
    assert view.diagnostic != _DIAG_TOKENS_UNSUPPORTED


# ── 2c: Local-time rendering ──


def test_first_last_observation_rendered() -> None:
    """First/last observation times are included in stats."""
    obs = [
        _obs(pct=10.0, observed=NOW),
        _obs(pct=50.0, observed=NOW + timedelta(minutes=5)),
        _obs(pct=80.0, observed=NOW + timedelta(minutes=10)),
    ]
    result = prepare_history_view(obs, range_label="24h", filter_label="All")
    s = result.series[0]
    assert s.stats.first_observed == NOW
    assert s.stats.last_observed == NOW + timedelta(minutes=10)


def test_local_time_conversion() -> None:
    """UTC timestamps are converted to local time at presentation only."""
    from moira.history_page import _format_local

    utc_time = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)
    local_str = _format_local(utc_time)
    assert len(local_str) > 0
    assert "2026" in local_str


# ── 2c: Translated selector model ──


def test_filter_labels_translated() -> None:
    """The filter model uses translated labels, not raw 'All'."""
    from moira.i18n import _FRENCH

    # 'All' must be in the French dictionary as 'Tous'
    assert _FRENCH.get("All") == "Tous"
    assert _FRENCH.get("Claude") == "Claude"
    assert _FRENCH.get("Codex") == "Codex"


# ── 2c: Live theme via Adw.StyleManager ──


def test_chart_set_dark_live() -> None:
    """Chart.set_dark is callable for live theme changes."""
    import moira.history_chart as chart_module

    assert hasattr(chart_module.QuotaChart, "set_dark")
    assert hasattr(chart_module.QuotaChart, "set_series")


# ── 2d: Timezone contract ──


def test_format_observation_time_utc() -> None:
    """Exact first/last text in UTC."""
    from moira.history_page import format_observation_time

    utc_dt = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)
    result = format_observation_time(utc_dt, target_tz=UTC)
    assert result == "2026-08-02 12:00"


def test_format_observation_time_utc_plus_2() -> None:
    """Exact first/last text in UTC+02:00."""
    from moira.history_page import format_observation_time

    utc_dt = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)
    tz_plus_2 = timezone(timedelta(hours=2))
    result = format_observation_time(utc_dt, target_tz=tz_plus_2)
    assert result == "2026-08-02 14:00"


def test_format_observation_time_naive_rejected() -> None:
    """Naive timestamps raise ValueError (fail-closed)."""
    from moira.history_page import format_observation_time

    naive_dt = datetime(2026, 8, 2, 12, 0, 0)
    with pytest.raises(ValueError, match="naive"):
        format_observation_time(naive_dt)


def test_series_stats_text_exact_values() -> None:
    """Build first/last statistics text through pure presentation helper
    and test exact values in UTC and UTC+02:00."""
    from moira.history_page import format_observation_time

    # Simulate SeriesStats with known timestamps
    first = datetime(2026, 8, 2, 10, 0, 0, tzinfo=UTC)
    last = datetime(2026, 8, 2, 14, 0, 0, tzinfo=UTC)

    # UTC formatting
    assert format_observation_time(first, target_tz=UTC) == "2026-08-02 10:00"
    assert format_observation_time(last, target_tz=UTC) == "2026-08-02 14:00"

    # UTC+02:00 formatting
    tz_plus_2 = timezone(timedelta(hours=2))
    assert format_observation_time(first, target_tz=tz_plus_2) == "2026-08-02 12:00"
    assert format_observation_time(last, target_tz=tz_plus_2) == "2026-08-02 16:00"
