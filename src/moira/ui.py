from __future__ import annotations

import concurrent.futures
import threading
import time
from datetime import UTC, datetime
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
from .exhaustion import derive_state
from .history_db import HistoryCoordinator
from .history_page import HistoryPage
from .i18n import is_french, tr
from .models import QuotaReading, QuotaStatus, Service
from .ntfy import Notification, send
from .persistence import (
    VALID_REFRESH_MINUTES,
    Settings,
    load_settings,
    load_state,
    save_settings,
    save_state,
)
from .secrets import get_ntfy_token, set_ntfy_token

_ = tr


def format_countdown(reset_at: datetime, now: datetime | None = None) -> str:
    local_now = now or datetime.now().astimezone()
    seconds = max(0, int((reset_at.astimezone() - local_now).total_seconds()))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    return f"{days}d {hours}h {minutes}m" if days else f"{hours}h {minutes}m"


def format_local_datetime(dt: datetime) -> str:
    """Format a datetime in the user's locale and timezone."""
    local = dt.astimezone()
    if is_french():
        days_fr = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]
        months_fr = [
            "janv",
            "févr",
            "mars",
            "avr",
            "mai",
            "juin",
            "juil",
            "août",
            "sept",
            "oct",
            "nov",
            "déc",
        ]
        day_name = days_fr[local.weekday()]
        month_name = months_fr[local.month - 1]
        return f"{day_name} {local.day:02d} {month_name} {local.year} {local:%H:%M}"
    return local.strftime("%a %d %b %Y, %H:%M %Z")


class QuotaCard(Gtk.Frame):
    def __init__(self, title: str) -> None:
        super().__init__()
        self._title = title
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)
        self.heading = Gtk.Label(label=title, xalign=0)
        self.heading.add_css_class("title-3")
        self.status = Gtk.Label(label=_("Loading…"), xalign=0)
        self.status.set_wrap(True)
        self.rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.updated = Gtk.Label(label=_("Not refreshed yet"), xalign=0)
        self.updated.add_css_class("dim-label")
        box.append(self.heading)
        box.append(self.status)
        box.append(self.rows)
        box.append(self.updated)
        self.set_child(box)

    def show_readings(self, readings: list[QuotaReading], snapshot=None) -> None:
        while child := self.rows.get_first_child():
            self.rows.remove(child)
        if not readings:
            self.status.set_text(_("Unavailable — no reading"))
            return
        status = readings[0].status
        if status not in {QuotaStatus.AVAILABLE, QuotaStatus.STALE}:
            self.status.set_text(f"{status.value.replace('_', ' ').title()}: {readings[0].detail}")
            return

        # ── Exhaustion display ──
        if snapshot and snapshot.exhausted:
            self._show_exhausted(readings, snapshot)
            return

        self.status.set_text(
            _("Stale — showing last successful values")
            if status is QuotaStatus.STALE
            else _("Available")
        )
        for reading in readings:
            if reading.percentage is None or reading.reset_at is None:
                continue
            # For exhausted Claude, hide the five-hour row
            if (
                snapshot
                and snapshot.service is Service.CLAUDE
                and snapshot.exhausted
                and (
                    "five" in reading.quota_label.lower()
                    or "session" in reading.quota_label.lower()
                )
            ):
                continue
            self._add_reading_row(reading)
        latest = max(item.retrieved_at for item in readings).astimezone()
        self.updated.set_text(
            f"{_('Last refresh: ')}{latest.strftime('%H:%M:%S')}"
            f"{_(' · Source: ')}{readings[0].source}"
        )

    def _add_reading_row(self, reading: QuotaReading) -> None:
        title = Gtk.Label(label=f"{reading.quota_label} — {reading.percentage:.0f}%", xalign=0)
        title.add_css_class("heading")
        progress = Gtk.ProgressBar(fraction=reading.percentage / 100)
        reset = reading.reset_at.astimezone()
        reset_text = format_local_datetime(reset)
        detail = Gtk.Label(
            label=f"{_('Resets ')}{reset_text}{_(' remaining')}",
            xalign=0,
        )
        detail.set_wrap(True)
        detail.add_css_class("dim-label")
        self.rows.append(title)
        self.rows.append(progress)
        self.rows.append(detail)

    def _show_exhausted(self, readings: list[QuotaReading], snapshot) -> None:
        """Show exhaustion state for Claude or Codex."""
        weekly = snapshot.weekly
        if weekly is None or weekly.reset_at is None:
            self.status.set_text(_("Unavailable until weekly reset"))
            return
        if snapshot.service is Service.CLAUDE:
            self.status.set_text(_("Weekly quota exhausted — usage blocked until reset"))
        else:
            self.status.set_text(_("Unavailable until weekly reset"))

        # Show weekly reset time + countdown
        reset = weekly.reset_at.astimezone()
        reset_text = format_local_datetime(reset)
        title = Gtk.Label(label=f"{weekly.quota_label} — {(weekly.percentage or 0):.0f}%", xalign=0)
        title.add_css_class("heading")
        progress = Gtk.ProgressBar(fraction=1.0)
        progress.add_css_class("error")
        detail = Gtk.Label(
            label=f"{_('Resets ')}{reset_text}{_(' remaining')}",
            xalign=0,
        )
        detail.set_wrap(True)
        detail.add_css_class("dim-label")
        self.rows.append(title)
        self.rows.append(progress)
        self.rows.append(detail)

        # For Claude: visually disable the five-hour row
        if snapshot.service is Service.CLAUDE and snapshot.five_hour:
            fh = snapshot.five_hour
            if fh.percentage is not None and fh.reset_at is not None:
                fh_title = Gtk.Label(label=f"{fh.quota_label} — {fh.percentage:.0f}%", xalign=0)
                fh_title.add_css_class("dim-label")
                fh_progress = Gtk.ProgressBar(fraction=fh.percentage / 100)
                fh_progress.set_sensitive(False)
                fh_detail = Gtk.Label(
                    label=_("Five-hour quota disabled due to weekly exhaustion"),
                    xalign=0,
                )
                fh_detail.set_wrap(True)
                fh_detail.add_css_class("dim-label")
                self.rows.append(fh_title)
                self.rows.append(fh_progress)
                self.rows.append(fh_detail)

        latest = max(item.retrieved_at for item in readings).astimezone()
        self.updated.set_text(
            f"{_('Last refresh: ')}{latest.strftime('%H:%M:%S')}"
            f"{_(' · Source: ')}{readings[0].source}"
        )


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application, smoke_test: bool = False) -> None:
        super().__init__(
            application=application, title=_("Moira"), default_width=620, default_height=680
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
        self._refresh_timer_id: int | None = None
        self._last_focus_time: float = 0.0
        self._focus_debounce_seconds = 2.0
        self._next_refresh_time: float = 0.0
        self._history_coordinator = HistoryCoordinator()
        self._history_coordinator.start()
        self._build()
        self._render()
        if not smoke_test:
            GLib.idle_add(self.refresh)
            self._arm_refresh_timer()
            # 30-second local recompute of countdowns
            GLib.timeout_add_seconds(30, self._local_recompute)
            # Focus regain handler
            self.connect("notify::is-active", self._on_focus_change)
        # Track stack visibility for History refresh
        self._stack.connect("notify::visible-child", self._on_stack_changed)
        # Shutdown handler: stop the history worker cleanly on window close
        self.connect("close-request", self._on_close_request)

    def _build(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        header = Adw.HeaderBar()
        self._stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher(stack=self._stack)
        header.set_title_widget(switcher)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text=_("Refresh now"))
        refresh.connect("clicked", lambda *_: self.refresh())
        header.pack_end(refresh)
        about = Gtk.Button(icon_name="help-about-symbolic", tooltip_text=_("About Moira"))
        about.connect("clicked", self._about)
        header.pack_end(about)
        root.append(header)
        root.append(self._stack)
        home = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        home.set_margin_top(18)
        home.set_margin_bottom(18)
        home.set_margin_start(18)
        home.set_margin_end(18)
        self.claude_card = QuotaCard(_("Claude"))
        self.codex_card = QuotaCard(_("Codex"))
        home.append(self.claude_card)
        home.append(self.codex_card)
        self.refresh_info = Gtk.Label(xalign=0)
        self.refresh_info.add_css_class("dim-label")
        home.append(self.refresh_info)
        self._stack.add_titled(home, "home", _("Quotas"))
        self._stack.add_titled(self._settings_page(), "notifications", _("Notifications"))
        self._history_page = HistoryPage(self.executor)
        history_scroll = Gtk.ScrolledWindow()
        history_scroll.set_child(self._history_page)
        self._stack.add_titled(history_scroll, "history", _("History"))
        self.set_content(root)

    def _settings_page(self) -> Gtk.Widget:
        page = Gtk.ScrolledWindow()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(18)
        box.set_margin_bottom(18)
        box.set_margin_start(18)
        box.set_margin_end(18)
        self.ntfy_enabled = Gtk.Switch(active=self.settings.ntfy_enabled)
        box.append(self._row(_("Enable NTFY alerts"), self.ntfy_enabled))
        self.server = Gtk.Entry(text=self.settings.ntfy_server)
        box.append(self._labeled(_("Server URL"), self.server))
        self.topic = Gtk.Entry(text=self.settings.ntfy_topic)
        box.append(self._labeled(_("Topic"), self.topic))
        self.token = Gtk.PasswordEntry(
            placeholder_text=_("Leave blank to keep current keyring token")
        )
        box.append(self._labeled(_("Optional access token"), self.token))
        self.thresholds = Gtk.Entry(text=", ".join(map(str, self.settings.thresholds)))
        box.append(self._labeled(_("Thresholds (%)"), self.thresholds))
        # Refresh interval dropdown
        self.refresh_combo = Gtk.DropDown.new_from_strings([str(m) for m in VALID_REFRESH_MINUTES])
        current_idx = VALID_REFRESH_MINUTES.index(
            self.settings.refresh_minutes
            if self.settings.refresh_minutes in VALID_REFRESH_MINUTES
            else VALID_REFRESH_MINUTES[0]
        )
        self.refresh_combo.set_selected(current_idx)
        refresh_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        refresh_box.append(Gtk.Label(label=_("Refresh interval"), xalign=0, hexpand=True))
        refresh_box.append(self.refresh_combo)
        refresh_box.append(Gtk.Label(label=_("minutes")))
        box.append(refresh_box)
        self.reset_alerts = Gtk.Switch(active=self.settings.reset_alerts)
        box.append(self._row(_("Alert when a quota resets"), self.reset_alerts))
        self.error_alerts = Gtk.Switch(active=self.settings.error_alerts)
        box.append(self._row(_("Alert on refresh errors"), self.error_alerts))
        self.autostart = Gtk.Switch(active=self.settings.autostart)
        box.append(self._row(_("Start automatically on login"), self.autostart))
        buttons = Gtk.Box(spacing=8)
        save = Gtk.Button(label=_("Save settings"))
        save.add_css_class("suggested-action")
        save.connect("clicked", self._save_settings)
        test = Gtk.Button(label=_("Send test notification"))
        test.connect("clicked", self._test_notification)
        buttons.append(save)
        buttons.append(test)
        box.append(buttons)
        integration_buttons = Gtk.Box(spacing=8)
        setup_claude = Gtk.Button(label=_("Set up Claude integration"))
        setup_claude.connect("clicked", self._setup_claude)
        remove_claude = Gtk.Button(label=_("Remove Claude integration"))
        remove_claude.connect("clicked", self._remove_claude)
        integration_buttons.append(setup_claude)
        integration_buttons.append(remove_claude)
        box.append(integration_buttons)
        shortcut_buttons = Gtk.Box(spacing=8)
        create_desktop = Gtk.Button(label=_("Create desktop shortcut"))
        create_desktop.connect("clicked", self._create_desktop_shortcut)
        remove_desktop = Gtk.Button(label=_("Remove desktop shortcut"))
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
        now = datetime.now(UTC)
        snapshots = derive_state(self.state.readings, now=now)
        claude_readings = [r for r in self.state.readings if r.service.value == "claude"]
        codex_readings = [r for r in self.state.readings if r.service.value == "codex"]
        self.claude_card.show_readings(claude_readings, snapshots.get(Service.CLAUDE))
        self.codex_card.show_readings(codex_readings, snapshots.get(Service.CODEX))
        self._update_refresh_info()

    def _update_refresh_info(self) -> None:
        parts: list[str] = []
        if self.state.last_refresh:
            parts.append(f"{_('Last refresh: ')}{self.state.last_refresh}")
        if self.state.next_refresh and not self.refreshing:
            parts.append(f"{_('Next refresh: ')}{self.state.next_refresh}")
        if parts:
            self.refresh_info.set_text(_(" · ").join(parts))
        else:
            self.refresh_info.set_text("")

    def refresh(self) -> bool:
        if self.refreshing:
            return False
        self.refreshing = True
        self.pending = []
        self.completed = 0
        self.claude_card.status.set_text(_("Loading…"))
        self.codex_card.status.set_text(_("Loading…"))
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
        now = datetime.now(UTC)
        if self.settings.ntfy_enabled:
            alerts = evaluate_alerts(
                previous, self.pending, self.settings, set(self.state.alert_keys), now=now
            )
            for alert in alerts:
                self.executor.submit(self._deliver_alert, alert.key, alert.notification)
        self.state.readings = merged
        self.state.last_refresh = now.strftime("%H:%M:%S")
        self._next_refresh_time = time.monotonic() + self.settings.refresh_minutes * 60
        self.state.next_refresh = self._compute_next_refresh_str()
        save_state(self.state)
        self._record_history(self.pending, now)
        self.refreshing = False
        self._render()
        # Notify History page that new data may be available
        self._history_page.on_refresh_complete()
        return False

    def _record_history(self, readings: list[QuotaReading], now: datetime) -> None:
        """Enqueue fresh quota observations for off-thread history writing.

        Never blocks the GTK thread. If the worker is busy, the newest batch
        replaces any pending batch (newest-wins) and a sanitized status is set.
        History failure does not affect quota state, display, or alerts.
        """
        self._history_coordinator.enqueue(readings, now)

    def _on_close_request(self, *_: Any) -> bool:
        """Stop the history worker cleanly on window close.

        The coordinator shutdown is bounded (joins with a 3-second timeout,
        strictly above the 1-second SQLite write timeout) so the worker
        terminates before the join expires. Pending work is discarded
        with a sanitized status. Never blocks GTK for more than 3 seconds.
        """
        self._history_coordinator.shutdown()
        return False

    def _on_stack_changed(self, *_: Any) -> None:
        """Refresh History tab when it becomes visible."""
        visible_child = self._stack.get_visible_child()
        if visible_child is not None:
            # Check if the visible page is the history scroll
            page = visible_child
            if isinstance(page, Gtk.ScrolledWindow):
                child = page.get_child()
                if isinstance(child, HistoryPage):
                    child.on_visible()

    def _compute_next_refresh_str(self) -> str:
        if self._next_refresh_time <= 0:
            return ""
        remaining = max(0, int(self._next_refresh_time - time.monotonic()))
        minutes = remaining // 60
        seconds = remaining % 60
        return f"{minutes}m {seconds}s"

    def _deliver_alert(self, key: str, notification: Notification) -> None:
        try:
            send(
                self.settings.ntfy_server,
                self.settings.ntfy_topic,
                notification,
                get_ntfy_token(),
            )
        except Exception:
            return
        GLib.idle_add(self._record_alert, key)

    def _record_alert(self, key: str) -> bool:
        if key not in self.state.alert_keys:
            self.state.alert_keys.append(key)
            save_state(self.state)
        return False

    def _arm_refresh_timer(self) -> None:
        """Arm the GLib periodic refresh timer, replacing any existing one."""
        if self._refresh_timer_id is not None:
            GLib.Source.remove(self._refresh_timer_id)
            self._refresh_timer_id = None
        interval = self.settings.refresh_minutes * 60
        self._refresh_timer_id = GLib.timeout_add_seconds(interval, self._scheduled_refresh)
        self._next_refresh_time = time.monotonic() + interval

    def _scheduled_refresh(self) -> bool:
        self.refresh()
        return True

    def _local_recompute(self) -> bool:
        """Recompute quota and next-refresh countdowns every 30 seconds
        without collectors. Claude data changes only after a Claude Code
        response; cache rereads are not fresh provider events.
        """
        self.state.next_refresh = self._compute_next_refresh_str()
        self._render()
        return True

    def _on_focus_change(self, *_: Any) -> None:
        """Refresh on focus regain with monotonic debounce and overlap guard."""
        if not self.is_active():
            return
        now_mono = time.monotonic()
        if now_mono - self._last_focus_time < self._focus_debounce_seconds:
            return
        self._last_focus_time = now_mono
        if not self.refreshing:
            self.refresh()

    def _read_form(self) -> Settings:
        values = [
            int(part.strip()) for part in self.thresholds.get_text().split(",") if part.strip()
        ]
        selected_idx = self.refresh_combo.get_selected()
        refresh_val = VALID_REFRESH_MINUTES[selected_idx]
        settings = Settings(
            ntfy_server=self.server.get_text().strip(),
            ntfy_topic=self.topic.get_text().strip(),
            ntfy_enabled=self.ntfy_enabled.get_active(),
            thresholds=values,
            reset_alerts=self.reset_alerts.get_active(),
            error_alerts=self.error_alerts.get_active(),
            autostart=self.autostart.get_active(),
            refresh_minutes=refresh_val,
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
            old_interval = self.settings.refresh_minutes
            self.settings = settings
            # Replace the GLib provider timer immediately if interval changed
            if old_interval != settings.refresh_minutes:
                self._arm_refresh_timer()
            self.settings_status.set_text(
                _("Settings saved. Token is stored only in GNOME Keyring.")
            )
        except Exception as exc:
            self.settings_status.set_text(f"{_('Could not save settings: ')}{exc}")

    def _test_notification(self, *_: Any) -> None:
        try:
            settings = self._read_form()
        except Exception as exc:
            self.settings_status.set_text(f"{_('Invalid settings: ')}{exc}")
            return
        self.settings_status.set_text(_("Sending test…"))
        future = self.executor.submit(
            send,
            settings.ntfy_server,
            settings.ntfy_topic,
            Notification(
                _("Moira test"),
                _("Notifications are configured correctly."),
                "white_check_mark",
            ),
            self.token.get_text() or get_ntfy_token(),
        )
        future.add_done_callback(lambda done: GLib.idle_add(self._test_done, done.exception()))

    def _test_done(self, error: BaseException | None) -> bool:
        self.settings_status.set_text(
            _("Test notification sent.") if error is None else f"{_('Test failed: ')}{error}"
        )
        return False

    def _setup_claude(self, *_: Any) -> None:
        try:
            changed = setup_claude_integration()
            self.settings_status.set_text(
                _("Claude integration installed. Complete one Claude response to populate quotas.")
                if changed
                else _("Claude integration is already installed.")
            )
        except Exception as exc:
            self.settings_status.set_text(f"{_('Claude integration was not changed: ')}{exc}")

    def _remove_claude(self, *_: Any) -> None:
        try:
            changed = remove_claude_integration()
            self.settings_status.set_text(
                _("Claude integration removed and the previous status line restored.")
                if changed
                else _("Claude integration is not installed.")
            )
        except Exception as exc:
            self.settings_status.set_text(f"{_('Claude integration was not changed: ')}{exc}")

    def _create_desktop_shortcut(self, *_: Any) -> None:
        try:
            target, changed = create_shortcut()
            action = _("created") if changed else _("already exists")
            self.settings_status.set_text(f"{_('Desktop shortcut ')}{action}: {target}")
        except Exception as exc:
            self.settings_status.set_text(f"{_('Desktop shortcut is unavailable: ')}{exc}")

    def _remove_desktop_shortcut(self, *_: Any) -> None:
        try:
            changed = remove_shortcut()
            removed = (
                _("Desktop shortcut removed.")
                if changed
                else _("Desktop shortcut is already absent.")
            )
            self.settings_status.set_text(removed)
        except Exception as exc:
            self.settings_status.set_text(f"{_('Desktop shortcut is unavailable: ')}{exc}")

    def _about(self, *_: Any) -> None:
        dialog = Adw.AboutDialog(
            application_name=_("Moira"),
            application_icon="io.github.moira.QuotaMonitor",
            version="0.2.2",
            developer_name="Moira contributors",
            license_type=Gtk.License.MIT_X11,
            comments=_("Claude and Codex quota monitor for Ubuntu"),
        )
        dialog.present(self)
