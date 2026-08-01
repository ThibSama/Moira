from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .models import QuotaReading, QuotaStatus
from .ntfy import Notification
from .persistence import Settings


@dataclass(frozen=True, slots=True)
class PendingAlert:
    key: str
    notification: Notification


def _identity(reading: QuotaReading) -> str:
    return f"{reading.service.value}:{reading.quota_label}"


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
            if (
                settings.reset_alerts
                and prior
                and prior.reset_at
                and prior.reset_at != reading.reset_at
            ):
                key = f"reset:{identity}:{reset_key}"
                if key not in sent_keys:
                    alerts.append(
                        PendingAlert(
                            key,
                            Notification(
                                f"{reading.service.value.title()} quota reset",
                                f"{reading.quota_label} quota entered a new window.",
                                "arrows_counterclockwise",
                            ),
                        )
                    )
            if prior and prior.percentage is not None:
                for threshold in settings.thresholds:
                    key = f"threshold:{identity}:{reset_key}:{threshold}"
                    if prior.percentage < threshold <= reading.percentage and key not in sent_keys:
                        alerts.append(
                            PendingAlert(
                                key,
                                Notification(
                                    f"{reading.service.value.title()} "
                                    f"{reading.quota_label}: {threshold}%",
                                    f"Usage reached {reading.percentage:.0f}%.",
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
                            f"{reading.service.value.title()} quota error",
                            "Moira could not refresh quota data.",
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
