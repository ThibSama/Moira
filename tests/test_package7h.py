"""Package 7h — finalize profile recovery invariants (ACCEPTANCE_CORRECTION).

RED tests for the five blocking findings on a794c36:

1. ``read_journal`` claims an exact eight-key schema but checks only
   ``set(data) <= _JOURNAL_KEYS``; save journals may omit fields and
   decode through defaults.
2. Backup deletion after a committed overwrite is best-effort; its
   failure is ignored, the journal is cleared and success is reported
   with an orphaned credential copy.
3. On metadata/toggle or remove persistence failure, journal cleanup is
   attempted without a durable rollback/no-op transition; later recovery
   may complete an operation already reported failed.
4. A failed reload recovery sets ``_recovery_blocked`` but ``_apply_op``
   still starts an existing pending mutation through ``_start_op``.
5. Startup recovery is off GTK but not bounded or generation-controlled;
   it can occupy a shared worker indefinitely.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import gi  # type: ignore[import-untyped]

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Secret", "1")
import pytest
from gi.repository import GLib, Secret  # type: ignore[import-untyped]  # noqa: E402

from moira.integrations import ProviderKind, ProviderProfile
from moira.persistence import Settings, load_settings, save_settings
from moira.profile_journal import recover_pending_transaction
from moira.provider_editor import ProfileOp, ProviderEditor, _execute_op

# ── Test doubles ─────────────────────────────────────────────────────────────


class _FakeSecret:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}
        self.fail: str | None = None
        self.fail_times: int = -1
        self.fail_on_call: dict[str, int | None] = {"lookup": None, "store": None, "clear": None}
        self.call_counters: dict[str, int] = {"lookup": 0, "store": 0, "clear": 0}
        self.fail_when: list[tuple[str, str, str, str]] = []
        self.ops: list[tuple[str, str, str, str]] = []

    def _attributes(self, attributes: dict[str, str]) -> tuple[str, str, str]:
        if attributes.get("account") == "ntfy-token":
            return ("ntfy", "", "")
        return (attributes["slug"], attributes["purpose"], attributes["slug"])

    def _should_fail(self, kind: str, purpose: str, slug: str, value: str) -> bool:
        self.call_counters[kind] += 1
        if self.fail == kind:
            if self.fail_times == -1:
                return True
            if self.fail_times > 0:
                self.fail_times -= 1
                return True
        if self.fail_on_call[kind] == self.call_counters[kind]:
            return True
        for i, (k, p, s, v) in enumerate(self.fail_when):
            if k == kind and p == purpose and s == slug and v == value:
                del self.fail_when[i]
                return True
        return False

    def password_lookup_sync(
        self, _schema: Any, attributes: dict[str, str], _cancellable: Any
    ) -> str | None:
        kind, purpose, slug = self._attributes(attributes)
        self.ops.append(("lookup", purpose, slug, ""))
        if self._should_fail("lookup", purpose, slug, ""):
            raise RuntimeError("secret vault locked")
        if kind == "ntfy":
            return self.items.get(("ntfy", ""))
        return self.items.get((slug, purpose))

    def password_store_sync(
        self,
        _schema: Any,
        attributes: dict[str, str],
        _collection: Any,
        _label: Any,
        value: str,
        _cancellable: Any,
    ) -> None:
        kind, purpose, slug = self._attributes(attributes)
        self.ops.append(("store", purpose, slug, value))
        if self._should_fail("store", purpose, slug, value):
            raise RuntimeError("secret vault locked")
        if kind == "ntfy":
            self.items[("ntfy", "")] = value
            return
        self.items[(slug, purpose)] = value

    def password_clear_sync(
        self, _schema: Any, attributes: dict[str, str], _cancellable: Any
    ) -> None:
        kind, purpose, slug = self._attributes(attributes)
        self.ops.append(("clear", purpose, slug, ""))
        if self._should_fail("clear", purpose, slug, ""):
            raise RuntimeError("secret vault locked")
        if kind == "ntfy":
            self.items.pop(("ntfy", ""), None)
            return
        self.items.pop((slug, purpose), None)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, _FakeSecret]:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
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
    env[1].ops = []
    env[1].fail_when = []


def _journal_path(env: tuple[Path, _FakeSecret]) -> Path:
    return Path(os.environ["XDG_STATE_HOME"]) / "moira" / "profile-tx.json"


# ── Finding 1: exact journal schema, concrete types, phase consistency ──────


def _valid_body(phase: str = "staged") -> dict[str, Any]:
    return {
        "version": 1,
        "op": "save_profile",
        "phase": phase,
        "profile": {
            "slug": "new-main",
            "label": "DeepSeek main",
            "kind": "deepseek",
            "model": "deepseek-chat",
            "enabled": True,
            "base_url": "",
            "hermes_label": "",
        },
        "old_slug": "",
        "slug": "new-main",
        "secret_slug": "new-main",
        "had_backup": False,
    }


def _write_raw_journal(env: tuple[Path, _FakeSecret], payload: dict[str, Any]) -> None:
    path = _journal_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_journal_missing_slug_field_is_rejected(env: tuple[Path, _FakeSecret]) -> None:
    body = _valid_body()
    del body["slug"]  # decoded through a default in 7g
    _write_raw_journal(env, body)
    assert recover_pending_transaction() is False
    assert _journal_path(env).exists()


def test_journal_missing_old_slug_field_is_rejected(env: tuple[Path, _FakeSecret]) -> None:
    body = _valid_body()
    del body["old_slug"]
    _write_raw_journal(env, body)
    assert recover_pending_transaction() is False
    assert _journal_path(env).exists()


def test_journal_missing_secret_slug_field_is_rejected(env: tuple[Path, _FakeSecret]) -> None:
    body = _valid_body()
    del body["secret_slug"]
    _write_raw_journal(env, body)
    assert recover_pending_transaction() is False
    assert _journal_path(env).exists()


def test_journal_non_string_slug_is_rejected(env: tuple[Path, _FakeSecret]) -> None:
    body = _valid_body()
    body["slug"] = 123
    _write_raw_journal(env, body)
    assert recover_pending_transaction() is False
    assert _journal_path(env).exists()


def test_journal_self_rename_is_rejected(env: tuple[Path, _FakeSecret]) -> None:
    body = _valid_body()
    body["old_slug"] = "new-main"  # old == new is phase-inconsistent
    _write_raw_journal(env, body)
    assert recover_pending_transaction() is False
    assert _journal_path(env).exists()


def test_journal_secret_slug_must_match_profile_slug(env: tuple[Path, _FakeSecret]) -> None:
    body = _valid_body()
    body["secret_slug"] = "other-main"
    _write_raw_journal(env, body)
    assert recover_pending_transaction() is False
    assert _journal_path(env).exists()


def test_journal_backup_requires_staged_secret(env: tuple[Path, _FakeSecret]) -> None:
    body = _valid_body()
    body["secret_slug"] = ""
    body["had_backup"] = True  # backup without any staged secret is inconsistent
    _write_raw_journal(env, body)
    assert recover_pending_transaction() is False
    assert _journal_path(env).exists()


def test_journal_remove_with_backup_flag_is_rejected(env: tuple[Path, _FakeSecret]) -> None:
    body = _valid_body()
    body["op"] = "remove_profile"
    body["slug"] = "deepseek-main"
    body["secret_slug"] = ""
    body["had_backup"] = True
    _write_raw_journal(env, body)
    assert recover_pending_transaction() is False
    assert _journal_path(env).exists()


def test_valid_journals_still_accepted(env: tuple[Path, _FakeSecret]) -> None:
    """Valid 7f/7g save and remove journals still recover."""
    body = _valid_body()
    _write_raw_journal(env, body)
    assert recover_pending_transaction() is True
    assert _journal_path(env).exists() is False
    remove = _valid_body()
    remove["op"] = "remove_profile"
    remove["profile"] = None
    remove["slug"] = "deepseek-main"
    remove["secret_slug"] = ""
    remove["had_backup"] = False
    _write_raw_journal(env, remove)
    assert recover_pending_transaction() is True
    assert _journal_path(env).exists() is False


# ── Finding 2: backup cleanup after commit is mandatory ─────────────────────


def test_backup_cleanup_failure_never_reports_success(
    env: tuple[Path, _FakeSecret],
) -> None:
    """After a committed overwrite, a failed backup deletion keeps the
    forward journal, returns a sanitized failure and lets idempotent
    recovery remove the backup before clearing the journal."""
    _seed(env, _profile("deepseek-main"))
    env[1].items[("deepseek-main", "api_key")] = "sk-old"
    op = ProfileOp(
        "save_profile", profile=_profile("deepseek-main", label="Renamed"), credential="sk-new"
    )
    env[1].fail_when = [("clear", "backup", "deepseek-main", "")]
    result = _execute_op(op)
    assert result.ok is False  # never success while the backup remains
    assert _journal_path(env).exists()  # forward journal kept
    assert ("deepseek-main", "backup") in env[1].items  # orphan backup still present
    assert recover_pending_transaction() is True
    assert ("deepseek-main", "backup") not in env[1].items  # recovery removed it
    assert env[1].items[("deepseek-main", "api_key")] == "sk-new"  # committed credential kept
    assert load_settings().provider_profiles[0].label == "Renamed"
    assert _journal_path(env).exists() is False


def test_facade_recovery_after_backup_cleanup_fault(
    env: tuple[Path, _FakeSecret], english: None, idle_inline: None
) -> None:
    """Recreate the facade after the backup-cleanup fault: the fresh
    editor's reload converges without loss or orphan."""
    _seed(env, _profile("deepseek-main"))
    env[1].items[("deepseek-main", "api_key")] = "sk-old"
    op = ProfileOp(
        "save_profile", profile=_profile("deepseek-main", label="Renamed"), credential="sk-new"
    )
    env[1].fail_when = [("clear", "backup", "deepseek-main", "")]
    assert _execute_op(op).ok is False
    ed = ProviderEditor(submit=lambda fn, *a: fn(*a))
    assert ed._recovery_blocked is False
    assert ed._configured.get("deepseek-main") is True
    assert ("deepseek-main", "backup") not in env[1].items
    assert _journal_path(env).exists() is False


# ── Finding 3: durable no-op transition before cleanup on config failure ────


def _flaky_clear_once(module_name: str = "moira.provider_editor") -> Any:
    import importlib

    real_clear = importlib.import_module(module_name).clear_journal
    state = {"n": 0}

    def flaky_clear() -> bool:
        state["n"] += 1
        if state["n"] == 1:
            return False
        return bool(real_clear())

    return flaky_clear


def test_toggle_config_failure_transitions_to_noop_before_cleanup(
    env: tuple[Path, _FakeSecret],
) -> None:
    """A metadata/toggle config failure durably selects the no-op phase
    BEFORE cleanup: if the journal clear fails, retry recovery preserves
    the original config (never completes the failed op forward)."""
    _seed(env, _profile("deepseek-main"))
    env[1].items[("deepseek-main", "api_key")] = "sk-old"
    op = ProfileOp("save_profile", profile=_profile("deepseek-main", enabled=False))
    with patch("moira.persistence.save_settings", side_effect=OSError("disk full")):
        with patch("moira.provider_editor.clear_journal", _flaky_clear_once()):
            result = _execute_op(op)
    assert result.ok is False
    from moira.profile_journal import JournalPhase, read_journal

    entry = read_journal()
    assert entry is not None and entry.phase == JournalPhase.STAGED  # no-op phase, never forward
    assert recover_pending_transaction() is True
    assert load_settings().provider_profiles[0].enabled is True  # original preserved
    assert env[1].items[("deepseek-main", "api_key")] == "sk-old"
    assert env[1].ops == []  # zero Keyring mutations
    assert _journal_path(env).exists() is False


def test_remove_config_failure_transitions_to_noop_before_cleanup(
    env: tuple[Path, _FakeSecret],
) -> None:
    """Same durable no-op selection for a removal whose persist fails."""
    _seed(env, _profile("deepseek-main"))
    env[1].items[("deepseek-main", "api_key")] = "sk-old"
    op = ProfileOp("remove_profile", slug="deepseek-main")
    with patch("moira.persistence.save_settings", side_effect=OSError("disk full")):
        with patch("moira.provider_editor.clear_journal", _flaky_clear_once()):
            result = _execute_op(op)
    assert result.ok is False
    from moira.profile_journal import JournalPhase, read_journal

    entry = read_journal()
    assert entry is not None and entry.phase == JournalPhase.STAGED
    assert recover_pending_transaction() is True
    assert [p.slug for p in load_settings().provider_profiles] == ["deepseek-main"]  # preserved
    assert env[1].items[("deepseek-main", "api_key")] == "sk-old"
    assert _journal_path(env).exists() is False


def test_remove_erase_failure_transitions_to_noop_after_restore(
    env: tuple[Path, _FakeSecret],
) -> None:
    """A removal whose credential erase fails restores the config, then
    durably selects the no-op phase; a failed cleanup keeps it so retry
    recovery preserves the original profile and credential."""
    _seed(env, _profile("deepseek-main"))
    env[1].items[("deepseek-main", "api_key")] = "sk-old"
    op = ProfileOp("remove_profile", slug="deepseek-main")
    env[1].fail = "clear"
    env[1].fail_times = 1  # the credential erase fails once
    with patch("moira.provider_editor.clear_journal", _flaky_clear_once()):
        result = _execute_op(op)
    assert result.ok is False
    from moira.profile_journal import JournalPhase, read_journal

    entry = read_journal()
    assert entry is not None and entry.phase == JournalPhase.STAGED
    assert recover_pending_transaction() is True
    assert [p.slug for p in load_settings().provider_profiles] == ["deepseek-main"]
    assert env[1].items[("deepseek-main", "api_key")] == "sk-old"
    assert _journal_path(env).exists() is False


# ── Finding 4: failed reload recovery admits no pending mutation ────────────


class _GatedSubmit:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, tuple[Any, ...]]] = []

    def __call__(self, fn: Any, *args: Any) -> None:
        self.calls.append((fn, args))

    def flush(self) -> None:
        while self.calls:
            fn, args = self.calls.pop(0)
            fn(*args)


def test_failed_reload_recovery_discards_pending_mutation(
    env: tuple[Path, _FakeSecret], english: None, idle_inline: None
) -> None:
    """A failed reload recovery must execute NO pending save/toggle; the
    pending work is discarded and only a later successful reload admits
    mutations."""
    _journal_path(env).parent.mkdir(parents=True, exist_ok=True)
    _journal_path(env).write_text('{"version": 7, "op": "save_profile"}', encoding="utf-8")
    gate = _GatedSubmit()
    ed = ProviderEditor(submit=gate)
    # The reload is parked in flight; a mutation is queued behind it.
    ed._request_op(ProfileOp("save_profile", profile=_profile("deepseek-main", enabled=False)))
    assert ed._pending_op is not None
    gate.flush()  # reload runs → recovery fails → blocked
    assert ed._recovery_blocked is True
    assert ed.status_label.get_text() == "Recovery required."
    assert load_settings().provider_profiles == ()  # the mutation never wrote
    assert ("deepseek-main", "api_key") not in env[1].items
    assert not gate.calls  # the pending op was discarded, not started


def test_successful_reload_still_promotes_pending(
    env: tuple[Path, _FakeSecret], english: None, idle_inline: None
) -> None:
    """Preserve-contract: a successful reload (recovery ok) promotes the
    parked mutation normally."""
    _seed(env, _profile("deepseek-main"))
    gate = _GatedSubmit()
    ed = ProviderEditor(submit=gate)
    ed._request_op(ProfileOp("save_profile", profile=_profile("deepseek-main", enabled=False)))
    gate.flush()
    assert load_settings().provider_profiles[0].enabled is False  # promoted and executed
    assert ed._recovery_blocked is False


# ── Finding 5 / criteria 8–9: bounded, generation-aware startup bootstrap ───


def _build_window(
    env: tuple[Path, _FakeSecret],
    monkeypatch: pytest.MonkeyPatch,
    *,
    slow_recovery: Any = None,
) -> tuple[Any, list[tuple[Any, tuple[Any, ...]]]]:
    from gi.repository import Adw

    from moira.ui import MainWindow

    app = Adw.Application(application_id="io.github.moira.QuotaMonitor.Test7h")
    callbacks: list[tuple[Any, tuple[Any, ...]]] = []
    monkeypatch.setattr(GLib, "idle_add", lambda cb, *a: callbacks.append((cb, a)))
    if slow_recovery is not None:
        monkeypatch.setattr("moira.ui.recover_pending_transaction", slow_recovery)
    win = MainWindow(app, smoke_test=True)
    return win, callbacks


def _wait_startup_callback(
    win: Any, callbacks: list[tuple[Any, tuple[Any, ...]]], timeout: float = 2.0
) -> tuple[Any, tuple[Any, ...]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for i, (cb, _args) in enumerate(callbacks):
            if getattr(cb, "__self__", None) is win and cb.__name__ == "_finish_startup_recovery":
                return callbacks.pop(i)
        time.sleep(0.01)
    raise AssertionError("startup recovery callback never recorded")


def test_startup_recovery_uses_dedicated_bootstrap_executor(
    env: tuple[Path, _FakeSecret], monkeypatch: pytest.MonkeyPatch, english: None
) -> None:
    """The bootstrap runs on its own single-worker executor, never
    occupying the shared collector executor."""
    win, callbacks = _build_window(env, monkeypatch)
    assert hasattr(win, "_bootstrap_executor")
    assert win._bootstrap_executor is not win.executor
    assert win._bootstrap_executor._max_workers == 1
    # The shared executor stays fully available while the bootstrap runs.
    done = threading.Event()

    def probe() -> None:
        done.set()

    win.executor.submit(probe)
    assert done.wait(2)
    win._on_close_request()
    win.close()


def test_startup_recovery_worker_exception_is_sanitized(
    env: tuple[Path, _FakeSecret], monkeypatch: pytest.MonkeyPatch, english: None
) -> None:
    """Every worker failure maps to a sanitized failure: controls stay
    disabled and the translated status is shown — no raw error."""

    def boom() -> bool:
        raise RuntimeError("vault exploded")

    win, callbacks = _build_window(env, monkeypatch, slow_recovery=boom)
    cb, args = _wait_startup_callback(win, callbacks)
    cb(*args)
    assert win._save_settings_button.get_sensitive() is False
    assert win._integrations_page.edit_providers_button.get_sensitive() is False
    assert win.settings_status.get_text() == "Recovery required."
    win._on_close_request()
    win.close()


def test_close_during_bootstrap_rejects_late_completion(
    env: tuple[Path, _FakeSecret], monkeypatch: pytest.MonkeyPatch, english: None
) -> None:
    started = threading.Event()
    release = threading.Event()

    def slow() -> bool:
        started.set()
        release.wait(2)
        return True

    win, callbacks = _build_window(env, monkeypatch, slow_recovery=slow)
    assert started.wait(2)
    win._on_close_request()  # close while the bootstrap is still running
    win.close()
    release.set()
    cb, args = _wait_startup_callback(win, callbacks)
    recorded = list(callbacks)
    cb(*args)  # late completion must be rejected by the closure guard
    assert callbacks == recorded  # nothing was re-scheduled
    assert win._save_settings_button.get_sensitive() is False  # never re-enabled after close


def test_bootstrap_submit_rejection_fails_closed(
    env: tuple[Path, _FakeSecret], monkeypatch: pytest.MonkeyPatch, english: None
) -> None:
    """If the bootstrap cannot be submitted, the window still opens with
    every mutation control disabled (fail closed)."""
    from concurrent.futures import ThreadPoolExecutor

    real_submit = ThreadPoolExecutor.submit

    def broken_submit(self: Any, *a: Any, **k: Any) -> Any:
        raise RuntimeError("executor rejected")

    monkeypatch.setattr(ThreadPoolExecutor, "submit", broken_submit)
    win, callbacks = _build_window(env, monkeypatch)
    assert win._save_settings_button.get_sensitive() is False
    assert win._integrations_page.edit_providers_button.get_sensitive() is False
    monkeypatch.setattr(ThreadPoolExecutor, "submit", real_submit)
    win._on_close_request()
    win.close()


def test_mutation_controls_gated_until_bootstrap_lands(
    env: tuple[Path, _FakeSecret], monkeypatch: pytest.MonkeyPatch, english: None
) -> None:
    """Controls are disabled during the bootstrap and re-enabled only
    after a successful recovery lands."""
    started = threading.Event()
    release = threading.Event()

    def slow() -> bool:
        started.set()
        release.wait(2)
        return True

    win, callbacks = _build_window(env, monkeypatch, slow_recovery=slow)
    assert started.wait(2)
    assert win._save_settings_button.get_sensitive() is False
    assert win._integrations_page.edit_providers_button.get_sensitive() is False
    release.set()
    cb, args = _wait_startup_callback(win, callbacks)
    cb(*args)
    assert win._save_settings_button.get_sensitive() is True
    assert win._integrations_page.edit_providers_button.get_sensitive() is True
    win._on_close_request()
    win.close()


def test_bootstrap_retry_on_next_window(
    env: tuple[Path, _FakeSecret], monkeypatch: pytest.MonkeyPatch, english: None
) -> None:
    """A failed bootstrap is retried on the next app start (fresh window):
    the second bootstrap succeeds and re-enables the controls."""

    def boom() -> bool:
        raise RuntimeError("vault exploded")

    win1, callbacks1 = _build_window(env, monkeypatch, slow_recovery=boom)
    cb, args = _wait_startup_callback(win1, callbacks1)
    cb(*args)
    assert win1._save_settings_button.get_sensitive() is False
    win1._on_close_request()
    win1.close()
    monkeypatch.setattr("moira.ui.recover_pending_transaction", recover_pending_transaction)
    win2, callbacks2 = _build_window(env, monkeypatch)
    cb, args = _wait_startup_callback(win2, callbacks2)
    cb(*args)
    assert win2._save_settings_button.get_sensitive() is True
    assert win2._integrations_page.edit_providers_button.get_sensitive() is True
    win2._on_close_request()
    win2.close()


# ── Criterion 6: fault at journal write converges without loss/orphan ───────


def test_journal_write_fault_before_any_effect_leaves_nothing(
    env: tuple[Path, _FakeSecret],
) -> None:
    _seed(env, _profile("deepseek-main"))
    env[1].items[("deepseek-main", "api_key")] = "sk-old"
    op = ProfileOp(
        "save_profile", profile=_profile("deepseek-main", label="Renamed"), credential="sk-new"
    )
    with patch("moira.provider_editor.write_journal", side_effect=OSError("no space")):
        result = _execute_op(op)
    assert result.ok is False
    assert load_settings().provider_profiles[0].label == "DeepSeek main"  # config untouched
    assert env[1].items[("deepseek-main", "api_key")] == "sk-old"  # keyring untouched
    assert _journal_path(env).exists() is False or recover_pending_transaction() is True


def test_journal_write_fault_after_store_keeps_rollback_phase(
    env: tuple[Path, _FakeSecret],
) -> None:
    """A fault on the config-committed write leaves the rollback phase:
    recovery rolls the staged secret back — no orphan, no loss."""
    _seed(env, _profile("deepseek-main"))
    env[1].items[("deepseek-main", "api_key")] = "sk-old"
    op = ProfileOp(
        "save_profile", profile=_profile("deepseek-main", label="Renamed"), credential="sk-new"
    )
    real_write = __import__("moira.provider_editor", fromlist=["write_journal"]).write_journal
    calls = {"n": 0}

    def flaky_write(entry: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 3:  # the config-committed write faults
            raise OSError("no space")
        real_write(entry)

    with patch("moira.provider_editor.write_journal", flaky_write):
        result = _execute_op(op)
    assert result.ok is False
    assert recover_pending_transaction() is True
    assert env[1].items[("deepseek-main", "api_key")] == "sk-old"  # overwritten value restored
    assert ("deepseek-main", "backup") not in env[1].items
    assert load_settings().provider_profiles[0].label == "DeepSeek main"
    assert _journal_path(env).exists() is False
