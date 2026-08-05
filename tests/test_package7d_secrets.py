"""Package 7d — generalized Keyring credentials (mocked libsecret).

The libsecret binding is mocked at the ``gi.repository.Secret`` level so
provider isolation, preserve-on-blank, explicit clear, unavailable
Keyring and leakage are tested deterministically, without touching a real
Keyring. The NTFY API is asserted unchanged.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import gi  # type: ignore[import-untyped]

gi.require_version("Secret", "1")
import pytest
from gi.repository import Secret  # type: ignore[import-untyped]  # noqa: E402

from moira.secrets import (
    clear_ntfy_token,
    clear_provider_secret,
    get_ntfy_token,
    get_provider_secret,
    has_provider_secret,
    set_ntfy_token,
    set_provider_secret,
)


class _FakeSecret:
    """In-memory libsecret stand-in recording every call."""

    def __init__(self) -> None:
        self.items: list[tuple[dict[str, str], str]] = []
        self.lookups: list[dict[str, str]] = []
        self.stores: list[tuple[dict[str, str], str]] = []
        self.clears: list[dict[str, str]] = []
        self.fail: str | None = None  # "lookup" | "store" | "clear"

    def password_lookup_sync(
        self, _schema: Any, attributes: dict[str, str], _cancellable: Any
    ) -> str | None:
        self.lookups.append(dict(attributes))
        if self.fail == "lookup":
            raise RuntimeError("secret vault locked")
        for attrs, value in self.items:
            if all(attributes.get(key) == value for key, value in attrs.items()):
                return value
        return None

    def password_store_sync(
        self,
        _schema: Any,
        attributes: dict[str, str],
        _collection: Any,
        label: str,
        value: str,
        _cancellable: Any,
    ) -> None:
        self.stores.append((dict(attributes), label))
        if self.fail == "store":
            raise RuntimeError("secret vault locked")
        self.items[:] = [(attrs, v) for attrs, v in self.items if not _match(attrs, attributes)]
        self.items.append((dict(attributes), value))

    def password_clear_sync(
        self, _schema: Any, attributes: dict[str, str], _cancellable: Any
    ) -> None:
        self.clears.append(dict(attributes))
        if self.fail == "clear":
            raise RuntimeError("secret vault locked")
        self.items[:] = [(attrs, v) for attrs, v in self.items if not _match(attrs, attributes)]


def _match(a: dict[str, str], b: dict[str, str]) -> bool:
    return all(a.get(key) == value for key, value in b.items())


@pytest.fixture
def vault(monkeypatch: pytest.MonkeyPatch) -> _FakeSecret:
    fake = _FakeSecret()
    monkeypatch.setattr(Secret, "password_lookup_sync", fake.password_lookup_sync)
    monkeypatch.setattr(Secret, "password_store_sync", fake.password_store_sync)
    monkeypatch.setattr(Secret, "password_clear_sync", fake.password_clear_sync)
    return fake


def test_ntfy_api_preserved(vault: _FakeSecret) -> None:
    """The NTFY API keeps its exact behavior and its own attributes."""
    set_ntfy_token("n-1234")
    assert vault.stores == [({"account": "ntfy-token"}, "Moira NTFY token")]
    assert get_ntfy_token() == "n-1234"
    assert vault.lookups == [({"account": "ntfy-token"})]
    set_ntfy_token("")  # legacy behavior: blank clears the NTFY token
    assert vault.clears == [({"account": "ntfy-token"})]
    assert get_ntfy_token() is None
    clear_ntfy_token()
    assert vault.clears == [({"account": "ntfy-token"}), ({"account": "ntfy-token"})]


def test_provider_credential_round_trip(vault: _FakeSecret) -> None:
    assert has_provider_secret("deepseek-main") is False
    assert set_provider_secret("deepseek-main", "sk-abc") is True
    assert has_provider_secret("deepseek-main") is True
    assert get_provider_secret("deepseek-main") == "sk-abc"
    assert clear_provider_secret("deepseek-main") is True
    assert get_provider_secret("deepseek-main") is None


def test_provider_isolation_between_slugs(vault: _FakeSecret) -> None:
    set_provider_secret("a-provider", "secret-a")
    set_provider_secret("b-provider", "secret-b")
    assert get_provider_secret("a-provider") == "secret-a"
    assert get_provider_secret("b-provider") == "secret-b"
    # Attributes must key by slug: clearing one never touches the other.
    clear_provider_secret("a-provider")
    assert get_provider_secret("a-provider") is None
    assert get_provider_secret("b-provider") == "secret-b"
    for attributes, _label in vault.stores:
        assert attributes["kind"] == "provider"
        assert attributes["purpose"] == "api_key"
        assert attributes["slug"] in ("a-provider", "b-provider")


def test_preserve_on_blank_never_touches_keyring(vault: _FakeSecret) -> None:
    assert set_provider_secret("deepseek-main", "sk-abc") is True
    before = len(vault.items)
    assert set_provider_secret("deepseek-main", "") is False
    assert set_provider_secret("deepseek-main", "   ") is False
    assert len(vault.stores) == 1  # no store call for blank input
    assert len(vault.items) == before
    assert get_provider_secret("deepseek-main") == "sk-abc"  # preserved


def test_unavailable_keyring_is_safe(vault: _FakeSecret) -> None:
    vault.fail = "lookup"
    assert get_provider_secret("deepseek-main") is None
    assert has_provider_secret("deepseek-main") is False
    vault.fail = "store"
    assert set_provider_secret("deepseek-main", "sk-abc") is False
    vault.fail = "clear"
    assert clear_provider_secret("deepseek-main") is False
    assert get_ntfy_token() is None  # NTFY lookups fail safe too


def test_no_raw_exception_or_secret_leaks(vault: _FakeSecret) -> None:
    """A failing vault raising an exception that mentions the secret value
    never propagates; the secret value never reaches labels or attributes."""

    class _SecretLeak(RuntimeError):
        pass

    def leaking_lookup(*_args: Any, **_kwargs: Any) -> None:
        raise _SecretLeak("locked sk-super-secret-token")

    with patch.object(Secret, "password_lookup_sync", leaking_lookup):
        assert get_provider_secret("deepseek-main") is None
    # Store labels never contain the secret itself.
    set_provider_secret("deepseek-main", "sk-super-secret-token")
    assert all("sk-super-secret-token" not in label for _attrs, label in vault.stores)
    assert all("sk-super-secret-token" not in attrs for attrs in vault.lookups)


def test_invalid_slug_and_purpose_are_safe(vault: _FakeSecret) -> None:
    for bad in ("", "x" * 65, "has space", "has\tcontrol"):
        assert get_provider_secret(bad) is None
        assert set_provider_secret(bad, "tok") is False
        assert has_provider_secret(bad) is False
        assert clear_provider_secret(bad) is False
    assert get_provider_secret("deepseek-main", purpose="refresh_token") is None
    assert set_provider_secret("deepseek-main", "tok", purpose="refresh_token") is False
    assert has_provider_secret("deepseek-main", purpose="refresh_token") is False
    assert clear_provider_secret("deepseek-main", purpose="refresh_token") is False
    assert vault.lookups == [] and vault.stores == [] and vault.clears == []


def test_oversized_secret_rejected(vault: _FakeSecret) -> None:
    assert set_provider_secret("deepseek-main", "x" * 4097) is False
    assert vault.stores == []


def test_ntfy_and_provider_credentials_do_not_collide(vault: _FakeSecret) -> None:
    set_ntfy_token("n-token")
    set_provider_secret("ntfy", "p-token")
    assert get_ntfy_token() == "n-token"
    assert get_provider_secret("ntfy") == "p-token"
