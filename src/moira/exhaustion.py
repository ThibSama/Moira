"""Pure weekly-exhaustion rule and service snapshot derivation.

An AVAILABLE weekly reading at >=100% means exhausted until its weekly reset.
STALE, missing, error, or sub-100 readings must not newly establish exhaustion.
State derivation stays outside GTK widgets: this module knows nothing about Gtk.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .models import QuotaReading, QuotaStatus, Service, utc_now


@dataclass(frozen=True, slots=True)
class ServiceSnapshot:
    """Derived display state for one service, computed from readings."""

    service: Service
    weekly: QuotaReading | None
    five_hour: QuotaReading | None
    exhausted: bool

    @property
    def weekly_reset_at(self) -> datetime | None:
        return self.weekly.reset_at if self.weekly and self.weekly.reset_at else None

    @property
    def weekly_percentage(self) -> float | None:
        return self.weekly.percentage if self.weekly else None


def is_weekly_exhausted(
    reading: QuotaReading | None,
    *,
    now: datetime | None = None,
) -> bool:
    """Return True iff the reading is an AVAILABLE weekly value at >=100%.

    STALE, missing, error, parse-error, unavailable, or sub-100 readings
    must not newly establish exhaustion. If the reset time has passed,
    the window is no longer exhausted.
    """
    if reading is None:
        return False
    if reading.status is not QuotaStatus.AVAILABLE:
        return False
    if reading.percentage is None or reading.percentage < 100:
        return False
    if reading.reset_at is not None:
        current = now if now is not None else utc_now()
        if current >= reading.reset_at:
            return False
    return True


def was_weekly_exhausted(
    reading: QuotaReading | None,
    *,
    now: datetime | None = None,
) -> bool:
    """Return True iff a prior weekly reading constituted exhaustion at observation time.

    This is the pure recovery-transition rule: it distinguishes prior evidence
    that an old weekly window reached exhaustion from the current non-exhausted
    state. Unlike is_weekly_exhausted (the canonical current-state rule),
    this predicate does NOT check whether reset_at has passed. A reset that
    has elapsed is exactly the recovery signal — the prior reading WAS
    exhausted, and the current reading is NOT.

    Requirements:
      - reading must be an AVAILABLE weekly reading at >=100%
      - STALE, error, parse-error, unavailable, sub-100, and five-hour
        readings are rejected
      - The optional now parameter is accepted for API symmetry but does
        not affect the historical-evidence evaluation
    """
    if reading is None:
        return False
    if reading.status is not QuotaStatus.AVAILABLE:
        return False
    if reading.percentage is None or reading.percentage < 100:
        return False
    label = reading.quota_label.lower()
    if "week" not in label and "seven" not in label and "7" not in label:
        return False
    return True


def derive_service(
    service: Service,
    readings: list[QuotaReading],
    *,
    now: datetime | None = None,
) -> ServiceSnapshot:
    """Derive display state for one service from a list of readings."""
    weekly: QuotaReading | None = None
    five_hour: QuotaReading | None = None
    for item in readings:
        if item.service is not service:
            continue
        label = item.quota_label.lower()
        if "weekly" in label or "week" in label or "seven" in label or "7" in label:
            weekly = item
        elif "five" in label or "session" in label or "5" in label:
            five_hour = item
    return ServiceSnapshot(
        service=service,
        weekly=weekly,
        five_hour=five_hour,
        exhausted=is_weekly_exhausted(weekly, now=now),
    )


def derive_state(
    readings: list[QuotaReading],
    *,
    now: datetime | None = None,
) -> dict[Service, ServiceSnapshot]:
    """Derive snapshots for both services."""
    return {
        Service.CLAUDE: derive_service(Service.CLAUDE, readings, now=now),
        Service.CODEX: derive_service(Service.CODEX, readings, now=now),
    }
