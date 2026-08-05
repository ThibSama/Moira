"""Keyring-backed secrets: the NTFY token and provider credentials.

The libsecret schema carries three attribute families under one schema
name: the legacy NTFY item (``account=ntfy-token``, API unchanged) and
provider credentials keyed by validated slug + purpose (``kind=provider``,
``slug=...``, ``purpose=api_key``). Provider operations are typed with
safe outcomes: a Keyring failure (unavailable or locked vault, DBus
errors) never raises — lookups return None, mutations return False, and
raw exception text never reaches callers. Blank credential input
preserves the existing secret; only an explicit ``clear_provider_secret``
removes it.
"""

from __future__ import annotations

import gi

gi.require_version("Secret", "1")
from gi.repository import Secret  # noqa: E402

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
_PROVIDER_PURPOSES = ("api_key",)
#: Bounded slug used as a Keyring attribute value.
_MAX_ATTRIBUTE_SLUG_LENGTH = 64
#: Bounded credential value (never persisted anywhere but the Keyring).
_MAX_SECRET_LENGTH = 4096


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


def _valid_slug(slug: str) -> bool:
    """Bounded, control-character-free slug for Keyring attributes.

    The strict profile slug rule is enforced at profile creation; this
    layer only guarantees bounded, printable attribute values.
    """
    return (
        isinstance(slug, str)
        and 0 < len(slug) <= _MAX_ATTRIBUTE_SLUG_LENGTH
        and not any(ord(ch) < 33 or ord(ch) == 127 for ch in slug)
    )


def _provider_attributes(slug: str, purpose: str) -> dict[str, str] | None:
    if not _valid_slug(slug) or purpose not in _PROVIDER_PURPOSES:
        return None
    return {"kind": _PROVIDER_KIND_ATTRIBUTE, "slug": slug, "purpose": purpose}


def get_provider_secret(slug: str, purpose: str = "api_key") -> str | None:
    """Return the stored provider credential, or None (safe outcome).

    A Keyring failure and an invalid slug/purpose both map to None; the
    raw libsecret exception never reaches the caller.
    """
    attributes = _provider_attributes(slug, purpose)
    if attributes is None:
        return None
    try:
        value = Secret.password_lookup_sync(SCHEMA, attributes, None)
    except Exception:
        return None
    return str(value) if value else None


def set_provider_secret(slug: str, token: str, purpose: str = "api_key") -> bool:
    """Store one provider credential; True on success.

    Blank input never touches the Keyring and returns False: the
    existing secret is preserved. Oversized credentials are rejected.
    A Keyring failure returns False without raising.
    """
    attributes = _provider_attributes(slug, purpose)
    if attributes is None:
        return False
    if not isinstance(token, str) or not token.strip():
        return False  # blank input preserves the existing secret
    if len(token) > _MAX_SECRET_LENGTH:
        return False
    try:
        Secret.password_store_sync(
            SCHEMA,
            attributes,
            Secret.COLLECTION_DEFAULT,
            f"Moira API key for {slug}",
            token,
            None,
        )
        return True
    except Exception:
        return False


def has_provider_secret(slug: str, purpose: str = "api_key") -> bool:
    """True when a credential is stored for the slug+purpose (safe)."""
    attributes = _provider_attributes(slug, purpose)
    if attributes is None:
        return False
    try:
        value = Secret.password_lookup_sync(SCHEMA, attributes, None)
    except Exception:
        return False
    return bool(value)


def clear_provider_secret(slug: str, purpose: str = "api_key") -> bool:
    """Explicitly remove the stored credential; True on success.

    Only this explicit call clears a provider credential — blank inputs
    to ``set_provider_secret`` never do. A Keyring failure returns False
    without raising.
    """
    attributes = _provider_attributes(slug, purpose)
    if attributes is None:
        return False
    try:
        Secret.password_clear_sync(SCHEMA, attributes, None)
        return True
    except Exception:
        return False
