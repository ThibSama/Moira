"""Package 5: sanitized copy/diagnostics text builders — used/remaining/
countdown lines, quota-status copy, diagnostics report, history summary copy.
All deterministic (injected clocks/translators), no secrets, errors, or paths."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from moira.diagnostics import (
    build_diagnostics_text,
    build_quota_status_text,
    build_reading_line,
    format_countdown,
)
from moira.history_view import (
    HistoryViewResult,
    build_history_summary_text,
    prepare_history_view,
)
from moira.models import QuotaReading, QuotaStatus, Service
from moira.persistence import Settings

NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)
RESET = NOW + timedelta(days=5)

identity = lambda s: s  # noqa: E731


def _fr(s: str) -> str:
    from moira.i18n import _FRENCH

    return _FRENCH.get(s, s)


def reading(
    service: Service = Service.CLAUDE,
    label: str = "Weekly",
    pct: float | None = 45.0,
    reset: datetime | None = RESET,
) -> QuotaReading:
    return QuotaReading(service, label, pct, reset, NOW, "fixture", QuotaStatus.AVAILABLE)


# ── format_countdown / build_reading_line ──


def test_format_countdown_deterministic() -> None:
    assert format_countdown(NOW + timedelta(days=2, hours=3, minutes=4), now=NOW) == "2d 3h 4m"
    assert format_countdown(NOW + timedelta(hours=3, minutes=4), now=NOW) == "3h 4m"
    assert format_countdown(NOW - timedelta(hours=1), now=NOW) == "0h 0m"


def test_build_reading_line_used_remaining_countdown() -> None:
    line = build_reading_line(reading(pct=45.0), now=NOW, translator=identity)
    assert line == "Weekly: 45% used · 55% remaining · resets in 5d 0h 0m"


def test_reading_line_remaining_is_100_minus_used() -> None:
    line = build_reading_line(reading(pct=100.0), now=NOW, translator=identity)
    assert "0% remaining" in line
    line = build_reading_line(reading(pct=0.0), now=NOW, translator=identity)
    assert "100% remaining" in line


def test_reading_line_french() -> None:
    line = build_reading_line(reading(pct=45.0), now=NOW, translator=_fr)
    assert "45% utilisé" in line
    assert "55% restant" in line
    assert "réinitialisation dans" in line


def test_reading_line_no_percentage() -> None:
    r = QuotaReading(Service.CLAUDE, "Weekly", None, None, NOW, "fixture", QuotaStatus.ERROR, "d")
    assert build_reading_line(r, now=NOW, translator=identity) == "Weekly"


def test_reading_line_never_derives_tokens() -> None:
    """The line contains only percentage-derived text — never token values."""
    line = build_reading_line(reading(pct=45.0), now=NOW, translator=identity)
    assert "token" not in line.lower()
    assert "tokens" not in line.lower()


# ── Quota status copy ──


def test_quota_status_copy_sanitized() -> None:
    settings = Settings(collect_claude=True, collect_codex=True)
    text = build_quota_status_text(
        [reading(), reading(Service.CODEX, pct=80.0)],
        settings,
        now=NOW,
        format_local=lambda dt: "2026-08-07 12:00",
        translator=identity,
    )
    assert "Claude — enabled" in text
    assert "Codex — enabled" in text
    assert "Weekly: 45% used · 55% remaining" in text
    assert "resets 2026-08-07 12:00" in text
    assert "fixture" not in text  # no source detail leakage
    assert "secret" not in text


def test_quota_status_copy_disabled_provider() -> None:
    settings = Settings(collect_claude=False, collect_codex=True)
    text = build_quota_status_text(
        [reading()],
        settings,
        now=NOW,
        format_local=lambda dt: "2026-08-07 12:00",
        translator=identity,
    )
    assert "Claude — disabled" in text
    assert "Weekly" not in text.split("Claude — disabled")[1].split("Codex")[0]


# ── Diagnostics report ──


def test_diagnostics_sanitized_no_secrets() -> None:
    settings = Settings(
        ntfy_enabled=True,
        ntfy_topic="my-secret-topic",
        ntfy_server="https://secret-server.example",
        native_notifications=True,
    )
    settings.validate()
    text = build_diagnostics_text(
        version="0.2.2",
        settings=settings,
        readings=[reading()],
        last_refresh="12:00:00",
        next_refresh="1m 30s",
        history_status="ok",
        history_lifecycle="running",
        translator=identity,
    )
    assert "Moira 0.2.2" in text
    assert "Claude: enabled" in text
    assert "Last refresh: 12:00:00" in text
    assert "Next refresh: 1m 30s" in text
    assert "History writer: ok (running)" in text
    assert "NTFY channel: enabled · configured" in text
    assert "Native channel: enabled" in text
    # Secrets, server, topic, paths, raw errors never appear.
    assert "secret" not in text.lower()
    assert "my-secret-topic" not in text
    assert "https://" not in text
    assert "/" not in text.replace("·", "").replace("—", "")
    assert "Traceback" not in text


def test_diagnostics_french() -> None:
    settings = Settings(ntfy_enabled=False, native_notifications=True)
    text = build_diagnostics_text(
        version="0.2.2",
        settings=settings,
        readings=[],
        last_refresh=None,
        next_refresh=None,
        history_status="ok",
        history_lifecycle="running",
        translator=_fr,
    )
    assert "Moira 0.2.2" in text
    assert "Claude: activé" in text
    assert "désactivé" in text
    assert "canal" in text
    assert "aucune donnée" in text


def test_diagnostics_disabled_provider_state() -> None:
    settings = Settings(collect_claude=False)
    text = build_diagnostics_text(
        version="0.2.2",
        settings=settings,
        readings=[reading(), reading(Service.CODEX, pct=40.0)],
        last_refresh=None,
        next_refresh=None,
        history_status="backlog saturated",
        history_lifecycle="running",
        translator=identity,
    )
    assert "Claude: disabled" in text
    assert "Codex: enabled · Available" in text
    assert "History writer: backlog saturated (running)" in text


# ── History summary copy ──


def test_history_summary_copy_builds_sections() -> None:
    from moira.history import HistoryStatus, QuotaObservation, TokenObservation

    quota_obs = QuotaObservation(Service.CLAUDE, "Weekly", 50.0, RESET, NOW, "fixture")
    token_obs = TokenObservation(
        Service.CODEX,
        datetime(2026, 8, 1, tzinfo=UTC),
        "day",
        NOW,
        "codex-app-server",
        HistoryStatus.AVAILABLE_EXACT,
        1000,
    )
    view = prepare_history_view(
        [quota_obs],
        range_label="30d",
        filter_label="All",
        token_observations=[token_obs],
    )
    text = build_history_summary_text(view, identity)
    assert "Claude Weekly" in text
    assert "Latest: 50.0%" in text
    assert "Daily total: 1,000" in text
    assert "Exact token usage is not available" not in text


def test_history_summary_copy_diagnostic_only() -> None:
    view = HistoryViewResult(
        series=(),
        diagnostic="schema mismatch",
        range_label="30d",
        filter_label="All",
    )
    text = build_history_summary_text(view, identity)
    assert text == "schema mismatch"
