"""Package 7f — recoverable profile transactions (ACCEPTANCE_CORRECTION).

RED tests for the four blocking findings:

1. 7e compensation is best-effort: a failed restore/rollback is swallowed
   and can leave config and Keyring divergent.
2. ``get_provider_secret`` maps ABSENT and KEYRING_UNAVAILABLE both to
   None: a blank-credential rename can treat a failed lookup as absence,
   persist the rename, then clear the only old credential.
3. ``shutdown`` only suppresses UI publication: a queued ``_run_op``
   still executes ``_execute_op`` and writes after closure.
4. Provider writes and Notifications saves use separate read-modify-write
   cycles, so concurrent saves can overwrite unrelated changes.
"""

from __future__ import annotations

import json
import os
import stat
import threading
from dataclasses import replace
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
from moira.persistence import Settings, load_settings, save_settings, update_settings
from moira.provider_editor import ProfileOp, ProfileOpResult, ProviderEditor, _execute_op
from moira.secrets import (
    KeyringLookup,
    inspect_provider_secret,
)

# ── Test doubles ─────────────────────────────────────────────────────────────


class _FakeSecret:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}
        self.fail: str | None = None
        #: -1 = fail every matching call; N > 0 = fail N calls then recover.
        self.fail_times: int = -1
        #: Fail the Nth call of a given kind (1-based), then recover.
        self.fail_on_call: dict[str, int | None] = {"lookup": None, "store": None, "clear": None}
        self.call_counters: dict[str, int] = {"lookup": 0, "store": 0, "clear": 0}
        self.calls: list[str] = []

    def _should_fail(self, kind: str) -> bool:
        self.call_counters[kind] += 1
        if self.fail == kind:
            if self.fail_times == -1:
                return True
            if self.fail_times > 0:
                self.fail_times -= 1
                return True
        if self.fail_on_call[kind] == self.call_counters[kind]:
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
        if attributes.get("account") == "ntfy-token":
            self.items[("ntfy", "")] = value
            return
        self.items[(attributes["slug"], attributes["purpose"])] = value

    def password_clear_sync(
        self, _schema: Any, attributes: dict[str, str], _cancellable: Any
    ) -> None:
        self.calls.append("clear")
        if self._should_fail("clear"):
            raise RuntimeError("secret vault locked")
        if attributes.get("account") == "ntfy-token":
            self.items.pop(("ntfy", ""), None)
            return
        self.items.pop((attributes["slug"], attributes["purpose"]), None)


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


class _GatedSubmit:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, tuple[Any, ...]]] = []

    def __call__(self, fn: Any, *args: Any) -> None:
        self.calls.append((fn, args))

    def flush(self) -> None:
        while self.calls:
            fn, args = self.calls.pop(0)
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


def _journal_path(env: tuple[Path, _FakeSecret]) -> Path:
    return Path(os.environ["XDG_STATE_HOME"]) / "moira" / "profile-tx.json"


# ── Finding 1: failed compensation must not be swallowed ─────────────────────


def test_failed_compensation_restore_is_not_swallowed(env: tuple[Path, _FakeSecret]) -> None:
    """An edit whose config persist fails AND whose compensation restore
    also fails must not silently leave the NEW credential in the Keyring
    (config and Keyring divergent)."""
    _seed(env, _profile("deepseek-main"))
    env[1].items[("deepseek-main", "api_key")] = "sk-old"
    op = ProfileOp(
        "save_profile",
        profile=_profile("deepseek-main", label="Renamed"),
        credential="sk-new",
    )
    env[1].fail_on_call["store"] = 2  # first store (sk-new) ok; the restore fails
    with patch("moira.persistence.save_settings", side_effect=OSError("disk full")):
        result = _execute_op(op)
    assert result.ok is False
    # The pre-op credential must be back (never a divergent sk-new).
    assert env[1].items[("deepseek-main", "api_key")] == "sk-old"


# ── Finding 2: unavailable lookup is never absence (no credential loss) ──────


def test_blank_rename_with_unavailable_lookup_fails_closed(
    env: tuple[Path, _FakeSecret],
) -> None:
    """A blank-credential rename must fail closed when the old-secret
    lookup is unavailable — never persist the rename and clear the only
    old credential."""
    _seed(env, _profile("old-slug"))
    env[1].items[("old-slug", "api_key")] = "sk-old"
    env[1].fail = "lookup"  # lookups fail; store/clear still work
    op = ProfileOp("save_profile", profile=_profile("new-slug"), old_slug="old-slug")
    result = _execute_op(op)
    assert result.ok is False
    assert [p.slug for p in load_settings().provider_profiles] == ["old-slug"]
    assert env[1].items[("old-slug", "api_key")] == "sk-old"


def test_blank_rename_with_absent_lookup_still_migrates(
    env: tuple[Path, _FakeSecret],
) -> None:
    """ABSENT is a real state: the rename proceeds with no credential."""
    _seed(env, _profile("old-slug"))  # no credential at old-slug
    op = ProfileOp("save_profile", profile=_profile("new-slug"), old_slug="old-slug")
    result = _execute_op(op)
    assert result.ok is True
    assert [p.slug for p in load_settings().provider_profiles] == ["new-slug"]
    assert ("new-slug", "api_key") not in env[1].items


# ── Finding 3: queued-not-started ops never write after shutdown ─────────────


def test_queued_run_op_after_shutdown_does_not_write(
    env: tuple[Path, _FakeSecret], english: None, idle_inline: None
) -> None:
    _seed(env, _profile("deepseek-main"))
    gate = _GatedSubmit()
    ed = _open_editor(env, submit=gate)
    gate.flush()  # reload lands
    ed._row_widgets["deepseek-main"]["switch"].set_active(False)  # mutation queued
    assert gate.calls
    ed.shutdown()
    gate.flush()  # _run_op executes AFTER shutdown
    disk = load_settings()
    assert disk.provider_profiles[0].enabled is True  # never written
    assert ("deepseek-main", "api_key") not in env[1].items


def test_run_op_before_shutdown_writes_and_no_callback_after(
    env: tuple[Path, _FakeSecret], english: None, idle_inline: None
) -> None:
    fired: list[int] = []

    def on_changed() -> None:
        fired.append(1)

    _seed(env, _profile("deepseek-main"))
    gate = _GatedSubmit()
    ed = _open_editor(env, submit=gate, on_profiles_changed=on_changed)
    gate.flush()  # reload lands
    ed._row_widgets["deepseek-main"]["switch"].set_active(False)
    gate.flush()  # op executes BEFORE shutdown → write lands, callback fired
    assert load_settings().provider_profiles[0].enabled is False
    assert fired == [1]
    ed.shutdown()
    delayed = ProfileOpResult(
        True,
        "",
        load_settings().provider_profiles,
        (("deepseek-main", True),),
        "save_profile",
        "deepseek-main",
    )
    ed._apply_op(delayed, ed._generation)
    assert fired == [1]  # no callback after closure


# ── Finding 4: one lock/read-merge-write boundary ────────────────────────────


def test_update_settings_concurrent_mutations_both_land(
    env: tuple[Path, _FakeSecret], english: None
) -> None:
    _seed(env, _profile("a-profile"))

    def writer1() -> None:
        update_settings(lambda s: replace(s, ntfy_topic="from-notifications"))

    def writer2() -> None:
        update_settings(
            lambda s: replace(s, provider_profiles=s.provider_profiles + (_profile("b-profile"),))
        )

    t1 = threading.Thread(target=writer1)
    t2 = threading.Thread(target=writer2)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    disk = load_settings()
    assert disk.ntfy_topic == "from-notifications"
    assert [p.slug for p in disk.provider_profiles] == ["a-profile", "b-profile"]


def test_profile_op_persist_preserves_concurrent_settings(
    env: tuple[Path, _FakeSecret], english: None
) -> None:
    _seed(env, _profile("a-profile"))
    update_settings(lambda s: replace(s, ntfy_topic="keep-me"))
    op = ProfileOp("save_profile", profile=_profile("b-profile"))
    assert _execute_op(op).ok is True
    disk = load_settings()
    assert disk.ntfy_topic == "keep-me"
    assert [p.slug for p in disk.provider_profiles] == ["a-profile", "b-profile"]


# ── Typed Keyring outcomes ───────────────────────────────────────────────────


def test_inspect_distinguishes_found_absent_unavailable(
    env: tuple[Path, _FakeSecret],
) -> None:
    env[1].items[("deepseek-main", "api_key")] = "sk-abc"
    result = inspect_provider_secret("deepseek-main")
    assert result is not None and result.state is KeyringLookup.FOUND
    assert result.value == "sk-abc"
    result = inspect_provider_secret("local-llm")
    assert result is not None and result.state is KeyringLookup.ABSENT
    assert result.value is None
    env[1].fail = "lookup"
    result = inspect_provider_secret("deepseek-main")
    assert result is not None and result.state is KeyringLookup.UNAVAILABLE


def test_inspect_invalid_slug_zero_calls(env: tuple[Path, _FakeSecret]) -> None:
    env[1].calls.clear()
    for bad in ("DeepSeek!", "claude", "x y", ""):
        assert inspect_provider_secret(bad) is None, bad
    assert env[1].calls == []


def test_ntfy_api_behavior_preserved(env: tuple[Path, _FakeSecret]) -> None:
    from moira.secrets import get_ntfy_token, set_ntfy_token

    assert get_ntfy_token() is None  # absent
    set_ntfy_token("tok-1")
    assert get_ntfy_token() == "tok-1"
    env[1].fail = "lookup"
    # The NTFY getter keeps its historical contract: it propagates
    # Keyring failures (no typed outcome, no interpretation as absence).
    with pytest.raises(RuntimeError):
        get_ntfy_token()
    env[1].fail = None
    assert get_ntfy_token() == "tok-1"


# ── Journal: location, permissions, contents, strictness ────────────────────


def test_journal_under_state_home_with_0600_and_no_secrets(
    env: tuple[Path, _FakeSecret], english: None
) -> None:
    from moira.profile_journal import clear_journal, journal_path, write_journal
    from moira.provider_editor import ProfileOp

    _seed(env, _profile("deepseek-main"))
    env[1].items[("deepseek-main", "api_key")] = "sk-super-secret"
    op = ProfileOp("save_profile", profile=_profile("other-main"), credential="sk-super-secret")
    assert _execute_op(op).ok is True
    path = journal_path()
    assert not path.exists()  # journal cleared after a completed op
    # A journal entry is written during the op; verify its format through
    # the public writer:
    from moira.profile_journal import JournalEntry, JournalPhase

    write_journal(
        JournalEntry(
            1,
            "save_profile",
            JournalPhase.STAGED_SECRET,
            _profile("x-main"),
            "",
            "",
            "x-main",
            True,
        )
    )
    raw = path.read_text(encoding="utf-8")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "sk-super-secret" not in raw
    data = json.loads(raw)
    assert data["phase"] == "staged-secret"
    assert data["profile"]["slug"] == "x-main"
    assert data["had_backup"] is True
    clear_journal()
    assert not path.exists()


def test_corrupt_journal_fails_recovery_and_is_kept(
    env: tuple[Path, _FakeSecret],
) -> None:
    from moira.profile_journal import recover_pending_transaction

    _journal_path(env).parent.mkdir(parents=True, exist_ok=True)
    _journal_path(env).write_text('{"version": 99, "op": "save_profile"}', encoding="utf-8")
    assert recover_pending_transaction() is False
    assert _journal_path(env).exists()  # kept for retry


# ── Recovery matrix: crash after every side effect ──────────────────────────


def test_recovery_add_crash_at_each_phase(env: tuple[Path, _FakeSecret]) -> None:
    """Add with credential — crash after journal staged, after staged-secret
    (store done), and after config-committed (persist done). Each converges
    to one documented state, idempotently."""
    from moira.profile_journal import (
        JournalEntry,
        JournalPhase,
        recover_pending_transaction,
        write_journal,
    )

    _seed(env)
    profile = _profile("new-main")
    # Crash after phase STAGED (no side effects).
    write_journal(
        JournalEntry(1, "save_profile", JournalPhase.STAGED, profile, "", "", "new-main", False)
    )
    assert recover_pending_transaction() is True
    assert load_settings().provider_profiles == ()
    assert ("new-main", "api_key") not in env[1].items
    assert _journal_path(env).exists() is False

    # Crash after the store (phase STAGED_SECRET, secret present).
    from moira.secrets import KeyringMutation, store_provider_secret

    write_journal(
        JournalEntry(
            1, "save_profile", JournalPhase.STAGED_SECRET, profile, "", "", "new-main", False
        )
    )
    assert store_provider_secret("new-main", "sk-staged") is KeyringMutation.DONE
    assert recover_pending_transaction() is True
    assert load_settings().provider_profiles == ()  # config untouched
    assert ("new-main", "api_key") not in env[1].items  # staged secret rolled back
    assert _journal_path(env).exists() is False

    # Crash after the persist (phase CONFIG_COMMITTED, profile on disk).
    write_journal(
        JournalEntry(
            1, "save_profile", JournalPhase.CONFIG_COMMITTED, profile, "", "", "new-main", False
        )
    )
    update_settings(lambda s: replace(s, provider_profiles=(profile,)))
    assert recover_pending_transaction() is True
    assert [p.slug for p in load_settings().provider_profiles] == ["new-main"]  # completed forward
    assert _journal_path(env).exists() is False
    # Idempotence: a repeated crash at the same phase converges again.
    assert recover_pending_transaction() is True


def test_recovery_edit_overwrite_restores_backup(env: tuple[Path, _FakeSecret]) -> None:
    """Edit with a new credential — crash after the staged store: the
    overwritten credential is restored from the Moira-owned backup entry."""
    from moira.profile_journal import (
        JournalEntry,
        JournalPhase,
        recover_pending_transaction,
        write_journal,
    )
    from moira.secrets import BACKUP_PURPOSE, KeyringMutation, store_provider_secret

    _seed(env, _profile("deepseek-main"))
    env[1].items[("deepseek-main", "api_key")] = "sk-old"
    profile = _profile("deepseek-main", label="Renamed")
    # Op steps up to the staged store: backup made, new value stored.
    assert store_provider_secret("deepseek-main", "sk-old", BACKUP_PURPOSE) is KeyringMutation.DONE
    assert store_provider_secret("deepseek-main", "sk-new") is KeyringMutation.DONE
    write_journal(
        JournalEntry(
            1, "save_profile", JournalPhase.STAGED_SECRET, profile, "", "", "deepseek-main", True
        )
    )
    assert recover_pending_transaction() is True
    assert env[1].items[("deepseek-main", "api_key")] == "sk-old"  # restored
    assert ("deepseek-main", BACKUP_PURPOSE) not in env[1].items  # backup cleared
    assert load_settings().provider_profiles[0].label == "DeepSeek main"  # config untouched


def test_recovery_rename_config_committed_clears_old_keeps_new(
    env: tuple[Path, _FakeSecret],
) -> None:
    """Rename — crash after persist (phase CONFIG_COMMITTED): recovery
    completes forward: new profile kept, old credential cleared, migrated
    credential preserved at the new slug."""
    from moira.profile_journal import (
        JournalEntry,
        JournalPhase,
        recover_pending_transaction,
        write_journal,
    )
    from moira.secrets import KeyringMutation, store_provider_secret

    _seed(env, _profile("old-slug"))
    env[1].items[("old-slug", "api_key")] = "sk-old"
    profile = _profile("new-slug")
    # Op steps: migration copy stored, config persisted, crash before old-clear.
    assert store_provider_secret("new-slug", "sk-old") is KeyringMutation.DONE
    update_settings(lambda s: replace(s, provider_profiles=(profile,)))
    write_journal(
        JournalEntry(
            1,
            "save_profile",
            JournalPhase.CONFIG_COMMITTED,
            profile,
            "old-slug",
            "",
            "new-slug",
            False,
        )
    )
    assert recover_pending_transaction() is True
    assert [p.slug for p in load_settings().provider_profiles] == ["new-slug"]
    assert env[1].items[("new-slug", "api_key")] == "sk-old"  # migrated credential kept
    assert ("old-slug", "api_key") not in env[1].items  # old removal completed
    assert _journal_path(env).exists() is False


def test_recovery_rename_staged_no_copy_never_clears_last_credential(
    env: tuple[Path, _FakeSecret],
) -> None:
    """Rename — crash before the migration copy (phase STAGED): the old
    credential is the last recoverable one and must never be cleared."""
    from moira.profile_journal import (
        JournalEntry,
        JournalPhase,
        recover_pending_transaction,
        write_journal,
    )

    _seed(env, _profile("old-slug"))
    env[1].items[("old-slug", "api_key")] = "sk-old"
    write_journal(
        JournalEntry(
            1,
            "save_profile",
            JournalPhase.STAGED,
            _profile("new-slug"),
            "old-slug",
            "",
            "new-slug",
            False,
        )
    )
    assert recover_pending_transaction() is True
    assert env[1].items[("old-slug", "api_key")] == "sk-old"  # never cleared
    assert [p.slug for p in load_settings().provider_profiles] == ["old-slug"]  # config untouched
    assert _journal_path(env).exists() is False


def test_recovery_remove_crash_at_each_phase(env: tuple[Path, _FakeSecret]) -> None:
    """Remove — crash after STAGED and after CONFIG_COMMITTED."""
    from moira.profile_journal import (
        JournalEntry,
        JournalPhase,
        recover_pending_transaction,
        write_journal,
    )

    _seed(env, _profile("deepseek-main"))
    env[1].items[("deepseek-main", "api_key")] = "sk-abc"
    # Crash after STAGED: nothing happened → no-op recovery.
    write_journal(
        JournalEntry(1, "remove_profile", JournalPhase.STAGED, None, "", "deepseek-main", "", False)
    )
    assert recover_pending_transaction() is True
    assert [p.slug for p in load_settings().provider_profiles] == ["deepseek-main"]
    assert env[1].items[("deepseek-main", "api_key")] == "sk-abc"
    # Crash after CONFIG_COMMITTED: recovery completes the removal.
    write_journal(
        JournalEntry(
            1, "remove_profile", JournalPhase.CONFIG_COMMITTED, None, "", "deepseek-main", "", False
        )
    )
    update_settings(lambda s: replace(s, provider_profiles=()))
    assert recover_pending_transaction() is True
    assert load_settings().provider_profiles == ()
    assert ("deepseek-main", "api_key") not in env[1].items
    assert _journal_path(env).exists() is False


def test_recovery_idempotent_across_repeated_crashes(env: tuple[Path, _FakeSecret]) -> None:
    """Recovery is idempotent: replaying it after every phase converges to
    the same documented state without side effects."""
    from moira.profile_journal import (
        JournalEntry,
        JournalPhase,
        recover_pending_transaction,
        write_journal,
    )
    from moira.secrets import store_provider_secret

    _seed(env, _profile("old-slug"))
    env[1].items[("old-slug", "api_key")] = "sk-old"
    profile = _profile("new-slug")
    store_provider_secret("new-slug", "sk-old")
    entry = JournalEntry(
        1, "save_profile", JournalPhase.CONFIG_COMMITTED, profile, "old-slug", "", "new-slug", False
    )
    for _ in range(3):  # three "crashes" during recovery
        write_journal(entry)
        assert recover_pending_transaction() is True
    assert [p.slug for p in load_settings().provider_profiles] == ["new-slug"]
    assert env[1].items[("new-slug", "api_key")] == "sk-old"
    assert ("old-slug", "api_key") not in env[1].items


# ── Facade recovery (criterion 8): a fresh editor converges ─────────────────


def test_facade_recovers_after_crash_mid_rename(
    env: tuple[Path, _FakeSecret], english: None, idle_inline: None
) -> None:
    from moira.profile_journal import (
        JournalEntry,
        JournalPhase,
        write_journal,
    )
    from moira.secrets import store_provider_secret

    _seed(env, _profile("old-slug"))
    env[1].items[("old-slug", "api_key")] = "sk-old"
    profile = _profile("new-slug")
    store_provider_secret("new-slug", "sk-old")
    update_settings(lambda s: replace(s, provider_profiles=(profile,)))
    write_journal(
        JournalEntry(
            1,
            "save_profile",
            JournalPhase.CONFIG_COMMITTED,
            profile,
            "old-slug",
            "",
            "new-slug",
            False,
        )
    )
    ed = _open_editor(env)  # facade reload runs recovery first
    assert [p.slug for p in ed._profiles] == ["new-slug"]
    assert ed._configured.get("new-slug") is True
    assert ("old-slug", "api_key") not in env[1].items
    assert _journal_path(env).exists() is False


def test_facade_blocked_on_failed_recovery(
    env: tuple[Path, _FakeSecret], english: None, idle_inline: None
) -> None:
    """Failed recovery: translated status, journal kept, mutations refused."""
    _journal_path(env).parent.mkdir(parents=True, exist_ok=True)
    _journal_path(env).write_text('{"version": 7, "op": "save_profile"}', encoding="utf-8")
    ed = _open_editor(env)
    assert ed.status_label.get_text() == "Recovery required."
    assert ed._recovery_blocked is True
    ed._show_form(None)
    ed.slug_entry.set_text("x-main")
    ed.label_entry.set_text("X")
    ed.form_save_button.emit("clicked")
    assert load_settings().provider_profiles == ()  # mutation refused
    assert _journal_path(env).exists()  # kept for retry


def test_facade_retries_recovery_on_reload(
    env: tuple[Path, _FakeSecret], english: None, idle_inline: None
) -> None:
    """After the journal issue is resolved, a fresh editor recovers."""
    from moira.profile_journal import (
        JournalEntry,
        JournalPhase,
        write_journal,
    )
    from moira.secrets import store_provider_secret

    _seed(env, _profile("old-slug"))
    env[1].items[("old-slug", "api_key")] = "sk-old"
    profile = _profile("new-slug")
    store_provider_secret("new-slug", "sk-old")
    update_settings(lambda s: replace(s, provider_profiles=(profile,)))
    write_journal(
        JournalEntry(
            1,
            "save_profile",
            JournalPhase.CONFIG_COMMITTED,
            profile,
            "old-slug",
            "",
            "new-slug",
            False,
        )
    )
    # The first facade recovers.
    ed = _open_editor(env)
    assert ed._recovery_blocked is False
    assert [p.slug for p in ed._profiles] == ["new-slug"]


# ── No credential loss on the op paths ───────────────────────────────────────


def test_op_failure_after_commit_keeps_journal_for_recovery(
    env: tuple[Path, _FakeSecret],
) -> None:
    """Rename whose old-clear fails AND whose config rollback also fails:
    the journal is KEPT at config-committed so forward recovery converges
    without losing the migrated credential."""
    _seed(env, _profile("old-slug"))
    env[1].items[("old-slug", "api_key")] = "sk-old"
    env[1].fail = "clear"
    env[1].fail_times = 1  # old-clear fails once
    op = ProfileOp("save_profile", profile=_profile("new-slug"), old_slug="old-slug")
    real_save = __import__("moira.persistence", fromlist=["save_settings"]).save_settings
    calls = {"n": 0}

    def flaky_save(settings: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")  # the rollback persist fails too
        real_save(settings)

    with patch("moira.persistence.save_settings", flaky_save):
        result = _execute_op(op)
    assert result.ok is False
    assert _journal_path(env).exists()  # journal kept → recovery converges later
    from moira.profile_journal import recover_pending_transaction

    assert recover_pending_transaction() is True
    disk = load_settings()
    assert [p.slug for p in disk.provider_profiles] == ["new-slug"]
    assert env[1].items[("new-slug", "api_key")] == "sk-old"  # migrated credential kept
    assert ("old-slug", "api_key") not in env[1].items
