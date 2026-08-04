"""Pure sanitized display and copy text builders (GTK-free).

Every string produced here is bounded and sanitized: no secrets, raw
exception text, private paths, server URLs, topics, or tokens. The
translator and timezone formatters are injected so tests are deterministic
and locale-independent.

``format_countdown`` lives here (pure datetime math) and is re-exported by
``ui.py`` for backward compatibility.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from .models import QuotaReading, Service
from .persistence import Settings

_SERVICES = (Service.CLAUDE, Service.CODEX)


def format_countdown(reset_at: datetime, now: datetime | None = None) -> str:
    """Format the time until ``reset_at`` as ``2d 3h 4m`` (or ``3h 4m``)."""
    local_now = now or datetime.now().astimezone()
    seconds = max(0, int((reset_at.astimezone() - local_now).total_seconds()))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    return f"{days}d {hours}h {minutes}m" if days else f"{hours}h {minutes}m"


def build_reading_line(
    reading: QuotaReading,
    *,
    now: datetime | None = None,
    translator: Callable[[str], str],
) -> str:
    """Build the one-line used/remaining/countdown text for a reading.

    Example: ``Weekly: 45% used · 55% remaining · resets in 2d 3h``.
    Remaining is always ``100 - used``; tokens are never derived from
    percentages. ``now`` is injectable for deterministic tests.
    """
    if reading.percentage is None:
        return reading.quota_label
    used = reading.percentage
    remaining = 100.0 - used
    _ = translator
    parts = [
        f"{reading.quota_label}: {used:.0f}% {_('used')}",
        f"{remaining:.0f}% {_('remaining')}",
    ]
    if reading.reset_at is not None:
        parts.append(f"{_('resets in ')}{format_countdown(reading.reset_at, now=now)}")
    return _(" · ").join(parts)


def _service_state_line(
    service: Service,
    settings: Settings,
    readings: list[QuotaReading],
    *,
    now: datetime | None = None,
    format_local: Callable[[datetime], str],
    translator: Callable[[str], str],
) -> list[str]:
    """Build sanitized per-service copy lines (no secrets, no raw errors)."""
    _ = translator
    enabled = settings.collect_claude if service is Service.CLAUDE else settings.collect_codex
    lines = [f"{service.value.title()} — {_('enabled') if enabled else _('disabled')}"]
    if not enabled:
        return lines
    service_readings = [r for r in readings if r.service is service]
    if not service_readings:
        lines.append(f"  {_('No reading')}")
        return lines
    for reading in service_readings:
        if reading.percentage is None:
            lines.append(f"  {reading.quota_label}: {_('no percentage')}")
            continue
        line = build_reading_line(reading, now=now, translator=translator)
        if reading.reset_at is not None:
            line += f" · {_('resets ')}{format_local(reading.reset_at)}"
        lines.append(f"  {line}")
    return lines


def build_quota_status_text(
    readings: list[QuotaReading],
    settings: Settings,
    *,
    now: datetime | None = None,
    format_local: Callable[[datetime], str],
    translator: Callable[[str], str],
) -> str:
    """Build the sanitized copyable quota-status text for both services."""
    sections: list[str] = []
    for service in _SERVICES:
        sections.extend(
            _service_state_line(
                service,
                settings,
                readings,
                now=now,
                format_local=format_local,
                translator=translator,
            )
        )
    return "\n".join(sections)


def build_diagnostics_text(
    *,
    version: str,
    settings: Settings,
    readings: list[QuotaReading],
    last_refresh: str | None,
    next_refresh: str | None,
    history_status: str,
    history_lifecycle: str,
    translator: Callable[[str], str],
) -> str:
    """Build the sanitized diagnostics report.

    Shows provider state, last/next refresh, History writer status, channel
    state, and the app version. Never shows server URLs, topics, tokens,
    paths, raw errors, or exception text.
    """
    _ = translator
    lines: list[str] = [f"Moira {version}"]
    for service in _SERVICES:
        enabled = settings.collect_claude if service is Service.CLAUDE else settings.collect_codex
        state = _("enabled") if enabled else _("disabled")
        reading = next((r for r in readings if r.service is service), None)
        if reading is not None:
            status = reading.status.value.replace("_", " ").title()
            lines.append(f"{service.value.title()}: {state} · {status}")
        else:
            lines.append(f"{service.value.title()}: {state} · {_('no data')}")
    lines.append(f"{_('Last refresh')}: {last_refresh or '—'}")
    lines.append(f"{_('Next refresh')}: {next_refresh or '—'}")
    lines.append(f"{_('History writer')}: {history_status} ({history_lifecycle})")
    ntfy_state = _("enabled") if settings.ntfy_enabled else _("disabled")
    if settings.ntfy_enabled:
        ntfy_state += (
            f" · {_('configured') if settings.ntfy_topic.strip() else _('not configured')}"
        )
    lines.append(f"NTFY {_('channel')}: {ntfy_state}")
    native_state = _("enabled") if settings.native_notifications else _("disabled")
    lines.append(f"{_('Native channel')}: {native_state}")
    return "\n".join(lines)
