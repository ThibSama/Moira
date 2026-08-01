from __future__ import annotations

import gi

gi.require_version("Secret", "1")
from gi.repository import Secret  # noqa: E402

SCHEMA = Secret.Schema.new(
    "io.github.moira.QuotaMonitor",
    Secret.SchemaFlags.NONE,
    {"account": Secret.SchemaAttributeType.STRING},
)
ATTRIBUTES = {"account": "ntfy-token"}


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
