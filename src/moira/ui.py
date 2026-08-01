from __future__ import annotations

import concurrent.futures
import threading
from datetime import datetime
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .alerts import evaluate_alerts, merge_with_stale
from .autostart import set_enabled as set_autostart
from .claude_integration import remove as remove_claude_integration
from .claude_integration import setup as setup_claude_integration
from .collectors import ClaudeCollector, CodexCollector
from .desktop import create_shortcut, remove_shortcut
from .models import QuotaReading, QuotaStatus
from .ntfy import Notification, send
from .persistence import Settings, load_settings, load_state, save_settings, save_state
from .secrets import get_ntfy_token, set_ntfy_token


def format_countdown(reset_at: datetime, now: datetime | None = None) -> str:
    local_now = now or datetime.now().astimezone()
    seconds = max(0, int((reset_at.astimezone() - local_now).total_seconds()))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    return f"{days}d {hours}h {minutes}m" if days else f"{hours}h {minutes}m"


class QuotaCard(Gtk.Frame):
    def __init__(self, title: str) -> None:
        super().__init__()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)
        self.heading = Gtk.Label(label=title, xalign=0)
        self.heading.add_css_class("title-3")
        self.status = Gtk.Label(label="Loading…", xalign=0)
        self.status.set_wrap(True)
        self.rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.updated = Gtk.Label(label="Not refreshed yet", xalign=0)
        self.updated.add_css_class("dim-label")
        box.append(self.heading)
        box.append(self.status)
        box.append(self.rows)
        box.append(self.updated)
        self.set_child(box)

    def show_readings(self, readings: list[QuotaReading]) -> None:
        while child := self.rows.get_first_child():
            self.rows.remove(child)
        if not readings:
            self.status.set_text("Unavailable — no reading")
            return
        status = readings[0].status
        if status not in {QuotaStatus.AVAILABLE, QuotaStatus.STALE}:
            self.status.set_text(f"{status.value.replace('_', ' ').title()}: {readings[0].detail}")
            return
        self.status.set_text(
            "Stale — showing last successful values" if status is QuotaStatus.STALE else "Available"
        )
        for reading in readings:
            if reading.percentage is None or reading.reset_at is None:
                continue
            title = Gtk.Label(label=f"{reading.quota_label} — {reading.percentage:.0f}%", xalign=0)
            title.add_css_class("heading")
            progress = Gtk.ProgressBar(fraction=reading.percentage / 100)
            reset = reading.reset_at.astimezone()
            reset_text = reset.strftime("%a %d %b %Y, %H:%M %Z")
            detail = Gtk.Label(
                label=f"Resets {reset_text} · {format_countdown(reset)} remaining",
                xalign=0,
            )
            detail.set_wrap(True)
            detail.add_css_class("dim-label")
            self.rows.append(title)
            self.rows.append(progress)
            self.rows.append(detail)
        latest = max(item.retrieved_at for item in readings).astimezone()
        self.updated.set_text(
            f"Last refresh: {latest.strftime('%H:%M:%S')} · Source: {readings[0].source}"
        )


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application, smoke_test: bool = False) -> None:
        super().__init__(
            application=application, title="Moira", default_width=620, default_height=680
        )
        self.settings = load_settings()
        self.state = load_state()
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="moira"
        )
        self.refreshing = False
        self.pending: list[QuotaReading] = []
        self.pending_lock = threading.Lock()
        self.completed = 0
        self._build()
        self._render()
        if not smoke_test:
            GLib.idle_add(self.refresh)
            GLib.timeout_add_seconds(self.settings.refresh_minutes * 60, self._scheduled_refresh)

    def _build(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        header = Adw.HeaderBar()
        stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher(stack=stack)
        header.set_title_widget(switcher)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh now")
        refresh.connect("clicked", lambda *_: self.refresh())
        header.pack_end(refresh)
        about = Gtk.Button(icon_name="help-about-symbolic", tooltip_text="About Moira")
        about.connect("clicked", self._about)
        header.pack_end(about)
        root.append(header)
        root.append(stack)
        home = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        home.set_margin_top(18)
        home.set_margin_bottom(18)
        home.set_margin_start(18)
        home.set_margin_end(18)
        self.claude_card = QuotaCard("Claude")
        self.codex_card = QuotaCard("Codex")
        home.append(self.claude_card)
        home.append(self.codex_card)
        stack.add_titled(home, "home", "Quotas")
        stack.add_titled(self._settings_page(), "notifications", "Notifications")
        self.set_content(root)

    def _settings_page(self) -> Gtk.Widget:
        page = Gtk.ScrolledWindow()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(18)
        box.set_margin_bottom(18)
        box.set_margin_start(18)
        box.set_margin_end(18)
        self.ntfy_enabled = Gtk.Switch(active=self.settings.ntfy_enabled)
        box.append(self._row("Enable NTFY alerts", self.ntfy_enabled))
        self.server = Gtk.Entry(text=self.settings.ntfy_server)
        box.append(self._labeled("Server URL", self.server))
        self.topic = Gtk.Entry(text=self.settings.ntfy_topic)
        box.append(self._labeled("Topic", self.topic))
        self.token = Gtk.PasswordEntry(placeholder_text="Leave blank to keep current keyring token")
        box.append(self._labeled("Optional access token", self.token))
        self.thresholds = Gtk.Entry(text=", ".join(map(str, self.settings.thresholds)))
        box.append(self._labeled("Thresholds (%)", self.thresholds))
        self.reset_alerts = Gtk.Switch(active=self.settings.reset_alerts)
        box.append(self._row("Alert when a quota resets", self.reset_alerts))
        self.error_alerts = Gtk.Switch(active=self.settings.error_alerts)
        box.append(self._row("Alert on refresh errors", self.error_alerts))
        self.autostart = Gtk.Switch(active=self.settings.autostart)
        box.append(self._row("Start automatically on login", self.autostart))
        buttons = Gtk.Box(spacing=8)
        save = Gtk.Button(label="Save settings")
        save.add_css_class("suggested-action")
        save.connect("clicked", self._save_settings)
        test = Gtk.Button(label="Send test notification")
        test.connect("clicked", self._test_notification)
        buttons.append(save)
        buttons.append(test)
        box.append(buttons)
        integration_buttons = Gtk.Box(spacing=8)
        setup_claude = Gtk.Button(label="Set up Claude integration")
        setup_claude.connect("clicked", self._setup_claude)
        remove_claude = Gtk.Button(label="Remove Claude integration")
        remove_claude.connect("clicked", self._remove_claude)
        integration_buttons.append(setup_claude)
        integration_buttons.append(remove_claude)
        box.append(integration_buttons)
        shortcut_buttons = Gtk.Box(spacing=8)
        create_desktop = Gtk.Button(label="Create desktop shortcut")
        create_desktop.connect("clicked", self._create_desktop_shortcut)
        remove_desktop = Gtk.Button(label="Remove desktop shortcut")
        remove_desktop.connect("clicked", self._remove_desktop_shortcut)
        shortcut_buttons.append(create_desktop)
        shortcut_buttons.append(remove_desktop)
        box.append(shortcut_buttons)
        self.settings_status = Gtk.Label(xalign=0)
        self.settings_status.set_wrap(True)
        box.append(self.settings_status)
        page.set_child(box)
        return page

    @staticmethod
    def _labeled(label: str, widget: Gtk.Widget) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.append(Gtk.Label(label=label, xalign=0))
        box.append(widget)
        return box

    @staticmethod
    def _row(label: str, widget: Gtk.Widget) -> Gtk.Widget:
        box = Gtk.Box(spacing=12)
        text = Gtk.Label(label=label, xalign=0, hexpand=True)
        box.append(text)
        box.append(widget)
        return box

    def _render(self) -> None:
        self.claude_card.show_readings(
            [r for r in self.state.readings if r.service.value == "claude"]
        )
        self.codex_card.show_readings(
            [r for r in self.state.readings if r.service.value == "codex"]
        )

    def refresh(self) -> bool:
        if self.refreshing:
            return False
        self.refreshing = True
        self.pending = []
        self.completed = 0
        self.claude_card.status.set_text("Loading…")
        self.codex_card.status.set_text("Loading…")
        for collector in (ClaudeCollector(), CodexCollector()):
            future = self.executor.submit(collector.collect)
            future.add_done_callback(self._collector_done)
        return False

    def _collector_done(self, future: concurrent.futures.Future[list[QuotaReading]]) -> None:
        try:
            result = future.result()
        except Exception:
            result = []
        with self.pending_lock:
            self.pending.extend(result)
            self.completed += 1
            complete = self.completed == 2
        if complete:
            GLib.idle_add(self._finish_refresh)

    def _finish_refresh(self) -> bool:
        previous = self.state.readings
        merged = merge_with_stale(previous, self.pending)
        if self.settings.ntfy_enabled:
            alerts = evaluate_alerts(
                previous, self.pending, self.settings, set(self.state.alert_keys)
            )
            for alert in alerts:
                self.executor.submit(self._deliver_alert, alert.key, alert.notification)
        self.state.readings = merged
        save_state(self.state)
        self.refreshing = False
        self._render()
        return False

    def _deliver_alert(self, key: str, notification: Notification) -> None:
        try:
            send(
                self.settings.ntfy_server, self.settings.ntfy_topic, notification, get_ntfy_token()
            )
        except Exception:
            return
        GLib.idle_add(self._record_alert, key)

    def _record_alert(self, key: str) -> bool:
        if key not in self.state.alert_keys:
            self.state.alert_keys.append(key)
            save_state(self.state)
        return False

    def _scheduled_refresh(self) -> bool:
        self.refresh()
        return True

    def _read_form(self) -> Settings:
        values = [
            int(part.strip()) for part in self.thresholds.get_text().split(",") if part.strip()
        ]
        settings = Settings(
            ntfy_server=self.server.get_text().strip(),
            ntfy_topic=self.topic.get_text().strip(),
            ntfy_enabled=self.ntfy_enabled.get_active(),
            thresholds=values,
            reset_alerts=self.reset_alerts.get_active(),
            error_alerts=self.error_alerts.get_active(),
            autostart=self.autostart.get_active(),
            refresh_minutes=self.settings.refresh_minutes,
        )
        settings.validate()
        return settings

    def _save_settings(self, *_: Any) -> None:
        try:
            settings = self._read_form()
            token = self.token.get_text()
            if token:
                set_ntfy_token(token)
                self.token.set_text("")
            save_settings(settings)
            set_autostart(settings.autostart)
            self.settings = settings
            self.settings_status.set_text("Settings saved. Token is stored only in GNOME Keyring.")
        except Exception as exc:
            self.settings_status.set_text(f"Could not save settings: {exc}")

    def _test_notification(self, *_: Any) -> None:
        try:
            settings = self._read_form()
        except Exception as exc:
            self.settings_status.set_text(f"Invalid settings: {exc}")
            return
        self.settings_status.set_text("Sending test…")
        future = self.executor.submit(
            send,
            settings.ntfy_server,
            settings.ntfy_topic,
            Notification(
                "Moira test", "Notifications are configured correctly.", "white_check_mark"
            ),
            self.token.get_text() or get_ntfy_token(),
        )
        future.add_done_callback(lambda done: GLib.idle_add(self._test_done, done.exception()))

    def _test_done(self, error: BaseException | None) -> bool:
        self.settings_status.set_text(
            "Test notification sent." if error is None else f"Test failed: {error}"
        )
        return False

    def _setup_claude(self, *_: Any) -> None:
        try:
            changed = setup_claude_integration()
            self.settings_status.set_text(
                "Claude integration installed. Complete one Claude response to populate quotas."
                if changed
                else "Claude integration is already installed."
            )
        except Exception as exc:
            self.settings_status.set_text(f"Claude integration was not changed: {exc}")

    def _remove_claude(self, *_: Any) -> None:
        try:
            changed = remove_claude_integration()
            self.settings_status.set_text(
                "Claude integration removed and the previous status line restored."
                if changed
                else "Claude integration is not installed."
            )
        except Exception as exc:
            self.settings_status.set_text(f"Claude integration was not changed: {exc}")

    def _create_desktop_shortcut(self, *_: Any) -> None:
        try:
            target, changed = create_shortcut()
            action = "created" if changed else "already exists"
            self.settings_status.set_text(f"Desktop shortcut {action}: {target}")
        except Exception as exc:
            self.settings_status.set_text(f"Desktop shortcut is unavailable: {exc}")

    def _remove_desktop_shortcut(self, *_: Any) -> None:
        try:
            changed = remove_shortcut()
            self.settings_status.set_text(
                "Desktop shortcut removed." if changed else "Desktop shortcut is already absent."
            )
        except Exception as exc:
            self.settings_status.set_text(f"Desktop shortcut is unavailable: {exc}")

    def _about(self, *_: Any) -> None:
        dialog = Adw.AboutDialog(
            application_name="Moira",
            application_icon="io.github.moira.QuotaMonitor",
            version="0.1.1",
            developer_name="Moira contributors",
            license_type=Gtk.License.MIT_X11,
            comments="Claude and Codex quota monitor for Ubuntu",
        )
        dialog.present(self)
