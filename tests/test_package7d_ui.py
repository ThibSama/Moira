"""Package 7d — Xvfb GTK tests for the provider editor and Integrations wiring.

The editor runs Keyring/persistence off GTK through a synchronous fake
submitter with ``GLib.idle_add`` patched to dispatch inline, so every
generation completes deterministically on the calling thread. The
libsecret binding is mocked; the configuration lives in a temporary
XDG_CONFIG_HOME.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import gi  # type: ignore[import-untyped]

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Secret", "1")
import pytest
from gi.repository import GLib, Gtk, Secret  # type: ignore[import-untyped]  # noqa: E402

from moira.integrations import ProviderKind, ProviderProfile
from moira.integrations_page import IntegrationsPage
from moira.persistence import Settings, load_settings, save_settings
from moira.provider_editor import ProviderEditor


def _all_texts(widget: Any) -> list[str]:
    texts: list[str] = []
    if isinstance(widget, Gtk.Label):
        texts.append(widget.get_text())
    child = widget.get_first_child()
    while child is not None:
        texts.extend(_all_texts(child))
        child = child.get_next_sibling()
    return texts


def _sync_submit(fn: Any, *args: Any) -> None:
    fn(*args)


class _FakeSecret:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}
        self.fail: str | None = None

    def password_lookup_sync(
        self, _schema: Any, attributes: dict[str, str], _cancellable: Any
    ) -> str | None:
        if self.fail == "lookup":
            raise RuntimeError("secret vault locked")
        if attributes.get("account") == "ntfy-token":
            return self.items.get(("ntfy", ""))
        return self.items.get((attributes["slug"], attributes["purpose"]))

    def password_store_sync(
        self,
        _schema: Any,
        attributes: dict[str, str],
        _collection: Any,
        _label: str,
        value: str,
        _cancellable: Any,
    ) -> None:
        if self.fail == "store":
            raise RuntimeError("secret vault locked")
        self.items[(attributes["slug"], attributes["purpose"])] = value

    def password_clear_sync(
        self, _schema: Any, attributes: dict[str, str], _cancellable: Any
    ) -> None:
        if self.fail == "clear":
            raise RuntimeError("secret vault locked")
        self.items.pop((attributes["slug"], attributes["purpose"]), None)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, _FakeSecret]:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    fake = _FakeSecret()
    monkeypatch.setattr(Secret, "password_lookup_sync", fake.password_lookup_sync)
    monkeypatch.setattr(Secret, "password_store_sync", fake.password_store_sync)
    monkeypatch.setattr(Secret, "password_clear_sync", fake.password_clear_sync)
    return tmp_path, fake


@pytest.fixture
def english(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the EN locale for the whole test (assertions on tr())."""
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("LC_ALL", "")
    monkeypatch.setenv("LC_MESSAGES", "")


@pytest.fixture
def idle_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dispatch GLib idle callbacks inline so operations complete
    synchronously on the calling thread, for the whole test."""
    monkeypatch.setattr(GLib, "idle_add", lambda cb, *a: cb(*a))


def _profile(slug: str = "deepseek-main", **overrides: Any) -> ProviderProfile:
    base: dict[str, Any] = {
        "slug": slug,
        "label": "DeepSeek main",
        "kind": ProviderKind.DEEPSEEK,
        "model": "deepseek-chat",
        "enabled": True,
    }
    base.update(overrides)
    return ProviderProfile(**base)


def _seed(env: tuple[Path, _FakeSecret], *profiles: ProviderProfile) -> None:
    save_settings(Settings(provider_profiles=profiles))
    env[1].items.clear()


def _open_editor(
    env: tuple[Path, _FakeSecret], *, on_profiles_changed: Any = None
) -> ProviderEditor:
    """Construct the editor; the reload op completes inline under the
    ``idle_inline`` fixture, loading whatever was seeded beforehand."""
    return ProviderEditor(submit=_sync_submit, on_profiles_changed=on_profiles_changed)


@pytest.fixture
def editor(env: tuple[Path, _FakeSecret], english: None, idle_inline: None) -> ProviderEditor:
    return _open_editor(env)


# ── Integrations page wiring ────────────────────────────────────────────────


def test_integrations_page_has_edit_providers_button(english: None) -> None:
    page = IntegrationsPage()
    assert page.edit_providers_button is not None
    assert page.edit_providers_button.get_label() == "Edit providers"


def test_edit_providers_button_routes_to_callback(english: None) -> None:
    calls: list[str] = []
    page = IntegrationsPage(on_edit_providers=lambda: calls.append("edit"))
    page.edit_providers_button.emit("clicked")
    assert calls == ["edit"]


def test_edit_providers_present_in_full_and_compact(
    env: tuple[Path, _FakeSecret], english: None
) -> None:
    """The Edit providers control exists in both full and compact modes."""
    from moira.ui import MainWindow

    for compact in (False, True):
        window = MainWindow.__new__(MainWindow)
        window.settings = Settings(compact_mode=compact)
        page = IntegrationsPage(on_edit_providers=window._open_provider_editor)
        window._integrations_page = page
        assert page.edit_providers_button.get_label() == "Edit providers"
        assert "Edit providers" in _all_texts(page)


def test_window_opens_provider_editor(
    env: tuple[Path, _FakeSecret], english: None, idle_inline: None
) -> None:
    from moira.ui import MainWindow

    window = MainWindow.__new__(MainWindow)
    window.executor = type("Executor", (), {"submit": staticmethod(_sync_submit)})()
    window._open_provider_editor()
    assert isinstance(window._provider_editor, ProviderEditor)
    # Opening again focuses the existing editor instead of stacking dialogs.
    editor = window._provider_editor
    window._open_provider_editor()
    assert window._provider_editor is editor


# ── Editor: list and credential-state rendering ─────────────────────────────


def test_editor_lists_profiles_with_credential_states(
    env: tuple[Path, _FakeSecret], english: None, idle_inline: None
) -> None:
    _seed(
        env,
        _profile("deepseek-main"),
        _profile("local-llm", label="Local LLM", kind=ProviderKind.LOCAL, enabled=False),
    )
    env[1].items[("deepseek-main", "api_key")] = "sk-abc"
    ed = _open_editor(env)
    assert [p.slug for p in ed._profiles] == ["deepseek-main", "local-llm"]
    texts = _all_texts(ed)
    assert "DeepSeek main" in texts and "Local LLM" in texts
    assert any("Credential: configured" == t for t in texts)
    assert any("Credential: not configured" == t for t in texts)
    assert ed._configured == {"deepseek-main": True, "local-llm": False}


# ── Editor: add / validation ────────────────────────────────────────────────


def test_editor_add_form_validation_reserved_slug(editor: Any) -> None:
    editor._show_form(None)
    editor.slug_entry.set_text("claude")
    editor.label_entry.set_text("Claude clone")
    editor.form_save_button.emit("clicked")
    assert editor.form_error.get_text() == "Slug is reserved."
    assert editor._in_flight is False and editor._profiles == ()


def test_editor_outcomes_are_translated_french(
    env: tuple[Path, _FakeSecret], idle_inline: None
) -> None:
    """UI failures are translated EN/FR: the French locale shows French
    validation and sanitized keyring outcomes, never raw exceptions."""
    with patch.dict(
        os.environ,
        {"LANG": "fr_FR.UTF-8", "LC_ALL": "", "LC_MESSAGES": ""},
        clear=False,
    ):
        _seed(env, _profile("deepseek-main"))
        ed = _open_editor(env)
        ed._show_form(None)
        ed.slug_entry.set_text("claude")
        ed.label_entry.set_text("X")
        ed.form_save_button.emit("clicked")
        assert ed.form_error.get_text() == "Identifiant réservé."
        env[1].fail = "clear"
        ed._row_widgets["deepseek-main"]["remove"].emit("clicked")
        ed._row_widgets["deepseek-main"]["confirm_remove"].emit("clicked")
        assert ed.status_label.get_text() == "Trousseau indisponible."
        assert "locked" not in ed.status_label.get_text()


def test_editor_add_form_validation_bad_slug(editor: Any) -> None:
    editor._show_form(None)
    editor.slug_entry.set_text("Bad Slug!")
    editor.label_entry.set_text("X")
    editor.form_save_button.emit("clicked")
    assert editor.form_error.get_text() == "Invalid slug."


def test_editor_add_form_validation_missing_label(editor: Any) -> None:
    editor._show_form(None)
    editor.slug_entry.set_text("ok-slug")
    editor.label_entry.set_text("   ")
    editor.form_save_button.emit("clicked")
    assert editor.form_error.get_text() == "Label is required."


def test_editor_add_form_validation_remote_http_rejected(editor: Any) -> None:
    editor._show_form(None)
    editor.slug_entry.set_text("remote")
    editor.label_entry.set_text("Remote")
    editor._set_kind(ProviderKind.DEEPSEEK)
    editor.base_url_entry.set_text("http://api.example.com/v1")
    editor.form_save_button.emit("clicked")
    assert editor.form_error.get_text() == "Remote base URLs must use https."


def test_editor_add_form_validation_duplicate_slug(
    env: tuple[Path, _FakeSecret], english: None, idle_inline: None
) -> None:
    _seed(env, _profile("deepseek-main"))
    ed = _open_editor(env)
    ed._show_form(None)
    ed.slug_entry.set_text("deepseek-main")
    ed.label_entry.set_text("Another")
    ed.form_save_button.emit("clicked")
    assert ed.form_error.get_text() == "Slug already in use."


def test_editor_add_profile_persists_and_refreshes(
    env: tuple[Path, _FakeSecret], english: None, idle_inline: None
) -> None:
    changed: list[int] = []

    def on_changed() -> None:
        changed.append(1)

    ed = _open_editor(env, on_profiles_changed=on_changed)
    ed._show_form(None)
    ed.slug_entry.set_text("openrouter-main")
    ed.label_entry.set_text("OpenRouter main")
    ed._set_kind(ProviderKind.OPENROUTER)
    ed.model_entry.set_text("o3-mini")
    ed.base_url_entry.set_text("https://openrouter.ai/api/v1")
    ed.form_save_button.emit("clicked")
    assert [p.slug for p in ed._profiles] == ["openrouter-main"]
    assert ed.status_label.get_text() == "Profile saved."
    assert changed == [1]
    disk = load_settings()
    assert [p.slug for p in disk.provider_profiles] == ["openrouter-main"]
    assert disk.provider_profiles[0].base_url == "https://openrouter.ai/api/v1"


# ── Editor: edit, credentials, enable/disable ───────────────────────────────


def test_editor_edit_preserves_credential_on_blank(
    env: tuple[Path, _FakeSecret], english: None, idle_inline: None
) -> None:
    _seed(env, _profile("deepseek-main"))
    env[1].items[("deepseek-main", "api_key")] = "sk-old"
    ed = _open_editor(env)
    ed._show_form(ed._profiles[0])
    ed.label_entry.set_text("Renamed")
    # API key left blank → the existing secret is preserved.
    ed.form_save_button.emit("clicked")
    assert env[1].items[("deepseek-main", "api_key")] == "sk-old"
    assert load_settings().provider_profiles[0].label == "Renamed"


def test_editor_edit_sets_credential(
    env: tuple[Path, _FakeSecret], english: None, idle_inline: None
) -> None:
    _seed(env, _profile("deepseek-main"))
    ed = _open_editor(env)
    ed._show_form(ed._profiles[0])
    ed.api_key_entry.set_text("sk-new")
    ed.form_save_button.emit("clicked")
    assert env[1].items[("deepseek-main", "api_key")] == "sk-new"
    assert ed._configured["deepseek-main"] is True


def test_editor_slug_change_is_create_new_plus_old_removal(
    env: tuple[Path, _FakeSecret], english: None, idle_inline: None
) -> None:
    _seed(env, _profile("deepseek-main"))
    env[1].items[("deepseek-main", "api_key")] = "sk-old"
    ed = _open_editor(env)
    ed._show_form(ed._profiles[0])
    ed.slug_entry.set_text("deepseek-renamed")
    ed.form_save_button.emit("clicked")
    assert [p.slug for p in ed._profiles] == ["deepseek-renamed"]
    assert "deepseek-main" not in [p.slug for p in load_settings().provider_profiles]
    # The old slug's Moira-owned credential was explicitly cleared.
    assert ("deepseek-main", "api_key") not in env[1].items


def test_editor_enable_disable_toggle_persists(
    env: tuple[Path, _FakeSecret], english: None, idle_inline: None
) -> None:
    _seed(env, _profile("deepseek-main"))
    ed = _open_editor(env)
    ed._row_widgets["deepseek-main"]["switch"].set_active(False)
    assert load_settings().provider_profiles[0].enabled is False
    assert ed.status_label.get_text() == "Profile saved."


def test_editor_remove_credential_is_explicit(
    env: tuple[Path, _FakeSecret], english: None, idle_inline: None
) -> None:
    _seed(env, _profile("deepseek-main"))
    env[1].items[("deepseek-main", "api_key")] = "sk-abc"
    ed = _open_editor(env)
    assert ed._configured["deepseek-main"] is True
    ed._row_widgets["deepseek-main"]["remove_credential"].emit("clicked")
    assert ("deepseek-main", "api_key") not in env[1].items
    assert ed._configured["deepseek-main"] is False
    assert ed.status_label.get_text() == "Credential removed."
    # The profile itself is untouched.
    assert [p.slug for p in load_settings().provider_profiles] == ["deepseek-main"]


# ── Editor: destructive removal with confirmation ───────────────────────────


def test_editor_remove_profile_requires_confirmation(
    env: tuple[Path, _FakeSecret], english: None, idle_inline: None
) -> None:
    _seed(env, _profile("deepseek-main"))
    env[1].items[("deepseek-main", "api_key")] = "sk-abc"
    ed = _open_editor(env)
    # First click reveals the confirmation, nothing is removed yet.
    ed._row_widgets["deepseek-main"]["remove"].emit("clicked")
    assert ed._pending_removal == "deepseek-main"
    assert [p.slug for p in load_settings().provider_profiles] == ["deepseek-main"]
    assert ("deepseek-main", "api_key") in env[1].items
    # Cancelling aborts the removal.
    ed._row_widgets["deepseek-main"]["confirm_cancel"].emit("clicked")
    assert ed._pending_removal is None
    # Confirming removes the profile AND clears its Moira-owned credential.
    ed._row_widgets["deepseek-main"]["remove"].emit("clicked")
    ed._row_widgets["deepseek-main"]["confirm_remove"].emit("clicked")
    assert ed._profiles == ()
    assert load_settings().provider_profiles == ()
    assert ("deepseek-main", "api_key") not in env[1].items
    assert ed.status_label.get_text() == "Profile removed."


def test_editor_remove_profile_keyring_failure_is_sanitized(
    env: tuple[Path, _FakeSecret], english: None, idle_inline: None
) -> None:
    _seed(env, _profile("deepseek-main"))
    ed = _open_editor(env)
    env[1].fail = "clear"
    ed._row_widgets["deepseek-main"]["remove"].emit("clicked")
    ed._row_widgets["deepseek-main"]["confirm_remove"].emit("clicked")
    assert ed.status_label.get_text() == "Keyring unavailable."
    assert "locked" not in ed.status_label.get_text()
    # Fail closed: the profile is still present.
    assert [p.slug for p in ed._profiles] == ["deepseek-main"]
    assert [p.slug for p in load_settings().provider_profiles] == ["deepseek-main"]


# ── Editor: bounded generations ─────────────────────────────────────────────


def test_editor_stale_completion_never_overwrites_form_state(
    env: tuple[Path, _FakeSecret], english: None, idle_inline: None
) -> None:
    _seed(env, _profile("deepseek-main"))
    ed = _open_editor(env)
    stale = type(
        "Result",
        (),
        {
            "ok": True,
            "reason": "",
            "profiles": (),
            "configured": (),
            "kind": "remove_profile",
            "slug": "deepseek-main",
        },
    )()
    ed._apply_op(stale, ed._generation - 1)  # stale completion
    assert [p.slug for p in ed._profiles] == ["deepseek-main"]  # untouched


def test_editor_pending_op_replaced_newest_wins(
    env: tuple[Path, _FakeSecret], english: None, idle_inline: None
) -> None:
    """Rapid mutations park at most one pending op; the newest replaces it."""
    from moira.provider_editor import ProfileOp, ProfileOpResult

    _seed(env, _profile("deepseek-main"))
    ed = _open_editor(env)
    # An op is in flight; two rapid requests arrive: newest wins.
    ed._in_flight = True
    first = ProfileOp("remove_credential", profiles=ed._profiles, slug="deepseek-main")
    second = ProfileOp("remove_credential", profiles=ed._profiles, slug="deepseek-main")
    ed._request_op(first)
    assert ed._pending_op is first
    ed._request_op(second)
    assert ed._pending_op is second
    # The in-flight completion promotes only the newest pending op.
    result = ProfileOpResult(
        True, "", ed._profiles, (("deepseek-main", False),), "remove_credential", "deepseek-main"
    )
    ed._apply_op(result, ed._generation)
    assert ed._in_flight is False and ed._pending_op is None
    assert ed._configured["deepseek-main"] is False
    assert ed.status_label.get_text() == "Credential removed."


def test_editor_shutdown_stops_ops(
    env: tuple[Path, _FakeSecret], english: None, idle_inline: None
) -> None:
    ed = _open_editor(env)
    ed.shutdown()
    ed._show_form(None)
    ed.slug_entry.set_text("x")
    ed.label_entry.set_text("X")
    ed.form_save_button.emit("clicked")
    assert ed._profiles == () and ed._in_flight is False
