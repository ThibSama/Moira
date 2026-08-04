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

Package 4 additions: frozen ``DailyTokenStats`` view models and pure
GTK-free builders for selected-range daily indicators (total, reported
days, average per reported day, range peak day/count and peak share),
computed only from AVAILABLE_EXACT ``day`` rows with integer/Decimal
arithmetic. Duplicate (service, day) daily inputs fail closed at the
aggregation boundary. The official Codex summary stays separate and is
explicitly labeled account-wide.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from .history import HistoryStatus, QuotaObservation, SchemaVersionError, TokenObservation
from .models import CodexSummary, Service, TokenAvailabilityRecord

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
class TokenSummary:
    """Aggregate token activity for one service/period-kind in the selected range.

    Empty when no exact token data is available. Totals are the sum of all
    exact token events in the range. Earliest/latest use the activity day
    (``period_start`` date part), independent of retrieval provenance.
    The official Codex summary is a separate typed ``CodexSummary`` record
    displayed apart from daily totals.
    """

    service: Service
    source: str
    period_kind: str
    total_tokens: int
    event_count: int
    earliest_day: str | None
    latest_day: str | None

    @property
    def has_data(self) -> bool:
        return self.event_count > 0


@dataclass(frozen=True, slots=True)
class DailyTokenStats:
    """Exact selected-range daily token statistics for one service.

    Computed ONLY from ``AVAILABLE_EXACT`` rows with ``period_kind ==
    'day'``. Migrated ``bucket`` events never contribute here — they stay
    in their existing ``TokenSummary``. Missing days are never filled with
    zero, nothing is extrapolated or annualized, and no value is derived
    from quota percentages.

    Arithmetic is integer/Decimal with documented rounding:
    - ``average_per_reported_day`` is total / reported_days rounded
      half-up to the nearest integer.
    - ``peak_share_percent`` is peak_tokens / total_tokens x 100 rounded
      half-up to one decimal place (None when total is 0).
    - Peak ties resolve to the earliest reported day (stable documented
      rule).

    One exact day is valid. Zero exact days produce no entry — there is
    never a zero card for missing data.
    """

    service: Service
    reported_days: int
    total_tokens: int
    average_per_reported_day: int
    peak_day: str | None
    peak_tokens: int | None
    peak_share_percent: Decimal | None

    @property
    def has_data(self) -> bool:
        return self.reported_days > 0


@dataclass(frozen=True, slots=True)
class TokenAvailabilityState:
    """Typed, provider-neutral availability state for one service.

    Only sanitized ``HistoryStatus`` enum values — never an arbitrary
    status string. Carries the latest observed availability for the
    service in the selected range.
    """

    service: Service
    status: HistoryStatus


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
    token_summaries: tuple[TokenSummary, ...] = ()
    daily_token_stats: tuple[DailyTokenStats, ...] = ()
    token_availability: tuple[TokenAvailabilityState, ...] = ()
    codex_summaries: tuple[CodexSummary, ...] = ()


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


def _reject_duplicate_daily_observations(
    token_observations: list[TokenObservation],
) -> None:
    """Reject duplicate (service, day) exact daily inputs at the boundary.

    The canonical daily identity is (service, period_kind='day', day); the
    schema makes duplicates impossible, so a duplicate here means corrupt
    input. Failing closed prevents silent double-counting in totals,
    averages and peak indicators. Migrated bucket rows are not part of the
    daily identity and are never checked.
    """
    seen: set[tuple[Service, date]] = set()
    for obs in token_observations:
        if obs.period_kind != "day" or not obs.has_exact_tokens:
            continue
        key = (obs.service, obs.day)
        if key in seen:
            raise ValueError(
                f"duplicate daily token observation for {obs.service.value} {obs.day.isoformat()}"
            )
        seen.add(key)


def _build_daily_token_stats(
    token_observations: list[TokenObservation],
) -> tuple[DailyTokenStats, ...]:
    """Build exact selected-range daily indicators per service.

    Only ``AVAILABLE_EXACT`` rows with ``period_kind == 'day'`` contribute.
    The denominator is the number of reported exact days — missing days
    are never filled with zero. Results are deterministic: rows are
    ordered by activity day before aggregation, arithmetic is
    integer/Decimal, and peak ties resolve to the earliest reported day.
    Callers must reject duplicate (service, day) inputs first.
    """
    groups: dict[Service, list[TokenObservation]] = {}
    for obs in token_observations:
        if obs.period_kind != "day" or not obs.has_exact_tokens:
            continue
        groups.setdefault(obs.service, []).append(obs)

    stats: list[DailyTokenStats] = []
    for service, obs_list in sorted(groups.items(), key=lambda item: item[0].value):
        ordered = sorted(obs_list, key=lambda o: (o.day, o.observed_at))
        total = sum(o.tokens or 0 for o in ordered)
        days = len(ordered)
        average = int(
            (Decimal(total) / Decimal(days)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
        peak_tokens = max(o.tokens or 0 for o in ordered)
        # Stable tie rule: the earliest reported day carrying the maximum.
        peak_obs = next(o for o in ordered if (o.tokens or 0) == peak_tokens)
        share: Decimal | None = None
        if total > 0:
            share = (Decimal(peak_tokens) * Decimal(100) / Decimal(total)).quantize(
                Decimal("0.1"), rounding=ROUND_HALF_UP
            )
        stats.append(
            DailyTokenStats(
                service=service,
                reported_days=days,
                total_tokens=total,
                average_per_reported_day=average,
                peak_day=peak_obs.day.isoformat(),
                peak_tokens=peak_tokens,
                peak_share_percent=share,
            )
        )
    return tuple(stats)


def _build_token_summaries(
    token_observations: list[TokenObservation],
) -> tuple[TokenSummary, ...]:
    """Build TokenSummary objects grouped by (service, period_kind).

    Only AVAILABLE_EXACT observations contribute to totals. Earliest/latest
    use the activity day (period_start date part); observed_at remains
    retrieval provenance. The source shown is the latest observation's
    source. The official aggregate summary is NOT folded in here — it is a
    separate typed CodexSummary record displayed apart from daily totals.
    """
    if not token_observations:
        return ()

    # Group by (service, period_kind) — canonical daily identity is
    # independent of the display source.
    groups: dict[tuple[Service, str], list[TokenObservation]] = {}
    for obs in token_observations:
        if not obs.has_exact_tokens:
            continue
        key = (obs.service, obs.period_kind)
        groups.setdefault(key, []).append(obs)

    summaries: list[TokenSummary] = []
    for (service, period_kind), obs_list in sorted(
        groups.items(), key=lambda item: (item[0][0].value, item[0][1])
    ):
        total_sum = sum(obs.tokens or 0 for obs in obs_list)
        ordered = sorted(obs_list, key=lambda o: (o.day, o.observed_at))
        summaries.append(
            TokenSummary(
                service=service,
                source=ordered[-1].source,
                period_kind=period_kind,
                total_tokens=total_sum,
                event_count=len(obs_list),
                earliest_day=ordered[0].day.isoformat(),
                latest_day=ordered[-1].day.isoformat(),
            )
        )

    return tuple(summaries)


def _build_token_availability_from_records(
    avail_records: list[TokenAvailabilityRecord],
) -> tuple[TokenAvailabilityState, ...]:
    """Build the latest sanitized availability state per service.

    Reads from dedicated token_availability records — one per provider
    attempt. Only the latest observed_at per service is kept.
    Independent from daily token_events: a non-exact state after exact
    daily data coexists with and never alters exact totals.
    """
    if not avail_records:
        return ()
    latest: dict[Service, TokenAvailabilityRecord] = {}
    for rec in avail_records:
        current = latest.get(rec.service)
        if current is None or rec.observed_at > current.observed_at:
            latest[rec.service] = rec
    return tuple(
        sorted(
            (
                TokenAvailabilityState(service=service, status=rec.status)
                for service, rec in latest.items()
            ),
            key=lambda state: state.service.value,
        )
    )


def _build_codex_summaries(summary_observations: list[CodexSummary]) -> tuple[CodexSummary, ...]:
    """Keep only the newest official summary record per service.

    Each record is one typed snapshot per refresh; the view displays the
    latest one.
    """
    newest: dict[Service, CodexSummary] = {}
    for summary in summary_observations:
        current = newest.get(summary.service)
        if current is None or summary.observed_at > current.observed_at:
            newest[summary.service] = summary
    return tuple(sorted(newest.values(), key=lambda s: s.service.value))


# ── Pure display-text builders (translator injected, GTK-free) ──


def build_token_summary_text(ts: TokenSummary, translator: Callable[[str], str]) -> str:
    """Build the complete token-activity label as a pure function.

    Daily totals (period_kind='day') and migrated 15-minute samples
    (period_kind='bucket') are labeled separately.
    """
    _ = translator
    parts: list[str] = [f"{ts.service.value.title()} {_('token activity')}"]
    if ts.period_kind != "day":
        parts[0] += f" ({_('15-min samples')})"
    total_label = _("Daily total") if ts.period_kind == "day" else _("Total")
    parts.append(f"{total_label}: {ts.total_tokens:,}")
    if ts.earliest_day and ts.latest_day:
        parts.append(f"{ts.earliest_day}–{ts.latest_day}")
    parts.append(f"{_('Source')}: {ts.source}")
    return _(" · ").join(parts)


def build_daily_token_stats_text(
    stats: DailyTokenStats,
    range_label: str,
    translator: Callable[[str], str],
) -> str:
    """Build the compact indicator line as a pure function.

    The service and range are part of the label so derived values are
    clearly range-aware. A zero-token reported day still renders (it was
    reported); missing data never renders a zero card because zero exact
    days produce no DailyTokenStats entry at all.
    """
    _ = translator
    parts: list[str] = [f"{stats.service.value.title()} · {range_label}"]
    parts.append(f"{_('Total')}: {stats.total_tokens:,}")
    parts.append(f"{_('Reported days')}: {stats.reported_days}")
    parts.append(f"{_('Avg/day')}: {stats.average_per_reported_day:,}")
    if stats.peak_day is not None:
        parts.append(f"{_('Peak')}: {stats.peak_day} ({stats.peak_tokens:,})")
    if stats.peak_share_percent is not None:
        parts.append(f"{_('Peak share')}: {stats.peak_share_percent}%")
    return _(" · ").join(parts)


def build_codex_summary_text(summary: CodexSummary, translator: Callable[[str], str]) -> str:
    """Build the official summary label as a pure function.

    The official five-field summary is displayed separately from daily
    totals and explicitly labeled account-wide — lifetime, provider peak,
    streaks and longest-turn values are never relabeled as selected-range
    data. A fully-null summary renders as an em-dash placeholder.
    """
    _ = translator
    fields: list[str] = []
    if summary.lifetime_tokens is not None:
        fields.append(f"{_('Lifetime')}: {summary.lifetime_tokens:,}")
    if summary.peak_daily_tokens is not None:
        fields.append(f"{_('Peak day')}: {summary.peak_daily_tokens:,}")
    if summary.current_streak_days is not None:
        fields.append(f"{_('Current streak')}: {summary.current_streak_days}")
    if summary.longest_streak_days is not None:
        fields.append(f"{_('Longest streak')}: {summary.longest_streak_days}")
    if summary.longest_running_turn_sec is not None:
        fields.append(f"{_('Longest turn')}: {summary.longest_running_turn_sec:,}s")
    if not fields:
        return f"{_('Codex summary')} ({_('account-wide')}): —"
    return f"{_('Codex summary')} ({_('account-wide')}) · " + _(" · ").join(fields)


def build_token_availability_note(status: HistoryStatus, translator: Callable[[str], str]) -> str:
    """Build the sanitized availability note text for one status.

    Unsupported/temporary/invalid map to fixed sanitized strings — never
    raw details. This note is secondary: it never hides older exact data
    or quota charts.
    """
    _ = translator
    if status is HistoryStatus.TEMPORARILY_UNAVAILABLE:
        return _("Exact tokens temporarily unavailable")
    if status is HistoryStatus.INVALID:
        return _("Exact token data invalid")
    return _("Exact token usage is not available")


def prepare_history_view(
    observations: list[QuotaObservation],
    *,
    range_label: str,
    filter_label: str,
    diagnostic: str = "ok",
    max_points: int = MAX_CHART_POINTS,
    token_observations: list[TokenObservation] | None = None,
    codex_summaries: list[CodexSummary] | None = None,
    token_availability_records: list[TokenAvailabilityRecord] | None = None,
) -> HistoryViewResult:
    """Prepare an immutable HistoryViewResult from raw observations.

    Pure deterministic function. No I/O, no side effects.
    Each metric is rendered independently — Claude five-hour, Claude weekly,
    and Codex weekly are never merged. Persisted daily totals and the
    official summary are carried separately; availability states are
    sanitized secondary notes from dedicated records that never hide quota
    series.
    """
    groups = _group_observations(observations)
    series: list[SeriesView] = []
    for (label, service), obs_list in sorted(groups.items()):
        resets = _detect_resets(obs_list)
        stats = _build_stats(label, service, obs_list, resets)
        points = _reduce_points(obs_list, resets, max_points)
        series.append(SeriesView(stats=stats, points=points))
    token_obs = token_observations or []
    avail_records = token_availability_records or []
    # Pure aggregation boundary: duplicate (service, day) daily inputs fail
    # closed instead of being double-counted.
    _reject_duplicate_daily_observations(token_obs)
    return HistoryViewResult(
        series=tuple(series),
        diagnostic=diagnostic,
        range_label=range_label,
        filter_label=filter_label,
        token_summaries=_build_token_summaries(token_obs),
        daily_token_stats=_build_daily_token_stats(token_obs),
        token_availability=_build_token_availability_from_records(avail_records),
        codex_summaries=_build_codex_summaries(codex_summaries or []),
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

    from .history_db import _connect, init_schema, query_token_availability

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
            token_obs: list[TokenObservation] = result.get("tokens", [])
            summary_obs: list[CodexSummary] = result.get("summaries", [])
            # Query token_availability separately (dedicated table)
            since = now - __import__("datetime").timedelta(days=90)
            avail_records: list[TokenAvailabilityRecord] = query_token_availability(
                conn, since=since
            )
            if service is not None:
                quota_obs = [o for o in quota_obs if o.service is service]
                token_obs = [o for o in token_obs if o.service is service]
                summary_obs = [s for s in summary_obs if s.service is service]
                avail_records = [a for a in avail_records if a.service is service]
            if not quota_obs and not token_obs and not summary_obs and not avail_records:
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
                token_observations=token_obs,
                codex_summaries=summary_obs,
                token_availability_records=avail_records,
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
