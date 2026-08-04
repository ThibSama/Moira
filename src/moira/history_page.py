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

from datetime import timedelta
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from .export import export_history  # noqa: E402
from .history_chart import QuotaChart  # noqa: E402
from .history_view import (  # noqa: E402
    DailyTokenStats,
    HistoryReader,
    HistoryViewResult,
    SeriesView,
    TokenSummary,
    _format_local,  # noqa: F401  (re-exported for tests)
    _system_local_converter,
    build_codex_summary_text,
    build_daily_token_stats_text,
    build_history_summary_text,
    build_series_stats_text,
    build_token_availability_note,
    build_token_summary_text,
    derive_content_state,
    format_observation_time,  # noqa: F401  (re-exported for tests)
)
from .i18n import tr  # noqa: E402
from .models import HistoryStatus, Service  # noqa: E402

_ = tr

# Explicit re-exports (pure helpers relocated to history_view for testability).
__all__ = [
    "HistoryPage",
    "build_series_stats_text",
    "format_observation_time",
    "_format_local",
    "_system_local_converter",
]

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
        self._executor = executor
        self._db_path = db_path
        self._exporting = False
        self._deleting = False
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

        # Export / copy / delete actions
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        csv_button = Gtk.Button(label=_("Export CSV"))
        csv_button.connect("clicked", lambda *_: self._on_export("csv"))
        json_button = Gtk.Button(label=_("Export JSON"))
        json_button.connect("clicked", lambda *_: self._on_export("json"))
        copy_button = Gtk.Button(label=_("Copy history summary"))
        copy_button.connect("clicked", self._copy_summary)
        delete_button = Gtk.Button(label=_("Delete all history…"))
        delete_button.connect("clicked", self._on_delete_clicked)
        actions.append(csv_button)
        actions.append(json_button)
        actions.append(copy_button)
        actions.append(delete_button)
        self.append(actions)
        self._export_status = Gtk.Label(xalign=0)
        self._export_status.set_wrap(True)
        self._export_status.add_css_class("dim-label")
        self.append(self._export_status)

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

    def _on_theme_changed(self, *_args: Any) -> None:
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

    def _on_range_changed(self, *_args: Any) -> None:
        self._range_idx = self._range_combo.get_selected()
        self.refresh()

    def _on_filter_changed(self, *_args: Any) -> None:
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
        self._status_label.set_text(_("Loading…"))
        self._reader.request(
            range_func=self._range_func(),
            range_label=RANGES[self._range_idx][1],
            filter_label=_FILTER_KEYS[self._filter_idx][1],
            service=self._current_service(),
        )

    def _range_func(self) -> Any:
        """Return the query function for the selected range."""
        from .history_db import query_7d, query_24h, query_30d, query_90d

        return [query_24h, query_7d, query_30d, query_90d][self._range_idx]

    def _current_service(self) -> Service | None:
        """Return the selected filter service (None = All)."""
        if self._filter_idx == 1:
            return Service.CLAUDE
        if self._filter_idx == 2:
            return Service.CODEX
        return None

    # ── Export (explicit destination, off-GTK read/write) ──

    def _on_export(self, fmt: str) -> None:
        """Open a save dialog for the explicit destination; runs the export
        on the worker thread. Cancelling the dialog writes nothing."""
        if self._destroyed or self._exporting or self._deleting:
            return
        self._exporting = True
        dialog = Gtk.FileDialog()
        dialog.set_title(_("Export history"))
        dialog.set_initial_name(f"moira-history.{fmt}")

        def done(dialog: Gtk.FileDialog, result: Gio.AsyncResult, _user_data: Any = None) -> None:
            self._on_export_dialog_done(dialog, result, fmt)

        try:
            dialog.save(self._parent_window(), None, done)
        except Exception:
            self._exporting = False
            self._export_status.set_text(_("Export failed."))

    def _parent_window(self) -> Gtk.Window | None:
        root = self.get_root()
        return root if isinstance(root, Gtk.Window) else None

    def _on_export_dialog_done(
        self, dialog: Gtk.FileDialog, result: Gio.AsyncResult, fmt: str = "csv"
    ) -> None:
        self._exporting = False
        try:
            file = dialog.save_finish(result)
        except GLib.Error as exc:
            if getattr(exc, "code", None) == Gio.IOErrorEnum.CANCELLED:
                self._export_status.set_text(_("Export cancelled."))
            else:
                self._export_status.set_text(_("Export failed."))
            return
        except Exception:
            self._export_status.set_text(_("Export failed."))
            return
        path = file.get_path()
        if not path:
            self._export_status.set_text(_("Export failed."))
            return
        self._export_status.set_text(_("Exporting…"))
        from pathlib import Path

        from .history_db import history_path

        future = self._executor.submit(
            export_history,
            self._db_path if self._db_path is not None else history_path(),
            range_func=self._range_func(),
            range_delta=RANGES[self._range_idx][2],
            service=self._current_service(),
            fmt=fmt,
            dest=Path(path),
        )
        future.add_done_callback(lambda done: GLib.idle_add(self._export_done, done))

    def _export_done(self, future: Any) -> bool:
        """Show the sanitized export outcome (never raw exceptions)."""
        try:
            result = future.result()
        except Exception:
            self._export_status.set_text(_("Export failed."))
            return False
        if result.ok and result.status == "exported":
            self._export_status.set_text(f"{_('Exported')} {result.rows} {_('rows')}.")
        elif result.status == "no data":
            self._export_status.set_text(_("Nothing to export."))
        elif result.status == "no database":
            self._export_status.set_text(_("No history database"))
        elif result.status == "schema mismatch":
            self._export_status.set_text(_("Schema mismatch"))
        elif result.status == "database unavailable":
            self._export_status.set_text(_("Database unavailable"))
        else:
            self._export_status.set_text(_("Export failed."))
        return False

    # ── Delete all history (confirmed) ──

    def _on_delete_clicked(self, *_args: Any) -> None:
        """Ask for confirmation before deleting every stored observation.
        Settings, keyring and current quota state are kept."""
        if self._destroyed or self._deleting or self._exporting:
            return
        dialog = Adw.MessageDialog.new(
            self._parent_window(),
            _("Delete all history?"),
            _(
                "This removes every stored observation. Settings, keyring and "
                "current quota state are kept."
            ),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("delete", _("Delete"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.connect("response", self._on_delete_response)
        dialog.present()

    def _on_delete_response(self, dialog: Any, response: str) -> None:
        try:
            dialog.close()
        except Exception:
            pass
        if response != "delete":
            return
        self._deleting = True
        self._export_status.set_text(_("Deleting…"))
        future = self._executor.submit(self._delete_worker)
        future.add_done_callback(lambda done: GLib.idle_add(self._delete_done, done))

    def _delete_worker(self) -> int:
        """Delete all history rows off-GTK. Returns the row count deleted."""
        from pathlib import Path

        from .history_db import _connect, delete_all, history_path, init_schema

        db_path = self._db_path if self._db_path is not None else history_path()
        path = Path(db_path) if not isinstance(db_path, Path) else db_path
        if not path.exists():
            return 0
        conn = _connect(path, timeout=5.0)
        try:
            init_schema(conn)
            return delete_all(conn)
        finally:
            conn.close()

    def _delete_done(self, future: Any) -> bool:
        self._deleting = False
        try:
            count = future.result()
        except Exception:
            self._export_status.set_text(_("Deletion failed."))
            return False
        if count > 0:
            self._export_status.set_text(_("History deleted."))
        else:
            self._export_status.set_text(_("History is already empty."))
        self.refresh()
        return False

    # ── Copy history summary (sanitized) ──

    def _copy_summary(self, *_args: Any) -> None:
        if self._current_result is None or self._destroyed:
            return
        text = build_history_summary_text(self._current_result, tr)
        self.get_clipboard().set_text(text)
        self._export_status.set_text(_("History summary copied."))

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

        # Data state — classified by the pure content-state helper so that
        # token summaries, daily statistics, official summaries and
        # availability all count as real History content. "No history data"
        # appears only when nothing at all is renderable; a missing quota
        # series with other content shows the narrower quota-absent note.
        state = derive_content_state(view)
        if state.is_empty:
            self._status_label.set_text(_("No history data for this range"))
        else:
            if state.has_quota_series:
                self._status_label.set_text("")
                for s in view.series:
                    self._stats_box.append(self._series_stats_label(s))
            else:
                self._status_label.set_text(_("No quota observations for this range"))

        # Persisted daily totals (token activity), shown per service/kind
        token_summaries = getattr(view, "token_summaries", ())
        for ts in token_summaries:
            self._stats_box.append(self._token_summary_label(ts))

        # Exact selected-range daily indicators (compact, clearly labeled).
        # Rendered only when exact day rows exist — zero exact days never
        # create a zero card. The service and range are part of the label.
        daily_stats = getattr(view, "daily_token_stats", ())
        for ds in daily_stats:
            self._stats_box.append(self._daily_stats_label(ds, view.range_label))

        # Official Codex summary — displayed separately from daily totals
        codex_summaries = getattr(view, "codex_summaries", ())
        for cs in codex_summaries:
            self._stats_box.append(self._codex_summary_label(cs))

        # Availability note: sanitized secondary state from dedicated records.
        # Show the note for every non-exact state, even alongside exact totals
        # — a temporary/unavailable/invalid state coexists with exact data.
        # Suppress only when latest availability is AVAILABLE_EXACT (i.e. the
        # current run succeeded).
        availability = getattr(view, "token_availability", ())
        notes: list[str] = []
        for state in availability:
            if state.status is HistoryStatus.AVAILABLE_EXACT:
                continue  # suppress: latest attempt succeeded
            text = build_token_availability_note(state.status, tr)
            notes.append(f"{state.service.value.title()}: {text}")
        if not token_summaries and not availability:
            notes.append(_("Exact token usage is not available"))
        self._token_note.set_text(_(" · ").join(notes))

        return False

    @staticmethod
    def _series_stats_label(s: SeriesView) -> Gtk.Widget:
        """Build a label showing stats for one series."""
        text = build_series_stats_text(s.stats, tr, converter=_system_local_converter)
        return Gtk.Label(label=text, xalign=0, wrap=True)

    @staticmethod
    def _token_summary_label(ts: TokenSummary) -> Gtk.Widget:
        """Build a label showing persisted daily token activity."""
        text = build_token_summary_text(ts, tr)
        return Gtk.Label(label=text, xalign=0, wrap=True)

    @staticmethod
    def _daily_stats_label(ds: DailyTokenStats, range_label: str) -> Gtk.Widget:
        """Build a compact label showing exact selected-range daily indicators.

        One wrapped text label per service — narrow-window friendly and
        never dependent on color alone.
        """
        text = build_daily_token_stats_text(ds, range_label, tr)
        return Gtk.Label(label=text, xalign=0, wrap=True)

    @staticmethod
    def _codex_summary_label(cs: Any) -> Gtk.Widget:
        """Build a label showing the official Codex summary, apart from daily totals."""
        text = build_codex_summary_text(cs, tr)
        return Gtk.Label(label=text, xalign=0, wrap=True)
