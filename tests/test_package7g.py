"""Package 7g — close recoverable-transaction edge cases (ACCEPTANCE_CORRECTION).

RED tests for the five blocking findings on dee6ac7:

1. A metadata edit/toggle with blank credential performs no Keyring side
   effect, but a config failure still rolls back with ``profile.slug``
   and erases the slug's existing API key.
2. A target-secret lookup in state UNAVAILABLE is treated as
   ``had_backup=False``; a later store can overwrite an unknown
   credential without backup.
3. CONFIG_COMMITTED is written before config persistence; on persistence
   failure the secret rollback runs under a FORWARD phase, so a partial
   rollback leaves a journal that upserts the profile and deletes the
   backup without restoring the target.
4. The 7f compensation test fails the new-value store, not the restore
   (the first store is the backup).
5. Startup recovery runs synchronously in ``MainWindow.__init__``.
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
from moira.profile_journal import (
    JournalPhase,
    read_journal,
    recover_pending_transaction,
)
from moira.provider_editor import ProfileOp, ProviderEditor, _execute_op

# ── Test doubles ─────────────────────────────────────────────────────────────


class _FakeSecret:
    """Records every call as (kind, purpose, slug, value) so backup store,
    target store, restore and backup clear cannot be confused."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}
        self.fail: str | None = None
        self.fail_times: int = -1
        self.fail_on_call: dict[str, int | None] = {"lookup": None, "store": None, "clear": None}
        self.call_counters: dict[str, int] = {"lookup": 0, "store": 0, "clear": 0}
        #: (kind, purpose, slug, value) entries; the FIRST matching call
        #: fails once, then the vault recovers.
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


# ── Finding 1: blank-credential config failure must not erase a secret ──────


def test_metadata_edit_config_failure_preserves_target_credential(
    env: tuple[Path, _FakeSecret],
) -> None:
    """A metadata-only edit (blank credential) performs ZERO Keyring
    mutations; a config failure preserves the target credential exactly."""
    _seed(env, _profile("deepseek-main"))
    env[1].items[("deepseek-main", "api_key")] = "sk-old"
    op = ProfileOp("save_profile", profile=_profile("deepseek-main", label="Renamed"))
    with patch("moira.persistence.save_settings", side_effect=OSError("disk full")):
        result = _execute_op(op)
    assert result.ok is False
    assert env[1].items[("deepseek-main", "api_key")] == "sk-old"
    assert env[1].ops == []  # zero Keyring mutations
    assert _journal_path(env).exists() is False


def test_toggle_config_failure_preserves_target_credential(
    env: tuple[Path, _FakeSecret],
) -> None:
    """Same guarantee for an enabled-flag toggle."""
    _seed(env, _profile("deepseek-main"))
    env[1].items[("deepseek-main", "api_key")] = "sk-old"
    op = ProfileOp("save_profile", profile=_profile("deepseek-main", enabled=False))
    with patch("moira.persistence.save_settings", side_effect=OSError("disk full")):
        result = _execute_op(op)
    assert result.ok is False
    assert env[1].items[("deepseek-main", "api_key")] == "sk-old"
    assert env[1].ops == []


# ── Finding 2: UNAVAILABLE target lookup fails before journal or mutation ───


def test_edit_target_unavailable_fails_before_journal_or_mutation(
    env: tuple[Path, _FakeSecret],
) -> None:
    _seed(env, _profile("deepseek-main"))
    env[1].fail = "lookup"  # the target's state is UNKNOWN
    op = ProfileOp(
        "save_profile", profile=_profile("deepseek-main", label="Renamed"), credential="sk-new"
    )
    result = _execute_op(op)
    assert result.ok is False
    assert env[1].ops == [("lookup", "api_key", "deepseek-main", "")]  # read only — never a store
    assert _journal_path(env).exists() is False  # fail before any journal


def test_rename_explicit_target_unavailable_fails_closed(
    env: tuple[Path, _FakeSecret],
) -> None:
    _seed(env, _profile("old-slug"))
    env[1].items[("old-slug", "api_key")] = "sk-old"
    env[1].fail = "lookup"
    op = ProfileOp(
        "save_profile", profile=_profile("new-slug"), old_slug="old-slug", credential="sk-new"
    )
    result = _execute_op(op)
    assert result.ok is False
    assert env[1].ops == [("lookup", "api_key", "new-slug", "")]  # read only
    assert env[1].items[("old-slug", "api_key")] == "sk-old"


def test_target_lookup_matrix_covers_edit_and_both_rename_modes(
    env: tuple[Path, _FakeSecret],
) -> None:
    """FOUND → durable backup before overwrite; ABSENT → no backup;
    UNAVAILABLE/invalid → fail before journal or mutation."""

    def stores(fake: _FakeSecret) -> list[tuple[str, str, str, str]]:
        return [op for op in fake.ops if op[0] != "lookup"]

    # Edit, target FOUND → backup store precedes the target store.
    _seed(env, _profile("deepseek-main"))
    env[1].items[("deepseek-main", "api_key")] = "sk-old"
    op = ProfileOp(
        "save_profile", profile=_profile("deepseek-main", label="Renamed"), credential="sk-new"
    )
    assert _execute_op(op).ok is True
    assert stores(env[1]) == [
        ("store", "backup", "deepseek-main", "sk-old"),
        ("store", "api_key", "deepseek-main", "sk-new"),
        ("clear", "backup", "deepseek-main", ""),
    ]
    assert env[1].items[("deepseek-main", "api_key")] == "sk-new"
    assert ("deepseek-main", "backup") not in env[1].items  # dropped after commit

    # Edit, target ABSENT → no backup store.
    _seed(env, _profile("deepseek-main"))
    op = ProfileOp(
        "save_profile", profile=_profile("deepseek-main", label="Renamed"), credential="sk-new"
    )
    assert _execute_op(op).ok is True
    assert stores(env[1]) == [("store", "api_key", "deepseek-main", "sk-new")]

    # Rename with explicit credential, new target ABSENT → no backup.
    _seed(env, _profile("old-slug"))
    env[1].items[("old-slug", "api_key")] = "sk-old"
    op = ProfileOp(
        "save_profile", profile=_profile("new-slug"), old_slug="old-slug", credential="sk-new"
    )
    assert _execute_op(op).ok is True
    assert stores(env[1]) == [
        ("store", "api_key", "new-slug", "sk-new"),
        ("clear", "api_key", "old-slug", ""),
    ]

    # Blank rename migrates the old credential (old FOUND, new ABSENT).
    _seed(env, _profile("old-slug"))
    env[1].items[("old-slug", "api_key")] = "sk-old"
    op = ProfileOp("save_profile", profile=_profile("new-slug"), old_slug="old-slug")
    assert _execute_op(op).ok is True
    assert stores(env[1]) == [
        ("store", "api_key", "new-slug", "sk-old"),
        ("clear", "api_key", "old-slug", ""),
    ]

    # Blank rename with old UNAVAILABLE fails closed (read only).
    _seed(env, _profile("old-slug"))
    env[1].items[("old-slug", "api_key")] = "sk-old"
    env[1].fail = "lookup"
    op = ProfileOp("save_profile", profile=_profile("new-slug"), old_slug="old-slug")
    assert _execute_op(op).ok is False
    assert stores(env[1]) == []
    assert env[1].items[("old-slug", "api_key")] == "sk-old"


# ── Finding 3: never roll back under a forward phase ────────────────────────


def test_config_failure_transitions_to_rollback_phase_before_secrets(
    env: tuple[Path, _FakeSecret],
) -> None:
    """A known config failure durably transitions the journal to the
    rollback phase (staged-secret) BEFORE touching secrets; a partial
    rollback keeps that phase so recovery RESTORES rather than completing
    forward."""
    _seed(env, _profile("deepseek-main"))
    env[1].items[("deepseek-main", "api_key")] = "sk-old"
    op = ProfileOp(
        "save_profile", profile=_profile("deepseek-main", label="Renamed"), credential="sk-new"
    )
    env[1].fail_when = [("store", "api_key", "deepseek-main", "sk-old")]  # restore fails
    with patch("moira.persistence.save_settings", side_effect=OSError("disk full")):
        result = _execute_op(op)
    assert result.ok is False
    entry = read_journal()
    assert entry is not None and entry.phase == JournalPhase.STAGED_SECRET
    # Retry recovery (vault healthy again) converges WITHOUT loss.
    assert recover_pending_transaction() is True
    assert env[1].items[("deepseek-main", "api_key")] == "sk-old"
    assert ("deepseek-main", "backup") not in env[1].items
    assert [p.slug for p in load_settings().provider_profiles] == ["deepseek-main"]
    assert _journal_path(env).exists() is False


def test_rename_tail_failure_restores_config_then_transitions(
    env: tuple[Path, _FakeSecret],
) -> None:
    """Rename whose old-credential clear fails: config is restored FIRST,
    then the rollback phase is entered. A failed staged-copy cleanup
    keeps the rollback journal; retry converges without loss or orphan."""
    _seed(env, _profile("old-slug"))
    env[1].items[("old-slug", "api_key")] = "sk-old"
    op = ProfileOp(
        "save_profile", profile=_profile("new-slug"), old_slug="old-slug", credential="sk-new"
    )
    env[1].fail_when = [
        ("clear", "api_key", "old-slug", ""),  # old-clear fails once
        ("clear", "api_key", "new-slug", ""),  # staged-copy cleanup fails once
    ]
    result = _execute_op(op)
    assert result.ok is False
    entry = read_journal()
    assert entry is not None and entry.phase == JournalPhase.STAGED_SECRET
    assert recover_pending_transaction() is True
    assert [p.slug for p in load_settings().provider_profiles] == ["old-slug"]  # rename rolled back
    assert env[1].items[("old-slug", "api_key")] == "sk-old"  # no loss
    assert ("new-slug", "api_key") not in env[1].items  # no orphan
    assert _journal_path(env).exists() is False


def test_rename_tail_config_restore_failure_retains_forward_recovery(
    env: tuple[Path, _FakeSecret],
) -> None:
    """If the config restoration itself fails, the journal stays at
    CONFIG_COMMITTED and forward recovery completes the rename."""
    _seed(env, _profile("old-slug"))
    env[1].items[("old-slug", "api_key")] = "sk-old"
    op = ProfileOp(
        "save_profile", profile=_profile("new-slug"), old_slug="old-slug", credential="sk-new"
    )
    env[1].fail_when = [("clear", "api_key", "old-slug", "")]  # old-clear fails once
    real_save = __import__("moira.persistence", fromlist=["save_settings"]).save_settings
    calls = {"n": 0}

    def flaky_save(settings: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")  # the config RESTORE fails
        real_save(settings)

    with patch("moira.persistence.save_settings", flaky_save):
        result = _execute_op(op)
    assert result.ok is False
    assert recover_pending_transaction() is True
    assert [p.slug for p in load_settings().provider_profiles] == ["new-slug"]  # forward completion
    assert env[1].items[("new-slug", "api_key")] == "sk-new"
    assert ("old-slug", "api_key") not in env[1].items
    assert _journal_path(env).exists() is False


# ── Finding 4: the compensation test must fail the RESTORE call ─────────────


def test_compensation_restore_failure_keeps_rollback_journal_and_retries(
    env: tuple[Path, _FakeSecret],
) -> None:
    """The first store is the BACKUP, the second is the target store, the
    third (restore) must be the failing call — and retry converges."""
    _seed(env, _profile("deepseek-main"))
    env[1].items[("deepseek-main", "api_key")] = "sk-old"
    op = ProfileOp(
        "save_profile", profile=_profile("deepseek-main", label="Renamed"), credential="sk-new"
    )
    env[1].fail_on_call["store"] = 3  # backup=1, target store=2, restore=3
    with patch("moira.persistence.save_settings", side_effect=OSError("disk full")):
        result = _execute_op(op)
    assert result.ok is False
    assert recover_pending_transaction() is True
    assert env[1].items[("deepseek-main", "api_key")] == "sk-old"
    assert _journal_path(env).exists() is False


# ── Criterion 6: compound failures converge on retry ────────────────────────


def _compound_failure(
    env: tuple[Path, _FakeSecret], fail_when: list[tuple[str, str, str, str]]
) -> None:
    _seed(env, _profile("deepseek-main"))
    env[1].items[("deepseek-main", "api_key")] = "sk-old"
    op = ProfileOp(
        "save_profile", profile=_profile("deepseek-main", label="Renamed"), credential="sk-new"
    )
    env[1].fail_when = list(fail_when)
    with patch("moira.persistence.save_settings", side_effect=OSError("disk full")):
        result = _execute_op(op)
    assert result.ok is False
    entry = read_journal()
    assert entry is not None and entry.phase == JournalPhase.STAGED_SECRET  # rollback phase kept
    assert recover_pending_transaction() is True
    assert env[1].items[("deepseek-main", "api_key")] == "sk-old"
    assert ("deepseek-main", "backup") not in env[1].items
    assert _journal_path(env).exists() is False


def test_compound_config_failure_plus_target_clear_failure(
    env: tuple[Path, _FakeSecret],
) -> None:
    _compound_failure(env, [("clear", "api_key", "deepseek-main", "")])


def test_compound_config_failure_plus_backup_restore_failure(
    env: tuple[Path, _FakeSecret],
) -> None:
    _compound_failure(env, [("store", "api_key", "deepseek-main", "sk-old")])


def test_compound_config_failure_plus_backup_clear_failure(
    env: tuple[Path, _FakeSecret],
) -> None:
    _compound_failure(env, [("clear", "backup", "deepseek-main", "")])


def test_compound_journal_clear_failure_never_reports_success(
    env: tuple[Path, _FakeSecret],
) -> None:
    """A required journal remaining after a committed op: never report
    success; recovery completes the committed state and clears it."""
    _seed(env, _profile("deepseek-main"))
    op = ProfileOp(
        "save_profile", profile=_profile("deepseek-main", label="Renamed"), credential="sk-new"
    )
    import moira.provider_editor as editor_mod

    real_clear = editor_mod.clear_journal  # type: ignore[attr-defined]
    state = {"n": 0}

    def flaky_clear() -> bool:
        state["n"] += 1
        if state["n"] == 1:
            return False
        return bool(real_clear())

    with patch("moira.provider_editor.clear_journal", flaky_clear):
        result = _execute_op(op)
    assert result.ok is False  # never success while the journal remains
    assert _journal_path(env).exists()
    assert recover_pending_transaction() is True
    assert load_settings().provider_profiles[0].label == "Renamed"  # committed state kept
    assert env[1].items[("deepseek-main", "api_key")] == "sk-new"
    assert _journal_path(env).exists() is False


def test_facade_retry_after_compound_failure_converges(
    env: tuple[Path, _FakeSecret], english: None, idle_inline: None
) -> None:
    """Recreate the facade after a compound failure: the fresh editor's
    reload recovers to the documented state with no loss or orphan."""
    _seed(env, _profile("deepseek-main"))
    env[1].items[("deepseek-main", "api_key")] = "sk-old"
    op = ProfileOp(
        "save_profile", profile=_profile("deepseek-main", label="Renamed"), credential="sk-new"
    )
    env[1].fail_when = [("store", "api_key", "deepseek-main", "sk-old")]  # restore fails
    with patch("moira.persistence.save_settings", side_effect=OSError("disk full")):
        assert _execute_op(op).ok is False
    ed = ProviderEditor(submit=lambda fn, *a: fn(*a))  # facade reload runs recovery
    assert ed._recovery_blocked is False
    assert [p.slug for p in ed._profiles] == ["deepseek-main"]
    assert ed._configured.get("deepseek-main") is True
    assert env[1].items[("deepseek-main", "api_key")] == "sk-old"
    assert _journal_path(env).exists() is False


# ── Criterion 9: exact journal keys/types, phase invariants ─────────────────


def _write_raw_journal(env: tuple[Path, _FakeSecret], payload: dict[str, Any]) -> None:
    path = _journal_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


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


def test_journal_rejects_boolean_version(env: tuple[Path, _FakeSecret]) -> None:
    body = _valid_body()
    body["version"] = True  # bool is an int subclass — must be rejected
    _write_raw_journal(env, body)
    assert recover_pending_transaction() is False
    assert _journal_path(env).exists()  # kept


def test_journal_rejects_unknown_keys(env: tuple[Path, _FakeSecret]) -> None:
    body = _valid_body()
    body["garbage"] = "x"
    _write_raw_journal(env, body)
    assert recover_pending_transaction() is False
    assert _journal_path(env).exists()


def test_journal_rejects_staged_secret_without_secret_slug(
    env: tuple[Path, _FakeSecret],
) -> None:
    body = _valid_body(phase="staged-secret")
    body["secret_slug"] = ""
    _write_raw_journal(env, body)
    assert recover_pending_transaction() is False
    assert _journal_path(env).exists()


def test_journal_rejects_remove_with_profile(env: tuple[Path, _FakeSecret]) -> None:
    body = _valid_body()
    body["op"] = "remove_profile"
    body["slug"] = "deepseek-main"
    _write_raw_journal(env, body)  # remove journals must carry profile: null
    assert recover_pending_transaction() is False
    assert _journal_path(env).exists()


def test_valid_package7f_journals_still_accepted(env: tuple[Path, _FakeSecret]) -> None:
    """A valid Package 7f journal (exact keys, int version) recovers."""
    body = _valid_body()
    _write_raw_journal(env, body)
    assert recover_pending_transaction() is True
    assert _journal_path(env).exists() is False
    # A valid 7f staged-secret journal for an overwrite: backup + staged
    # store present → recovery restores the overwritten credential.
    from moira.secrets import BACKUP_PURPOSE, store_provider_secret

    assert store_provider_secret("new-main", "sk-old", BACKUP_PURPOSE) is not None
    assert store_provider_secret("new-main", "sk-new") is not None
    body = _valid_body(phase="staged-secret")
    body["had_backup"] = True
    _write_raw_journal(env, body)
    assert recover_pending_transaction() is True
    assert env[1].items[("new-main", "api_key")] == "sk-old"  # overwritten value restored
    assert ("new-main", "backup") not in env[1].items


# ── Finding 5 / criterion 10: startup recovery is off-GTK ───────────────────


def test_startup_recovery_is_async_and_gates_mutation_controls(
    env: tuple[Path, _FakeSecret], english: None
) -> None:
    """Recovery must not run synchronously in ``MainWindow.__init__``;
    mutation controls stay disabled until the recovery lands."""
    from gi.repository import Adw

    from moira.ui import MainWindow

    started = threading.Event()
    release = threading.Event()

    def slow_recovery(self: Any) -> bool:
        started.set()
        release.wait(5)
        return True

    app = Adw.Application(application_id="io.github.moira.QuotaMonitor.Test7g")
    win: MainWindow | None = None
    try:
        with patch("moira.ui.MainWindow._run_recovery_bounded", slow_recovery):
            t0 = time.monotonic()
            win = MainWindow(app, smoke_test=True)
            elapsed = time.monotonic() - t0
            assert started.wait(2)  # recovery ran — but off the constructor
            assert win._save_settings_button.get_sensitive() is False  # gated
            release.set()
        assert elapsed < 1.0  # the constructor never blocked on recovery
    finally:
        release.set()
        if win is not None:
            win._on_close_request()
            win.close()
