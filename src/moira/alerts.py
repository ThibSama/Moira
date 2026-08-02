from __future__ import annotations

import hashlib
from dataclasses import dataclass

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


def _is_weekly_exhausted(reading: QuotaReading) -> bool:
    """Check if a reading represents an exhausted weekly window at >=100%."""
    return (
        reading.status is QuotaStatus.AVAILABLE
        and reading.percentage is not None
        and reading.percentage >= 100
        and ("week" in reading.quota_label.lower() or "seven" in reading.quota_label.lower())
    )


def evaluate_alerts(
    previous: list[QuotaReading],
    current: list[QuotaReading],
    settings: Settings,
    sent_keys: set[str],
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
            # Exhaustion/recovery events: dedup per service/window, independent from thresholds
            if _is_weekly_exhausted(reading):
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
                # Suppress the duplicate generic 100% threshold alert
                continue
            elif prior and _is_weekly_exhausted(prior) and not _is_weekly_exhausted(reading):
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
                and not _is_weekly_exhausted(prior)
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
            # ── Threshold alerts (suppressed at 100% to avoid duplicate with exhaustion) ──
            if prior and prior.percentage is not None and reading.percentage < 100:
                for threshold in settings.thresholds:
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
