"""GTK activity panel and file watcher.

The panel (``ActivityPanel``) shows one row per agent runtime (Claude Code,
Codex CLI, Hermes): a ``Gtk.Spinner`` while the runtime is active, the
active count when more than one session is running, the latest sanitized
model label, and the correct symbolic state icon for exactly five minutes
after the last active turn ends — then the row hides.

``ActivityWatcher`` watches the activity store with ``Gio.FileMonitor`` on
the state directory (replacement-safe, tolerates deletion), coalesces
bursts onto the GLib idle loop, runs the watchdog (stale running sessions
expire to INTERRUPTED, never to success) and hides terminal rows after
five minutes — all with a single bounded, self-rescheduling timer that is
cancelled when there is nothing to show, and released on shutdown.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

from .activity import (
    TERMINAL_WINDOW_SECONDS,
    WATCHDOG_STALE_SECONDS,
    ActivityState,
    ActivityStore,
    AgentRuntime,
    RuntimeActivity,
    derive_runtime_activity,
)
from .i18n import tr

_ = tr

#: Symbolic icon per terminal state.
_STATE_ICONS = {
    ActivityState.COMPLETED: "emblem-ok-symbolic",
    ActivityState.FAILED: "dialog-error-symbolic",
    ActivityState.INTERRUPTED: "process-stop-symbolic",
}

#: Human labels per state (translated at call time).
_STATE_LABELS = {
    ActivityState.RUNNING: "Active",
    ActivityState.COMPLETED: "Completed",
    ActivityState.FAILED: "Failed",
    ActivityState.INTERRUPTED: "Interrupted",
}

_RUNTIME_LABELS = {
    AgentRuntime.CLAUDE: "Claude Code",
    AgentRuntime.CODEX: "Codex CLI",
    AgentRuntime.HERMES: "Hermes",
}

#: Watchdog period used by the panel's timer arithmetic.
_WATCHDOG_TICK_SECONDS = 60


class ActivityRow(Gtk.Box):
    """One runtime row: spinner/icon, state, count and model label."""

    def __init__(self, runtime: AgentRuntime) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.runtime = runtime
        self.spinner = Gtk.Spinner()
        self.spinner.set_tooltip_text(_("Agent is working"))
        self.icon = Gtk.Image(icon_name="emblem-ok-symbolic")
        self.icon.set_visible(False)
        self.append(self.spinner)
        self.append(self.icon)
        self.runtime_label = Gtk.Label(label=_(runtime.value), xalign=0)
        self.runtime_label.add_css_class("heading")
        self.append(self.runtime_label)
        self.state_label = Gtk.Label(label="", xalign=0)
        self.state_label.add_css_class("dim-label")
        self.append(self.state_label)
        self.count_label = Gtk.Label(label="", xalign=0)
        self.count_label.add_css_class("dim-label")
        self.append(self.count_label)
        self.model_label = Gtk.Label(label="", xalign=0)
        self.model_label.add_css_class("dim-label")
        self.append(self.model_label)

    def update(self, view: RuntimeActivity) -> None:
        """Render one derived runtime state (must run on the GTK thread)."""
        if not view.visible or view.state is None:
            self.set_visible(False)
            self.spinner.stop()
            self.spinner.set_visible(False)
            self.icon.set_visible(False)
            return
        self.set_visible(True)
        self.runtime_label.set_text(_(_RUNTIME_LABELS[self.runtime]))
        if view.state is ActivityState.RUNNING:
            self.spinner.set_visible(True)
            self.spinner.start()
            self.icon.set_visible(False)
        else:
            self.spinner.stop()
            self.spinner.set_visible(False)
            self.icon.set_from_icon_name(_STATE_ICONS.get(view.state, "emblem-ok-symbolic"))
            self.icon.set_visible(True)
        self.state_label.set_text(_(_STATE_LABELS.get(view.state, "")))
        if view.active_count > 1:
            self.count_label.set_text(_("{count} active").format(count=view.active_count))
        else:
            self.count_label.set_text("")
        self.model_label.set_text(view.model if view.model else "")


class ActivityPanel(Gtk.Frame):
    """Accessible EN/FR agent activity panel (below the quota cards).

    Present in both full and compact modes; independent from quotas. The
    whole panel is hidden when no runtime has anything to show.
    """

    def __init__(self) -> None:
        super().__init__()
        self._box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._box.set_margin_top(8)
        self._box.set_margin_bottom(8)
        self._box.set_margin_start(16)
        self._box.set_margin_end(16)
        heading = Gtk.Label(label=_("Agent activity"), xalign=0)
        heading.add_css_class("title-3")
        self._box.append(heading)
        self.rows: dict[AgentRuntime, ActivityRow] = {}
        for runtime in AgentRuntime:
            row = ActivityRow(runtime)
            self._box.append(row)
            self.rows[runtime] = row
        self.set_child(self._box)
        self.set_visible(False)

    def update(self, views: dict[AgentRuntime, RuntimeActivity]) -> None:
        """Render the derived per-runtime states (GTK thread only)."""
        any_visible = False
        for runtime, view in views.items():
            self.rows[runtime].update(view)
            any_visible = any_visible or view.visible
        self.set_visible(any_visible)


class ActivityWatcher:
    """FileMonitor + bounded-timer bridge between the store and the UI.

    ``on_change`` is invoked on the GTK thread after any store mutation is
    observed (external hook writes via FileMonitor, watchdog expiries, or
    five-minute hide deadlines). Monitors and timers are released by
    ``shutdown``.
    """

    def __init__(
        self,
        store: ActivityStore,
        on_change: Callable[[], None],
    ) -> None:
        self._store = store
        self._on_change = on_change
        self._closed = False
        self._monitor: Any = None
        self._timer_id: int | None = None
        self._reload_scheduled = False
        self._start_monitor()
        self._schedule_next()

    # ── FileMonitor ──

    def _start_monitor(self) -> None:
        try:
            directory = self._store.path.parent
            directory.mkdir(parents=True, exist_ok=True)
            monitor = Gio.File.new_for_path(str(directory)).monitor_directory(
                Gio.FileMonitorFlags.NONE, None
            )
            if monitor is None:
                return
            monitor.connect("changed", self._on_file_changed)
            self._monitor = monitor
        except Exception:
            self._monitor = None

    def _on_file_changed(
        self, _monitor: Any, file: Any, other_file: Any | None, event_type: int
    ) -> None:
        if self._closed or file is None:
            return
        name = file.get_basename() or ""
        if name != self._store.path.name:
            return
        # Coalesce bursts: react only to the done hint, and marshal a
        # single reload onto the idle loop even for monitor noise.
        if event_type in (Gio.FileMonitorEvent.CHANGES_DONE_HINT, Gio.FileMonitorEvent.DELETED):
            self._marshal_reload()

    def _marshal_reload(self) -> None:
        if self._reload_scheduled:
            return
        self._reload_scheduled = True
        GLib.idle_add(self._do_reload)

    def _do_reload(self) -> bool:
        self._reload_scheduled = False
        if self._closed:
            return False
        self._store.reload()
        self._schedule_next()
        self._on_change()
        return False

    # ── Bounded timers (watchdog + five-minute hide) ──

    def _schedule_next(self) -> None:
        if self._closed:
            return
        if self._timer_id is not None:
            GLib.source_remove(self._timer_id)
            self._timer_id = None
        now = datetime.now(UTC)
        changed = self._store.expire_stale(now, stale_after=WATCHDOG_STALE_SECONDS)
        if changed:
            GLib.idle_add(self._notify_change)
        views = derive_runtime_activity(self._store.snapshot(), now=now)
        next_times: list[datetime] = []
        for view in views.values():
            if not view.visible:
                continue
            if view.state is ActivityState.RUNNING:
                running = self._store.sessions_for(view.runtime)
                updates = [
                    datetime.fromisoformat(turn["updated_at"])
                    for session in running.values()
                    for turn in session["turns"].values()
                    if turn["state"] == ActivityState.RUNNING.value
                ]
                if updates:
                    next_times.append(max(updates) + timedelta(seconds=WATCHDOG_STALE_SECONDS))
            elif view.at is not None:
                next_times.append(view.at + timedelta(seconds=TERMINAL_WINDOW_SECONDS))
        if not next_times:
            return  # nothing to watch: the timer stays cancelled (bounded)
        delay = max(1.0, (min(next_times) - now).total_seconds())
        self._timer_id = GLib.timeout_add(int(delay * 1000), self._on_timer)

    def _on_timer(self) -> bool:
        self._timer_id = None
        if self._closed:
            return False
        self._schedule_next()
        self._notify_change()
        return False

    def _notify_change(self) -> None:
        if not self._closed:
            self._on_change()

    # ── Shutdown ──

    def shutdown(self) -> None:
        """Release the monitor and every timer (idempotent)."""
        if self._closed:
            return
        self._closed = True
        if self._monitor is not None:
            try:
                self._monitor.cancel()
            except Exception:
                pass
            self._monitor = None
        if self._timer_id is not None:
            GLib.source_remove(self._timer_id)
            self._timer_id = None


def latest_terminal_event(
    data: dict[str, Any], runtime: AgentRuntime
) -> tuple[ActivityState, datetime, str] | None:
    """Most recent terminal event for a runtime (last-event + sessions).

    Sanitized display input for the settings page; returns
    ``(state, at, model)`` or None when the runtime has no terminal event.
    """
    candidates: list[tuple[datetime, ActivityState, str]] = []
    last_event = data.get("last_events", {}).get(runtime.value)
    if last_event is not None:
        try:
            candidates.append(
                (
                    datetime.fromisoformat(last_event["at"]),
                    ActivityState(last_event["state"]),
                    last_event.get("model", ""),
                )
            )
        except (ValueError, KeyError):
            pass
    for session in data.get("sessions", {}).get(runtime.value, {}).values():
        for turn in session["turns"].values():
            if turn["state"] == ActivityState.RUNNING.value:
                continue
            try:
                candidates.append(
                    (
                        datetime.fromisoformat(turn["updated_at"]),
                        ActivityState(turn["state"]),
                        turn.get("model", ""),
                    )
                )
            except (ValueError, KeyError):
                pass
    if not candidates:
        return None
    at, state, model = max(candidates, key=lambda item: item[0])
    return state, at, model
