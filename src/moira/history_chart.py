"""GTK4/Libadwaita chart widget using DrawingArea/Cairo.

Renders quota percentage evolution as a line chart with reset markers.
All series drawn on a shared time axis (the union of all observation
times). Dash patterns provide non-color-only identification. Adapts
background/foreground to the GTK light/dark theme.
"""

from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk  # noqa: E402

from .history_view import SeriesView  # noqa: E402
from .i18n import tr  # noqa: E402

_ = tr

# Line patterns for non-color-only identification.
_LINE_PATTERNS: list[list[float]] = [
    [],  # solid
    [4.0, 2.0],  # dashed
    [1.0, 2.0],  # dotted
    [6.0, 2.0, 1.0, 2.0],  # dash-dot
]


class QuotaChart(Gtk.DrawingArea):
    """A line chart for quota percentage evolution with shared time axis."""

    def __init__(self) -> None:
        super().__init__()
        self._series: list[SeriesView] = []
        self._width = 400
        self._height = 200
        self._dark = False
        self.set_content_width(self._width)
        self.set_content_height(self._height)
        self.set_draw_func(self._draw)

    def set_series(self, series: list[SeriesView]) -> None:
        """Update the chart data and queue a redraw."""
        self._series = series
        self.queue_draw()

    def set_dark(self, dark: bool) -> None:
        """Set whether to render in dark mode."""
        self._dark = dark
        self.queue_draw()

    def _draw(self, _area: Gtk.DrawingArea, cr: Any, width: int, height: int) -> None:
        """Draw the chart using Cairo."""
        self._width = width
        self._height = height

        # Theme-adaptive colors
        if self._dark:
            bg = (0.12, 0.12, 0.12)
            grid = (0.25, 0.25, 0.25)
            text = (0.7, 0.7, 0.7)
            empty_text = (0.5, 0.5, 0.5)
        else:
            bg = (0.95, 0.95, 0.95)
            grid = (0.8, 0.8, 0.8)
            text = (0.3, 0.3, 0.3)
            empty_text = (0.5, 0.5, 0.5)

        # Background
        cr.set_source_rgb(*bg)
        cr.rectangle(0, 0, width, height)
        cr.fill()

        if not self._series or all(not s.points for s in self._series):
            cr.set_source_rgb(*empty_text)
            cr.move_to(width // 2 - 50, height // 2)
            cr.show_text(_("No data"))
            return

        # Margins
        margin_left = 40
        margin_right = 10
        margin_top = 10
        margin_bottom = 20
        chart_w = width - margin_left - margin_right
        chart_h = height - margin_top - margin_bottom

        # Shared time axis: union of all observation times across series
        all_times: list[Any] = []
        for s in self._series:
            all_times.extend(p.observed_at for p in s.points)
        if not all_times:
            cr.set_source_rgb(*empty_text)
            cr.move_to(width // 2 - 50, height // 2)
            cr.show_text(_("No data"))
            return

        t_min = min(all_times)
        t_max = max(all_times)
        t_range = (t_max - t_min).total_seconds() or 1.0

        # Draw Y axis (0-100%)
        cr.set_source_rgb(*grid)
        cr.set_line_width(1.0)
        for pct in (0, 25, 50, 75, 100):
            y = margin_top + chart_h * (1 - pct / 100)
            cr.move_to(margin_left, y)
            cr.line_to(width - margin_right, y)
            cr.stroke()
            cr.set_source_rgb(*text)
            cr.move_to(5, y + 4)
            cr.show_text(f"{pct}%")
            cr.set_source_rgb(*grid)

        # Colors for series
        colors = [
            (0.2, 0.4, 0.8) if not self._dark else (0.4, 0.6, 1.0),  # blue
            (0.8, 0.4, 0.2) if not self._dark else (1.0, 0.6, 0.4),  # orange
            (0.2, 0.7, 0.3) if not self._dark else (0.4, 0.9, 0.5),  # green
            (0.7, 0.2, 0.7) if not self._dark else (0.9, 0.4, 0.9),  # purple
        ]

        # Draw each series on the shared time axis
        for idx, s in enumerate(self._series):
            if not s.points:
                continue

            color = colors[idx % len(colors)]
            dash = _LINE_PATTERNS[idx % len(_LINE_PATTERNS)]
            cr.set_source_rgb(*color)
            cr.set_line_width(2.0)
            cr.set_dash(dash)

            for i, pt in enumerate(s.points):
                x = margin_left + chart_w * ((pt.observed_at - t_min).total_seconds() / t_range)
                y = margin_top + chart_h * (1 - pt.percentage / 100)
                if i == 0:
                    cr.move_to(x, y)
                else:
                    cr.line_to(x, y)
            cr.stroke()

            # Reset markers
            cr.set_dash([])
            cr.set_source_rgb(*color)
            for pt in s.points:
                if pt.is_reset:
                    x = margin_left + chart_w * ((pt.observed_at - t_min).total_seconds() / t_range)
                    y = margin_top + chart_h * (1 - pt.percentage / 100)
                    cr.arc(x, y, 3, 0, 6.28)
                    cr.fill()

            # Series label
            label_y = margin_top + 12 * idx + 2
            cr.move_to(width - margin_right - 80, label_y)
            cr.show_text(f"{s.stats.service.value} {s.stats.label}")
