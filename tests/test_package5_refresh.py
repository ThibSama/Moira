"""Package 5: collection toggles (zero/one/two providers), disabled-provider
isolation, per-channel dispatch, deliver-record semantics, and test-button
dedup-state neutrality. Deterministic harnesses — no sleeps, no network."""

from __future__ import annotations

import concurrent.futures
import threading
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

from moira.history import HistoryStatus
from moira.models import (
    CollectorResult,
    QuotaReading,
    QuotaStatus,
    Service,
    TokenAvailabilityRecord,
)
from moira.ntfy import Notification, NtfyResult
from moira.persistence import AppState, Settings, load_settings, save_settings


class FakeLabel:
    def __init__(self) -> None:
        self.text = ""

    def set_text(self, text: str) -> None:
        self.text = text

    def get_text(self) -> str:
        return self.text


class FakeRows:
    def get_first_child(self) -> None:
        return None

    def remove(self, child: Any) -> None:
        pass

    def append(self, child: Any) -> None:
        pass


class FakeCard:
    def __init__(self) -> None:
        self.status = FakeLabel()
        self.rows = FakeRows()
        self.updated = FakeLabel()
        self.last_show: Any = None

    def show_readings(self, readings: list[QuotaReading], snapshot: Any = None) -> None:
        self.last_show = ("readings", list(readings), snapshot)

    def show_disabled(self) -> None:
        self.last_show = ("disabled",)

    def set_compact(self, compact: bool) -> None:
        pass


class FakeDiag:
    def __init__(self) -> None:
        self.text = ""

    def update(self, text: str) -> None:
        self.text = text


class RecordingCoordinator:
    def __init__(self) -> None:
        self.batches: list[list[Any]] = []
        self.status = "ok"
        self.lifecycle_state = "running"

    def enqueue(self, readings: list[Any], now: datetime) -> bool:
        self.batches.append(list(readings))
        return True

    def start(self) -> None:
        pass

    def set_write_success_callback(self, callback: Any) -> None:
        pass

    def clear_write_success_callback(self) -> None:
        pass

    def shutdown(self, *, timeout: float | None = None) -> None:
        pass


class RecordingExecutor:
    def __init__(self) -> None:
        self.submitted: list[tuple[Any, tuple[Any, ...], dict[str, Any], Any]] = []

    def submit(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        self.submitted.append((fn, args, kwargs, future))
        return future


class DirectExecutor:
    def submit(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001
            future.set_exception(exc)
        return future


class FakeApp:
    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []

    def send_notification(self, key: str, notification: Any) -> None:
        self.sent.append((key, notification))


class FakeEntry:
    def __init__(self, text: str = "") -> None:
        self._text = text

    def get_text(self) -> str:
        return self._text


NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)


def reading(
    service: Service, pct: float | None, status: QuotaStatus = QuotaStatus.AVAILABLE
) -> QuotaReading:
    from datetime import timedelta

    return QuotaReading(
        service,
        "Weekly",
        pct,
        NOW + timedelta(days=5) if pct is not None else None,
        NOW,
        "fixture",
        status,
    )


def _submit_window(settings: Settings) -> tuple[Any, RecordingExecutor]:
    from moira.ui import MainWindow

    window = MainWindow.__new__(MainWindow)
    window.settings = settings
    window.pending = []
    window.pending_tokens = []
    window.pending_summary = None
    window.pending_availability = []
    window.pending_lock = threading.Lock()
    window.completed = 0
    window._enabled_services = []
    executor = RecordingExecutor()
    window.executor = executor  # type: ignore[assignment]
    return window, executor


def _finish_window(
    settings: Settings,
    previous: list[QuotaReading],
    pending: list[QuotaReading],
) -> tuple[Any, RecordingExecutor]:
    from moira.ui import MainWindow

    window = MainWindow.__new__(MainWindow)
    window.settings = settings
    window.state = AppState(
        readings=list(previous), alert_keys=[], last_refresh=None, next_refresh=None
    )
    window.pending = list(pending)
    window.pending_tokens = []
    window.pending_summary = None
    window.pending_availability = []
    window.pending_lock = threading.Lock()
    window.completed = 0
    window._enabled_services = settings.enabled_services()
    window.refreshing = True
    window._next_refresh_time = 0.0
    window._history_coordinator = RecordingCoordinator()  # type: ignore[assignment]
    executor = RecordingExecutor()
    window.executor = executor  # type: ignore[assignment]
    window.claude_card = FakeCard()  # type: ignore[assignment]
    window.codex_card = FakeCard()  # type: ignore[assignment]
    window.refresh_info = FakeLabel()
    window._diagnostics_page = FakeDiag()  # type: ignore[assignment]
    window.get_application = lambda: None
    return window, executor


# ── Zero / one / two enabled providers ──


def test_zero_providers_submits_nothing_and_finishes() -> None:
    settings = Settings(collect_claude=False, collect_codex=False)
    window, executor = _submit_window(settings)
    with patch("moira.ui.GLib.idle_add", return_value=0) as idle:
        window._submit_collectors()
    assert executor.submitted == []
    assert window._enabled_services == []
    assert idle.call_count == 1  # immediate finish scheduled


def test_one_provider_submits_only_that_collector() -> None:
    settings = Settings(collect_claude=False, collect_codex=True)
    window, executor = _submit_window(settings)
    with patch("moira.ui.GLib.idle_add", return_value=0):
        window._submit_collectors()
    assert len(executor.submitted) == 1
    future = executor.submitted[0][3]
    future.set_result(
        CollectorResult(
            Service.CODEX,
            (),
            (),
            token_availability_records=(
                TokenAvailabilityRecord(
                    Service.CODEX, NOW, "codex-app-server", HistoryStatus.UNSUPPORTED
                ),
            ),
        )
    )
    assert window.completed == 1


def test_two_providers_submit_both_and_complete_once() -> None:
    settings = Settings(collect_claude=True, collect_codex=True)
    window, executor = _submit_window(settings)
    with patch("moira.ui.GLib.idle_add", return_value=0) as idle:
        window._submit_collectors()
        assert len(executor.submitted) == 2
        for _, _, _, future in executor.submitted:
            future.set_result(
                CollectorResult(
                    Service.CODEX,
                    (),
                    (),
                    token_availability_records=(
                        TokenAvailabilityRecord(Service.CODEX, NOW, "s", HistoryStatus.UNSUPPORTED),
                    ),
                )
            )
        assert window.completed == 2
        assert idle.call_count == 1


# ── Disabled-provider isolation in _finish_refresh ──


def test_disabled_provider_keeps_previous_readings_unchanged(tmp_path: Any) -> None:
    settings = Settings(collect_claude=False, collect_codex=True, ntfy_enabled=True)
    previous = [reading(Service.CLAUDE, 80.0), reading(Service.CODEX, 40.0)]
    pending = [reading(Service.CODEX, 45.0)]
    window, executor = _finish_window(settings, previous, pending)
    with (
        patch.dict("os.environ", {"XDG_STATE_HOME": str(tmp_path)}),
        patch("moira.ui.GLib.idle_add", return_value=0),
    ):
        window._finish_refresh()
    merged = window.state.readings
    claude_rows = [r for r in merged if r.service is Service.CLAUDE]
    codex_rows = [r for r in merged if r.service is Service.CODEX]
    # Disabled Claude: previous AVAILABLE reading preserved, never marked stale.
    assert len(claude_rows) == 1
    assert claude_rows[0].percentage == 80.0
    assert claude_rows[0].status is QuotaStatus.AVAILABLE
    # Enabled Codex: fresh reading applied.
    assert len(codex_rows) == 1
    assert codex_rows[0].percentage == 45.0
    # No Claude alerts (its collector never ran → no fresh Claude readings).
    assert not any(r.service is Service.CLAUDE for r in window.pending)
    # No fresh history for the disabled provider.
    assert executor.submitted == []
    batch = window._history_coordinator.batches[0]
    assert not any(
        isinstance(item, QuotaReading) and item.service is Service.CLAUDE for item in batch
    )


def test_zero_provider_finish_keeps_everything(tmp_path: Any) -> None:
    settings = Settings(collect_claude=False, collect_codex=False, ntfy_enabled=True)
    previous = [reading(Service.CLAUDE, 80.0)]
    window, _ = _finish_window(settings, previous, [])
    with (
        patch.dict("os.environ", {"XDG_STATE_HOME": str(tmp_path)}),
        patch("moira.ui.GLib.idle_add", return_value=0),
    ):
        window._finish_refresh()
    assert [r.percentage for r in window.state.readings] == [80.0]
    assert window._history_coordinator.batches[0] == []


# ── Per-channel dispatch in _finish_refresh ──


def test_finish_refresh_dispatches_per_channel(tmp_path: Any) -> None:
    settings = Settings(
        collect_claude=True,
        collect_codex=True,
        ntfy_enabled=True,
        native_notifications=True,
        thresholds=[50],
    )
    previous = [reading(Service.CLAUDE, 49.0), reading(Service.CODEX, 49.0)]
    pending = [reading(Service.CLAUDE, 76.0), reading(Service.CODEX, 76.0)]
    window, executor = _finish_window(settings, previous, pending)
    with (
        patch.dict("os.environ", {"XDG_STATE_HOME": str(tmp_path)}),
        patch("moira.ui.GLib.idle_add", return_value=0),
    ):
        window._finish_refresh()
    # 2 events (Claude + Codex threshold 50) × 2 channels.
    from moira.ui import MainWindow

    ntfy_submissions = [
        s for s in executor.submitted if getattr(s[0], "__func__", s[0]) is MainWindow._deliver_ntfy
    ]
    assert len(ntfy_submissions) == 2
    for _fn, args, _, _ in ntfy_submissions:
        key = args[0]
        assert key.endswith(":ntfy")


# ── Deliver + record semantics ──


def test_deliver_ntfy_records_only_after_success(tmp_path: Any) -> None:
    window, _ = _finish_window(Settings(), [], [])
    key = "exhausted:claude:Weekly:2026-08-07T12:00:00+00:00:ntfy"
    with (
        patch("moira.ui.send", return_value=NtfyResult(True, "sent")) as send_mock,
        patch("moira.ui.GLib.idle_add", side_effect=lambda cb, k: cb(k)),
        patch.dict("os.environ", {"XDG_STATE_HOME": str(tmp_path)}),
    ):
        window._deliver_ntfy(key, Notification("t", "m"))
    assert send_mock.call_count == 1
    assert key in window.state.alert_keys

    # Failure → no record, no repeat suppression.
    window.state.alert_keys = []
    with (
        patch("moira.ui.send", return_value=NtfyResult(False, "network failure")),
        patch("moira.ui.GLib.idle_add", return_value=0) as idle,
    ):
        window._deliver_ntfy(key, Notification("t", "m"))
    assert window.state.alert_keys == []
    assert idle.call_count == 0


def test_deliver_native_records_after_success(tmp_path: Any) -> None:
    window, _ = _finish_window(Settings(), [], [])
    app = FakeApp()
    window.get_application = lambda: app
    key = "exhausted:codex:Weekly:2026-08-07T12:00:00+00:00:native"
    with patch.dict("os.environ", {"XDG_STATE_HOME": str(tmp_path)}):
        window._deliver_native(key, Notification("t", "m"))
    assert [k for k, _ in app.sent] == [key]
    assert key in window.state.alert_keys


def test_deliver_native_without_application_records_nothing() -> None:
    window, _ = _finish_window(Settings(), [], [])
    window._deliver_native("key:native", Notification("t", "m"))
    assert window.state.alert_keys == []


# ── Test buttons never change dedup state ──


def test_test_notifications_never_change_dedup_state(tmp_path: Any) -> None:
    window, _ = _finish_window(Settings(ntfy_enabled=True), [], [])
    window.executor = DirectExecutor()
    window._read_form = lambda: Settings(ntfy_enabled=True)
    window.token = FakeEntry()
    window.settings_status = FakeLabel()
    window.update_status = FakeLabel()
    app = FakeApp()
    window.get_application = lambda: app
    with (
        patch("moira.ui.send", return_value=NtfyResult(True, "sent")),
        patch("moira.ui.GLib.idle_add", side_effect=lambda cb, *a: cb(*a)),
        patch.dict("os.environ", {"XDG_STATE_HOME": str(tmp_path)}),
    ):
        window._test_notification()
        window._test_native_notification()
    assert [k for k, _ in app.sent] == ["moira-native-test"]
    from moira.i18n import tr

    assert window.settings_status.text == tr("Test notification sent.")
    assert window.state.alert_keys == []


# ── Geometry persistence ──


def test_geometry_fields_round_trip(tmp_path: Any) -> None:
    settings = Settings(window_width=800, window_height=600, window_maximized=True)
    with patch.dict("os.environ", {"XDG_CONFIG_HOME": str(tmp_path)}):
        save_settings(settings)
        loaded = load_settings()
    assert loaded.window_width == 800
    assert loaded.window_height == 600
    assert loaded.window_maximized is True


def test_compact_mode_round_trip(tmp_path: Any) -> None:
    settings = Settings(compact_mode=True)
    with patch.dict("os.environ", {"XDG_CONFIG_HOME": str(tmp_path)}):
        save_settings(settings)
        loaded = load_settings()
    assert loaded.compact_mode is True


def test_no_position_fields_exist() -> None:
    """GTK 4 / PyGObject exposes no window-position API here and Wayland
    provides none; Moira persists size + maximized only, truthfully."""
    settings = Settings()
    assert not hasattr(settings, "window_x")
    assert not hasattr(settings, "window_y")
