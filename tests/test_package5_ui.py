"""Package 5: GTK render-path tests — compact vs full quota-card rendering,
translated Disabled state, and the Diagnostics page. Run only when a display
exists (skip-guard); assertions are locale-agnostic (compared against tr())."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import gi  # type: ignore[import-untyped]
import pytest

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib  # type: ignore[import-untyped]  # noqa: E402

from moira.i18n import tr
from moira.models import QuotaReading, QuotaStatus, Service

NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)
RESET = NOW + timedelta(days=5)


def _reading(pct: float) -> QuotaReading:
    return QuotaReading(Service.CLAUDE, "Weekly", pct, RESET, NOW, "fixture", QuotaStatus.AVAILABLE)


def _rows_texts(rows: Any) -> list[str]:
    texts: list[str] = []
    child = rows.get_first_child()
    while child is not None:
        text = child.get_text() if hasattr(child, "get_text") else None
        texts.append(text or "<widget>")
        child = child.get_next_sibling()
    return texts


def _all_texts(widget: Any) -> list[str]:
    """Collect label/button texts from the widget and all descendants."""
    texts: list[str] = []
    stack = [widget]
    while stack:
        current = stack.pop()
        if hasattr(current, "get_text"):
            text = current.get_text()
            if text:
                texts.append(text)
        child = current.get_first_child()
        while child is not None:
            stack.append(child)
            child = child.get_next_sibling()
    return texts


def _card() -> Any:
    from moira.ui import QuotaCard

    try:
        return QuotaCard(tr("Claude"))
    except Exception as exc:  # headless environments
        pytest.skip(f"GTK display unavailable: {exc}")


def test_full_mode_renders_used_remaining_and_reset() -> None:
    card = _card()
    card.show_readings([_reading(45.0)], None)
    texts = _rows_texts(card.rows)
    assert any(f"45% {tr('used')}" in t for t in texts)
    assert any(f"55% {tr('remaining')}" in t for t in texts)
    assert any(tr("Resets ") in t for t in texts)
    assert any(tr("in ") in t for t in texts)
    # Full mode keeps the progress bar (3 children per reading row).
    assert len(texts) == 3


def test_compact_mode_keeps_provider_status_and_reset() -> None:
    card = _card()
    card.set_compact(True)
    card.show_readings([_reading(45.0)], None)
    texts = _rows_texts(card.rows)
    # One compact line per reading.
    assert len(texts) == 1
    line = texts[0]
    assert "Weekly" in line
    assert f"45% {tr('used')}" in line
    assert f"55% {tr('remaining')}" in line
    assert tr("resets in ") in line
    # Status line still present (provider + status never hidden).
    assert card.status.get_text() == tr("Available")


def test_disabled_card_shows_translated_disabled() -> None:
    card = _card()
    card.show_disabled()
    assert card.status.get_text() == tr("Disabled")
    assert _rows_texts(card.rows) == []


def test_exhausted_card_keeps_reset_countdown() -> None:
    card = _card()
    from moira.exhaustion import derive_service

    snapshot = derive_service(Service.CLAUDE, [_reading(100.0)], now=NOW)
    card.show_readings([_reading(100.0)], snapshot)
    texts = _rows_texts(card.rows)
    assert card.status.get_text() == tr("Weekly quota exhausted — usage blocked until reset")
    assert any(tr("in ") in t for t in texts)
    assert any("100%" in t and tr("used") in t for t in texts)


def test_diagnostics_page_updates_text() -> None:
    from moira.ui import DiagnosticsPage

    try:
        page = DiagnosticsPage()
    except Exception as exc:  # headless environments
        pytest.skip(f"GTK display unavailable: {exc}")
    page.update("Moira 0.2.2\nClaude: enabled")
    assert page._label.get_text() == "Moira 0.2.2\nClaude: enabled"
    assert page._text == "Moira 0.2.2\nClaude: enabled"


def test_history_page_has_export_delete_buttons() -> None:
    from moira.history_page import HistoryPage

    class _DummyExecutor:
        def submit(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("no executor submits expected in render tests")

    try:
        page = HistoryPage(_DummyExecutor())
    except Exception as exc:  # headless environments
        pytest.skip(f"GTK display unavailable: {exc}")
    joined = " | ".join(_all_texts(page))
    assert tr("Export CSV") in joined
    assert tr("Export JSON") in joined
    assert tr("Copy history summary") in joined
    assert tr("Delete all history…") in joined


def _page() -> Any:
    from moira.history_page import HistoryPage

    class _DummyExecutor:
        def submit(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("no executor submits expected in render tests")

    try:
        return HistoryPage(_DummyExecutor())
    except Exception as exc:  # headless environments
        pytest.skip(f"GTK display unavailable: {exc}")


def test_export_dialog_cancel_writes_nothing() -> None:
    """Cancelling the save dialog shows 'Export cancelled.' and writes nothing."""
    page = _page()

    class _FakeDialog:
        def save_finish(self, result: object) -> None:
            raise GLib.Error("cancelled", Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED)

    page._on_export_dialog_done(_FakeDialog(), None, "csv")
    assert page._export_status.get_text() == tr("Export cancelled.")
    assert page._exporting is False


def test_export_dialog_failure_is_sanitized() -> None:
    page = _page()

    class _FakeDialog:
        def save_finish(self, result: object) -> None:
            raise GLib.Error("boom", Gio.io_error_quark(), Gio.IOErrorEnum.FAILED)

    page._on_export_dialog_done(_FakeDialog(), None, "csv")
    assert page._export_status.get_text() == tr("Export failed.")
    assert "boom" not in page._export_status.get_text()


def test_export_done_failure_is_sanitized() -> None:
    import concurrent.futures

    page = _page()
    future: concurrent.futures.Future[int] = concurrent.futures.Future()
    future.set_exception(RuntimeError("disk full on /dev/sda"))
    page._export_done(future)
    assert page._export_status.get_text() == tr("Export failed.")
    assert "disk full" not in page._export_status.get_text()


def test_delete_done_outcomes() -> None:
    import concurrent.futures

    page = _page()
    page.refresh = lambda: None
    future: concurrent.futures.Future[int] = concurrent.futures.Future()
    future.set_result(5)
    page._delete_done(future)
    assert page._export_status.get_text() == tr("History deleted.")
    empty: concurrent.futures.Future[int] = concurrent.futures.Future()
    empty.set_result(0)
    page._delete_done(empty)
    assert page._export_status.get_text() == tr("History is already empty.")
    failed: concurrent.futures.Future[int] = concurrent.futures.Future()
    failed.set_exception(RuntimeError("locked"))
    page._delete_done(failed)
    assert page._export_status.get_text() == tr("Deletion failed.")
    assert "locked" not in page._export_status.get_text()
