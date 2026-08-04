"""Package 5b: UI-save preservation of non-editable settings, and sanitized
test-button failures (keyring, future, Gio) with fixed translated outcomes.
Deterministic harnesses — no sleeps, no network, no real keyring."""

from __future__ import annotations

import concurrent.futures
from typing import Any
from unittest.mock import patch

from moira.i18n import tr
from moira.ntfy import NtfyResult
from moira.persistence import Settings


class FakeLabel:
    def __init__(self) -> None:
        self.text = ""

    def set_text(self, text: str) -> None:
        self.text = text

    def get_text(self) -> str:
        return self.text


class FakeEntry:
    def __init__(self, text: str = "") -> None:
        self._text = text

    def get_text(self) -> str:
        return self._text

    def set_text(self, text: str) -> None:
        self._text = text


class FakeSwitch:
    def __init__(self, active: bool) -> None:
        self._active = active

    def get_active(self) -> bool:
        return self._active


class FakeCombo:
    def __init__(self, selected: int) -> None:
        self._selected = selected

    def get_selected(self) -> int:
        return self._selected


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
        self.raise_on_send = False

    def send_notification(self, key: str, notification: Any) -> None:
        if self.raise_on_send:
            raise RuntimeError("Gio delivery failed on the bus")
        self.sent.append((key, notification))


def _window() -> Any:
    from moira.ui import MainWindow

    window = MainWindow.__new__(MainWindow)
    window.settings = Settings(
        repo="Custom/Repo",
        window_width=900,
        window_height=700,
        window_maximized=True,
    )
    window.settings.validate()
    window.state = None  # type: ignore[assignment]  # unused by these paths
    window.executor = DirectExecutor()  # type: ignore[assignment]
    window.settings_status = FakeLabel()
    window.token = FakeEntry()
    return window


def _form_window() -> Any:
    window = _window()
    window._thresholds_entries = {
        "claude": FakeEntry("50, 75"),
        "codex": FakeEntry("90"),
    }
    window._reset_switches = {
        "claude": FakeSwitch(True),
        "codex": FakeSwitch(False),
    }
    window._error_switches = {
        "claude": FakeSwitch(True),
        "codex": FakeSwitch(True),
    }
    window.refresh_combo = FakeCombo(1)
    window.server = FakeEntry("https://ntfy.sh")
    window.topic = FakeEntry("my-topic")
    window.ntfy_enabled = FakeSwitch(True)
    window.native_notifications = FakeSwitch(True)
    window.collect_claude = FakeSwitch(True)
    window.collect_codex = FakeSwitch(False)
    window.compact_mode = FakeSwitch(True)
    window.autostart = FakeSwitch(False)
    return window


# ── UI save preserves every non-editable field ──


def test_read_form_preserves_repo_and_geometry() -> None:
    """Saving normal settings must never reset the repository or the
    current geometry/maximized state — they are not editable in the form."""
    window = _form_window()
    settings = window._read_form()
    assert settings.repo == "Custom/Repo"
    assert settings.window_width == 900
    assert settings.window_height == 700
    assert settings.window_maximized is True
    # Editable fields still reflect the form.
    assert settings.ntfy_server == "https://ntfy.sh"
    assert settings.ntfy_topic == "my-topic"
    assert settings.ntfy_enabled is True
    assert settings.native_notifications is True
    assert settings.collect_claude is True
    assert settings.collect_codex is False
    assert settings.compact_mode is True
    assert settings.autostart is False
    assert settings.rules_for("claude").thresholds == [50, 75]
    assert settings.rules_for("codex").thresholds == [90]
    assert settings.rules_for("codex").reset_alerts is False
    settings.validate()  # the merged result is a valid v3 configuration


def test_read_form_preserves_null_geometry() -> None:
    window = _form_window()
    window.settings = Settings()  # fresh install: no geometry yet
    settings = window._read_form()
    assert settings.window_width is None
    assert settings.window_height is None
    assert settings.window_maximized is False
    settings.validate()


# ── Test buttons: keyring / future / Gio failures are sanitized ──


def test_ntfy_test_keyring_failure_is_sanitized() -> None:
    window = _form_window()
    window._read_form = lambda: Settings(ntfy_enabled=True)
    with patch(
        "moira.ui.get_ntfy_token", side_effect=RuntimeError("secret vault locked")
    ) as keyring:
        window._test_notification()
    assert keyring.call_count == 1
    assert window.settings_status.text == tr("Test failed: keyring unavailable.")
    assert "vault" not in window.settings_status.text
    assert "locked" not in window.settings_status.text


def test_ntfy_test_delivery_failure_is_sanitized() -> None:
    window = _form_window()
    window._read_form = lambda: Settings(ntfy_enabled=True)
    with patch("moira.ui.get_ntfy_token", return_value=None):
        with patch("moira.ui.send", return_value=NtfyResult(False, "network failure")):
            with patch("moira.ui.GLib.idle_add", side_effect=lambda cb, *a: cb(*a)):
                window._test_notification()
    assert window.settings_status.text == f"{tr('Test failed: ')}{tr('network failure')}"


def test_ntfy_test_failed_future_is_sanitized() -> None:
    """A future that failed (executor error) maps to a fixed translated
    outcome — the raw exception never reaches the UI."""
    window = _window()
    window.settings_status = FakeLabel()
    future: concurrent.futures.Future[Any] = concurrent.futures.Future()
    future.set_exception(RuntimeError("boom on worker thread"))
    window._test_done(future)
    assert window.settings_status.text == tr("Test failed: notification unavailable.")
    assert "boom" not in window.settings_status.text


def test_native_test_gio_failure_is_sanitized() -> None:
    window = _window()
    app = FakeApp()
    app.raise_on_send = True
    window.get_application = lambda: app
    window._test_native_notification()
    assert window.settings_status.text == tr("Test failed: native notification unavailable.")
    assert "bus" not in window.settings_status.text
    assert app.sent == []


def test_native_test_success_still_sends() -> None:
    window = _window()
    app = FakeApp()
    window.get_application = lambda: app
    window._test_native_notification()
    assert [k for k, _ in app.sent] == ["moira-native-test"]
    assert window.settings_status.text == tr("Test notification sent.")


def test_test_failures_never_persist_alert_keys(tmp_path: Any) -> None:
    from moira.persistence import AppState, load_state, save_state

    window = _window()
    window.state = AppState(readings=[], alert_keys=["old:key"], last_refresh=None)
    window._read_form = lambda: Settings(ntfy_enabled=True)
    with patch("moira.ui.get_ntfy_token", side_effect=RuntimeError("locked")):
        window._test_notification()
    app = FakeApp()
    app.raise_on_send = True
    window.get_application = lambda: app
    window._test_native_notification()
    # Neither failure path touched the dedup state or persisted new keys.
    assert window.state.alert_keys == ["old:key"]
    with patch.dict("os.environ", {"XDG_STATE_HOME": str(tmp_path)}):
        save_state(window.state)
        loaded = load_state()
    assert loaded.alert_keys == ["old:key"]
