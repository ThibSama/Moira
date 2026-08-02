from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from .exhaustion import is_weekly_exhausted, was_weekly_exhausted
from .i18n import tr
from .models import QuotaReading, QuotaStatus, Service
from .ntfy import Notification
from .persistence import Settings


@dataclass(frozen=True, slots=True)
class PendingAlert:
    key: str
    notification: Notification


def _identity(reading: QuotaReading) -> str:
    return f"{reading.service.value}:{reading.quota_label}"


def _service_name(service: Service) -> str:
    return service.value.title()


def _is_weekly(reading: QuotaReading) -> bool:
    """Return True if the reading's quota label indicates a weekly window."""
    label = reading.quota_label.lower()
    return "week" in label or "seven" in label or "7" in label


def _currently_exhausted(reading: QuotaReading | None, *, now: datetime | None) -> bool:
    """Canonical current-state exhaustion rule, scoped to weekly readings only."""
    if reading is None or not _is_weekly(reading):
        return False
    return is_weekly_exhausted(reading, now=now)


def evaluate_alerts(
    previous: list[QuotaReading],
    current: list[QuotaReading],
    settings: Settings,
    sent_keys: set[str],
    *,
    now: datetime | None = None,
) -> list[PendingAlert]:
    old = {_identity(item): item for item in previous}
    alerts: list[PendingAlert] = []
    for reading in current:
        identity = _identity(reading)
        prior = old.get(identity)
        if (
            reading.status is QuotaStatus.AVAILABLE
            and reading.percentage is not None
            and reading.reset_at
        ):
            reset_key = reading.reset_at.isoformat()
            exhausted = _currently_exhausted(reading, now=now)
            prior_exhausted = was_weekly_exhausted(prior, now=now)
            # Exhaustion/recovery events: dedup per service/window, independent from thresholds
            if exhausted:
                exh_key = f"exhausted:{identity}:{reset_key}"
                if exh_key not in sent_keys:
                    alerts.append(
                        PendingAlert(
                            exh_key,
                            Notification(
                                f"{_service_name(reading.service)} {tr('quota exhausted')}",
                                tr(
                                    "Weekly usage has reached 100%. "
                                    "Usage is blocked until the weekly reset."
                                ),
                                "no_entry",
                                5,
                            ),
                        )
                    )
                # Fall through to threshold checks: lower thresholds still fire,
                # only the generic 100% threshold is suppressed below.
            elif (
                prior_exhausted
                and not exhausted
                and reading.percentage is not None
                and reading.percentage < 100
            ):
                # Recovery: previously exhausted, now below 100% or reset
                rec_key = f"recovered:{identity}:{reset_key}"
                if rec_key not in sent_keys:
                    alerts.append(
                        PendingAlert(
                            rec_key,
                            Notification(
                                f"{_service_name(reading.service)} {tr('quota recovered')}",
                                tr("Weekly quota has reset and usage is available again."),
                                "white_check_mark",
                                3,
                            ),
                        )
                    )
                continue
            # ── Reset alerts ──
            if (
                settings.reset_alerts
                and prior
                and prior.reset_at
                and prior.reset_at != reading.reset_at
                and not prior_exhausted
            ):
                key = f"reset:{identity}:{reset_key}"
                if key not in sent_keys:
                    alerts.append(
                        PendingAlert(
                            key,
                            Notification(
                                f"{_service_name(reading.service)} {tr('quota reset')}",
                                f"{reading.quota_label}{tr(' quota entered a new window.')}",
                                "arrows_counterclockwise",
                            ),
                        )
                    )
            # ── Threshold alerts (the generic 100% threshold is suppressed
            #    when an exhaustion event fires for this reading) ──
            if prior and prior.percentage is not None:
                for threshold in settings.thresholds:
                    # Suppress the generic 100% threshold when exhaustion fires
                    if exhausted and threshold == 100:
                        continue
                    key = f"threshold:{identity}:{reset_key}:{threshold}"
                    if prior.percentage < threshold <= reading.percentage and key not in sent_keys:
                        alerts.append(
                            PendingAlert(
                                key,
                                Notification(
                                    f"{_service_name(reading.service)} "
                                    f"{reading.quota_label}: {threshold}%",
                                    f"{tr('Usage reached ')}{reading.percentage:.0f}{tr('%.')}",
                                    "warning",
                                    4 if threshold >= 90 else 3,
                                ),
                            )
                        )
        elif settings.error_alerts and reading.status in {
            QuotaStatus.ERROR,
            QuotaStatus.PARSE_ERROR,
        }:
            digest = hashlib.sha256(reading.detail.encode()).hexdigest()[:12]
            key = f"error:{identity}:{reading.status.value}:{digest}"
            if key not in sent_keys:
                alerts.append(
                    PendingAlert(
                        key,
                        Notification(
                            f"{_service_name(reading.service)} {tr('quota error')}",
                            tr("Moira could not refresh quota data."),
                            "warning",
                        ),
                    )
                )
    return alerts


def merge_with_stale(previous: list[QuotaReading], fresh: list[QuotaReading]) -> list[QuotaReading]:
    old_by_service: dict[str, list[QuotaReading]] = {}
    for item in previous:
        old_by_service.setdefault(item.service.value, []).append(item)
    new_by_service: dict[str, list[QuotaReading]] = {}
    for item in fresh:
        new_by_service.setdefault(item.service.value, []).append(item)
    merged: list[QuotaReading] = []
    for service in ("claude", "codex"):
        incoming = new_by_service.get(service, [])
        successful = [item for item in incoming if item.status is QuotaStatus.AVAILABLE]
        if successful:
            merged.extend(successful)
        elif old_by_service.get(service):
            detail = incoming[0].detail if incoming else "refresh failed"
            merged.extend(
                item.stale(detail)
                for item in old_by_service[service]
                if item.percentage is not None
            )
        else:
            merged.extend(incoming)
    return merged
