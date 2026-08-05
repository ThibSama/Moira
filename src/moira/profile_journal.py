"""Package 7f — durable phase journal for recoverable profile transactions.

Profile/credential writes span two stores (the config file and the
Keyring), so a crash between them can leave them divergent. Every
mutation is therefore recorded in a minimal atomic ``0600`` journal
under ``$XDG_STATE_HOME/moira`` BEFORE its first side effect, with
explicit phases:

* ``staged`` — intent recorded; no side effect has happened yet.
* ``staged-secret`` — a credential store/migration at ``secret_slug``
  is intended (a Moira-owned ``backup`` Keyring entry may hold the
  overwritten value).
* ``config-committed`` — the config persist is intended.

Recovery (``recover_pending_transaction``) converges to ONE documented
consistent state, idempotently, from any crash point:

* ``staged`` → nothing happened: clear the journal (no-op rollback).
* ``staged-secret`` → roll the secret effect back (clear the staged
  value; restore the backup if one was made) — config untouched.
* ``config-committed`` → complete the operation FORWARD: upsert the
  journaled profile under the config lock, clear the old slug's
  credential on rename, clear the obsolete backup.

The journal NEVER contains credentials, fingerprints, raw exceptions or
provider responses — only the operation kind, validated slugs, the
phase, the strict ``had_backup`` flag and sanitized profile metadata. A
corrupt or unsupported journal fails recovery closed: it is kept and
retried on the next reload.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from .integrations import ProviderKind, ProviderProfile, is_valid_profile_slug
from .persistence import Settings, state_dir, update_settings
from .secrets import (
    BACKUP_PURPOSE,
    KeyringMutation,
    erase_provider_secret,
    inspect_provider_secret,
    store_provider_secret,
)

JOURNAL_VERSION = 1
JOURNAL_FILENAME = "profile-tx.json"


class JournalPhase(StrEnum):
    """Durable phases of one profile transaction."""

    STAGED = "staged"
    STAGED_SECRET = "staged-secret"
    CONFIG_COMMITTED = "config-committed"


_VALID_PHASES = tuple(JournalPhase)


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """One durable journal record (never carries secrets)."""

    version: int
    op: str  # save_profile | remove_profile
    phase: str
    profile: ProviderProfile | None = None
    old_slug: str = ""
    slug: str = ""  # remove_profile target
    secret_slug: str = ""
    had_backup: bool = False


def journal_path() -> Path:
    return state_dir() / JOURNAL_FILENAME


def _serialize_profile(profile: ProviderProfile) -> dict[str, Any]:
    return {
        "slug": profile.slug,
        "label": profile.label,
        "kind": profile.kind.value,
        "model": profile.model,
        "enabled": profile.enabled,
        "base_url": profile.base_url,
        "hermes_label": profile.hermes_label,
    }


def _serialize(entry: JournalEntry) -> dict[str, Any]:
    return {
        "version": entry.version,
        "op": entry.op,
        "phase": entry.phase,
        "profile": _serialize_profile(entry.profile) if entry.profile is not None else None,
        "old_slug": entry.old_slug,
        "slug": entry.slug,
        "secret_slug": entry.secret_slug,
        "had_backup": entry.had_backup,
    }


def write_journal(entry: JournalEntry) -> None:
    """Atomically persist the journal with 0600 permissions."""
    path = journal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(_serialize(entry), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def clear_journal() -> None:
    """Remove the journal (the transaction is converged)."""
    try:
        journal_path().unlink(missing_ok=True)
    except OSError:
        pass


def read_journal() -> JournalEntry | None:
    """Read and strictly validate the journal; None when absent.

    Raises ValueError for a corrupt or unsupported journal (fail closed:
    recovery must not guess).
    """
    path = journal_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("journal is not readable JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("journal must be an object")
    if data.get("version") != JOURNAL_VERSION:
        raise ValueError("unsupported journal version")
    op = data.get("op")
    phase = data.get("phase")
    if op not in ("save_profile", "remove_profile") or phase not in _VALID_PHASES:
        raise ValueError("invalid journal op or phase")
    assert op is not None and phase is not None
    if not isinstance(data.get("had_backup"), bool):
        raise ValueError("invalid journal had_backup")
    old_slug = data.get("old_slug") or ""
    slug = data.get("slug") or ""
    secret_slug = data.get("secret_slug") or ""
    for name, value in (("old_slug", old_slug), ("slug", slug), ("secret_slug", secret_slug)):
        if value and not is_valid_profile_slug(value):
            raise ValueError(f"invalid journal {name}")
    if op == "remove_profile":
        if not is_valid_profile_slug(slug):
            raise ValueError("invalid journal slug")
        return JournalEntry(JOURNAL_VERSION, op, phase, None, "", slug, "", data["had_backup"])
    raw = data.get("profile")
    if not isinstance(raw, dict):
        raise ValueError("invalid journal profile")
    try:
        profile = ProviderProfile(
            slug=raw["slug"],
            label=raw["label"],
            kind=ProviderKind(raw["kind"]),
            model=raw["model"],
            enabled=raw["enabled"],
            base_url=raw["base_url"],
            hermes_label=raw["hermes_label"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid journal profile") from exc
    return JournalEntry(
        JOURNAL_VERSION, op, phase, profile, old_slug, "", secret_slug, data["had_backup"]
    )


# ── Recovery helpers (shared with the op failure paths) ──────────────────────


def _rollback_staged_secret(entry: JournalEntry) -> bool:
    """Roll the secret side-effect of a ``staged-secret`` transaction back.

    Lossless: when a backup exists, the overwritten credential is
    restored; otherwise the staged value (if any) is cleared. Config was
    never persisted at this phase and is left untouched.
    """
    target = entry.secret_slug
    if not target:
        return True
    if entry.had_backup:
        backup = inspect_provider_secret(target, BACKUP_PURPOSE)
        if backup is None or backup.state.value == "unavailable":
            return False
        if backup.state.value == "found" and backup.value:
            if erase_provider_secret(target) is not KeyringMutation.DONE:
                return False
            if store_provider_secret(target, backup.value) is not KeyringMutation.DONE:
                return False
            # Restored; drop the backup copy.
            return erase_provider_secret(target, BACKUP_PURPOSE) is KeyringMutation.DONE
        # Backup already gone: the target was restored earlier (crash
        # between backup-clear and journal-clear) — nothing to do.
        return True
    return erase_provider_secret(target) is KeyringMutation.DONE


def _upsert_transform(profile: ProviderProfile, old_slug: str) -> Any:
    def transform(current: Settings) -> Settings:
        base = [
            p for p in current.provider_profiles if p.slug != profile.slug and p.slug != old_slug
        ]
        return replace(
            current,
            provider_profiles=tuple(sorted(base + [profile], key=lambda p: p.slug)),
        )

    return transform


def _complete_forward(entry: JournalEntry) -> bool:
    """Complete a ``config-committed`` transaction forward, idempotently."""
    assert entry.profile is not None
    try:
        update_settings(_upsert_transform(entry.profile, entry.old_slug))
    except Exception:
        return False
    if entry.old_slug and entry.old_slug != entry.profile.slug:
        # Old removal tail: the replacement (staged before the commit) is
        # durable, so clearing the old slug's credential loses nothing.
        if erase_provider_secret(entry.old_slug) is not KeyringMutation.DONE:
            return False
    if entry.had_backup:
        if erase_provider_secret(entry.profile.slug, BACKUP_PURPOSE) is not KeyringMutation.DONE:
            return False
    return True


def _complete_remove(entry: JournalEntry) -> bool:
    """Complete a ``config-committed`` removal forward, idempotently."""
    try:
        update_settings(
            lambda current: replace(
                current,
                provider_profiles=tuple(
                    p for p in current.provider_profiles if p.slug != entry.slug
                ),
            )
        )
    except Exception:
        return False
    return erase_provider_secret(entry.slug) is KeyringMutation.DONE


def _recover_save(entry: JournalEntry) -> bool:
    phase = entry.phase
    if phase == JournalPhase.STAGED:
        # No side effect happened: purge any early backup intent and drop
        # the journal (the op never started).
        if entry.had_backup and entry.secret_slug:
            if erase_provider_secret(entry.secret_slug, BACKUP_PURPOSE) is not KeyringMutation.DONE:
                return False
        clear_journal()
        return True
    if phase == JournalPhase.STAGED_SECRET:
        if not _rollback_staged_secret(entry):
            return False
        clear_journal()
        return True
    # CONFIG_COMMITTED: complete forward.
    if not _complete_forward(entry):
        return False
    clear_journal()
    return True


def _recover_remove(entry: JournalEntry) -> bool:
    if entry.phase == JournalPhase.STAGED:
        clear_journal()
        return True
    if not _complete_remove(entry):
        return False
    clear_journal()
    return True


def recover_pending_transaction() -> bool:
    """Recover any journaled operation; True when converged.

    On failure the journal is KEPT and the caller must surface the
    translated ``Recovery required.`` outcome and retry on the next
    reload. Idempotent: replaying recovery from any phase converges to
    the same documented state.
    """
    try:
        entry = read_journal()
    except ValueError:
        return False  # corrupt journal: kept for retry
    if entry is None:
        return True
    if entry.op == "save_profile":
        return _recover_save(entry)
    if entry.op == "remove_profile":
        return _recover_remove(entry)
    return False
