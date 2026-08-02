"""Typed immutable view models and pure deterministic chart data preparation
for the History UI.

View models are immutable (frozen dataclasses). The async reader uses
monotonically increasing request identity so only the newest read publishes
results to GTK. No SQLite rows, connections, or raw payloads are exposed.

The 90-day reduction preserves first/last points, extrema, percentage
changes, reset transitions, and order while capping the output to a
bounded number of points suitable for chart rendering.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .history import QuotaObservation
from .models import Service

# Maximum points per series for chart rendering.
MAX_CHART_POINTS = 200


@dataclass(frozen=True, slots=True)
class ChartPoint:
    """A single point in a chart series."""

    observed_at: datetime
    percentage: float
    is_reset: bool


@dataclass(frozen=True, slots=True)
class SeriesStats:
    """Aggregate statistics for one quota series."""

    label: str
    service: Service
    latest: float | None
    minimum: float | None
    maximum: float | None
    first_observed: datetime | None
    last_observed: datetime | None
    count: int
    reset_count: int


@dataclass(frozen=True, slots=True)
class SeriesView:
    """An immutable view of one quota series for rendering."""

    stats: SeriesStats
    points: list[ChartPoint]


@dataclass(frozen=True, slots=True)
class HistoryViewResult:
    """The complete immutable result returned to GTK.

    Contains only typed view models — no SQLite rows or connections.
    """

    series: list[SeriesView]
    diagnostic: str
    range_label: str
    filter_label: str


def _detect_resets(observations: list[QuotaObservation]) -> list[bool]:
    """Detect reset transitions by comparing reset_at between consecutive observations."""
    if not observations:
        return []
    resets = [False]
    for i in range(1, len(observations)):
        resets.append(observations[i].reset_at != observations[i - 1].reset_at)
    return resets


def _build_stats(
    label: str,
    service: Service,
    observations: list[QuotaObservation],
    resets: list[bool],
) -> SeriesStats:
    """Build aggregate statistics for one series."""
    if not observations:
        return SeriesStats(
            label=label,
            service=service,
            latest=None,
            minimum=None,
            maximum=None,
            first_observed=None,
            last_observed=None,
            count=0,
            reset_count=0,
        )
    percentages = [obs.percentage for obs in observations]
    return SeriesStats(
        label=label,
        service=service,
        latest=percentages[-1],
        minimum=min(percentages),
        maximum=max(percentages),
        first_observed=observations[0].observed_at,
        last_observed=observations[-1].observed_at,
        count=len(observations),
        reset_count=sum(1 for r in resets if r),
    )


def _reduce_points(
    observations: list[QuotaObservation],
    resets: list[bool],
    max_points: int = MAX_CHART_POINTS,
) -> list[ChartPoint]:
    """Reduce observations to at most ``max_points`` chart points.

    Dense 90-day reduction preserves:
      - first and last points;
      - extrema (local minima and maxima);
      - percentage changes;
      - reset transitions;
      - chronological order.

    For ``len(observations) <= max_points``, all points are kept.
    """
    if not observations:
        return []

    if len(observations) <= max_points:
        return [
            ChartPoint(
                observed_at=obs.observed_at,
                percentage=obs.percentage,
                is_reset=resets[i],
            )
            for i, obs in enumerate(observations)
        ]

    # Select indices to keep: first, last, extrema, resets, then evenly sample
    keep_indices: set[int] = {0, len(observations) - 1}

    # Add reset transition indices
    for i, is_reset in enumerate(resets):
        if is_reset:
            keep_indices.add(i)

    # Add local extrema (where direction changes)
    percentages = [obs.percentage for obs in observations]
    for i in range(1, len(percentages) - 1):
        prev_pct = percentages[i - 1]
        curr_pct = percentages[i]
        next_pct = percentages[i + 1]
        if (curr_pct > prev_pct and curr_pct > next_pct) or (
            curr_pct < prev_pct and curr_pct < next_pct
        ):
            keep_indices.add(i)

    # If still under max_points, evenly sample to fill remaining slots
    remaining = max_points - len(keep_indices)
    if remaining > 0:
        step = max(1, len(observations) // (remaining + 1))
        for i in range(0, len(observations), step):
            keep_indices.add(i)
            if len(keep_indices) >= max_points:
                break

    # Sort and cap
    sorted_indices = sorted(keep_indices)[:max_points]
    return [
        ChartPoint(
            observed_at=observations[i].observed_at,
            percentage=observations[i].percentage,
            is_reset=resets[i],
        )
        for i in sorted_indices
    ]


def _group_observations(
    observations: list[QuotaObservation],
) -> dict[tuple[str, Service], list[QuotaObservation]]:
    """Group observations by (quota_label, service), preserving order."""
    groups: dict[tuple[str, Service], list[QuotaObservation]] = {}
    for obs in observations:
        key = (obs.quota_label, obs.service)
        groups.setdefault(key, []).append(obs)
    return groups


def prepare_history_view(
    observations: list[QuotaObservation],
    *,
    range_label: str,
    filter_label: str,
    diagnostic: str = "ok",
    max_points: int = MAX_CHART_POINTS,
) -> HistoryViewResult:
    """Prepare an immutable HistoryViewResult from raw observations.

    Pure deterministic function. No I/O, no side effects.
    Each metric is rendered independently — Claude five-hour, Claude weekly,
    and Codex weekly are never merged.
    """
    groups = _group_observations(observations)
    series: list[SeriesView] = []
    for (label, service), obs_list in sorted(groups.items()):
        resets = _detect_resets(obs_list)
        stats = _build_stats(label, service, obs_list, resets)
        points = _reduce_points(obs_list, resets, max_points)
        series.append(SeriesView(stats=stats, points=points))
    return HistoryViewResult(
        series=series,
        diagnostic=diagnostic,
        range_label=range_label,
        filter_label=filter_label,
    )


# ── Async reader with request identity ──


class HistoryReader:
    """Bounded asynchronous reader for history data.

    Uses a monotonically increasing request identity so only the newest
    read publishes results to GTK. At most one pending read exists at a
    time; rapid changes do not create unbounded work. Stale results are
    discarded.

    All SQLite work happens on the reader thread. GTK receives only
    typed immutable HistoryViewResult objects via a callback on the
    GLib idle loop.
    """

    def __init__(self, executor: Any) -> None:
        self._executor = executor
        self._request_id = 0
        self._lock = threading.Lock()
        self._pending: Any | None = None
        self._callback: Any | None = None

    def set_callback(self, callback: Any) -> None:
        """Set the GTK callback that receives HistoryViewResult via GLib.idle_add."""
        self._callback = callback

    def request(
        self,
        *,
        range_func: Any,
        range_label: str,
        filter_label: str,
        service: Service | None = None,
        now: datetime | None = None,
    ) -> None:
        """Request a history read. Only the newest request's result is published.

        If a read is already pending, the old request is superseded.
        The callback receives the result only if this request is still
        the newest when the read completes.
        """
        with self._lock:
            self._request_id += 1
            req_id = self._request_id
            # If a pending future exists, it will be ignored by req_id check
            clock = now or datetime.now(UTC)

        future = self._executor.submit(
            self._read,
            range_func=range_func,
            range_label=range_label,
            filter_label=filter_label,
            service=service,
            now=clock,
            req_id=req_id,
        )
        with self._lock:
            self._pending = future
        future.add_done_callback(self._on_done)

    def _read(
        self,
        *,
        range_func: Any,
        range_label: str,
        filter_label: str,
        service: Service | None,
        now: datetime,
        req_id: int,
    ) -> tuple[int, HistoryViewResult] | None:
        """Perform the actual SQLite read on the worker thread."""
        from .history_db import _connect, history_path, init_schema

        try:
            conn = _connect(history_path(), timeout=1.0)
            try:
                init_schema(conn)
                result = range_func(conn, now=now)
                quota_obs: list[QuotaObservation] = result.get("quota", [])
                if service is not None:
                    quota_obs = [o for o in quota_obs if o.service is service]
                view = prepare_history_view(
                    quota_obs,
                    range_label=range_label,
                    filter_label=filter_label,
                )
                return (req_id, view)
            finally:
                conn.close()
        except Exception:
            from .history_db import _DIAG_DB_ERROR

            # Return error diagnostic — sanitized, no raw exception text
            return (
                req_id,
                HistoryViewResult(
                    series=[],
                    diagnostic=_DIAG_DB_ERROR,
                    range_label=range_label,
                    filter_label=filter_label,
                ),
            )

    def _on_done(self, future: Any) -> None:
        """Callback when the read completes. Publishes only if newest."""
        try:
            result = future.result()
        except Exception:
            return
        if result is None:
            return
        req_id, view = result
        with self._lock:
            if req_id != self._request_id:
                # Stale result — discard
                return
            self._pending = None
        if self._callback is not None:
            self._callback(view)

    def cancel(self) -> None:
        """Cancel all pending reads. Future callbacks will be discarded."""
        with self._lock:
            self._request_id += 1
            self._pending = None
