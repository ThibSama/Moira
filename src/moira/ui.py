"""Main window for Moira.

Package 5 additions:
- Quota cards show used/remaining percentages (100 - used) plus reset
  countdown; a compact mode keeps provider, status, exhaustion and reset
  visible while dropping progress bars.
- Separate Claude/Codex collection toggles: disabled providers are never
  started, never alert, never write fresh history, and show a translated
  "Disabled" state while old History stays readable.
- Typed per-service alert rules (thresholds/reset/error) from the v3
  configuration.
- Native desktop notifications via Gio.Notification on the application;
  NTFY and native channels are independently enabled and deduplicated per
  channel (a key is persisted only after the channel reports success).
- NTFY delivery returns typed sanitized outcomes (never raw exceptions,
  server, topic, token, response body, or paths).
- A sanitized Diagnostics tab, copy actions for quota status / History
  summary / diagnostics (copied text contains no secrets or paths), and a
  manual GitHub release check (no startup check, no telemetry, no token).
- Window size and maximized state persist on close; position is not
  restored because GTK 4 / PyGObject exposes no window-position API here
  (and Wayland provides none) — stated truthfully, with no X11/shell hacks.
"""

from __future__ import annotations

import concurrent.futures
import functools
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from . import __version__ as APP_VERSION
from .activity import ActivityState, ActivityStore, AgentRuntime, derive_runtime_activity
from .activity_view import ActivityPanel, ActivityWatcher, latest_terminal_event
from .agent_integration import (
    CapabilityReport,
    IntegrationResult,
    probe_capability,
    remove_runtime,
    setup_runtime,
    test_runtime,
)
from .alerts import CHANNEL_NATIVE, CHANNEL_NTFY, evaluate_alerts, merge_with_stale
from .autostart import set_enabled as set_autostart
from .collectors import ClaudeCollector, CodexCollector
from .desktop import create_shortcut, remove_shortcut
from .diagnostics import (  # noqa: F401  (re-exported for compatibility)
    build_diagnostics_text,
    build_quota_status_text,
    format_countdown,
)
from .exhaustion import derive_state
from .history import HistoryStatus
from .history_db import HistoryCoordinator
from .history_page import HistoryPage
from .i18n import is_french, tr
from .integrations import (
    IntegrationCoordinator,
    IntegrationProbe,
    IntegrationState,
    TokenStatusView,
    build_snapshot,
    probe_hermes_inventory,
    read_token_status_view,
)
from .integrations_page import IntegrationsPage
from .models import (
    CodexSummary,
    CollectorResult,
    QuotaReading,
    QuotaStatus,
    Service,
    TokenAvailabilityRecord,
    TokenReading,
)
from .ntfy import Notification, send
from .persistence import (
    VALID_REFRESH_MINUTES,
    ProviderRules,
    Settings,
    load_settings,
    load_state,
    save_state,
    update_settings,
)
from .profile_journal import recover_pending_transaction
from .provider_editor import ProviderEditor
from .secrets import get_ntfy_token, set_ntfy_token
from .updates import (
    STATUS_CHECK_FAILED,
    STATUS_INVALID_RESPONSE,
    STATUS_UP_TO_DATE,
    STATUS_UPDATE_AVAILABLE,
    check_latest_release,
)

_ = tr


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
    """A provider card. Compact mode keeps provider, status, exhaustion and
    reset visible while dropping the per-reading progress bars."""

    def __init__(self, title: str) -> None:
        super().__init__()
        self._title = title
        self._compact = False
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

    def set_compact(self, compact: bool) -> None:
        """Toggle compact rendering; applied on the next show_readings call."""
        self._compact = compact

    def show_disabled(self) -> None:
        """Show the translated Disabled state (provider not collected)."""
        while child := self.rows.get_first_child():
            self.rows.remove(child)
        self.status.set_text(_("Disabled"))
        self.updated.set_text("")

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
            self._add_reading_row(reading)
        latest = max(item.retrieved_at for item in readings).astimezone()
        self.updated.set_text(
            f"{_('Last refresh: ')}{latest.strftime('%H:%M:%S')}"
            f"{_(' · Source: ')}{readings[0].source}"
        )

    def _add_reading_row(self, reading: QuotaReading) -> None:
        used = reading.percentage
        remaining = 100.0 - used
        if self._compact:
            line = f"{reading.quota_label}: {used:.0f}% {_('used')}"
            line += f" · {remaining:.0f}% {_('remaining')}"
            line += f" · {_('resets in ')}{format_countdown(reading.reset_at)}"
            label = Gtk.Label(label=line, xalign=0)
            label.set_wrap(True)
            self.rows.append(label)
            return
        title = Gtk.Label(
            label=(
                f"{reading.quota_label} — {used:.0f}% {_('used')} · "
                f"{remaining:.0f}% {_('remaining')}"
            ),
            xalign=0,
        )
        title.add_css_class("heading")
        progress = Gtk.ProgressBar(fraction=reading.percentage / 100)
        reset = reading.reset_at.astimezone()
        reset_text = format_local_datetime(reset)
        detail = Gtk.Label(
            label=f"{_('Resets ')}{reset_text} · {_('in ')}{format_countdown(reading.reset_at)}",
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
        used = weekly.percentage or 0
        title = Gtk.Label(
            label=(
                f"{weekly.quota_label} — {used:.0f}% {_('used')} · "
                f"{100.0 - used:.0f}% {_('remaining')}"
            ),
            xalign=0,
        )
        title.add_css_class("heading")
        detail = Gtk.Label(
            label=f"{_('Resets ')}{reset_text} · {_('in ')}{format_countdown(weekly.reset_at)}",
            xalign=0,
        )
        detail.set_wrap(True)
        detail.add_css_class("dim-label")
        self.rows.append(title)
        if not self._compact:
            progress = Gtk.ProgressBar(fraction=1.0)
            progress.add_css_class("error")
            self.rows.append(progress)
        self.rows.append(detail)

        # For Claude: visually disable the five-hour row
        if snapshot.service is Service.CLAUDE and snapshot.five_hour:
            fh = snapshot.five_hour
            if fh.percentage is not None and fh.reset_at is not None:
                fh_title = Gtk.Label(label=f"{fh.quota_label} — {fh.percentage:.0f}%", xalign=0)
                fh_title.add_css_class("dim-label")
                fh_detail = Gtk.Label(
                    label=_("Five-hour quota disabled due to weekly exhaustion"),
                    xalign=0,
                )
                fh_detail.set_wrap(True)
                fh_detail.add_css_class("dim-label")
                self.rows.append(fh_title)
                if not self._compact:
                    fh_progress = Gtk.ProgressBar(fraction=fh.percentage / 100)
                    fh_progress.set_sensitive(False)
                    self.rows.append(fh_progress)
                self.rows.append(fh_detail)

        latest = max(item.retrieved_at for item in readings).astimezone()
        self.updated.set_text(
            f"{_('Last refresh: ')}{latest.strftime('%H:%M:%S')}"
            f"{_(' · Source: ')}{readings[0].source}"
        )


class DiagnosticsPage(Gtk.Box):
    """Sanitized diagnostics tab with a copy action."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.set_margin_top(18)
        self.set_margin_bottom(18)
        self.set_margin_start(18)
        self.set_margin_end(18)
        self._text = ""
        self._label = Gtk.Label(xalign=0)
        self._label.set_wrap(True)
        self._label.add_css_class("monospace")
        self.append(self._label)
        actions = Gtk.Box(spacing=8)
        copy = Gtk.Button(label=_("Copy"))
        copy.connect("clicked", self._copy)
        actions.append(copy)
        self._status = Gtk.Label(xalign=0)
        self._status.add_css_class("dim-label")
        actions.append(self._status)
        self.append(actions)

    def update(self, text: str) -> None:
        """Replace the diagnostics text (called from the main thread)."""
        self._text = text
        self._label.set_text(text)

    def _copy(self, *_args: Any) -> None:
        if not self._text:
            return
        self.get_clipboard().set_text(self._text)
        self._status.set_text(_("Diagnostics copied."))


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application, smoke_test: bool = False) -> None:
        super().__init__(
            application=application, title=_("Moira"), default_width=620, default_height=680
        )
        # Best-effort convergence of any crashed profile transaction
        # before the persisted state is loaded. On failure the journal is
        # kept and the provider editor retries recovery on its next
        # reload (surfacing the translated "Recovery required." outcome).
        recover_pending_transaction()
        self.settings = load_settings()
        self.state = load_state()
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="moira"
        )
        self.refreshing = False
        self.pending: list[QuotaReading] = []
        self.pending_tokens: list[TokenReading] = []
        self.pending_summary: CodexSummary | None = None
        self.pending_availability: list[TokenAvailabilityRecord] = []
        self.pending_lock = threading.Lock()
        self.completed = 0
        self._enabled_services: list[Service] = []
        self._refresh_timer_id: int | None = None
        self._last_focus_time: float = 0.0
        self._focus_debounce_seconds = 2.0
        self._next_refresh_time: float = 0.0
        self._history_coordinator = HistoryCoordinator()
        self._history_coordinator.start()
        self._history_coordinator.set_write_success_callback(self._on_history_write_success)
        # Agent activity: store + watcher (FileMonitor + bounded timers).
        self._activity_store = ActivityStore()
        self._activity_watcher = ActivityWatcher(self._activity_store, self._update_activity_panel)
        self._capabilities: dict[AgentRuntime, CapabilityReport] = {}
        # Integration inventory: bounded newest-wins probes off the GTK
        # thread. Probes start only on page visibility / explicit Refresh;
        # each probe reads the Hermes inventory AND the history-backed
        # exact-token status view under one generation.
        self._integration_coordinator = IntegrationCoordinator(
            submit=self._submit_probe,
            probe=self._integration_probe,
            publish=self._publish_inventory,
            fallback=self._integration_fallback,
        )
        self._integration_coordinator.start()
        self._last_probe: IntegrationProbe | None = None
        self._restore_geometry()
        self._build()
        self._render()
        if not smoke_test:
            GLib.idle_add(self.refresh)
            self._arm_refresh_timer()
            # 30-second local recompute of countdowns
            GLib.timeout_add_seconds(30, self._local_recompute)
            # Focus regain handler
            self.connect("notify::is-active", self._on_focus_change)
            # Probe agent capabilities off the GTK thread (bounded probes).
            for runtime in AgentRuntime:
                self.executor.submit(self._probe_capability_async, runtime)
        # Track stack visibility for History refresh
        self._stack.connect("notify::visible-child", self._on_stack_changed)
        # Shutdown handler: stop the history worker cleanly on window close
        self.connect("close-request", self._on_close_request)

    def _restore_geometry(self) -> None:
        """Restore persisted window size and maximized state.

        Position is intentionally NOT restored: GTK 4 / PyGObject exposes no
        window-position API in this environment, and Wayland provides none.
        No X11/shell hacks are used; the limitation is stated truthfully.
        """
        if self.settings.window_width and self.settings.window_height:
            self.set_default_size(self.settings.window_width, self.settings.window_height)
        if self.settings.window_maximized:
            self.maximize()

    def _persist_geometry(self) -> None:
        """Save window size and maximized state on close (best-effort)."""
        try:
            width, height = self.get_width(), self.get_height()

            def transform(current: Settings) -> Settings:
                updated = replace(current, window_maximized=self.is_maximized())
                if width > 0 and height > 0:
                    updated = replace(updated, window_width=width, window_height=height)
                return updated

            update_settings(transform)
        except Exception:
            pass

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
        self.claude_card.set_compact(self.settings.compact_mode)
        self.codex_card.set_compact(self.settings.compact_mode)
        home.append(self.claude_card)
        home.append(self.codex_card)
        # Agent activity panel: below the quota cards, separate from quotas,
        # present in both full and compact modes.
        self._activity_panel = ActivityPanel()
        home.append(self._activity_panel)
        self.refresh_info = Gtk.Label(xalign=0)
        self.refresh_info.add_css_class("dim-label")
        home.append(self.refresh_info)
        actions = Gtk.Box(spacing=8)
        copy_status = Gtk.Button(label=_("Copy quota status"))
        copy_status.connect("clicked", self._copy_quota_status)
        actions.append(copy_status)
        home.append(actions)
        self._stack.add_titled(home, "home", _("Quotas"))
        self._stack.add_titled(self._settings_page(), "notifications", _("Notifications"))
        self._integrations_page = self._integrations_page_content()
        integrations_scroll = Gtk.ScrolledWindow()
        integrations_scroll.set_child(self._integrations_page)
        self._stack.add_titled(integrations_scroll, "integrations", _("Integrations"))
        self._history_page = HistoryPage(self.executor)
        history_scroll = Gtk.ScrolledWindow()
        history_scroll.set_child(self._history_page)
        self._stack.add_titled(history_scroll, "history", _("History"))
        self._diagnostics_page = DiagnosticsPage()
        self._stack.add_titled(self._diagnostics_page, "diagnostics", _("Diagnostics"))
        self.set_content(root)

    def _settings_page(self) -> Gtk.Widget:
        page = Gtk.ScrolledWindow()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(18)
        box.set_margin_bottom(18)
        box.set_margin_start(18)
        box.set_margin_end(18)
        # ── Collection toggles ──
        self.collect_claude = Gtk.Switch(active=self.settings.collect_claude)
        box.append(self._row(_("Collect Claude"), self.collect_claude))
        self.collect_codex = Gtk.Switch(active=self.settings.collect_codex)
        box.append(self._row(_("Collect Codex"), self.collect_codex))
        # ── NTFY channel ──
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
        # ── Native channel ──
        self.native_notifications = Gtk.Switch(active=self.settings.native_notifications)
        box.append(self._row(_("Native desktop notifications"), self.native_notifications))
        # ── Per-service alert rules ──
        self._thresholds_entries: dict[str, Gtk.Entry] = {}
        self._reset_switches: dict[str, Gtk.Switch] = {}
        self._error_switches: dict[str, Gtk.Switch] = {}
        for key in ("claude", "codex"):
            rules = self.settings.rules_for(key)
            title = _("Claude") if key == "claude" else _("Codex")
            label = Gtk.Label(label=title, xalign=0)
            label.add_css_class("heading")
            box.append(label)
            entry = Gtk.Entry(text=", ".join(map(str, rules.thresholds)))
            box.append(
                self._labeled(
                    _("Claude thresholds (%)") if key == "claude" else _("Codex thresholds (%)"),
                    entry,
                )
            )
            self._thresholds_entries[key] = entry
            reset_switch = Gtk.Switch(active=rules.reset_alerts)
            box.append(
                self._row(
                    _("Claude reset alerts") if key == "claude" else _("Codex reset alerts"),
                    reset_switch,
                )
            )
            self._reset_switches[key] = reset_switch
            error_switch = Gtk.Switch(active=rules.error_alerts)
            box.append(
                self._row(
                    _("Claude error alerts") if key == "claude" else _("Codex error alerts"),
                    error_switch,
                )
            )
            self._error_switches[key] = error_switch
        # ── Refresh interval ──
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
        # ── Appearance / misc ──
        self.compact_mode = Gtk.Switch(active=self.settings.compact_mode)
        box.append(self._row(_("Compact mode"), self.compact_mode))
        self.autostart = Gtk.Switch(active=self.settings.autostart)
        box.append(self._row(_("Start automatically on login"), self.autostart))
        buttons = Gtk.Box(spacing=8)
        save = Gtk.Button(label=_("Save settings"))
        save.add_css_class("suggested-action")
        save.connect("clicked", self._save_settings)
        test = Gtk.Button(label=_("Send test notification"))
        test.connect("clicked", self._test_notification)
        test_native = Gtk.Button(label=_("Send native test notification"))
        test_native.connect("clicked", self._test_native_notification)
        buttons.append(save)
        buttons.append(test)
        buttons.append(test_native)
        box.append(buttons)
        update_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        update_row = Gtk.Box(spacing=8)
        check_updates = Gtk.Button(label=_("Check for updates"))
        check_updates.connect("clicked", self._check_updates)
        update_row.append(check_updates)
        self.update_status = Gtk.Label(xalign=0)
        self.update_status.set_wrap(True)
        self.update_status.add_css_class("dim-label")
        update_row.append(self.update_status)
        update_box.append(update_row)
        box.append(update_box)
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

    def _integrations_page_content(self) -> IntegrationsPage:
        """Build the scrollable Integrations page and its Agents section.

        The Set up / Remove / Test controls are moved here from the
        Settings view without any behavior change: the same handlers,
        the same status labels and the same activity-event suffix.
        """
        page = IntegrationsPage(
            on_visible_refresh=self._request_integrations_refresh,
            on_edit_providers=self._open_provider_editor,
        )
        self._integrations_page = page
        self._build_agents_section()
        return page

    def _build_agents_section(self) -> None:
        """Per-runtime Set up / Remove / Test rows (moved from Settings)."""
        self._integration_status: dict[AgentRuntime, Gtk.Label] = {}
        for runtime in AgentRuntime:
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            buttons = Gtk.Box(spacing=8)
            runtime_name = (
                _("Claude Code")
                if runtime is AgentRuntime.CLAUDE
                else _("Codex CLI")
                if runtime is AgentRuntime.CODEX
                else _("Hermes")
            )
            buttons.append(Gtk.Label(label=runtime_name, xalign=0, hexpand=True))
            setup_btn = Gtk.Button(label=_("Set up"))
            setup_btn.connect("clicked", self._setup_agent, runtime)
            remove_btn = Gtk.Button(label=_("Remove"))
            remove_btn.connect("clicked", self._remove_agent, runtime)
            test_btn = Gtk.Button(label=_("Test"))
            test_btn.connect("clicked", self._test_agent, runtime)
            buttons.append(setup_btn)
            buttons.append(remove_btn)
            buttons.append(test_btn)
            row.append(buttons)
            status = Gtk.Label(label="", xalign=0)
            status.set_wrap(True)
            status.add_css_class("dim-label")
            row.append(status)
            self._integrations_page.agents_box.append(row)
            self._integration_status[runtime] = status

    # ── Integration inventory (bounded newest-wins coordinator) ──

    def _submit_probe(self, fn: Any) -> None:
        """Run one bounded inventory probe on the shared worker executor."""
        self.executor.submit(fn)

    def _integration_probe(self) -> IntegrationProbe:
        """One bounded off-GTK probe: the Hermes inventory and the
        history-backed exact-token status view, read under one generation.
        Never raises (both readers fail closed)."""
        inventory = probe_hermes_inventory()
        view = read_token_status_view()
        return IntegrationProbe(inventory=inventory, token_status=view)

    def _integration_fallback(self) -> IntegrationProbe:
        """Sanitized fallback when the injected probe itself raises."""
        from .integrations import HermesInventory

        return IntegrationProbe(
            inventory=HermesInventory(
                IntegrationState.TEMPORARILY_UNAVAILABLE, detail="inventory probe failed"
            ),
            token_status=TokenStatusView((), False),
        )

    def _request_integrations_refresh(self, *_args: Any) -> None:
        """Request one bounded inventory probe (page visibility or Refresh)."""
        self._integrations_page.render_status(_("Checking…"))
        self._integration_coordinator.request_refresh()

    def _publish_inventory(self, probe: IntegrationProbe) -> None:
        """Worker-thread publication: dispatch the newest probe onto the
        GLib idle loop. Stale generations and results after shutdown never
        reach this point (the coordinator gates them)."""
        GLib.idle_add(self._apply_inventory, probe)

    def _apply_inventory(self, probe: IntegrationProbe) -> bool:
        if self._integrations_page._shutdown:
            return False
        self._last_probe = probe
        inventory = probe.inventory
        if inventory.state is IntegrationState.AVAILABLE:
            text = f"{_('Inventory refreshed.')} {_('Hermes ')}{inventory.version}"
        else:
            text = f"{_('Inventory unavailable: ')}{_(inventory.detail)}"
        self._integrations_page.render_status(text)
        self._maybe_render_integrations()
        return False

    def _maybe_render_integrations(self) -> None:
        """Re-render the Integrations page from the newest cached probe
        (inventory + history token view) when the page is visible. Never
        triggers a probe."""
        probe = getattr(self, "_last_probe", None)
        if probe is None:
            return
        page = getattr(self, "_integrations_page", None)
        if page is None or not page.is_visible_page():
            return
        snapshot = build_snapshot(
            hermes=probe.inventory,
            capabilities=self._capabilities,
            quota_readings=self.state.readings,
            token_status=probe.token_status,
            collect_claude=self.settings.collect_claude,
            collect_codex=self.settings.collect_codex,
        )
        page.render_snapshot(snapshot)

    # ── Provider profiles (Edit providers) ──

    def _open_provider_editor(self, *_args: Any) -> None:
        """Open the provider editor once; subsequent clicks focus it.

        The editor is transient to the main window and its lifecycle is
        tied to it: closing the editor shuts it down (bounded: no further
        operations, no callbacks after closure) and drops the reference,
        so the next click opens one fresh live editor.
        """
        editor = getattr(self, "_provider_editor", None)
        if editor is not None:
            editor.present()
            return
        editor = ProviderEditor(
            submit=self.executor.submit,
            on_profiles_changed=self._on_profiles_persisted,
        )
        # Transient ownership: a fully-constructed window (the real app)
        # marks its editor as a dialog. Bare-window tests are guarded.
        if getattr(self, "_stack", None) is not None:
            try:
                editor.set_transient_for(self)
            except Exception:
                pass
        editor.connect("close-request", self._on_editor_close_request)
        self._provider_editor = editor
        editor.present()

    def _on_editor_close_request(self, _editor: Any = None) -> bool:
        """Editor closed: bounded shutdown and drop of the reference."""
        editor = getattr(self, "_provider_editor", None)
        if editor is not None:
            editor.shutdown()
            self._provider_editor = None
        return False

    def _on_profiles_persisted(self) -> None:
        """A profile change landed on disk: refresh the in-memory settings
        and request one bounded inventory refresh."""
        fresh = load_settings()
        self.settings.provider_profiles = fresh.provider_profiles
        page = getattr(self, "_integrations_page", None)
        if page is not None:
            self._request_integrations_refresh()
            self._maybe_render_integrations()

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
        if self.settings.collect_claude:
            self.claude_card.show_readings(claude_readings, snapshots.get(Service.CLAUDE))
        else:
            self.claude_card.show_disabled()
        if self.settings.collect_codex:
            self.codex_card.show_readings(codex_readings, snapshots.get(Service.CODEX))
        else:
            self.codex_card.show_disabled()
        self._update_refresh_info()
        self._update_diagnostics()
        self._maybe_render_integrations()

    def _update_diagnostics(self) -> None:
        """Refresh the sanitized diagnostics tab content."""
        text = build_diagnostics_text(
            version=APP_VERSION,
            settings=self.settings,
            readings=self.state.readings,
            last_refresh=self.state.last_refresh,
            next_refresh=self.state.next_refresh,
            history_status=self._history_coordinator.status,
            history_lifecycle=self._history_coordinator.lifecycle_state,
            translator=tr,
            activity=self._activity_summary(),
        )
        self._diagnostics_page.update(text)

    def _activity_summary(self) -> str:
        """Sanitized activity summary for diagnostics (levels only)."""
        capabilities = getattr(self, "_capabilities", {})
        parts: list[str] = []
        for runtime in AgentRuntime:
            report = capabilities.get(runtime)
            level = report.level if report is not None else "unknown"
            parts.append(f"{runtime.value}: {level}")
        return " · ".join(parts)

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
        self.pending_tokens = []
        self.pending_summary = None
        self.pending_availability = []
        self.completed = 0
        if self.settings.collect_claude:
            self.claude_card.status.set_text(_("Loading…"))
        else:
            self.claude_card.show_disabled()
        if self.settings.collect_codex:
            self.codex_card.status.set_text(_("Loading…"))
        else:
            self.codex_card.show_disabled()
        self._submit_collectors()
        return False

    def _submit_collectors(self) -> None:
        """Submit one collector per ENABLED provider, binding each future to
        its Service via functools.partial (identity travels with the future,
        independent of completion order). Disabled providers are never
        started. Zero enabled providers completes the refresh immediately.
        """
        self._enabled_services = self.settings.enabled_services()
        for service in self._enabled_services:
            collector = ClaudeCollector() if service is Service.CLAUDE else CodexCollector()
            future = self.executor.submit(collector.collect)
            future.add_done_callback(functools.partial(self._collector_done, service=service))
        if not self._enabled_services:
            GLib.idle_add(self._finish_refresh)

    def _collector_done(
        self, future: concurrent.futures.Future[CollectorResult], service: Service
    ) -> None:
        try:
            result = future.result()
        except Exception:
            # Unexpected collector failure: synthesize one sanitized
            # TEMPORARILY_UNAVAILABLE record for the bound provider so the
            # UI always has an availability state to display. No exception
            # text is ever stored — TokenAvailabilityRecord has no detail.
            now = datetime.now(UTC)
            result = CollectorResult(
                service=service,
                quota_readings=(),
                token_readings=(),
                token_availability_records=(
                    TokenAvailabilityRecord(
                        service=service,
                        observed_at=now,
                        source="moira",
                        status=HistoryStatus.TEMPORARILY_UNAVAILABLE,
                    ),
                ),
            )
        with self.pending_lock:
            self.pending.extend(result.quota_readings)
            self.pending_tokens.extend(result.token_readings)
            if result.codex_summary is not None:
                self.pending_summary = result.codex_summary
            self.pending_availability.extend(result.token_availability_records)
            self.completed += 1
            complete = self.completed == len(self._enabled_services)
        if complete:
            GLib.idle_add(self._finish_refresh)

    def _finish_refresh(self) -> bool:
        previous = self.state.readings
        merged = merge_with_stale(previous, self.pending)
        # Disabled providers keep their previous readings unchanged (never
        # marked stale); only enabled services appear in the merge.
        enabled = set(self._enabled_services)
        merged = [r for r in merged if r.service in enabled]
        merged.extend(r for r in previous if r.service not in enabled)
        now = datetime.now(UTC)
        alerts = evaluate_alerts(
            previous, self.pending, self.settings, set(self.state.alert_keys), now=now
        )
        for alert in alerts:
            if alert.channel == CHANNEL_NTFY:
                self.executor.submit(self._deliver_ntfy, alert.key, alert.notification)
            elif alert.channel == CHANNEL_NATIVE:
                self._deliver_native(alert.key, alert.notification)
        self.state.readings = merged
        self.state.last_refresh = now.strftime("%H:%M:%S")
        self._next_refresh_time = time.monotonic() + self.settings.refresh_minutes * 60
        self.state.next_refresh = self._compute_next_refresh_str()
        save_state(self.state)
        self._record_history(self.pending, now)
        self.refreshing = False
        self._render()
        # History refresh happens via the write-success callback,
        # not here — only after the write actually succeeds.
        return False

    def _record_history(self, readings: list[QuotaReading], now: datetime) -> None:
        """Enqueue fresh quota, token, and summary observations for off-thread history writing.

        Never blocks the GTK thread. If the worker is busy, the newest batch
        replaces any pending batch (newest-wins) and a sanitized status is set.
        History failure does not affect quota state, display, or alerts.
        The official Codex summary travels as one typed record — never
        duplicated onto daily buckets. Each provider's token availability
        record is forwarded independently. Disabled providers never write
        fresh history: only enabled collectors produced ``readings``.
        """
        combined: list[Any] = list(readings)
        combined.extend(self.pending_tokens)
        if self.pending_summary is not None:
            combined.append(self.pending_summary)
        combined.extend(self.pending_availability)
        self._history_coordinator.enqueue(combined, now)

    def _on_close_request(self, *_args: Any) -> bool:
        """Persist geometry, then stop the history worker cleanly on close.

        The coordinator shutdown is bounded (idempotent, no joins): the
        in-flight inventory probe self-bounds through its subprocess
        timeout and never publishes after shutdown. The integrations page
        stops routing refreshes.
        """
        self._persist_geometry()
        self._activity_watcher.shutdown()
        self._integration_coordinator.shutdown()
        self._integrations_page.shutdown()
        editor = getattr(self, "_provider_editor", None)
        if editor is not None:
            editor.shutdown()
        self._history_coordinator.clear_write_success_callback()
        self._history_page.shutdown()
        self._history_coordinator.shutdown()
        return False

    def _on_history_write_success(self) -> None:
        """Called from the history worker thread after a successful write.

        Schedules a History tab refresh on the GLib idle loop. Only fires
        after the write actually succeeds — failed, dropped, or saturated
        writes do not trigger a refresh.
        """
        GLib.idle_add(self._history_page.on_refresh_complete)

    def _on_stack_changed(self, *_args: Any) -> None:
        """Refresh the History and Integrations tabs when they become
        visible; inventory probes run only on page visibility and the
        explicit page Refresh button."""
        visible_child = self._stack.get_visible_child()
        if isinstance(visible_child, Gtk.ScrolledWindow):
            child = visible_child.get_child()
            if isinstance(child, HistoryPage):
                self._history_page.on_visible()
                self._integrations_page.on_hidden()
                return
            if isinstance(child, IntegrationsPage):
                self._history_page.on_hidden()
                self._integrations_page.on_visible()
                return
        self._history_page.on_hidden()
        self._integrations_page.on_hidden()

    def _compute_next_refresh_str(self) -> str:
        if self._next_refresh_time <= 0:
            return ""
        remaining = max(0, int(self._next_refresh_time - time.monotonic()))
        minutes = remaining // 60
        seconds = remaining % 60
        return f"{minutes}m {seconds}s"

    # ── Alert delivery (per-channel dedup) ──

    def _deliver_ntfy(self, key: str, notification: Notification) -> None:
        """Deliver via NTFY on the worker thread. The channel key is recorded
        (via the main thread) ONLY after the typed outcome reports success —
        a failed delivery keeps its pending key so it is retried next refresh
        without repeating channels that already succeeded."""
        try:
            result = send(
                self.settings.ntfy_server,
                self.settings.ntfy_topic,
                notification,
                get_ntfy_token(),
            )
        except Exception:
            return
        if result.ok:
            GLib.idle_add(self._record_alert, key)

    def _deliver_native(self, key: str, notification: Notification) -> None:
        """Deliver a native desktop notification on the main thread via
        Gio.Notification on the application. Never calls notify-send.
        The channel key is recorded only after send_notification succeeds."""
        try:
            app = self.get_application()
            if app is None:
                return
            gio_notification = Gio.Notification.new(notification.title)
            gio_notification.set_body(notification.message)
            self._apply_native_priority(gio_notification, notification.priority)
            app.send_notification(key, gio_notification)
        except Exception:
            return
        self._record_alert(key)

    @staticmethod
    def _apply_native_priority(gio_notification: Any, priority: int) -> None:
        """Map Moira priority levels to Gio.NotificationPriority (best-effort)."""
        if not hasattr(gio_notification, "set_priority"):
            return
        mapping = {
            5: Gio.NotificationPriority.URGENT,
            4: Gio.NotificationPriority.HIGH,
            3: Gio.NotificationPriority.NORMAL,
        }
        gio_notification.set_priority(mapping.get(priority, Gio.NotificationPriority.LOW))

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

    def _on_focus_change(self, *_args: Any) -> None:
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
        def parse_thresholds(text: str) -> list[int]:
            return [int(part.strip()) for part in text.split(",") if part.strip()]

        claude_rules = ProviderRules(
            parse_thresholds(self._thresholds_entries["claude"].get_text()),
            self._reset_switches["claude"].get_active(),
            self._error_switches["claude"].get_active(),
        )
        codex_rules = ProviderRules(
            parse_thresholds(self._thresholds_entries["codex"].get_text()),
            self._reset_switches["codex"].get_active(),
            self._error_switches["codex"].get_active(),
        )
        selected_idx = self.refresh_combo.get_selected()
        refresh_val = VALID_REFRESH_MINUTES[selected_idx]
        settings = Settings(
            ntfy_server=self.server.get_text().strip(),
            ntfy_topic=self.topic.get_text().strip(),
            ntfy_enabled=self.ntfy_enabled.get_active(),
            native_notifications=self.native_notifications.get_active(),
            thresholds=list(claude_rules.thresholds),
            reset_alerts=claude_rules.reset_alerts,
            error_alerts=claude_rules.error_alerts,
            rules={"claude": claude_rules, "codex": codex_rules},
            collect_claude=self.collect_claude.get_active(),
            collect_codex=self.collect_codex.get_active(),
            compact_mode=self.compact_mode.get_active(),
            autostart=self.autostart.get_active(),
            refresh_minutes=refresh_val,
        )
        # Explicit merge: the form never edits the repository, the window
        # geometry, the maximized state, or the provider profiles —
        # saving settings must preserve every non-editable field exactly
        # as loaded (a whole-config rewrite must never wipe profiles).
        settings.repo = self.settings.repo
        settings.window_width = self.settings.window_width
        settings.window_height = self.settings.window_height
        settings.window_maximized = self.settings.window_maximized
        settings.provider_profiles = self.settings.provider_profiles
        settings.validate()
        return settings

    def _save_settings(self, *_args: Any) -> None:
        try:
            form = self._read_form()
            token = self.token.get_text()

            def transform(current: Settings) -> Settings:
                # Apply only the form-editable fields onto the freshly
                # loaded settings through the single config boundary, so
                # concurrent profile/geometry changes survive.
                return replace(
                    current,
                    ntfy_server=form.ntfy_server,
                    ntfy_topic=form.ntfy_topic,
                    ntfy_enabled=form.ntfy_enabled,
                    native_notifications=form.native_notifications,
                    thresholds=list(form.thresholds),
                    reset_alerts=form.reset_alerts,
                    error_alerts=form.error_alerts,
                    rules=form.rules,
                    collect_claude=form.collect_claude,
                    collect_codex=form.collect_codex,
                    compact_mode=form.compact_mode,
                    autostart=form.autostart,
                    refresh_minutes=form.refresh_minutes,
                )

            settings = update_settings(transform)
            if token:
                set_ntfy_token(token)
                self.token.set_text("")
            old_interval = self.settings.refresh_minutes
            old_collect = (self.settings.collect_claude, self.settings.collect_codex)
            old_compact = self.settings.compact_mode
            set_autostart(settings.autostart)
            self.settings = settings
            # Replace the GLib provider timer immediately if interval changed
            if old_interval != settings.refresh_minutes:
                self._arm_refresh_timer()
            # Apply compact mode and collection toggles immediately
            if old_compact != settings.compact_mode:
                self.claude_card.set_compact(settings.compact_mode)
                self.codex_card.set_compact(settings.compact_mode)
            self._render()
            self.settings_status.set_text(
                _("Settings saved. Token is stored only in GNOME Keyring.")
            )
            if old_collect != (settings.collect_claude, settings.collect_codex):
                GLib.idle_add(self.refresh)
        except Exception as exc:
            self.settings_status.set_text(f"{_('Could not save settings: ')}{exc}")

    def _test_notification(self, *_args: Any) -> None:
        """Send a test NTFY notification. Never changes dedup state; every
        failure (invalid settings, keyring retrieval, delivery) shows a fixed
        translated outcome — no raw exception text reaches the UI."""
        try:
            settings = self._read_form()
        except Exception:
            self.settings_status.set_text(_("Test failed: invalid settings."))
            return
        try:
            token = self.token.get_text() or get_ntfy_token()
        except Exception:
            self.settings_status.set_text(_("Test failed: keyring unavailable."))
            return
        self.settings_status.set_text(_("Sending test…"))
        try:
            future = self.executor.submit(
                send,
                settings.ntfy_server,
                settings.ntfy_topic,
                Notification(
                    _("Moira test"),
                    _("Notifications are configured correctly."),
                    "white_check_mark",
                ),
                token,
            )
        except Exception:
            # A stopped or rejecting executor raises synchronously at
            # submit(); never let that escape the GTK handler.
            self.settings_status.set_text(_("Test failed: notification unavailable."))
            return
        future.add_done_callback(lambda done: GLib.idle_add(self._test_done, done))

    def _test_done(self, future: Any) -> bool:
        """Show the sanitized typed outcome. Never exposes server, topic,
        token, response body, or raw exceptions; never changes dedup state.
        A failed future (executor error) maps to a fixed translated outcome."""
        try:
            result = future.result()
        except Exception:
            self.settings_status.set_text(_("Test failed: notification unavailable."))
            return False
        if result.ok:
            self.settings_status.set_text(_("Test notification sent."))
        else:
            self.settings_status.set_text(f"{_('Test failed: ')}{tr(result.status)}")
        return False

    def _test_native_notification(self, *_args: Any) -> None:
        """Send a test native notification. Never changes dedup state; Gio
        delivery failures show a fixed translated outcome."""
        app = self.get_application()
        if app is None:
            self.settings_status.set_text(_("Native notifications are unavailable."))
            return
        try:
            gio_notification = Gio.Notification.new(_("Moira test"))
            gio_notification.set_body(_("Notifications are configured correctly."))
            app.send_notification("moira-native-test", gio_notification)
        except Exception:
            self.settings_status.set_text(_("Test failed: native notification unavailable."))
            return
        self.settings_status.set_text(_("Test notification sent."))

    # ── Update check (manual only) ──

    def _check_updates(self, *_args: Any) -> None:
        """Manually check the repository's latest release. No startup check,
        no telemetry, no token, no auto-download or install."""
        self.update_status.set_text(_("Checking for updates…"))
        repo = self.settings.repo
        try:
            future = self.executor.submit(check_latest_release, repo, current=APP_VERSION)
        except Exception:
            # A stopped or rejecting executor raises synchronously at
            # submit(); never let that escape the GTK handler.
            self.update_status.set_text(_("Update check failed."))
            return
        future.add_done_callback(lambda done: GLib.idle_add(self._update_done, done))

    def _update_done(self, future: Any) -> bool:
        try:
            result = future.result()
        except Exception:
            self.update_status.set_text(_("Update check failed."))
            return False
        if result.status == STATUS_UPDATE_AVAILABLE:
            self.update_status.set_text(f"{_('A new version is available: ')}{result.latest}")
        elif result.status == STATUS_UP_TO_DATE:
            self.update_status.set_text(_("Moira is up to date."))
        elif result.status == STATUS_INVALID_RESPONSE:
            self.update_status.set_text(_("The update server returned an invalid response."))
        elif result.status == STATUS_CHECK_FAILED:
            self.update_status.set_text(_("Update check failed."))
        return False

    # ── Copy actions (sanitized; no secrets, raw errors, or paths) ──

    def _copy_quota_status(self, *_args: Any) -> None:
        text = build_quota_status_text(
            self.state.readings,
            self.settings,
            format_local=format_local_datetime,
            translator=tr,
        )
        self.get_clipboard().set_text(text)
        self.refresh_info.set_text(_("Quota status copied."))

    # ── Agent activity (independent from quotas) ──

    def _update_activity_panel(self) -> None:
        """Re-render the activity panel from the store (GTK thread)."""
        views = derive_runtime_activity(self._activity_store.snapshot())
        self._activity_panel.update(views)

    def _probe_capability_async(self, runtime: AgentRuntime) -> None:
        """Probe one runtime's capability off the GTK thread; never raises."""
        try:
            report = probe_capability(runtime)
        except Exception:
            report = CapabilityReport("unsupported", _("Agent activity is unavailable."))
        GLib.idle_add(self._apply_capability, runtime, report)

    def _apply_capability(self, runtime: AgentRuntime, report: CapabilityReport) -> bool:
        self._capabilities[runtime] = report
        self._update_integration_status(runtime)
        self._maybe_render_integrations()
        return False

    def _update_integration_status(self, runtime: AgentRuntime) -> None:
        """Show capability and last sanitized event for one runtime."""
        label = self._integration_status.get(runtime)
        if label is None:
            return
        report = self._capabilities.get(runtime)
        parts = [report.detail] if report is not None else [_("Checking…")]
        terminal = latest_terminal_event(self._activity_store.snapshot(), runtime)
        if terminal is not None:
            state, at, _model = terminal
            state_text = _(
                {
                    ActivityState.COMPLETED: "Completed",
                    ActivityState.FAILED: "Failed",
                    ActivityState.INTERRUPTED: "Interrupted",
                    ActivityState.RUNNING: "Active",
                }[state]
            )
            parts.append(f"{_('Last event: ')}{state_text} {at.astimezone():%H:%M:%S}")
        label.set_text(_(" · ").join(parts))

    def _setup_agent(self, _button: Any, runtime: AgentRuntime) -> None:
        try:
            result = setup_runtime(runtime)
        except Exception:
            self._update_integration_status(runtime)
            return
        self._capabilities[runtime] = result.capability
        self._update_integration_status(runtime)
        self._maybe_render_integrations()

    def _remove_agent(self, _button: Any, runtime: AgentRuntime) -> None:
        try:
            result = remove_runtime(runtime)
        except Exception:
            self._update_integration_status(runtime)
            return
        self._capabilities[runtime] = result.capability
        self._update_integration_status(runtime)
        self._maybe_render_integrations()

    def _test_agent(self, _button: Any, runtime: AgentRuntime) -> None:
        """Prove the integration boundary off the GTK thread.

        The Codex test drives a real app-server session (bounded ~30 s),
        so the verification always runs on the executor; fake events never
        persist into the real store.
        """
        self._integration_status[runtime].set_text(_("Checking…"))

        def run() -> IntegrationResult:
            try:
                return test_runtime(runtime)
            except Exception:
                return IntegrationResult(
                    False, CapabilityReport("unsupported", _("Callback verification failed."))
                )

        self.executor.submit(run).add_done_callback(
            lambda future: GLib.idle_add(self._apply_test_result, runtime, future.result())
        )

    def _apply_test_result(self, runtime: AgentRuntime, result: IntegrationResult) -> bool:
        self._capabilities[runtime] = result.capability
        self._update_integration_status(runtime)
        self._maybe_render_integrations()
        return False

    def _create_desktop_shortcut(self, *_args: Any) -> None:
        try:
            target, changed = create_shortcut()
            action = _("created") if changed else _("already exists")
            self.settings_status.set_text(f"{_('Desktop shortcut ')}{action}: {target}")
        except Exception as exc:
            self.settings_status.set_text(f"{_('Desktop shortcut is unavailable: ')}{exc}")

    def _remove_desktop_shortcut(self, *_args: Any) -> None:
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

    def _about(self, *_args: Any) -> None:
        dialog = Adw.AboutDialog(
            application_name=_("Moira"),
            application_icon="io.github.moira.QuotaMonitor",
            version=APP_VERSION,
            developer_name="Moira contributors",
            license_type=Gtk.License.MIT_X11,
            comments=_("Claude and Codex quota monitor for Ubuntu"),
        )
        dialog.present(self)
