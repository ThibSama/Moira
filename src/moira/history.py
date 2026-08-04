"""Typed history domain objects separating quota observations from optional
exact-token observations.

Both record types carry service, UTC time, source, and a typed availability
state describing the telemetry availability. Token data is always optional
and never estimated.

All datetimes are normalized to UTC. Labels and sources must be non-empty.
Percentages are in 0–100. Token counts are non-negative int64 values.
AVAILABLE_EXACT requires exact data under a documented total policy;
non-available statuses carry no counts.

``HistoryStatus`` is defined in ``models.py`` (the provider-neutral typed
availability model) and re-exported here for backward compatibility.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time

from .models import HistoryStatus, QuotaReading, QuotaStatus, Service, _validate_int64

__all__ = [
    "HistoryStatus",
    "QuotaObservation",
    "SchemaVersionError",
    "TokenObservation",
    "HistoryWriteResult",
]


class SchemaVersionError(ValueError):
    """Raised when the database schema version does not match the expected version."""


def _ensure_utc(dt: datetime) -> datetime:
    """Normalize a datetime to UTC. Naive datetimes are rejected."""
    if dt.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    if dt.tzinfo is not UTC:
        return dt.astimezone(UTC)
    return dt


def _validate_non_empty(value: str, field_name: str) -> str:
    if not value or not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    return value


def _validate_percentage(value: float) -> float:
    if not 0 <= value <= 100:
        raise ValueError("percentage must be between 0 and 100")
    return value


def _validate_non_negative_int(value: int | None, field_name: str) -> int | None:
    return _validate_int64(value, field_name)


@dataclass(frozen=True, slots=True)
class QuotaObservation:
    """A single validated quota percentage observation for history storage.

    Only fresh AVAILABLE readings are eligible. Stale, error, unavailable,
    or parse-error readings must not produce a QuotaObservation. Quota
    observations always expose AVAILABLE_EXACT status.
    """

    service: Service
    quota_label: str
    percentage: float
    reset_at: datetime
    observed_at: datetime
    source: str
    status: HistoryStatus = HistoryStatus.AVAILABLE_EXACT

    def __post_init__(self) -> None:
        if self.status is not HistoryStatus.AVAILABLE_EXACT:
            raise ValueError("QuotaObservation status must be AVAILABLE_EXACT")
        _validate_non_empty(self.quota_label, "quota_label")
        _validate_percentage(self.percentage)
        _validate_non_empty(self.source, "source")
        object.__setattr__(self, "reset_at", _ensure_utc(self.reset_at))
        object.__setattr__(self, "observed_at", _ensure_utc(self.observed_at))

    @classmethod
    def from_reading(cls, reading: QuotaReading) -> QuotaObservation | None:
        """Create a QuotaObservation from a QuotaReading, or None if ineligible.

        Only AVAILABLE readings with a percentage and reset_at qualify.
        """
        if reading.status is not QuotaStatus.AVAILABLE:
            return None
        if reading.percentage is None or reading.reset_at is None:
            return None
        return cls(
            service=reading.service,
            quota_label=reading.quota_label,
            percentage=reading.percentage,
            reset_at=reading.reset_at,
            observed_at=reading.retrieved_at,
            source=reading.source,
        )


_VALID_PERIOD_KINDS = ("day", "bucket")


@dataclass(frozen=True, slots=True)
class TokenObservation:
    """An optional exact-token observation from the Codex structured surface.

    Represents one persisted token event. ``period_start`` is the logical
    start of the activity period (UTC midnight for daily rows, the 15-minute
    bucket instant for migrated v2 rows) and ``period_kind`` is ``day`` or
    ``bucket``. Ordering and earliest/latest are derived from the activity
    day (``period_start.date()``); ``observed_at`` remains retrieval
    provenance only.

    AVAILABLE_EXACT requires total_tokens. Non-available statuses carry no
    counts. The official aggregate summary is a separate typed
    ``CodexSummary`` record — never duplicated onto daily events.
    """

    service: Service
    period_start: datetime
    period_kind: str
    observed_at: datetime
    source: str
    status: HistoryStatus
    tokens: int | None = None

    def __post_init__(self) -> None:
        if self.period_kind not in _VALID_PERIOD_KINDS:
            raise ValueError(f"period_kind must be one of {_VALID_PERIOD_KINDS}")
        _validate_non_empty(self.source, "source")
        if not isinstance(self.status, HistoryStatus):
            raise ValueError("status must be a HistoryStatus value")
        object.__setattr__(self, "period_start", _ensure_utc(self.period_start))
        object.__setattr__(self, "observed_at", _ensure_utc(self.observed_at))
        object.__setattr__(self, "tokens", _validate_non_negative_int(self.tokens, "tokens"))

        if self.status is HistoryStatus.AVAILABLE_EXACT:
            if self.tokens is None:
                raise ValueError("AVAILABLE_EXACT requires total_tokens")
        else:
            if self.tokens is not None:
                raise ValueError("non-available statuses must not carry token counts")

    @property
    def day(self) -> date:
        """Return the activity day (``period_start`` date part).

        This is the canonical ordering/labeling key for daily identity —
        independent of retrieval time.
        """
        return self.period_start.date()

    @classmethod
    def unsupported(cls, service: Service, observed_at: datetime, source: str) -> TokenObservation:
        """Create an UNSUPPORTED token observation for the activity day of retrieval."""
        return cls(
            service=service,
            period_start=_day_start(observed_at),
            period_kind="day",
            observed_at=observed_at,
            source=source,
            status=HistoryStatus.UNSUPPORTED,
        )

    @classmethod
    def temporarily_unavailable(
        cls, service: Service, observed_at: datetime, source: str
    ) -> TokenObservation:
        """Create a TEMPORARILY_UNAVAILABLE token observation."""
        return cls(
            service=service,
            period_start=_day_start(observed_at),
            period_kind="day",
            observed_at=observed_at,
            source=source,
            status=HistoryStatus.TEMPORARILY_UNAVAILABLE,
        )

    @classmethod
    def invalid(cls, service: Service, observed_at: datetime, source: str) -> TokenObservation:
        """Create an INVALID token observation for malformed telemetry."""
        return cls(
            service=service,
            period_start=_day_start(observed_at),
            period_kind="day",
            observed_at=observed_at,
            source=source,
            status=HistoryStatus.INVALID,
        )

    @property
    def has_exact_tokens(self) -> bool:
        """Return True if this observation carries exact token counts."""
        return self.status is HistoryStatus.AVAILABLE_EXACT and self.tokens is not None


def _day_start(dt: datetime) -> datetime:
    """Return UTC midnight of ``dt``'s date (the activity day start)."""
    return datetime.combine(dt.date(), time.min, tzinfo=UTC)


class HistoryWriteResult:
    """Bounded success/diagnostic result returned to GTK.

    Contains only a sanitized status string — never raw exception text, SQL,
    payloads, private paths, account data, or secrets.
    """

    __slots__ = ("ok", "diagnostic")

    def __init__(self, ok: bool, diagnostic: str = "") -> None:
        self.ok = ok
        self.diagnostic = diagnostic

    def __repr__(self) -> str:
        return f"HistoryWriteResult(ok={self.ok}, diagnostic={self.diagnostic!r})"
