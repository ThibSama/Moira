"""Typed history domain objects separating quota observations from optional
exact-token observations.

Both record types carry service, UTC time, source, and a status describing
the telemetry availability. Token data is always optional and never estimated.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .models import QuotaReading, QuotaStatus, Service


class HistoryStatus(StrEnum):
    """Availability status for a telemetry observation."""

    AVAILABLE_EXACT = "available_exact"
    UNSUPPORTED = "unsupported"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class QuotaObservation:
    """A single validated quota percentage observation for history storage.

    Only fresh AVAILABLE readings are eligible. Stale, error, unavailable,
    or parse-error readings must not produce a QuotaObservation.
    """

    service: Service
    quota_label: str
    percentage: float
    reset_at: datetime
    observed_at: datetime
    source: str

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


@dataclass(frozen=True, slots=True)
class TokenObservation:
    """An optional exact-token observation from a structured provider surface.

    Token counts are never estimated or derived from percentages. When the
    provider does not expose exact counts, status is UNSUPPORTED and all
    token fields are None.
    """

    service: Service
    observed_at: datetime
    source: str
    status: HistoryStatus
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    total_tokens: int | None = None

    @classmethod
    def unsupported(cls, service: Service, observed_at: datetime, source: str) -> TokenObservation:
        """Create an UNSUPPORTED token observation."""
        return cls(
            service=service,
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
            observed_at=observed_at,
            source=source,
            status=HistoryStatus.TEMPORARILY_UNAVAILABLE,
        )

    @classmethod
    def invalid(cls, service: Service, observed_at: datetime, source: str) -> TokenObservation:
        """Create an INVALID token observation for malformed telemetry."""
        return cls(
            service=service,
            observed_at=observed_at,
            source=source,
            status=HistoryStatus.INVALID,
        )

    @property
    def has_exact_tokens(self) -> bool:
        """Return True if this observation carries exact token counts."""
        return self.status is HistoryStatus.AVAILABLE_EXACT and self.total_tokens is not None
