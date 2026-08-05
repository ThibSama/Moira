"""Package 7i — durable recovery direction and bounded bootstrap.

RED tests for the three blocking findings on 5033827:

1. ``read_journal`` accepts unknown keys INSIDE ``profile`` (e.g. an
   ``api_key`` field survives strict decoding).
2. Save/remove write CONFIG_COMMITTED before config persistence; if the
   persistence fails AND the transition write also fails, the old forward
   journal remains and recovery completes an already-failed op.
3. Startup recovery has no timeout or cancellation boundary: a hung
   recovery occupies the dedicated worker forever and the tests must
   manually release blocked fakes.
"""

from __future__ import annotations

import json
import os
import subprocess
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
from moira.profile_journal import JournalPhase, read_journal, recover_pending_transaction
from moira.provider_editor import ProfileOp, _execute_op

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


# ── Finding 1: exact nested profile schema ──────────────────────────────────


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


def test_journal_profile_extra_secret_key_rejected(env: tuple[Path, _FakeSecret]) -> None:
    body = _valid_body()
    body["profile"]["api_key"] = "sk-leak"  # undeclared secret-bearing field
    _write_raw_journal(env, body)
    assert recover_pending_transaction() is False
    assert _journal_path(env).exists()


def test_journal_profile_extra_metadata_rejected(env: tuple[Path, _FakeSecret]) -> None:
    body = _valid_body()
    body["profile"]["notes"] = "arbitrary"
    _write_raw_journal(env, body)
    assert recover_pending_transaction() is False
    assert _journal_path(env).exists()


def test_journal_profile_missing_field_rejected(env: tuple[Path, _FakeSecret]) -> None:
    body = _valid_body()
    del body["profile"]["hermes_label"]
    _write_raw_journal(env, body)
    assert recover_pending_transaction() is False
    assert _journal_path(env).exists()


def test_journal_profile_enabled_must_be_bool(env: tuple[Path, _FakeSecret]) -> None:
    body = _valid_body()
    body["profile"]["enabled"] = 1  # int is not bool
    _write_raw_journal(env, body)
    assert recover_pending_transaction() is False
    assert _journal_path(env).exists()


def test_journal_profile_kind_must_be_str(env: tuple[Path, _FakeSecret]) -> None:
    body = _valid_body()
    body["profile"]["kind"] = 7
    _write_raw_journal(env, body)
    assert recover_pending_transaction() is False
    assert _journal_path(env).exists()


def test_valid_profile_records_still_accepted(env: tuple[Path, _FakeSecret]) -> None:
    """A valid 7f–7h profile record (exactly seven fields, concrete
    types) still recovers."""
    body = _valid_body()
    _write_raw_journal(env, body)
    assert recover_pending_transaction() is True
    assert _journal_path(env).exists() is False


# ── Finding 2: persist-before-forward-write ordering ────────────────────────


def _flaky_write(env: tuple[Path, _FakeSecret], fail_call: int) -> Any:
    import moira.provider_editor as editor_mod

    real_write = editor_mod.write_journal  # type: ignore[attr-defined]
    calls = {"n": 0}

    def flaky_write(entry: Any) -> None:
        calls["n"] += 1
        if calls["n"] == fail_call:
            raise OSError("no space")
        real_write(entry)

    return flaky_write


def test_toggle_persist_failure_with_journal_write_fault_preserves_state(
    env: tuple[Path, _FakeSecret],
) -> None:
    """Metadata/toggle: a config failure plus a failed journal write must
    never leave a forward journal that completes the failed op."""
    _seed(env, _profile("deepseek-main"))
    env[1].items[("deepseek-main", "api_key")] = "sk-old"
    op = ProfileOp("save_profile", profile=_profile("deepseek-main", enabled=False))
    with patch("moira.persistence.save_settings", side_effect=OSError("disk full")):
        with patch("moira.provider_editor.write_journal", _flaky_write(env, 3)):
            result = _execute_op(op)
    assert result.ok is False
    assert recover_pending_transaction() is True
    assert load_settings().provider_profiles[0].enabled is True  # original preserved
    assert env[1].items[("deepseek-main", "api_key")] == "sk-old"
    assert _journal_path(env).exists() is False


def test_remove_persist_failure_with_journal_write_fault_preserves_profile(
    env: tuple[Path, _FakeSecret],
) -> None:
    _seed(env, _profile("deepseek-main"))
    env[1].items[("deepseek-main", "api_key")] = "sk-old"
    op = ProfileOp("remove_profile", slug="deepseek-main")
    with patch("moira.persistence.save_settings", side_effect=OSError("disk full")):
        with patch("moira.provider_editor.write_journal", _flaky_write(env, 3)):
            result = _execute_op(op)
    assert result.ok is False
    assert recover_pending_transaction() is True
    assert [p.slug for p in load_settings().provider_profiles] == ["deepseek-main"]  # preserved
    assert env[1].items[("deepseek-main", "api_key")] == "sk-old"
    assert _journal_path(env).exists() is False


def test_edit_persist_then_forward_write_fault_completes_forward(
    env: tuple[Path, _FakeSecret],
) -> None:
    """Edit with a new credential: a fault on the forward write (which
    now happens AFTER a successful persist) must complete FORWARD — a
    durably committed operation never rolls back."""
    _seed(env, _profile("deepseek-main"))
    env[1].items[("deepseek-main", "api_key")] = "sk-old"
    op = ProfileOp(
        "save_profile", profile=_profile("deepseek-main", label="Renamed"), credential="sk-new"
    )
    with patch("moira.provider_editor.write_journal", _flaky_write(env, 3)):
        result = _execute_op(op)
    assert result.ok is False
    assert recover_pending_transaction() is True
    assert env[1].items[("deepseek-main", "api_key")] == "sk-new"  # committed forward
    assert ("deepseek-main", "backup") not in env[1].items
    assert load_settings().provider_profiles[0].label == "Renamed"
    assert _journal_path(env).exists() is False


# ── Criterion 5: journal-write faults around persistence, per operation ─────


def test_write_fault_matrix_around_persistence(env: tuple[Path, _FakeSecret]) -> None:
    """For add/edit/toggle/rename/remove, a fault on the forward write
    (after persist) converges without direction inversion, credential
    loss or orphan; a fault before any effect leaves nothing."""
    # Add with credential: fault before any effect (staged write) → nothing.
    _seed(env)
    op = ProfileOp("save_profile", profile=_profile("new-main"), credential="sk-new")
    with patch("moira.provider_editor.write_journal", side_effect=OSError("no space")):
        result = _execute_op(op)
    assert result.ok is False
    assert load_settings().provider_profiles == ()
    assert ("new-main", "api_key") not in env[1].items
    assert recover_pending_transaction() is True

    # Add: fault on the forward write (after persist) → completed forward.
    _seed(env)
    op = ProfileOp("save_profile", profile=_profile("new-main"), credential="sk-new")
    with patch("moira.provider_editor.write_journal", _flaky_write(env, 3)):
        result = _execute_op(op)
    assert result.ok is False
    assert recover_pending_transaction() is True
    assert [p.slug for p in load_settings().provider_profiles] == ["new-main"]
    assert env[1].items[("new-main", "api_key")] == "sk-new"
    assert _journal_path(env).exists() is False

    # Edit: fault on the forward write → committed forward, no backup.
    _seed(env, _profile("deepseek-main"))
    env[1].items[("deepseek-main", "api_key")] = "sk-old"
    op = ProfileOp(
        "save_profile", profile=_profile("deepseek-main", label="Renamed"), credential="sk-new"
    )
    with patch("moira.provider_editor.write_journal", _flaky_write(env, 3)):
        result = _execute_op(op)
    assert result.ok is False
    assert recover_pending_transaction() is True
    assert env[1].items[("deepseek-main", "api_key")] == "sk-new"
    assert ("deepseek-main", "backup") not in env[1].items

    # Toggle: fault on the forward write (after persist) → toggle stays.
    _seed(env, _profile("deepseek-main"))
    op = ProfileOp("save_profile", profile=_profile("deepseek-main", enabled=False))
    with patch("moira.provider_editor.write_journal", _flaky_write(env, 2)):
        result = _execute_op(op)
    assert result.ok is False
    assert recover_pending_transaction() is True
    assert load_settings().provider_profiles[0].enabled is False  # committed forward

    # Rename (blank migration): fault on the forward write → forward, no
    # loss, no orphan.
    _seed(env, _profile("old-slug"))
    env[1].items[("old-slug", "api_key")] = "sk-old"
    op = ProfileOp("save_profile", profile=_profile("new-slug"), old_slug="old-slug")
    with patch("moira.provider_editor.write_journal", _flaky_write(env, 3)):
        result = _execute_op(op)
    assert result.ok is False
    assert recover_pending_transaction() is True
    assert [p.slug for p in load_settings().provider_profiles] == ["new-slug"]
    assert env[1].items[("new-slug", "api_key")] == "sk-old"
    assert ("old-slug", "api_key") not in env[1].items

    # Remove: fault on the forward write (after persist) → completed
    # forward, no orphan credential.
    _seed(env, _profile("deepseek-main"))
    env[1].items[("deepseek-main", "api_key")] = "sk-old"
    op = ProfileOp("remove_profile", slug="deepseek-main")
    with patch("moira.provider_editor.write_journal", _flaky_write(env, 2)):
        result = _execute_op(op)
    assert result.ok is False
    assert recover_pending_transaction() is True
    assert load_settings().provider_profiles == ()
    assert ("deepseek-main", "api_key") not in env[1].items
    assert _journal_path(env).exists() is False


def test_remove_staged_recovery_completes_forward_when_persisted(
    env: tuple[Path, _FakeSecret],
) -> None:
    """A staged remove journal whose persist already landed completes
    forward so no orphan credential outlives its profile."""
    from moira.profile_journal import JournalEntry, write_journal

    _seed(env, _profile("deepseek-main"))
    env[1].items[("deepseek-main", "api_key")] = "sk-old"
    save_settings(Settings(provider_profiles=()))  # the removal persisted
    write_journal(
        JournalEntry(1, "remove_profile", JournalPhase.STAGED, None, "", "deepseek-main", "", False)
    )
    assert recover_pending_transaction() is True
    assert ("deepseek-main", "api_key") not in env[1].items  # orphan erased
    assert _journal_path(env).exists() is False


def test_save_staged_secret_recovery_completes_forward_when_persisted(
    env: tuple[Path, _FakeSecret],
) -> None:
    """A staged-secret save journal whose persist already landed completes
    forward (the durably committed credential is kept, the backup is
    dropped)."""
    from moira.profile_journal import JournalEntry, write_journal
    from moira.secrets import BACKUP_PURPOSE, KeyringMutation, store_provider_secret

    _seed(env, _profile("deepseek-main"))
    env[1].items[("deepseek-main", "api_key")] = "sk-old"
    profile = _profile("deepseek-main", label="Renamed")
    assert store_provider_secret("deepseek-main", "sk-old", BACKUP_PURPOSE) is KeyringMutation.DONE
    assert store_provider_secret("deepseek-main", "sk-new") is KeyringMutation.DONE
    save_settings(Settings(provider_profiles=(profile,)))  # the persist landed
    write_journal(
        JournalEntry(
            1, "save_profile", JournalPhase.STAGED_SECRET, profile, "", "", "deepseek-main", True
        )
    )
    assert recover_pending_transaction() is True
    assert env[1].items[("deepseek-main", "api_key")] == "sk-new"  # committed forward
    assert ("deepseek-main", "backup") not in env[1].items
    assert _journal_path(env).exists() is False


# ── Criterion 6: backup-clear combined with journal failures ────────────────


def test_backup_clear_with_journal_clear_failure_converges(
    env: tuple[Path, _FakeSecret],
) -> None:
    """Backup-clear failure plus a journal-clear failure: the op reports
    failure, recovery retries idempotently and converges without the
    backup or any journal."""
    _seed(env, _profile("deepseek-main"))
    env[1].items[("deepseek-main", "api_key")] = "sk-old"
    op = ProfileOp(
        "save_profile", profile=_profile("deepseek-main", label="Renamed"), credential="sk-new"
    )
    env[1].fail_when = [("clear", "backup", "deepseek-main", "")]
    import moira.profile_journal as journal_mod

    real_clear = journal_mod.clear_journal
    state = {"n": 0}

    def flaky_clear() -> bool:
        state["n"] += 1
        if state["n"] == 1:
            return False
        return bool(real_clear())

    with patch.object(journal_mod, "clear_journal", flaky_clear):
        result = _execute_op(op)
        assert result.ok is False
        assert _journal_path(env).exists()
        assert recover_pending_transaction() is False  # journal clear failed once
        assert ("deepseek-main", "backup") not in env[1].items  # backup already removed
        assert _journal_path(env).exists()
        assert recover_pending_transaction() is True  # idempotent retry converges
    assert ("deepseek-main", "backup") not in env[1].items
    assert env[1].items[("deepseek-main", "api_key")] == "sk-new"
    assert _journal_path(env).exists() is False


def test_backup_clear_with_forward_write_fault_converges(
    env: tuple[Path, _FakeSecret],
) -> None:
    """Backup-clear failure combined with a forward-write fault: the
    journal stays at the rollback phase, the config is durably committed,
    and recovery retries the mandatory backup removal — no orphan."""
    _seed(env, _profile("deepseek-main"))
    env[1].items[("deepseek-main", "api_key")] = "sk-old"
    op = ProfileOp(
        "save_profile", profile=_profile("deepseek-main", label="Renamed"), credential="sk-new"
    )
    env[1].fail_when = [("clear", "backup", "deepseek-main", "")]
    with patch("moira.provider_editor.write_journal", _flaky_write(env, 3)):
        result = _execute_op(op)
    assert result.ok is False
    entry = read_journal()
    assert entry is not None and entry.phase == JournalPhase.STAGED_SECRET
    assert recover_pending_transaction() is False  # backup erase failed once
    assert ("deepseek-main", "backup") in env[1].items
    assert recover_pending_transaction() is True  # idempotent retry converges
    assert ("deepseek-main", "backup") not in env[1].items  # removed by recovery
    assert env[1].items[("deepseek-main", "api_key")] == "sk-new"
    assert _journal_path(env).exists() is False


# ── Finding 3: permanent-hang bound on startup recovery ─────────────────────


def _build_window(
    env: tuple[Path, _FakeSecret], monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, list[tuple[Any, tuple[Any, ...]]]]:
    from gi.repository import Adw

    from moira.ui import MainWindow

    app = Adw.Application(application_id="io.github.moira.QuotaMonitor.Test7i")
    callbacks: list[tuple[Any, tuple[Any, ...]]] = []
    monkeypatch.setattr(GLib, "idle_add", lambda cb, *a: callbacks.append((cb, a)))
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


def test_bootstrap_permanent_hang_is_bounded(
    env: tuple[Path, _FakeSecret], monkeypatch: pytest.MonkeyPatch, english: None
) -> None:
    """A permanently hung recovery child is terminated and reaped at the
    wall-time bound: the completion lands (sanitized fail-closed), all
    mutation controls stay disabled, and the test never releases the
    blocked operation."""
    from moira.ui import MainWindow

    monkeypatch.setattr("moira.ui._BOOTSTRAP_RECOVERY_TIMEOUT", 0.5)
    monkeypatch.setattr(MainWindow, "_recovery_command", lambda self: ["sleep", "1000"])
    win, callbacks = _build_window(env, monkeypatch)
    t0 = time.monotonic()
    cb, args = _wait_startup_callback(win, callbacks, timeout=6.0)
    elapsed = time.monotonic() - t0
    assert elapsed < 3.0  # bounded, not forever
    assert elapsed >= 0.3  # the timeout was actually exercised
    cb(*args)
    assert win._save_settings_button.get_sensitive() is False  # fail-closed
    assert win._integrations_page.edit_providers_button.get_sensitive() is False
    assert win.settings_status.get_text() == "Recovery required."
    # No leaked child: the hung sleeper was reaped.
    leaked = subprocess.run(
        ["pgrep", "-f", "sleep 1000"], capture_output=True, text=True, check=False
    )
    assert leaked.stdout.strip() == ""
    # No leaked worker: the bootstrap worker is free again.
    done = threading.Event()

    def probe() -> None:
        done.set()

    win._bootstrap_executor.submit(probe)
    assert done.wait(2)
    win._on_close_request()
    win.close()


def test_close_during_hang_does_not_block_and_rejects_late_completion(
    env: tuple[Path, _FakeSecret], monkeypatch: pytest.MonkeyPatch, english: None
) -> None:
    from moira.ui import MainWindow

    monkeypatch.setattr("moira.ui._BOOTSTRAP_RECOVERY_TIMEOUT", 0.6)
    monkeypatch.setattr(MainWindow, "_recovery_command", lambda self: ["sleep", "1000"])
    win, callbacks = _build_window(env, monkeypatch)
    t0 = time.monotonic()
    win._on_close_request()  # close while the child hangs
    win.close()
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0  # close never blocks on the hang
    cb, args = _wait_startup_callback(win, callbacks, timeout=6.0)
    recorded = list(callbacks)
    cb(*args)  # late completion after close → rejected by the guard
    assert callbacks == recorded
    assert win._save_settings_button.get_sensitive() is False
