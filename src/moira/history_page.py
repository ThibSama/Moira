"""History tab UI for Moira.

Shows quota evolution for Claude and Codex with selectable ranges
(24h, 7d, 30d, 90d) and filters (All, Claude, Codex). Uses a genuinely
bounded asynchronous reader so no SQLite work runs on GTK. Refreshes
when the tab becomes visible and after a successful history write.

Lifecycle: ``shutdown()`` is idempotent and must be invoked before the
coordinator shuts down. It cancels reads, disconnects theme handlers,
and rejects all future refresh/render calls. Visibility is tracked both
ways so hidden pages never receive write-triggered reads.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .history_chart import QuotaChart  # noqa: E402
from .history_view import HistoryReader, HistoryViewResult, SeriesView  # noqa: E402
from .i18n import tr  # noqa: E402
from .models import Service  # noqa: E402

_ = tr

RANGES = [
    ("24h", "24h", timedelta(hours=24)),
    ("7d", "7d", timedelta(days=7)),
    ("30d", "30d", timedelta(days=30)),
    ("90d", "90d", timedelta(days=90)),
]

# Filter labels are translated at construction time.
_FILTER_KEYS = [("all", "All"), ("claude", "Claude"), ("codex", "Codex")]


def _glib_dispatcher(callback: Any, view: Any, req_id: int = 0) -> None:
    """Dispatch the callback via GLib.idle_add."""
    GLib.idle_add(callback, view, req_id)


class HistoryPage(Gtk.Box):
    """The History tab page."""

    def __init__(self, executor: Any, *, db_path: Any = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.set_margin_top(18)
        self.set_margin_bottom(18)
        self.set_margin_start(18)
        self.set_margin_end(18)

        self._reader = HistoryReader(
            executor,
            dispatcher=_glib_dispatcher,
            db_path=db_path,
        )
        self._reader.set_callback(self._on_result)
        self._current_result: HistoryViewResult | None = None
        self._visible = False
        self._range_idx = 0
        self._filter_idx = 0
        self._destroyed = False
        self._style_manager: Any = None
        self._theme_handler_id: int = 0

        # Translate filter labels before constructing the DropDown
        self._filter_labels = [tr(f[1]) for f in _FILTER_KEYS]

        self._build()

    def _build(self) -> None:
        # Range selector
        range_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        range_box.append(Gtk.Label(label=_("Range"), xalign=0, hexpand=True))
        self._range_combo = Gtk.DropDown.new_from_strings([tr(r[1]) for r in RANGES])
        self._range_combo.set_selected(0)
        self._range_combo.connect("notify::selected", self._on_range_changed)
        range_box.append(self._range_combo)
        self.append(range_box)

        # Filter selector (translated)
        filter_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        filter_box.append(Gtk.Label(label=_("Filter"), xalign=0, hexpand=True))
        self._filter_combo = Gtk.DropDown.new_from_strings(self._filter_labels)
        self._filter_combo.set_selected(0)
        self._filter_combo.connect("notify::selected", self._on_filter_changed)
        filter_box.append(self._filter_combo)
        self.append(filter_box)

        # Status label (loading, empty, error states)
        self._status_label = Gtk.Label(xalign=0)
        self._status_label.set_wrap(True)
        self._status_label.add_css_class("dim-label")
        self.append(self._status_label)

        # Token availability note (secondary, never replaces quota data)
        self._token_note = Gtk.Label(xalign=0)
        self._token_note.set_wrap(True)
        self._token_note.add_css_class("dim-label")
        self._token_note.set_text(_("Exact token usage is not available"))
        self.append(self._token_note)

        # Chart
        self._chart = QuotaChart()
        self._chart.set_vexpand(True)
        self.append(self._chart)

        # Stats box (per-series stats)
        self._stats_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.append(self._stats_box)

        # Theme detection via Adw.StyleManager
        self._setup_theme()

    def _setup_theme(self) -> None:
        """Connect to Adw.StyleManager for live dark-state changes."""
        try:
            self._style_manager = Adw.StyleManager.get_default()
            if self._style_manager is not None:
                self._theme_handler_id = self._style_manager.connect(
                    "notify::dark", self._on_theme_changed
                )
                self._apply_theme()
        except Exception:
            pass

    def _on_theme_changed(self, *_: Any) -> None:
        """React to live dark-state changes."""
        self._apply_theme()

    def _apply_theme(self) -> None:
        """Apply the current theme from Adw.StyleManager."""
        if self._style_manager is not None:
            self._chart.set_dark(self._style_manager.get_dark())

    def shutdown(self) -> None:
        """Idempotent shutdown. Cancels reads, disconnects theme, rejects future work."""
        if self._destroyed:
            return
        self._destroyed = True
        self._reader.cancel()
        if self._theme_handler_id and self._style_manager is not None:
            try:
                self._style_manager.disconnect(self._theme_handler_id)
            except Exception:
                pass
            self._theme_handler_id = 0

    def _on_range_changed(self, *_: Any) -> None:
        self._range_idx = self._range_combo.get_selected()
        self.refresh()

    def _on_filter_changed(self, *_: Any) -> None:
        self._filter_idx = self._filter_combo.get_selected()
        self.refresh()

    def on_visible(self) -> None:
        """Called when the History tab becomes visible."""
        self._visible = True
        self._apply_theme()
        self.refresh()

    def on_hidden(self) -> None:
        """Called when the History tab becomes hidden."""
        self._visible = False

    def on_refresh_complete(self) -> bool:
        """Called after a successful history write (via GLib.idle_add)."""
        if self._destroyed:
            return False
        if self._visible:
            self.refresh()
        return False

    def refresh(self) -> None:
        """Request a history read from the worker thread."""
        if self._destroyed:
            return
        from .history_db import query_7d, query_24h, query_30d, query_90d

        range_funcs = [query_24h, query_7d, query_30d, query_90d]
        range_func = range_funcs[self._range_idx]
        range_label = RANGES[self._range_idx][1]
        filter_label = _FILTER_KEYS[self._filter_idx][1]

        service: Service | None = None
        if self._filter_idx == 1:
            service = Service.CLAUDE
        elif self._filter_idx == 2:
            service = Service.CODEX

        self._status_label.set_text(_("Loading…"))
        self._reader.request(
            range_func=range_func,
            range_label=range_label,
            filter_label=filter_label,
            service=service,
        )

    def _on_result(self, view: HistoryViewResult, req_id: int = 0) -> None:
        """Called via GLib.idle_add with the newest result.

        Rechecks cancellation/request generation at dispatch time to
        prevent delivery to a destroyed or superseded page.
        """
        if self._destroyed:
            return
        # Recheck: if the reader has been cancelled or a newer request
        # has been issued since this callback was queued, discard.
        if req_id != 0 and not self._reader.is_current(req_id):
            return
        self._render_result(view)

    def _render_result(self, view: HistoryViewResult) -> bool:
        if self._destroyed:
            return False
        self._current_result = view
        self._chart.set_series(list(view.series))

        # Clear stats
        while self._stats_box.get_first_child() is not None:
            self._stats_box.remove(self._stats_box.get_first_child())

        # Handle all diagnostic states
        diag = view.diagnostic
        if diag == "no database":
            self._status_label.set_text(_("No history database"))
            self._chart.set_series([])
            return False
        if diag == "database unavailable":
            self._status_label.set_text(_("Database unavailable"))
            self._chart.set_series([])
            return False
        if diag == "schema mismatch":
            self._status_label.set_text(_("Schema mismatch"))
            self._chart.set_series([])
            return False
        if diag == "empty range":
            self._status_label.set_text(_("No history data for this range"))
            self._chart.set_series([])
            return False
        if diag == "loading":
            self._status_label.set_text(_("Loading…"))
            return False

        # Data state
        if not view.series:
            self._status_label.set_text(_("No history data for this range"))
        else:
            self._status_label.set_text("")
            for s in view.series:
                self._stats_box.append(self._series_stats_label(s))

        return False

    @staticmethod
    def _series_stats_label(s: SeriesView) -> Gtk.Widget:
        """Build a label showing stats for one series."""
        stats = s.stats
        parts: list[str] = [f"{stats.service.value.title()} {stats.label}"]
        if stats.count == 0:
            parts.append(_("No observations"))
        else:
            if stats.latest is not None:
                parts.append(f"{_('Latest')}: {stats.latest:.1f}%")
            if stats.minimum is not None:
                parts.append(f"{_('Min')}: {stats.minimum:.1f}%")
            if stats.maximum is not None:
                parts.append(f"{_('Max')}: {stats.maximum:.1f}%")
            parts.append(f"{_('Count')}: {stats.count}")
            if stats.reset_count > 0:
                parts.append(f"{_('Resets')}: {stats.reset_count}")
            # First/last observation in local time (pure presentation helper)
            if stats.first_observed is not None:
                parts.append(f"{_('First')}: {format_observation_time(stats.first_observed)}")
            if stats.last_observed is not None:
                parts.append(f"{_('Last')}: {format_observation_time(stats.last_observed)}")
        label = Gtk.Label(label=_(" · ").join(parts), xalign=0, wrap=True)
        return label


def format_observation_time(
    utc_dt: datetime,
    target_tz: timezone | None = None,
) -> str:
    """Format a UTC datetime in the target timezone for display only.

    Pure function. Defaults to the system local timezone when
    ``target_tz`` is None. Naive timestamps (no tzinfo) raise
    ValueError (fail-closed) — they must never be silently interpreted.
    """
    if utc_dt.tzinfo is None:
        raise ValueError("naive timestamps are not allowed; use timezone-aware datetimes")
    tz = target_tz if target_tz is not None else UTC
    local_dt = utc_dt.astimezone(tz)
    return local_dt.strftime("%Y-%m-%d %H:%M")


def _format_local(utc_dt: datetime) -> str:
    """Format a UTC datetime in the system local timezone for display only."""
    return format_observation_time(utc_dt, target_tz=None)
