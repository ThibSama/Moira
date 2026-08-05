"""Keyring-backed secrets: the NTFY token and provider credentials.

The libsecret schema carries three attribute families under one schema
name: the legacy NTFY item (``account=ntfy-token``, API unchanged) and
provider credentials keyed by validated slug + purpose (``kind=provider``,
``slug=...``, ``purpose=api_key`` or ``purpose=backup``). Provider
operations are typed: lookups distinguish FOUND / ABSENT / UNAVAILABLE
(never interpreting an unavailable vault as absence), and mutations
distinguish DONE / UNAVAILABLE / REJECTED. A Keyring failure (unavailable
or locked vault, DBus errors) never raises; raw exception text never
reaches callers. Blank credential input preserves the existing secret;
only an explicit ``erase_provider_secret`` removes it. ``backup`` entries
are Moira-owned transient copies used by the recoverable transaction
protocol (never shown by the UI, never persisted outside the Keyring).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import gi

gi.require_version("Secret", "1")
from gi.repository import Secret  # noqa: E402

from .integrations import is_valid_profile_slug

SCHEMA = Secret.Schema.new(
    "io.github.moira.QuotaMonitor",
    Secret.SchemaFlags.NONE,
    {
        "account": Secret.SchemaAttributeType.STRING,
        "kind": Secret.SchemaAttributeType.STRING,
        "slug": Secret.SchemaAttributeType.STRING,
        "purpose": Secret.SchemaAttributeType.STRING,
    },
)
ATTRIBUTES = {"account": "ntfy-token"}

#: Provider-credential attribute family (isolated from the NTFY item).
_PROVIDER_KIND_ATTRIBUTE = "provider"
#: Credential purpose values. ``backup`` holds transient Moira-owned
#: copies of overwritten credentials for the recoverable transaction
#: protocol; it is never rendered by the UI.
_PROVIDER_PURPOSES = ("api_key", "backup")
BACKUP_PURPOSE = "backup"
#: Bounded credential value (never persisted anywhere but the Keyring).
_MAX_SECRET_LENGTH = 4096


class KeyringLookup(StrEnum):
    """Typed outcome of a provider-secret lookup."""

    FOUND = "found"
    ABSENT = "absent"
    UNAVAILABLE = "unavailable"


class KeyringMutation(StrEnum):
    """Typed outcome of a provider-secret mutation."""

    DONE = "done"
    UNAVAILABLE = "unavailable"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class ProviderSecret:
    """One typed lookup result; ``value`` is set only when FOUND."""

    state: KeyringLookup
    value: str | None = None


def get_ntfy_token() -> str | None:
    value = Secret.password_lookup_sync(SCHEMA, ATTRIBUTES, None)
    return str(value) if value else None


def set_ntfy_token(token: str) -> None:
    if not token:
        clear_ntfy_token()
        return
    Secret.password_store_sync(
        SCHEMA,
        ATTRIBUTES,
        Secret.COLLECTION_DEFAULT,
        "Moira NTFY token",
        token,
        None,
    )


def clear_ntfy_token() -> None:
    Secret.password_clear_sync(SCHEMA, ATTRIBUTES, None)


def _provider_attributes(slug: str, purpose: str) -> dict[str, str] | None:
    # The strict profile slug contract is shared with ProviderProfile:
    # invalid or reserved slugs perform ZERO libsecret calls.
    if not is_valid_profile_slug(slug) or purpose not in _PROVIDER_PURPOSES:
        return None
    return {"kind": _PROVIDER_KIND_ATTRIBUTE, "slug": slug, "purpose": purpose}


def inspect_provider_secret(slug: str, purpose: str = "api_key") -> ProviderSecret | None:
    """Typed lookup: FOUND(value), ABSENT, UNAVAILABLE.

    Returns None only for an invalid/reserved slug or purpose (zero
    libsecret calls). UNAVAILABLE is a real state — callers must never
    interpret it as absence (a blank-credential rename fails closed).
    """
    attributes = _provider_attributes(slug, purpose)
    if attributes is None:
        return None
    try:
        value = Secret.password_lookup_sync(SCHEMA, attributes, None)
    except Exception:
        return ProviderSecret(KeyringLookup.UNAVAILABLE)
    if value:
        return ProviderSecret(KeyringLookup.FOUND, str(value))
    return ProviderSecret(KeyringLookup.ABSENT)


def store_provider_secret(slug: str, token: str, purpose: str = "api_key") -> KeyringMutation:
    """Store one provider credential: DONE on success.

    Blank input never touches the Keyring and returns REJECTED: the
    existing secret is preserved. Oversized credentials and invalid
    slugs/purposes are REJECTED; a Keyring failure returns UNAVAILABLE
    without raising.
    """
    attributes = _provider_attributes(slug, purpose)
    if attributes is None:
        return KeyringMutation.REJECTED
    if not isinstance(token, str) or not token.strip():
        return KeyringMutation.REJECTED  # blank input preserves the existing secret
    if len(token) > _MAX_SECRET_LENGTH:
        return KeyringMutation.REJECTED
    try:
        Secret.password_store_sync(
            SCHEMA,
            attributes,
            Secret.COLLECTION_DEFAULT,
            f"Moira API key for {slug}",
            token,
            None,
        )
        return KeyringMutation.DONE
    except Exception:
        return KeyringMutation.UNAVAILABLE


def erase_provider_secret(slug: str, purpose: str = "api_key") -> KeyringMutation:
    """Explicitly remove the stored credential: DONE on success.

    An absent credential is a successful no-op (DONE). Only this explicit
    call clears a provider credential — blank inputs to
    ``store_provider_secret`` never do. A Keyring failure returns
    UNAVAILABLE without raising.
    """
    attributes = _provider_attributes(slug, purpose)
    if attributes is None:
        return KeyringMutation.REJECTED
    try:
        Secret.password_clear_sync(SCHEMA, attributes, None)
        return KeyringMutation.DONE
    except Exception:
        return KeyringMutation.UNAVAILABLE


# ── Legacy boolean/None wrappers (unchanged public semantics) ────────────────


def get_provider_secret(slug: str, purpose: str = "api_key") -> str | None:
    """Legacy safe lookup: the value, or None for absent AND unavailable."""
    result = inspect_provider_secret(slug, purpose)
    if result is None or result.state is not KeyringLookup.FOUND:
        return None
    return result.value


def has_provider_secret(slug: str, purpose: str = "api_key") -> bool:
    """Legacy safe check: True only when a credential is stored."""
    result = inspect_provider_secret(slug, purpose)
    return result is not None and result.state is KeyringLookup.FOUND


def set_provider_secret(slug: str, token: str, purpose: str = "api_key") -> bool:
    """Legacy wrapper: True when the store succeeded (DONE)."""
    return store_provider_secret(slug, token, purpose) is KeyringMutation.DONE


def clear_provider_secret(slug: str, purpose: str = "api_key") -> bool:
    """Legacy wrapper: True when the erase succeeded (DONE)."""
    return erase_provider_secret(slug, purpose) is KeyringMutation.DONE
