"""Package 7d — GTK provider profile editor ("Edit providers").

The editor manages local provider profiles and their Moira-owned Keyring
credentials. Keyring and persistence work runs off GTK through a bounded
generation pipeline: at most one operation is in flight plus one parked
(newest-wins replacement), and a stale completion — whose generation no
longer matches — never overwrites newer form state. The editor never
mutates Hermes configuration; it only writes Moira's own config file and
its own Keyring items.

Privacy: the UI shows only ``configured`` / ``not configured`` and
translated sanitized outcomes. Raw libsecret exceptions, paths, URLs,
profile JSON and secrets never reach the UI.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib, Gtk  # noqa: E402

from .i18n import tr
from .integrations import (
    MAX_PROFILES,
    ProviderKind,
    ProviderProfile,
)
from .persistence import load_settings, save_settings
from .secrets import (
    clear_provider_secret,
    get_provider_secret,
    has_provider_secret,
    set_provider_secret,
)

_ = tr

#: Kind order for the dropdown and display labels (translated at render).
_KIND_ORDER: tuple[ProviderKind, ...] = tuple(ProviderKind)
_KIND_LABELS = {
    ProviderKind.DEEPSEEK: "DeepSeek",
    ProviderKind.OPENAI_COMPATIBLE: "OpenAI compatible",
    ProviderKind.OPENROUTER: "OpenRouter",
    ProviderKind.ANTHROPIC: "Anthropic",
    ProviderKind.OPENAI: "OpenAI",
    ProviderKind.LOCAL: "Local",
    ProviderKind.CUSTOM: "Custom",
}

#: Stable sanitized success text per operation kind.
_SUCCESS_TEXT = {
    "save_profile": "Profile saved.",
    "remove_profile": "Profile removed.",
    "remove_credential": "Credential removed.",
}


@dataclass(frozen=True, slots=True)
class ProfileOp:
    """One bounded off-GTK mutation, captured entirely on the GTK thread.

    Ops are typed DELTAS, never full profile tuples: each op carries the
    profile being upserted (``save_profile``) or the slug being removed,
    and is applied to the latest committed collection at execution time.
    Pending ops therefore never capture stale collections, so rapid edits
    cannot overwrite committed changes. ``old_slug`` marks a rename
    (create-new plus explicit old removal); ``credential`` may be blank
    (preserve/migrate the existing secret).
    """

    kind: str  # reload | save_profile | remove_profile | remove_credential
    profile: ProviderProfile | None = None
    slug: str = ""
    old_slug: str = ""
    credential: str = ""


@dataclass(frozen=True, slots=True)
class ProfileOpResult:
    """Sanitized outcome of one operation (never carries secrets or raw
    exceptions; ``reason`` is a stable translated key)."""

    ok: bool
    reason: str = ""
    profiles: tuple[ProviderProfile, ...] = ()
    configured: tuple[tuple[str, bool], ...] = ()
    kind: str = ""
    slug: str = ""


def _keyring_states(profiles: tuple[ProviderProfile, ...]) -> tuple[tuple[str, bool], ...]:
    """Slug → credential-configured for every profile (safe lookups)."""
    return tuple((profile.slug, has_provider_secret(profile.slug)) for profile in profiles)


def _persist_profiles(profiles: tuple[ProviderProfile, ...]) -> None:
    """Validate the whole collection and persist it (raises on failure)."""
    settings = load_settings()
    settings.provider_profiles = profiles
    settings.validate()
    save_settings(settings)


def _restore_credential(slug: str, value: str | None) -> None:
    """Best-effort compensation: restore the prior secret state at a slug."""
    if value is None:
        clear_provider_secret(slug)
    else:
        set_provider_secret(slug, value)


def _ok_result(kind: str, profiles: tuple[ProviderProfile, ...], slug: str = "") -> ProfileOpResult:
    return ProfileOpResult(True, "", profiles, _keyring_states(profiles), kind, slug)


def _fail_result(kind: str, reason: str, slug: str = "") -> ProfileOpResult:
    return ProfileOpResult(False, reason, (), (), kind, slug)


def _execute_save_profile(op: ProfileOp) -> ProfileOpResult:
    """Transaction-safe upsert with explicit ordering and compensation.

    Order: store/migrate the credential (new slug) → persist the
    collection → remove the old slug's credential on rename. Any failure
    restores the original profiles and the old/new secret states, so the
    last recoverable state is never destroyed and a credential store
    followed by a config failure never leaves an orphan.
    """
    assert op.profile is not None
    profile = op.profile
    rename = bool(op.old_slug) and op.old_slug != profile.slug
    current = load_settings().provider_profiles
    base = [p for p in current if p.slug != profile.slug and p.slug != op.old_slug]
    if len(base) + 1 > MAX_PROFILES:
        return _fail_result(op.kind, "Operation failed.", profile.slug)

    value = op.credential.strip()
    if rename and not value:
        # Blank credential on rename migrates the existing secret.
        value = get_provider_secret(op.old_slug) or ""
    prior_new: str | None = None
    stored = False
    if value:
        prior_new = get_provider_secret(profile.slug)  # capture before overwrite
        if not set_provider_secret(profile.slug, value):
            return _fail_result(op.kind, "Keyring unavailable.", profile.slug)
        stored = True

    new_collection = tuple(sorted(base + [profile], key=lambda p: p.slug))
    try:
        _persist_profiles(new_collection)
    except Exception:
        # Compensate the credential store: restore the new slug's prior
        # secret state (no orphan, no lost overwritten value).
        if stored:
            _restore_credential(profile.slug, prior_new)
        return _fail_result(op.kind, "Operation failed.", profile.slug)

    if rename:
        if not clear_provider_secret(op.old_slug):
            # Roll the whole rename back: original profiles on disk and
            # the new slug's secret state restored.
            try:
                _persist_profiles(current)
            except Exception:
                pass
            if stored:
                _restore_credential(profile.slug, prior_new)
            return _fail_result(op.kind, "Keyring unavailable.", profile.slug)
    return _ok_result(op.kind, new_collection, profile.slug)


def _execute_remove_profile(op: ProfileOp) -> ProfileOpResult:
    """Confirmed removal: either removes both the profile and its Moira
    credential, or leaves both unchanged. An absent credential is a
    successful no-op; an unavailable Keyring is a sanitized failure.

    Order: persist the collection without the profile, then clear the
    credential. A failed clear rolls the collection back (the credential
    was never touched), so the two stores never diverge.
    """
    current = load_settings().provider_profiles
    new_collection = tuple(p for p in current if p.slug != op.slug)
    if len(new_collection) == len(current):
        return _ok_result(op.kind, current, op.slug)  # nothing to remove
    try:
        _persist_profiles(new_collection)
    except Exception:
        return _fail_result(op.kind, "Operation failed.", op.slug)
    if not clear_provider_secret(op.slug):
        try:
            _persist_profiles(current)
        except Exception:
            pass
        return _fail_result(op.kind, "Keyring unavailable.", op.slug)
    return _ok_result(op.kind, new_collection, op.slug)


def _execute_op(op: ProfileOp) -> ProfileOpResult:
    """Run one operation off GTK (executor thread). Never raises: every
    failure maps to a sanitized stable outcome."""
    try:
        if op.kind == "reload":
            profiles = load_settings().provider_profiles
            return _ok_result("reload", profiles)
        if op.kind == "remove_credential":
            if not clear_provider_secret(op.slug):
                return _fail_result(op.kind, "Keyring unavailable.", op.slug)
            return _ok_result(op.kind, load_settings().provider_profiles, op.slug)
        if op.kind == "save_profile":
            return _execute_save_profile(op)
        if op.kind == "remove_profile":
            return _execute_remove_profile(op)
    except Exception:
        return _fail_result(op.kind, "Operation failed.", op.slug)
    return _fail_result(op.kind, "Operation failed.", op.slug)


def _form_error_key(exc: ValueError) -> str:
    """Map a ProviderProfile validation error to a translated key."""
    message = str(exc)
    for fragment, key in (
        ("profile slug is reserved", "Slug is reserved."),
        ("profile slug must", "Invalid slug."),
        ("must not contain control characters", "Invalid value."),
        ("profile label", "Label is required."),
        ("remote profiles require an https", "Remote base URLs must use https."),
        ("local profiles require a loopback", "Local base URLs must use a loopback address."),
        (
            "remote profiles must not use a loopback",
            "Remote base URLs must not use a loopback address.",
        ),
        ("must not embed credentials", "Base URL must not embed credentials, query or fragment."),
        (
            "must not contain a query or fragment",
            "Base URL must not embed credentials, query or fragment.",
        ),
        ("base URL", "Invalid base URL."),
        ("profile model", "Invalid model."),
        ("profile hermes_label", "Invalid Hermes label."),
        ("profile kind", "Invalid kind."),
        ("profile enabled", "Invalid profile."),
        ("profile base_url", "Invalid base URL."),
    ):
        if fragment in message:
            return key
    return "Invalid profile."


class ProviderEditor(Gtk.Window):
    """Modal editor window: list, add, edit, enable/disable, remove."""

    def __init__(
        self,
        *,
        submit: Any,
        on_profiles_changed: Any = None,
    ) -> None:
        super().__init__(title=_("Edit providers"))
        self.set_default_size(720, 520)
        self._submit = submit
        self._on_profiles_changed = on_profiles_changed
        self._shutdown = False
        self._in_flight = False
        self._pending_op: ProfileOp | None = None
        self._generation = 0
        self._profiles: tuple[ProviderProfile, ...] = ()
        self._configured: dict[str, bool] = {}
        self._editing_slug: str | None = None
        self._pending_removal: str | None = None
        self._rendering = False

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        root.set_margin_top(14)
        root.set_margin_bottom(14)
        root.set_margin_start(14)
        root.set_margin_end(14)

        header = Gtk.Box(spacing=8)
        title = Gtk.Label(label=_("Edit providers"), xalign=0)
        title.add_css_class("heading")
        header.append(title)
        self.status_label = Gtk.Label(xalign=0)
        self.status_label.set_wrap(True)
        self.status_label.add_css_class("dim-label")
        header.append(self.status_label)
        root.append(header)

        self.add_button = Gtk.Button(label=_("Add provider"))
        self.add_button.connect("clicked", lambda *_args: self._show_form(None))
        root.append(self.add_button)

        self._list_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        self.list_box = Gtk.ListBox()
        self.list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        scroller.set_child(self.list_box)
        self._list_area.append(scroller)
        root.append(self._list_area)

        self._form_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._form_area.set_visible(False)
        self._build_form()
        root.append(self._form_area)

        self.set_child(root)
        self._render_list()
        # Initial state: load the persisted collection and keyring states.
        self._request_op(ProfileOp("reload"))

    # ── Bounded generation pipeline (off GTK) ──

    def _request_op(self, op: ProfileOp) -> None:
        if self._shutdown:
            return
        if self._in_flight:
            self._pending_op = op  # newest-wins replacement
            return
        self._start_op(op)

    def _start_op(self, op: ProfileOp) -> None:
        self._generation += 1
        generation = self._generation
        self._in_flight = True
        self.status_label.set_text(_("Loading…") if op.kind == "reload" else _("Saving…"))
        try:
            self._submit(self._run_op, op, generation)
        except Exception:
            # Submit rejection (e.g. a closed executor): clear the slot,
            # show a fixed translated outcome, then accept later work.
            # The pending op is promoted immediately (at most one level of
            # recursion: the pending slot is emptied before promotion).
            self._in_flight = False
            self.status_label.set_text(_("Operation failed."))
            pending = self._pending_op
            self._pending_op = None
            if pending is not None:
                self._start_op(pending)

    def _run_op(self, op: ProfileOp, generation: int) -> None:
        result = _execute_op(op)
        GLib.idle_add(self._apply_op, result, generation)

    def _apply_op(self, result: ProfileOpResult, generation: int) -> bool:
        if self._shutdown or generation != self._generation:
            return False  # stale completion never overwrites newer form state
        self._in_flight = False
        pending = self._pending_op
        self._pending_op = None
        if result.ok:
            self._profiles = result.profiles
            self._configured = dict(result.configured)
            self._render_list()
            self.status_label.set_text(_(_SUCCESS_TEXT.get(result.kind, "")))
            if result.kind in ("save_profile", "remove_profile"):
                self._show_list()
                if self._on_profiles_changed is not None:
                    self._on_profiles_changed()
        else:
            self.status_label.set_text(_(result.reason))
        if pending is not None:
            self._start_op(pending)
        return False

    # ── Views ──

    def _show_list(self) -> None:
        if self._shutdown:
            return
        self._form_area.set_visible(False)
        self._list_area.set_visible(True)
        self._render_list()

    def _render_list(self) -> None:
        self._rendering = True
        try:
            self.list_box.remove_all()
            self._row_widgets: dict[str, dict[str, Gtk.Widget]] = {}
            for profile in self._profiles:
                row = Gtk.ListBoxRow()
                row.set_child(self._build_row(profile))
                self.list_box.append(row)
        finally:
            self._rendering = False

    def _build_row(self, profile: ProviderProfile) -> Gtk.Widget:
        slug = profile.slug
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(8)
        box.set_margin_end(8)

        top = Gtk.Box(spacing=8)
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        name = Gtk.Label(label=profile.label, xalign=0)
        name.add_css_class("heading")
        info.append(name)
        model_text = profile.model or "—"
        detail = Gtk.Label(label=f"{_(_KIND_LABELS[profile.kind])} · {model_text}", xalign=0)
        detail.add_css_class("dim-label")
        info.append(detail)
        top.append(info)
        switch = Gtk.Switch(active=profile.enabled, valign=Gtk.Align.CENTER)
        switch.connect("state-set", self._on_enabled_toggled, slug)
        top.append(switch)
        box.append(top)

        actions = Gtk.Box(spacing=8)
        credential = Gtk.Label(
            label=f"{_('Credential')}: "
            f"{_('configured') if self._configured.get(slug, False) else _('not configured')}",
            xalign=0,
        )
        actions.append(credential)
        remove_credential = Gtk.Button(label=_("Remove credential"))
        remove_credential.set_visible(self._configured.get(slug, False))
        remove_credential.connect("clicked", self._on_remove_credential, slug)
        actions.append(remove_credential)
        edit_button = Gtk.Button(label=_("Edit"))
        edit_button.connect("clicked", self._on_edit_clicked, slug)
        actions.append(edit_button)
        remove_button = Gtk.Button(label=_("Remove"))
        remove_button.connect("clicked", self._on_remove_clicked, slug)
        actions.append(remove_button)
        box.append(actions)

        confirm = Gtk.Box(spacing=8)
        confirm_label = Gtk.Label(label=_("Remove profile?"), xalign=0)
        confirm_note = Gtk.Label(
            label=_("This removes the profile and its Moira Keyring credential."), xalign=0
        )
        confirm_note.add_css_class("dim-label")
        confirm_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        confirm_box.append(confirm_label)
        confirm_box.append(confirm_note)
        confirm.append(confirm_box)
        confirm_remove = Gtk.Button(label=_("Remove"))
        confirm_remove.add_css_class("destructive-action")
        confirm_remove.connect("clicked", self._on_confirm_remove, slug)
        confirm.append(confirm_remove)
        confirm_cancel = Gtk.Button(label=_("Cancel"))
        confirm_cancel.connect("clicked", self._on_cancel_remove, slug)
        confirm.append(confirm_cancel)
        confirm.set_visible(False)
        box.append(confirm)

        self._row_widgets[slug] = {
            "switch": switch,
            "credential": credential,
            "remove_credential": remove_credential,
            "edit": edit_button,
            "remove": remove_button,
            "confirm": confirm,
            "confirm_remove": confirm_remove,
            "confirm_cancel": confirm_cancel,
        }
        self._refresh_row_confirm(slug)
        return box

    def _refresh_row_confirm(self, slug: str) -> None:
        widgets = self._row_widgets.get(slug)
        if widgets is None:
            return
        confirming = self._pending_removal == slug
        widgets["confirm"].set_visible(confirming)
        widgets["remove_credential"].set_visible(
            not confirming and self._configured.get(slug, False)
        )
        widgets["edit"].set_visible(not confirming)
        widgets["remove"].set_visible(not confirming)

    # ── Row actions ──

    def _on_enabled_toggled(self, switch: Gtk.Switch, state: bool, slug: str) -> bool:
        if self._shutdown or self._rendering:
            return False
        profile = next((p for p in self._profiles if p.slug == slug), None)
        if profile is None:
            return False
        updated = replace(profile, enabled=state)
        self._request_op(ProfileOp("save_profile", profile=updated))
        return False

    def _on_edit_clicked(self, _button: Any, slug: str) -> None:
        if self._shutdown:
            return
        profile = next((p for p in self._profiles if p.slug == slug), None)
        if profile is not None:
            self._show_form(profile)

    def _on_remove_clicked(self, _button: Any, slug: str) -> None:
        if self._shutdown:
            return
        self._pending_removal = slug
        self._refresh_row_confirm(slug)

    def _on_confirm_remove(self, _button: Any, slug: str) -> None:
        self._pending_removal = None
        if self._shutdown:
            return
        self._request_op(ProfileOp("remove_profile", slug=slug))

    def _on_cancel_remove(self, _button: Any, slug: str) -> None:
        if self._pending_removal == slug:
            self._pending_removal = None
        self._refresh_row_confirm(slug)

    def _on_remove_credential(self, _button: Any, slug: str) -> None:
        if self._shutdown:
            return
        self._request_op(ProfileOp("remove_credential", slug=slug))

    # ── Add/edit form ──

    def _build_form(self) -> None:
        self.form_title = Gtk.Label(label=_("Add provider"), xalign=0)
        self.form_title.add_css_class("heading")
        self._form_area.append(self.form_title)

        self.slug_entry = Gtk.Entry(placeholder_text="deepseek-main")
        self._form_area.append(self._labeled(_("Slug"), self.slug_entry))
        self.label_entry = Gtk.Entry()
        self._form_area.append(self._labeled(_("Label"), self.label_entry))
        self.kind_dropdown = Gtk.DropDown.new_from_strings(
            [_(_KIND_LABELS[kind]) for kind in _KIND_ORDER]
        )
        self._form_area.append(self._labeled(_("Kind"), self.kind_dropdown))
        self.model_entry = Gtk.Entry(placeholder_text="deepseek-chat")
        self._form_area.append(self._labeled(_("Model"), self.model_entry))
        self.enabled_switch = Gtk.Switch(active=True, valign=Gtk.Align.CENTER)
        self._form_area.append(self._labeled(_("Enabled"), self.enabled_switch))
        self.base_url_entry = Gtk.Entry(placeholder_text="https://api.deepseek.com/v1")
        self._form_area.append(self._labeled(_("API base URL"), self.base_url_entry))
        self.hermes_label_entry = Gtk.Entry()
        self._form_area.append(self._labeled(_("Hermes label"), self.hermes_label_entry))
        self.api_key_entry = Gtk.PasswordEntry()
        self._form_area.append(self._labeled(_("API key"), self.api_key_entry))
        api_note = Gtk.Label(label=_("Leave blank to keep the current credential."), xalign=0)
        api_note.add_css_class("dim-label")
        self._form_area.append(api_note)
        self.slug_change_note = Gtk.Label(
            label=_("Changing the slug removes the previous profile and its credential."), xalign=0
        )
        self.slug_change_note.add_css_class("dim-label")
        self.slug_change_note.set_visible(False)
        self._form_area.append(self.slug_change_note)

        self.form_error = Gtk.Label(xalign=0)
        self.form_error.add_css_class("error")
        self._form_area.append(self.form_error)

        buttons = Gtk.Box(spacing=8)
        self.form_save_button = Gtk.Button(label=_("Save"))
        self.form_save_button.add_css_class("suggested-action")
        self.form_save_button.connect("clicked", self._on_form_save)
        buttons.append(self.form_save_button)
        self.form_cancel_button = Gtk.Button(label=_("Cancel"))
        self.form_cancel_button.connect("clicked", lambda *_args: self._show_list())
        buttons.append(self.form_cancel_button)
        self._form_area.append(buttons)

    @staticmethod
    def _labeled(label: str, widget: Gtk.Widget) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.append(Gtk.Label(label=label, xalign=0))
        box.append(widget)
        return box

    def _show_form(self, profile: ProviderProfile | None) -> None:
        if self._shutdown:
            return
        self._editing_slug = profile.slug if profile else None
        self._pending_removal = None
        self._list_area.set_visible(False)
        self._form_area.set_visible(True)
        self.form_title.set_text(_("Edit provider") if profile else _("Add provider"))
        self.slug_entry.set_text(profile.slug if profile else "")
        self.label_entry.set_text(profile.label if profile else "")
        self._set_kind(profile.kind if profile else ProviderKind.DEEPSEEK)
        self.model_entry.set_text(profile.model if profile else "")
        self.enabled_switch.set_active(profile.enabled if profile else True)
        self.base_url_entry.set_text(profile.base_url if profile else "")
        self.hermes_label_entry.set_text(profile.hermes_label if profile else "")
        self.api_key_entry.set_text("")
        self.form_error.set_text("")
        self.slug_change_note.set_visible(profile is not None)

    def _set_kind(self, kind: ProviderKind) -> None:
        self.kind_dropdown.set_selected(_KIND_ORDER.index(kind))

    def _form_kind(self) -> ProviderKind:
        return _KIND_ORDER[self.kind_dropdown.get_selected()]

    def _on_form_save(self, *_args: Any) -> None:
        if self._shutdown:
            return
        slug = self.slug_entry.get_text().strip()
        label = self.label_entry.get_text().strip()
        model = self.model_entry.get_text().strip()
        base_url = self.base_url_entry.get_text().strip()
        hermes_label = self.hermes_label_entry.get_text().strip()
        kind = self._form_kind()
        enabled = self.enabled_switch.get_active()
        api_key = self.api_key_entry.get_text()
        try:
            profile = ProviderProfile(
                slug=slug,
                label=label,
                kind=kind,
                model=model,
                enabled=enabled,
                base_url=base_url,
                hermes_label=hermes_label,
            )
        except ValueError as exc:
            self.form_error.set_text(_(_form_error_key(exc)))
            return
        existing = {p.slug for p in self._profiles}
        if self._editing_slug:
            existing.discard(self._editing_slug)
        if profile.slug in existing:
            self.form_error.set_text(_("Slug already in use."))
            return
        others = [p for p in self._profiles if p.slug != self._editing_slug]
        if len(others) + 1 > MAX_PROFILES:
            self.form_error.set_text(_("Too many profiles."))
            return
        self.form_error.set_text("")
        op = ProfileOp(
            "save_profile",
            profile=profile,
            old_slug=self._editing_slug or "",
            credential=api_key,
        )
        self._request_op(op)

    def shutdown(self) -> None:
        """Idempotent: stop accepting operations and drop the pending one."""
        self._shutdown = True
        self._pending_op = None
