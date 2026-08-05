"""Package 7e — lossless, transactional provider profiles (ACCEPTANCE_CORRECTION).

RED tests for the five blocking findings:

1. ``MainWindow._read_form()`` rebuilds Settings without ``provider_profiles``,
   so saving unrelated settings wipes every profile and its Keyring items.
2. Profile/credential writes are not failure-atomic (rename clears the old
   secret before config persistence; add/edit store before persistence;
   removal clears before persistence) — a later failure can lose a valid
   credential or leave an orphan.
3. Keyring slug validation accepts arbitrary printable strings instead of
   the exact profile contract (shared strict validator).
4. HTTPS loopback is accepted for remote kinds although loopback is
   reserved for ``local``.
5. Submit rejection escapes and latches ``_in_flight``; pending operations
   capture stale full profile tuples so rapid edits overwrite committed
   changes.
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
from gi.repository import Adw, GLib, Gtk, Secret  # type: ignore[import-untyped]  # noqa: E402

from moira.integrations import ProviderKind, ProviderProfile
from moira.persistence import Settings, load_settings, save_settings
from moira.provider_editor import ProfileOp, ProfileOpResult, ProviderEditor, _execute_op

# ── Test doubles ─────────────────────────────────────────────────────────────


class _FakeSecret:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}
        self.fail: str | None = None
        #: -1 = fail every matching call; N > 0 = fail N calls then recover.
        self.fail_times: int = -1
        self.calls: list[str] = []

    def _should_fail(self, kind: str) -> bool:
        if self.fail != kind:
            return False
        if self.fail_times == -1:
            return True
        if self.fail_times > 0:
            self.fail_times -= 1
            return True
        return False

    def password_lookup_sync(
        self, _schema: Any, attributes: dict[str, str], _cancellable: Any
    ) -> str | None:
        self.calls.append("lookup")
        if self._should_fail("lookup"):
            raise RuntimeError("secret vault locked")
        if attributes.get("account") == "ntfy-token":
            return self.items.get(("ntfy", ""))
        return self.items.get((attributes["slug"], attributes["purpose"]))

    def password_store_sync(
        self,
        _schema: Any,
        attributes: dict[str, str],
        _collection: Any,
        _label: Any,
        value: str,
        _cancellable: Any,
    ) -> None:
        self.calls.append("store")
        if self._should_fail("store"):
            raise RuntimeError("secret vault locked")
        self.items[(attributes["slug"], attributes["purpose"])] = value

    def password_clear_sync(
        self, _schema: Any, attributes: dict[str, str], _cancellable: Any
    ) -> None:
        self.calls.append("clear")
        if self._should_fail("clear"):
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
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("LC_ALL", "")
    monkeypatch.setenv("LC_MESSAGES", "")


@pytest.fixture
def idle_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dispatch GLib idle callbacks inline for the whole test."""
    monkeypatch.setattr(GLib, "idle_add", lambda cb, *a: cb(*a))


class _GatedSubmit:
    """Parks every submitted call until ``flush`` runs them inline, in
    order — deterministic control over the in-flight/pending pipeline."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, tuple[Any, ...]]] = []

    def __call__(self, fn: Any, *args: Any) -> None:
        self.calls.append((fn, args))

    def flush(self) -> None:
        while self.calls:
            fn, args = self.calls.pop(0)
            fn(*args)


class _FlakySubmit:
    """Raises on the first submit (executor rejection), then works."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, fn: Any, *args: Any) -> None:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("executor closed")
        fn(*args)


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
    env: tuple[Path, _FakeSecret], *, submit: Any = None, on_profiles_changed: Any = None
) -> ProviderEditor:
    return ProviderEditor(
        submit=submit if submit is not None else lambda fn, *a: fn(*a),
        on_profiles_changed=on_profiles_changed,
    )


# ── Finding 1: saving unrelated settings must preserve profiles ─────────────


def _entry(text: str = "") -> Gtk.Entry:
    entry = Gtk.Entry()
    entry.set_text(text)
    return entry


def _switch(active: bool) -> Gtk.Switch:
    switch = Gtk.Switch(active=active)
    return switch


def _combo(index: int) -> Gtk.DropDown:
    combo = Gtk.DropDown.new_from_strings(["1", "2", "5", "10", "15", "30"])
    combo.set_selected(index)
    return combo


def _stub_window(settings: Settings) -> Any:
    from moira.ui import MainWindow

    window = MainWindow.__new__(MainWindow)
    window.settings = settings
    window._thresholds_entries = {"claude": _entry("50,75"), "codex": _entry("30,60")}
    window._reset_switches = {"claude": _switch(False), "codex": _switch(True)}
    window._error_switches = {"claude": _switch(False), "codex": _switch(True)}
    window.refresh_combo = _combo(1)
    window.server = _entry("https://ntfy.sh")
    window.topic = _entry("topic")
    window.ntfy_enabled = _switch(False)
    window.native_notifications = _switch(False)
    window.collect_claude = _switch(True)
    window.collect_codex = _switch(False)
    window.compact_mode = _switch(False)
    window.autostart = _switch(False)
    return window


def test_read_form_preserves_provider_profiles(
    env: tuple[Path, _FakeSecret], english: None
) -> None:
    """Saving unrelated Notifications settings must keep profiles and
    Keyring items identical (whole-config rewrite)."""
    seeded = (_profile("deepseek-main"), _profile("local-llm", kind=ProviderKind.LOCAL))
    _seed(env, *seeded)
    env[1].items[("deepseek-main", "api_key")] = "sk-abc"

    window = _stub_window(load_settings())
    settings = window._read_form()
    assert settings.provider_profiles == seeded

    save_settings(settings)
    reloaded = load_settings()
    assert reloaded.provider_profiles == seeded
    # Keyring items untouched by the config rewrite.
    assert env[1].items[("deepseek-main", "api_key")] == "sk-abc"


# ── Finding 3: shared strict slug validator (zero libsecret calls) ───────────


def test_keyring_rejects_non_profile_slugs_with_zero_calls(
    env: tuple[Path, _FakeSecret],
) -> None:
    from moira.secrets import (
        clear_provider_secret,
        get_provider_secret,
        has_provider_secret,
        set_provider_secret,
    )

    env[1].calls.clear()
    for bad in ("DeepSeek!", "deepseek main", "deepseek.main", "UPPER", "é", "deepseek-", "-x", ""):
        assert get_provider_secret(bad) is None, bad
        assert set_provider_secret(bad, "sk-x") is False, bad
        assert has_provider_secret(bad) is False, bad
        assert clear_provider_secret(bad) is False, bad
    assert env[1].calls == []  # zero libsecret calls for invalid slugs


def test_keyring_rejects_reserved_slugs_with_zero_calls(env: tuple[Path, _FakeSecret]) -> None:
    from moira.secrets import get_provider_secret, set_provider_secret

    env[1].calls.clear()
    for reserved in ("claude", "codex", "hermes"):
        assert get_provider_secret(reserved) is None
        assert set_provider_secret(reserved, "sk-x") is False
    assert env[1].calls == []


def test_keyring_accepts_valid_profile_slug(env: tuple[Path, _FakeSecret]) -> None:
    from moira.secrets import get_provider_secret, set_provider_secret

    assert set_provider_secret("deepseek-main", "sk-x") is True
    assert get_provider_secret("deepseek-main") == "sk-x"
    assert ("deepseek-main", "api_key") in env[1].items


# ── Finding 4: loopback reserved for local; remote requires https ───────────


def test_remote_kinds_reject_https_loopback() -> None:
    for kind in (
        ProviderKind.DEEPSEEK,
        ProviderKind.OPENAI_COMPATIBLE,
        ProviderKind.OPENROUTER,
        ProviderKind.ANTHROPIC,
        ProviderKind.OPENAI,
        ProviderKind.CUSTOM,
    ):
        for url in (
            "https://127.0.0.1/v1",
            "https://localhost/v1",
            "https://[::1]/v1",
        ):
            with pytest.raises(ValueError):
                _profile(kind=kind, base_url=url), url


def test_local_kind_accepts_only_loopback_over_http_or_https() -> None:
    ok = _profile(kind=ProviderKind.LOCAL, base_url="http://127.0.0.1:11434")
    assert ok.base_url == "http://127.0.0.1:11434"
    ok = _profile(kind=ProviderKind.LOCAL, base_url="https://localhost:8080")
    assert ok.base_url == "https://localhost:8080"
    ok = _profile(kind=ProviderKind.LOCAL, base_url="http://[::1]:8080")
    assert ok.base_url == "http://[::1]:8080"
    for bad in (
        "http://api.example.com/v1",
        "https://api.example.com/v1",
        "http://192.168.1.10:8080",
    ):
        with pytest.raises(ValueError):
            _profile(kind=ProviderKind.LOCAL, base_url=bad), bad


# ── Criterion 12: control characters rejected in labels/models/hermes labels ─


def test_control_characters_rejected_in_labels_models_hermes_labels() -> None:
    for bad_label in ("bad\nlabel", "bad\x00label", "bad\x07label", "bad\rlabel"):
        with pytest.raises(ValueError):
            _profile(label=bad_label)
    for bad_model in ("m\x01", "m\nodel", "m\x00"):
        with pytest.raises(ValueError):
            _profile(model=bad_model)
    for bad_hermes in ("h\x1b", "h\nx", "h\x00"):
        with pytest.raises(ValueError):
            _profile(hermes_label=bad_hermes)


def test_form_rejects_control_characters_in_label(
    env: tuple[Path, _FakeSecret], english: None, idle_inline: None
) -> None:
    ed = _open_editor(env)
    ed._show_form(None)
    ed.slug_entry.set_text("ok-slug")
    ed.label_entry.set_text("bad\nlabel")
    ed.form_save_button.emit("clicked")
    assert ed.form_error.get_text() == "Invalid value."
    assert ed._profiles == () and load_settings().provider_profiles == ()


# ── Finding 2 / criteria 5–8: transactional matrix (fault injection) ─────────


def test_add_credential_store_then_config_failure_leaves_no_orphan(
    env: tuple[Path, _FakeSecret],
) -> None:
    _seed(env)
    op = ProfileOp("save_profile", profile=_profile("new-slug"), credential="sk-new")
    with patch("moira.provider_editor.save_settings", side_effect=OSError("disk full")):
        result = _execute_op(op)
    assert result.ok is False and result.reason == "Operation failed."
    assert load_settings().provider_profiles == ()  # nothing persisted
    assert ("new-slug", "api_key") not in env[1].items  # no orphan credential


def test_edit_new_credential_then_config_failure_restores_old_credential(
    env: tuple[Path, _FakeSecret],
) -> None:
    _seed(env, _profile("deepseek-main"))
    env[1].items[("deepseek-main", "api_key")] = "sk-old"
    op = ProfileOp(
        "save_profile",
        profile=_profile("deepseek-main", label="Renamed"),
        credential="sk-new",
    )
    with patch("moira.provider_editor.save_settings", side_effect=OSError("disk full")):
        result = _execute_op(op)
    assert result.ok is False
    assert load_settings().provider_profiles[0].label == "DeepSeek main"  # original
    assert env[1].items[("deepseek-main", "api_key")] == "sk-old"  # restored


def test_rename_migrates_then_config_failure_restores_both_slug_states(
    env: tuple[Path, _FakeSecret],
) -> None:
    _seed(env, _profile("old-slug"))
    env[1].items[("old-slug", "api_key")] = "sk-old"
    op = ProfileOp("save_profile", profile=_profile("new-slug"), old_slug="old-slug")
    with patch("moira.provider_editor.save_settings", side_effect=OSError("disk full")):
        result = _execute_op(op)
    assert result.ok is False
    assert [p.slug for p in load_settings().provider_profiles] == ["old-slug"]
    assert env[1].items[("old-slug", "api_key")] == "sk-old"  # old credential intact
    assert ("new-slug", "api_key") not in env[1].items  # no orphan copy


def test_rename_store_failure_leaves_old_credential_intact(
    env: tuple[Path, _FakeSecret],
) -> None:
    _seed(env, _profile("old-slug"))
    env[1].items[("old-slug", "api_key")] = "sk-old"
    env[1].fail = "store"
    op = ProfileOp(
        "save_profile",
        profile=_profile("new-slug"),
        old_slug="old-slug",
        credential="sk-new",
    )
    result = _execute_op(op)
    assert result.ok is False and result.reason == "Keyring unavailable."
    assert [p.slug for p in load_settings().provider_profiles] == ["old-slug"]
    # The old credential was NOT cleared before the new one could be stored.
    assert env[1].items[("old-slug", "api_key")] == "sk-old"


def test_rename_old_clear_failure_rolls_back_profiles_and_new_secret(
    env: tuple[Path, _FakeSecret],
) -> None:
    _seed(env, _profile("old-slug"))
    env[1].items[("old-slug", "api_key")] = "sk-old"
    env[1].fail = "clear"
    env[1].fail_times = 1  # the old-slug clear fails once; compensation recovers
    op = ProfileOp("save_profile", profile=_profile("new-slug"), old_slug="old-slug")
    result = _execute_op(op)
    assert result.ok is False and result.reason == "Keyring unavailable."
    # Original profiles restored on disk (rename fully rolled back).
    assert [p.slug for p in load_settings().provider_profiles] == ["old-slug"]
    # Old/new secret states restored: old kept, no orphan copy at new slug.
    assert env[1].items[("old-slug", "api_key")] == "sk-old"
    assert ("new-slug", "api_key") not in env[1].items


def test_remove_profile_config_failure_keeps_credential(
    env: tuple[Path, _FakeSecret],
) -> None:
    _seed(env, _profile("deepseek-main"))
    env[1].items[("deepseek-main", "api_key")] = "sk-abc"
    op = ProfileOp("remove_profile", slug="deepseek-main")
    with patch("moira.provider_editor.save_settings", side_effect=OSError("disk full")):
        result = _execute_op(op)
    assert result.ok is False
    # The credential was never cleared ahead of persistence.
    assert [p.slug for p in load_settings().provider_profiles] == ["deepseek-main"]
    assert env[1].items[("deepseek-main", "api_key")] == "sk-abc"


def test_remove_profile_clear_failure_leaves_both_unchanged(
    env: tuple[Path, _FakeSecret],
) -> None:
    _seed(env, _profile("deepseek-main"))
    env[1].items[("deepseek-main", "api_key")] = "sk-abc"
    env[1].fail = "clear"
    op = ProfileOp("remove_profile", slug="deepseek-main")
    result = _execute_op(op)
    assert result.ok is False and result.reason == "Keyring unavailable."
    assert [p.slug for p in load_settings().provider_profiles] == ["deepseek-main"]
    assert env[1].items[("deepseek-main", "api_key")] == "sk-abc"


def test_remove_profile_absent_credential_is_successful_noop(
    env: tuple[Path, _FakeSecret],
) -> None:
    _seed(env, _profile("deepseek-main"))
    op = ProfileOp("remove_profile", slug="deepseek-main")
    result = _execute_op(op)
    assert result.ok is True
    assert load_settings().provider_profiles == ()


def test_rename_with_explicit_new_credential_migrates_and_clears_old(
    env: tuple[Path, _FakeSecret],
) -> None:
    _seed(env, _profile("old-slug"))
    env[1].items[("old-slug", "api_key")] = "sk-old"
    op = ProfileOp(
        "save_profile",
        profile=_profile("new-slug"),
        old_slug="old-slug",
        credential="sk-new",
    )
    result = _execute_op(op)
    assert result.ok is True
    assert [p.slug for p in load_settings().provider_profiles] == ["new-slug"]
    assert env[1].items[("new-slug", "api_key")] == "sk-new"
    assert ("old-slug", "api_key") not in env[1].items


def test_rename_blank_credential_migrates_existing_secret(
    env: tuple[Path, _FakeSecret],
) -> None:
    _seed(env, _profile("old-slug"))
    env[1].items[("old-slug", "api_key")] = "sk-old"
    op = ProfileOp("save_profile", profile=_profile("new-slug"), old_slug="old-slug")
    result = _execute_op(op)
    assert result.ok is True
    assert env[1].items[("new-slug", "api_key")] == "sk-old"  # migrated
    assert ("old-slug", "api_key") not in env[1].items  # old removed


def test_edit_blank_credential_preserves_existing(
    env: tuple[Path, _FakeSecret],
) -> None:
    _seed(env, _profile("deepseek-main"))
    env[1].items[("deepseek-main", "api_key")] = "sk-old"
    op = ProfileOp("save_profile", profile=_profile("deepseek-main", label="Renamed"))
    result = _execute_op(op)
    assert result.ok is True
    assert load_settings().provider_profiles[0].label == "Renamed"
    assert env[1].items[("deepseek-main", "api_key")] == "sk-old"


def test_secrets_never_reach_config(env: tuple[Path, _FakeSecret]) -> None:
    _seed(env)
    op = ProfileOp("save_profile", profile=_profile("deepseek-main"), credential="sk-super-secret")
    assert _execute_op(op).ok is True
    raw = (Path(os.environ["XDG_CONFIG_HOME"]) / "moira" / "config.json").read_text(
        encoding="utf-8"
    )
    assert "sk-super-secret" not in raw


# ── Finding 5 / criterion 9: submit rejection recovery ───────────────────────


def test_submit_rejection_clears_slot_and_accepts_later_work(
    env: tuple[Path, _FakeSecret], english: None, idle_inline: None
) -> None:
    ed = ProviderEditor(submit=_FlakySubmit(), on_profiles_changed=None)
    # The construction reload was rejected: fixed translated outcome, slot free.
    assert ed._in_flight is False
    assert ed.status_label.get_text() == "Operation failed."
    # Later work is accepted and lands.
    ed._show_form(None)
    ed.slug_entry.set_text("deepseek-main")
    ed.label_entry.set_text("DeepSeek main")
    ed.form_save_button.emit("clicked")
    assert ed.status_label.get_text() == "Profile saved."
    assert [p.slug for p in load_settings().provider_profiles] == ["deepseek-main"]


# ── Criterion 10: barrier tests (no lost updates) ────────────────────────────


def test_barrier_toggle_a_then_edit_b_both_land(
    env: tuple[Path, _FakeSecret], english: None, idle_inline: None
) -> None:
    """A toggle on A parked while an edit of B is requested must not
    overwrite A's committed change."""
    _seed(
        env,
        _profile("a-profile"),
        _profile("b-profile", label="B before"),
    )
    gate = _GatedSubmit()
    ed = _open_editor(env, submit=gate)
    gate.flush()  # reload lands

    ed._row_widgets["a-profile"]["switch"].set_active(False)  # toggle A (parked)
    ed._row_widgets["b-profile"]["edit"].emit("clicked")
    ed.label_entry.set_text("B after")
    ed.form_save_button.emit("clicked")  # edit B (parked)
    gate.flush()

    disk = load_settings().provider_profiles
    by_slug = {p.slug: p for p in disk}
    assert by_slug["a-profile"].enabled is False  # toggle survived the edit
    assert by_slug["b-profile"].label == "B after"


def test_barrier_add_x_then_remove_y_both_land(
    env: tuple[Path, _FakeSecret], english: None, idle_inline: None
) -> None:
    """An add parked while a removal of another profile is requested must
    not lose the added profile."""
    _seed(env, _profile("y-profile"))
    gate = _GatedSubmit()
    ed = _open_editor(env, submit=gate)
    gate.flush()

    ed._show_form(None)
    ed.slug_entry.set_text("x-profile")
    ed.label_entry.set_text("X profile")
    ed.form_save_button.emit("clicked")  # add X (parked)
    ed._row_widgets["y-profile"]["remove"].emit("clicked")
    ed._row_widgets["y-profile"]["confirm_remove"].emit("clicked")  # remove Y (parked)
    gate.flush()

    slugs = [p.slug for p in load_settings().provider_profiles]
    assert slugs == ["x-profile"]  # X added AND Y removed


def test_barrier_pending_replacement_newest_wins(
    env: tuple[Path, _FakeSecret], english: None, idle_inline: None
) -> None:
    gate = _GatedSubmit()
    ed = _open_editor(env, submit=gate)
    gate.flush()
    ed._in_flight = True
    first = ProfileOp("remove_credential", slug="deepseek-main")
    second = ProfileOp("remove_credential", slug="deepseek-main")
    ed._request_op(first)
    assert ed._pending_op is first
    ed._request_op(second)
    assert ed._pending_op is second
    result = ProfileOpResult(True, "", (), (), "remove_credential", "deepseek-main")
    ed._apply_op(result, ed._generation)
    gate.flush()
    assert ed._in_flight is False and ed._pending_op is None
    assert ed.status_label.get_text() == "Credential removed."


def test_delayed_completion_after_shutdown_touches_neither_widgets_nor_disk(
    env: tuple[Path, _FakeSecret], english: None, idle_inline: None
) -> None:
    gate = _GatedSubmit()
    ed = _open_editor(env, submit=gate)  # reload parked, in flight
    ed.shutdown()
    ghost = ProfileOpResult(
        True, "", (_profile("ghost"),), (("ghost", True),), "save_profile", "ghost"
    )
    assert ed._apply_op(ghost, ed._generation) is False
    assert ed._profiles == ()
    assert "ghost" not in ed._row_widgets
    assert load_settings().provider_profiles == ()


def test_stale_completion_updates_neither_widgets_nor_disk(
    env: tuple[Path, _FakeSecret], english: None, idle_inline: None
) -> None:
    _seed(env, _profile("deepseek-main"))
    ed = _open_editor(env)
    stale = ProfileOpResult(
        True, "", (_profile("ghost"),), (("ghost", True),), "save_profile", "ghost"
    )
    assert ed._apply_op(stale, ed._generation - 1) is False
    assert [p.slug for p in ed._profiles] == ["deepseek-main"]
    assert load_settings().provider_profiles != ()
    assert [p.slug for p in load_settings().provider_profiles] == ["deepseek-main"]


# ── Criterion 11: editor lifecycle tied to MainWindow ────────────────────────


def test_editor_close_cleanup_and_reopen_creates_new(
    env: tuple[Path, _FakeSecret], english: None, idle_inline: None
) -> None:
    from moira.ui import MainWindow

    window = MainWindow.__new__(MainWindow)
    window.executor = type("Executor", (), {"submit": staticmethod(lambda fn, *a: fn(*a))})()
    window._open_provider_editor()
    first = window._provider_editor
    assert first is not None
    first.emit("close-request")
    assert window._provider_editor is None
    assert first._shutdown is True
    window._open_provider_editor()
    assert window._provider_editor is not first  # one live editor, fresh instance


def test_no_callback_after_closure(
    env: tuple[Path, _FakeSecret], english: None, idle_inline: None
) -> None:
    fired: list[int] = []

    def on_changed() -> None:
        fired.append(1)

    ed = _open_editor(env, on_profiles_changed=on_changed)
    ed.shutdown()
    result = ProfileOpResult(True, "", (_profile("x"),), (("x", True),), "save_profile", "x")
    ed._apply_op(result, ed._generation)
    assert fired == []  # no callback after closure


def test_editor_is_transient_to_real_window(env: tuple[Path, _FakeSecret], english: None) -> None:
    from moira.ui import MainWindow

    app = Adw.Application(application_id="io.github.moira.QuotaMonitorTest")
    window = MainWindow(app, smoke_test=True)
    try:
        window._open_provider_editor()
        editor = window._provider_editor
        assert editor is not None
        assert editor.get_transient_for() is window
    finally:
        window._on_close_request()
