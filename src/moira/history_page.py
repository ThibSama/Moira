"""History tab UI for Moira.

Shows quota evolution for Claude and Codex with selectable ranges
(24h, 7d, 30d, 90d) and filters (All, Claude, Codex). Uses a genuinely
bounded asynchronous reader so no SQLite work runs on GTK. Refreshes
when the tab becomes visible and after a successful history write.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib, Gtk  # noqa: E402

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

FILTERS = [("all", "All"), ("claude", "Claude"), ("codex", "Codex")]


def _glib_dispatcher(callback: Any, view: Any) -> None:
    """Dispatch the callback via GLib.idle_add."""
    GLib.idle_add(callback, view)


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

        self._build()

    def _build(self) -> None:
        # Range selector
        range_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        range_box.append(Gtk.Label(label=_("Range"), xalign=0, hexpand=True))
        self._range_combo = Gtk.DropDown.new_from_strings([r[1] for r in RANGES])
        self._range_combo.set_selected(0)
        self._range_combo.connect("notify::selected", self._on_range_changed)
        range_box.append(self._range_combo)
        self.append(range_box)

        # Filter selector
        filter_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        filter_box.append(Gtk.Label(label=_("Filter"), xalign=0, hexpand=True))
        self._filter_combo = Gtk.DropDown.new_from_strings([f[1] for f in FILTERS])
        self._filter_combo.set_selected(0)
        self._filter_combo.connect("notify::selected", self._on_filter_changed)
        filter_box.append(self._filter_combo)
        self.append(filter_box)

        # Status label (loading, empty, error states)
        self._status_label = Gtk.Label(xalign=0)
        self._status_label.set_wrap(True)
        self._status_label.add_css_class("dim-label")
        self.append(self._status_label)

        # Chart
        self._chart = QuotaChart()
        self._chart.set_vexpand(True)
        self.append(self._chart)

        # Stats box (per-series stats)
        self._stats_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.append(self._stats_box)

        # Detect dark mode from the style context
        self._update_theme()

    def _update_theme(self) -> None:
        """Detect light/dark theme from the application's color scheme."""
        try:
            settings = Gtk.Settings.get_default()
            if settings is not None:
                dark = settings.get_property("gtk-application-prefer-dark-theme")
                self._chart.set_dark(bool(dark))
        except Exception:
            pass

    def _on_range_changed(self, *_: Any) -> None:
        self._range_idx = self._range_combo.get_selected()
        self.refresh()

    def _on_filter_changed(self, *_: Any) -> None:
        self._filter_idx = self._filter_combo.get_selected()
        self.refresh()

    def on_visible(self) -> None:
        """Called when the History tab becomes visible."""
        self._visible = True
        self._update_theme()
        self.refresh()

    def on_refresh_complete(self) -> bool:
        """Called after a successful history write (via GLib.idle_add)."""
        if self._destroyed:
            return False
        if self._visible:
            self.refresh()
        return False

    def refresh(self) -> None:
        """Request a history read from the worker thread."""
        from .history_db import query_7d, query_24h, query_30d, query_90d

        range_funcs = [query_24h, query_7d, query_30d, query_90d]
        range_func = range_funcs[self._range_idx]
        range_label = RANGES[self._range_idx][1]
        filter_label = FILTERS[self._filter_idx][1]

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

    def _on_result(self, view: HistoryViewResult) -> None:
        """Called via GLib.idle_add with the newest result."""
        if self._destroyed:
            return
        self._render_result(view)

    def _render_result(self, view: HistoryViewResult) -> bool:
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
        if diag == "exact tokens unavailable":
            self._status_label.set_text(_("Exact token usage is not available"))
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
        label = Gtk.Label(label=_(" · ").join(parts), xalign=0, wrap=True)
        return label
