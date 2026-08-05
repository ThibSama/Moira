"""Package 7d — ProviderProfile types, closed provider kinds and base-URL policy."""

from __future__ import annotations

import pytest

from moira.integrations import (
    MAX_PROFILE_HERMES_LABEL_LENGTH,
    MAX_PROFILE_LABEL_LENGTH,
    MAX_PROFILE_MODEL_LENGTH,
    MAX_PROFILE_URL_LENGTH,
    RESERVED_PROFILE_SLUGS,
    ProviderKind,
    ProviderProfile,
)


def _profile(**overrides: object) -> ProviderProfile:
    base: dict[str, object] = {
        "slug": "deepseek-main",
        "label": "DeepSeek main",
        "kind": ProviderKind.DEEPSEEK,
        "model": "deepseek-chat",
        "enabled": True,
    }
    base.update(overrides)
    return ProviderProfile(**base)  # type: ignore[arg-type]


def test_all_supported_kinds_construct() -> None:
    for kind in ProviderKind:
        profile = _profile(slug=f"p-{kind.value}", kind=kind)
        assert profile.kind is kind
        assert profile.slug == f"p-{kind.value}"
        assert profile.enabled is True


def test_kind_set_is_closed() -> None:
    with pytest.raises(ValueError):
        ProviderKind("claude")
    with pytest.raises(ValueError):
        ProviderKind("ollama")
    assert {kind.value for kind in ProviderKind} == {
        "deepseek",
        "openai_compatible",
        "openrouter",
        "anthropic",
        "openai",
        "local",
        "custom",
    }


def test_reserved_slugs_are_rejected() -> None:
    assert RESERVED_PROFILE_SLUGS == frozenset({"claude", "codex", "hermes"})
    for slug in RESERVED_PROFILE_SLUGS:
        with pytest.raises(ValueError):
            _profile(slug=slug)


@pytest.mark.parametrize(
    "slug",
    [
        "",
        "Uppercase",
        "has space",
        "has\tcontrol",
        "with/slash",
        "a" * 65,
        "-leading-dash",
        "trailing-dash-",
        "9fine",  # numeric prefix is allowed
        "ok-underscore_2",
    ],
)
def test_slug_validation(slug: str) -> None:
    if slug in ("9fine", "ok-underscore_2"):
        assert _profile(slug=slug).slug == slug
    else:
        with pytest.raises(ValueError):
            _profile(slug=slug)


def test_label_validation() -> None:
    with pytest.raises(ValueError):
        _profile(label="")
    with pytest.raises(ValueError):
        _profile(label="   ")
    with pytest.raises(ValueError):
        _profile(label="x" * (MAX_PROFILE_LABEL_LENGTH + 1))
    assert _profile(label="DeepSeek prod").label == "DeepSeek prod"


def test_model_and_hermes_label_bounds() -> None:
    with pytest.raises(ValueError):
        _profile(model="x" * (MAX_PROFILE_MODEL_LENGTH + 1))
    with pytest.raises(ValueError):
        _profile(hermes_label="x" * (MAX_PROFILE_HERMES_LABEL_LENGTH + 1))
    assert _profile(model="", hermes_label="").model == ""
    assert _profile(model="deepseek-reasoner", hermes_label="Main").hermes_label == "Main"


def test_enabled_is_strict_boolean() -> None:
    with pytest.raises(ValueError):
        _profile(enabled="yes")
    with pytest.raises(ValueError):
        _profile(enabled=1)
    assert _profile(enabled=False).enabled is False


def test_kind_field_must_be_provider_kind() -> None:
    with pytest.raises(ValueError):
        _profile(kind="deepseek")
    with pytest.raises(ValueError):
        _profile(kind=None)


# ── Base-URL policy ──────────────────────────────────────────────────────────


def test_remote_kinds_require_https() -> None:
    for kind in (
        ProviderKind.DEEPSEEK,
        ProviderKind.OPENAI_COMPATIBLE,
        ProviderKind.OPENROUTER,
        ProviderKind.ANTHROPIC,
        ProviderKind.OPENAI,
        ProviderKind.CUSTOM,
    ):
        assert _profile(kind=kind, base_url="https://api.example.com/v1").base_url
        with pytest.raises(ValueError):
            _profile(kind=kind, base_url="http://api.example.com/v1")
        with pytest.raises(ValueError):
            _profile(kind=kind, base_url="ftp://api.example.com/v1")


def test_local_kind_allows_loopback_only() -> None:
    assert _profile(kind=ProviderKind.LOCAL, base_url="http://localhost:1234/v1")
    assert _profile(kind=ProviderKind.LOCAL, base_url="http://127.0.0.1:8080")
    assert _profile(kind=ProviderKind.LOCAL, base_url="http://127.0.0.2:8080")
    assert _profile(kind=ProviderKind.LOCAL, base_url="http://[::1]:8080")
    assert _profile(kind=ProviderKind.LOCAL, base_url="https://localhost:8443")
    with pytest.raises(ValueError):
        _profile(kind=ProviderKind.LOCAL, base_url="http://192.168.1.10:8080")
    with pytest.raises(ValueError):
        _profile(kind=ProviderKind.LOCAL, base_url="http://example.com:8080")
    with pytest.raises(ValueError):
        _profile(kind=ProviderKind.LOCAL, base_url="http://0.0.0.0:8080")


def test_empty_base_url_is_unset() -> None:
    assert _profile(base_url="").base_url == ""


@pytest.mark.parametrize(
    "url",
    [
        "https://user:pass@api.example.com/v1",
        "https://api.example.com/v1?key=value",
        "https://api.example.com/v1#fragment",
        "https://api.example.com/v1\x07",
        "https://",
        "api.example.com/v1",
        "x" * (MAX_PROFILE_URL_LENGTH + 1),
        "https://exa mple.com/v1",
    ],
)
def test_rejected_url_shapes(url: str) -> None:
    with pytest.raises(ValueError):
        _profile(base_url=url)


def test_path_and_port_are_allowed() -> None:
    assert _profile(base_url="https://api.example.com:443/v1/chat").base_url
