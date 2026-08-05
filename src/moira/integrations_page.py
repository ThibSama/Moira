"""Package 7a — GTK Integrations page.

The scrollable Integrations page renders the immutable
``IntegrationSnapshot``: an Agents section holding the per-runtime
Set up / Remove / Test controls (owned by MainWindow, moved here from
Settings without behavior change) and a Providers and models section
with sanitized assignments and independent capability badges.

Refreshes happen only on page visibility and the explicit Refresh
button, both routed through MainWindow's bounded newest-wins
``IntegrationCoordinator``; the page itself never probes.
"""

from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk  # noqa: E402

from .i18n import tr
from .integrations import IntegrationSnapshot, IntegrationState

_ = tr

#: Capability slug → display key (translated at render time).
_CAPABILITY_LABELS = {
    "activity": "Activity",
    "quota_percentage": "Quota percentage",
    "exact_tokens": "Exact tokens",
    "balance": "Balance",
    "cost": "Cost",
}

#: Exact state → display key (translated at render time).
_STATE_LABELS = {
    IntegrationState.AVAILABLE: "Available",
    IntegrationState.NOT_CONFIGURED: "Not configured",
    IntegrationState.NOT_INSTALLED: "Not installed",
    IntegrationState.UNSUPPORTED: "Unsupported",
    IntegrationState.TEMPORARILY_UNAVAILABLE: "Temporarily unavailable",
    IntegrationState.INVALID: "Invalid",
}

_RUNTIME_SLUGS = ("claude", "codex", "hermes")


class IntegrationsPage(Gtk.Box):
    """Scrollable page: Agents controls + Providers and models.

    MainWindow appends the three agent rows (Set up / Remove / Test) into
    ``agents_box`` and keeps its existing status-label wiring; the
    providers section is rebuilt from each published snapshot.
    """

    def __init__(self, *, on_visible_refresh: Any = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._shutdown = False
        self._visible = False
        self._on_visible_refresh = on_visible_refresh
        self.set_margin_top(18)
        self.set_margin_bottom(18)
        self.set_margin_start(18)
        self.set_margin_end(18)

        header = Gtk.Box(spacing=8)
        self.refresh_button = Gtk.Button(label=_("Refresh"))
        self.refresh_button.connect("clicked", self._on_refresh_clicked)
        header.append(self.refresh_button)
        self.status_label = Gtk.Label(xalign=0)
        self.status_label.set_wrap(True)
        self.status_label.add_css_class("dim-label")
        header.append(self.status_label)
        self.append(header)

        agents_heading = Gtk.Label(label=_("Agents"), xalign=0)
        agents_heading.add_css_class("heading")
        self.append(agents_heading)
        self.agents_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.append(self.agents_box)

        providers_heading = Gtk.Label(label=_("Providers and models"), xalign=0)
        providers_heading.add_css_class("heading")
        self.append(providers_heading)
        self._providers_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.append(self._providers_box)
        self._updated_label = Gtk.Label(xalign=0)
        self._updated_label.add_css_class("dim-label")
        self.append(self._updated_label)

    # ── Lifecycle / refresh routing ──

    def _on_refresh_clicked(self, *_args: Any) -> None:
        if self._shutdown or self._on_visible_refresh is None:
            return
        self._on_visible_refresh()

    def on_visible(self) -> None:
        """Page became visible: request one bounded inventory refresh."""
        self._visible = True
        if self._shutdown or self._on_visible_refresh is None:
            return
        self._on_visible_refresh()

    def on_hidden(self) -> None:
        self._visible = False

    def is_visible_page(self) -> bool:
        return self._visible

    def shutdown(self) -> None:
        """Idempotent: stop routing refreshes and reject further renders."""
        self._shutdown = True
        self._visible = False

    def render_status(self, text: str) -> None:
        """Set the sanitized translated status line (main thread)."""
        self.status_label.set_text(text)

    # ── Snapshot rendering ──

    def render_snapshot(self, snapshot: IntegrationSnapshot) -> None:
        """Rebuild the Providers and models section from one snapshot."""
        while child := self._providers_box.get_first_child():
            self._providers_box.remove(child)
        if not snapshot.assignments:
            note = Gtk.Label(label=_("No model assignments discovered."), xalign=0)
            note.add_css_class("dim-label")
            self._providers_box.append(note)
        for provider in snapshot.providers:
            self._providers_box.append(self._provider_row(snapshot, provider))
        local = snapshot.observed_at.astimezone()
        self._updated_label.set_text(f"{_('Last refresh: ')}{local:%H:%M:%S} · {snapshot.source}")

    def _provider_row(self, snapshot: IntegrationSnapshot, provider: Any) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        header = Gtk.Box(spacing=8)
        label = Gtk.Label(label=provider.label, xalign=0, hexpand=True)
        label.add_css_class("heading")
        header.append(label)
        assignments = [a for a in snapshot.assignments if a.provider.slug == provider.slug]
        if not assignments:
            if provider.slug in _RUNTIME_SLUGS:
                placeholder = Gtk.Label(label="—", xalign=0)
                placeholder.add_css_class("dim-label")
                header.append(placeholder)
        else:
            for assignment in assignments:
                role = _("Main") if assignment.role == "main" else _("Named")
                model = assignment.model or "—"
                text = f"{model} ({role})"
                if assignment.state is not IntegrationState.AVAILABLE:
                    state_label = _STATE_LABELS.get(assignment.state, assignment.state.value)
                    text += f" · {_(state_label)}"
                header.append(Gtk.Label(label=text, xalign=0))
        row.append(header)

        flow = Gtk.FlowBox()
        flow.set_column_spacing(8)
        flow.set_row_spacing(4)
        flow.set_max_children_per_line(5)
        for capability in snapshot.capabilities:
            if capability.provider != provider.slug:
                continue
            flow.append(self._badge(capability))
        row.append(flow)
        return row

    def _badge(self, capability: Any) -> Gtk.Widget:
        cap_label = tr(_CAPABILITY_LABELS.get(capability.capability, capability.capability))
        state_label = tr(_STATE_LABELS.get(capability.state, capability.state.value))
        text = f"{cap_label}: {state_label}"
        if capability.detail:
            text += f" ({tr(capability.detail)})"
        badge = Gtk.Label(label=text)
        badge.set_wrap(True)
        if capability.state is IntegrationState.AVAILABLE:
            badge.add_css_class("success")
        elif capability.state is IntegrationState.TEMPORARILY_UNAVAILABLE:
            badge.add_css_class("warning")
        else:
            badge.add_css_class("dim-label")
        return badge
