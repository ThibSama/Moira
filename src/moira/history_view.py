"""Typed immutable view models and pure deterministic chart data preparation
for the History UI.

View models are deeply immutable (frozen dataclasses with tuple fields).
The async reader uses a genuinely bounded policy: at most one running read
and one pending newest request. Stale generations never publish. No SQLite
rows, connections, or raw payloads are exposed.

Reduction policy: first/last and all reset transitions are always kept.
Local extrema are kept next. If mandatory points exceed the soft cap,
the cap expands to accommodate all mandatory points (soft cap). Remaining
slots are evenly sampled. Chronological order is always preserved.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .history import QuotaObservation, SchemaVersionError
from .models import Service

# Soft cap for chart points. Mandatory points (first/last/resets/extrema)
# may exceed this; the cap is expanded to accommodate them.
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
    points: tuple[ChartPoint, ...]


@dataclass(frozen=True, slots=True)
class HistoryViewResult:
    """The complete immutable result returned to GTK.

    Contains only typed view models — no SQLite rows or connections.
    Series and points are tuples (deeply immutable).
    """

    series: tuple[SeriesView, ...]
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


def _find_mandatory_indices(
    observations: list[QuotaObservation],
    resets: list[bool],
) -> set[int]:
    """Find all mandatory indices that must be preserved.

    Mandatory: first, last, and all reset transitions.
    """
    keep: set[int] = {0, len(observations) - 1}
    for i, is_reset in enumerate(resets):
        if is_reset:
            keep.add(i)
    return keep


def _find_extrema_indices(
    percentages: list[float],
) -> set[int]:
    """Find local extrema (direction changes)."""
    extrema: set[int] = set()
    for i in range(1, len(percentages) - 1):
        prev_pct = percentages[i - 1]
        curr_pct = percentages[i]
        next_pct = percentages[i + 1]
        if (curr_pct > prev_pct and curr_pct > next_pct) or (
            curr_pct < prev_pct and curr_pct < next_pct
        ):
            extrema.add(i)
    return extrema


def _reduce_points(
    observations: list[QuotaObservation],
    resets: list[bool],
    max_points: int = MAX_CHART_POINTS,
) -> tuple[ChartPoint, ...]:
    """Reduce observations to a bounded number of chart points.

    Priority policy (deterministic):
      1. First and last points are always kept.
      2. All reset transitions are always kept.
      3. Local extrema are kept next.
      4. Remaining slots are evenly sampled.

    If mandatory points (first/last/resets) exceed ``max_points``, the
    cap is expanded to accommodate all mandatory points (soft cap).
    Extrema are added only if slots remain after mandatory points.
    Even sampling fills the rest up to ``max_points``.

    Does not claim to preserve every percentage change. Chronological
    order is always preserved.
    """
    if not observations:
        return ()

    if len(observations) <= max_points:
        return tuple(
            ChartPoint(
                observed_at=obs.observed_at,
                percentage=obs.percentage,
                is_reset=resets[i],
            )
            for i, obs in enumerate(observations)
        )

    # Step 1: mandatory indices (first, last, resets)
    keep = _find_mandatory_indices(observations, resets)

    # Step 2: add extrema if slots remain
    available = max_points - len(keep)
    if available > 0:
        percentages = [obs.percentage for obs in observations]
        extrema = _find_extrema_indices(percentages)
        # Add extrema until we reach the cap
        for idx in sorted(extrema):
            if len(keep) >= max_points:
                break
            keep.add(idx)

    # Step 3: evenly sample remaining slots
    available = max_points - len(keep)
    if available > 0:
        step = max(1, len(observations) // (available + 1))
        for i in range(0, len(observations), step):
            keep.add(i)
            if len(keep) >= max_points:
                break

    # Sort and build result — soft cap may exceed max_points for mandatory
    sorted_indices = sorted(keep)
    return tuple(
        ChartPoint(
            observed_at=observations[i].observed_at,
            percentage=observations[i].percentage,
            is_reset=resets[i],
        )
        for i in sorted_indices
    )


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
        series=tuple(series),
        diagnostic=diagnostic,
        range_label=range_label,
        filter_label=filter_label,
    )


# ── Sanitized diagnostic constants ──

_DIAG_OK = "ok"
_DIAG_NO_DATABASE = "no database"
_DIAG_EMPTY = "empty range"
_DIAG_DB_ERROR = "database unavailable"
_DIAG_SCHEMA_ERROR = "schema mismatch"
_DIAG_LOADING = "loading"
_DIAG_TOKENS_UNSUPPORTED = "exact tokens unavailable"


def _safe_read(
    *,
    range_func: Any,
    range_label: str,
    filter_label: str,
    service: Service | None,
    now: datetime,
    req_id: int,
    db_path: Any,
) -> tuple[int, HistoryViewResult] | None:
    """Perform the SQLite read on the worker thread with proper error handling."""
    from pathlib import Path

    from .history_db import _connect, init_schema

    path = Path(db_path) if not isinstance(db_path, Path) else db_path

    # Detect absence without creating the database
    if not path.exists():
        return (
            req_id,
            HistoryViewResult(
                series=(),
                diagnostic=_DIAG_NO_DATABASE,
                range_label=range_label,
                filter_label=filter_label,
            ),
        )

    try:
        conn = _connect(path, timeout=1.0)
        try:
            init_schema(conn)
            result = range_func(conn, now=now)
            quota_obs: list[QuotaObservation] = result.get("quota", [])
            if service is not None:
                quota_obs = [o for o in quota_obs if o.service is service]
            if not quota_obs:
                return (
                    req_id,
                    HistoryViewResult(
                        series=(),
                        diagnostic=_DIAG_EMPTY,
                        range_label=range_label,
                        filter_label=filter_label,
                    ),
                )
            view = prepare_history_view(
                quota_obs,
                range_label=range_label,
                filter_label=filter_label,
            )
            return (req_id, view)
        finally:
            conn.close()
    except SchemaVersionError:
        return (
            req_id,
            HistoryViewResult(
                series=(),
                diagnostic=_DIAG_SCHEMA_ERROR,
                range_label=range_label,
                filter_label=filter_label,
            ),
        )
    except Exception:
        return (
            req_id,
            HistoryViewResult(
                series=(),
                diagnostic=_DIAG_DB_ERROR,
                range_label=range_label,
                filter_label=filter_label,
            ),
        )


class HistoryReader:
    """Genuinely bounded asynchronous reader for history data.

    At most one running read and one pending newest request. Rapid
    changes replace only the pending request — never submit unbounded
    work. Stale generations never publish.

    All SQLite work happens on the reader thread. GTK receives only
    typed immutable HistoryViewResult objects via an injected dispatcher
    (defaults to GLib.idle_add). No callback may update a destroyed page.
    """

    def __init__(
        self,
        executor: Any,
        *,
        dispatcher: Callable[..., None] | None = None,
        db_path: Any = None,
    ) -> None:
        self._executor = executor
        self._dispatcher = dispatcher
        self._db_path = db_path
        self._request_id = 0
        self._lock = threading.RLock()
        self._running: Any | None = None
        self._pending_request: dict[str, Any] | None = None
        self._callback: Callable[[HistoryViewResult], None] | None = None
        self._cancelled = False

    def set_callback(self, callback: Callable[[HistoryViewResult], None]) -> None:
        """Set the callback that receives HistoryViewResult via the dispatcher."""
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

        If a read is already running and a pending request exists, the
        pending request is replaced (newest-wins). If no pending request
        exists, it is stored. At most one running read and one pending
        request exist at any time. Never submits unbounded work.
        """
        with self._lock:
            if self._cancelled:
                return
            self._request_id += 1
            req_id = self._request_id
            clock = now or datetime.now(UTC)

            request_params = {
                "range_func": range_func,
                "range_label": range_label,
                "filter_label": filter_label,
                "service": service,
                "now": clock,
                "req_id": req_id,
            }

            if self._running is not None:
                # A read is running — store/replace the pending request
                self._pending_request = request_params
                return

            # No running read — submit immediately
            self._running = req_id

        future = self._executor.submit(self._read, **request_params)
        future.add_done_callback(self._on_done)

    def _maybe_submit_pending(self) -> None:
        """Submit the pending request if one exists and no read is running."""
        with self._lock:
            if self._cancelled or self._pending_request is None:
                return
            params = self._pending_request
            self._pending_request = None
            self._running = params["req_id"]

        future = self._executor.submit(self._read, **params)
        future.add_done_callback(self._on_done)

    def _read(self, **kwargs: Any) -> tuple[int, HistoryViewResult] | None:
        """Perform the actual SQLite read on the worker thread."""
        db_path = self._db_path
        if db_path is None:
            from .history_db import history_path

            db_path = history_path()
        return _safe_read(db_path=db_path, **kwargs)

    def _on_done(self, future: Any) -> None:
        """Callback when the read completes. Publishes only if newest.

        The request id is passed to the dispatcher so the GTK thread
        can recheck at idle-dispatch time — cancelling after the worker
        completed but before idle dispatch must still prevent delivery.
        """
        try:
            result = future.result()
        except Exception:
            result = None

        with self._lock:
            self._running = None
            if result is None:
                self._maybe_submit_pending()
                return
            req_id, view = result
            if self._cancelled or req_id != self._request_id:
                # Stale or cancelled — discard
                self._maybe_submit_pending()
                return
            # Snapshot req_id for dispatch — recheck at dispatch time
            dispatch_req_id = self._request_id

        if self._callback is not None:
            if self._dispatcher is not None:
                self._dispatcher(self._callback, view, dispatch_req_id)
            else:
                self._callback(view)

        self._maybe_submit_pending()

    def is_current(self, req_id: int) -> bool:
        """Return True if ``req_id`` matches the current request id.

        Used by the GTK callback to recheck at idle-dispatch time.
        """
        with self._lock:
            return not self._cancelled and req_id == self._request_id

    def cancel(self) -> None:
        """Cancel all pending and future reads. No callback will fire."""
        with self._lock:
            self._cancelled = True
            self._request_id += 1
            self._pending_request = None
            self._running = None
