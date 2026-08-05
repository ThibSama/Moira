"""Package 7d — config v3→v4 migration, strict profile decode and round trips."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from moira.integrations import MAX_PROFILES, ProviderKind, ProviderProfile
from moira.persistence import (
    CONFIG_VERSION,
    Settings,
    load_settings,
    save_settings,
)


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from moira.persistence import config_dir as _config_dir

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return _config_dir()


def _v3_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "version": 3,
        "refresh_minutes": 15,
        "ntfy_server": "https://ntfy.sh",
        "ntfy_topic": "my-topic",
        "ntfy_enabled": True,
        "native_notifications": True,
        "thresholds": [20, 40, 60],
        "reset_alerts": False,
        "error_alerts": False,
        "rules": {
            "claude": {"thresholds": [20, 40, 60], "reset_alerts": False, "error_alerts": False},
            "codex": {"thresholds": [20, 40, 60], "reset_alerts": False, "error_alerts": False},
        },
        "collect_claude": False,
        "collect_codex": True,
        "compact_mode": True,
        "window_width": 900,
        "window_height": 700,
        "window_maximized": True,
        "repo": "ThibSama/moira",
        "autostart": False,
    }
    payload.update(overrides)
    return payload


def _write(config_dir: Path, payload: dict[str, Any]) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.json").write_text(json.dumps(payload), encoding="utf-8")


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


def test_config_version_is_four() -> None:
    assert CONFIG_VERSION == 4


def test_v3_file_migrates_to_v4_preserving_every_setting(config_dir: Path) -> None:
    _write(config_dir, _v3_payload())
    settings = load_settings()
    assert settings.version == 4
    assert settings.refresh_minutes == 15
    assert settings.ntfy_server == "https://ntfy.sh"
    assert settings.ntfy_topic == "my-topic"
    assert settings.ntfy_enabled is True
    assert settings.native_notifications is True
    assert settings.thresholds == [20, 40, 60]
    assert settings.reset_alerts is False and settings.error_alerts is False
    rules = settings.rules
    assert isinstance(rules, dict)
    assert rules["claude"].thresholds == [20, 40, 60]
    assert rules["codex"].reset_alerts is False
    assert settings.collect_claude is False and settings.collect_codex is True
    assert settings.compact_mode is True
    assert settings.window_width == 900 and settings.window_height == 700
    assert settings.window_maximized is True
    assert settings.repo == "ThibSama/moira"
    assert settings.provider_profiles == ()


def test_v3_round_trip_preserves_settings_and_empty_profiles(config_dir: Path) -> None:
    _write(config_dir, _v3_payload())
    settings = load_settings()
    save_settings(settings)
    again = load_settings()
    assert again == settings
    assert again.provider_profiles == ()
    payload = json.loads((config_dir / "config.json").read_text(encoding="utf-8"))
    assert payload["version"] == 4
    assert payload["provider_profiles"] == {}


def test_versionless_file_migrates_through_the_chain(config_dir: Path) -> None:
    _write(config_dir, {"refresh_minutes": 10, "ntfy_topic": "legacy"})
    settings = load_settings()
    assert settings.version == 4
    assert settings.refresh_minutes == 10
    assert settings.ntfy_topic == "legacy"
    assert settings.provider_profiles == ()


def test_v4_valid_profiles_decode_sorted_and_exact(config_dir: Path) -> None:
    payload = _v3_payload(
        provider_profiles={
            "z-local": {
                "label": "Local",
                "kind": "local",
                "model": "",
                "enabled": False,
                "base_url": "http://localhost:1234/v1",
                "hermes_label": "",
            },
            "a-deepseek": {
                "label": "DeepSeek",
                "kind": "deepseek",
                "model": "deepseek-chat",
                "enabled": True,
                "base_url": "",
                "hermes_label": "Main",
            },
        }
    )
    _write(config_dir, payload)
    settings = load_settings()
    assert settings.version == 4
    assert [p.slug for p in settings.provider_profiles] == ["a-deepseek", "z-local"]  # sorted
    first = settings.provider_profiles[0]
    assert first.kind is ProviderKind.DEEPSEEK
    assert first.model == "deepseek-chat"
    assert first.enabled is True
    assert first.hermes_label == "Main"
    assert settings.provider_profiles[1].kind is ProviderKind.LOCAL
    assert settings.provider_profiles[1].base_url == "http://localhost:1234/v1"
    # Every v3 setting preserved alongside the profiles.
    assert settings.ntfy_topic == "my-topic"
    assert settings.thresholds == [20, 40, 60]


def test_v4_unknown_kind_falls_back_without_partial_acceptance(config_dir: Path) -> None:
    _write(
        config_dir,
        _v3_payload(
            provider_profiles={
                "good": {
                    "label": "Good",
                    "kind": "deepseek",
                    "model": "m",
                    "enabled": True,
                    "base_url": "",
                    "hermes_label": "",
                },
                "bad": {
                    "label": "Bad",
                    "kind": "ollama",
                    "model": "m",
                    "enabled": True,
                    "base_url": "",
                    "hermes_label": "",
                },
            }
        ),
    )
    settings = load_settings()
    assert settings.provider_profiles == ()  # all-or-nothing, never partial


@pytest.mark.parametrize(
    "records",
    [
        # extra field
        {
            "p": {
                "label": "P",
                "kind": "deepseek",
                "model": "m",
                "enabled": True,
                "base_url": "",
                "hermes_label": "",
                "api_key": "sk-nope",
            }
        },
        # missing field
        {
            "p": {
                "label": "P",
                "kind": "deepseek",
                "model": "m",
                "enabled": True,
                "base_url": "",
            }
        },
        # record not an object
        {"p": ["deepseek"]},
        # kind not a string
        {
            "p": {
                "label": "P",
                "kind": 7,
                "model": "m",
                "enabled": True,
                "base_url": "",
                "hermes_label": "",
            }
        },
        # enabled not a strict boolean
        {
            "p": {
                "label": "P",
                "kind": "deepseek",
                "model": "m",
                "enabled": "yes",
                "base_url": "",
                "hermes_label": "",
            }
        },
        # reserved slug
        {
            "claude": {
                "label": "P",
                "kind": "deepseek",
                "model": "m",
                "enabled": True,
                "base_url": "",
                "hermes_label": "",
            }
        },
        # invalid base URL
        {
            "p": {
                "label": "P",
                "kind": "deepseek",
                "model": "m",
                "enabled": True,
                "base_url": "http://api.example.com",
                "hermes_label": "",
            }
        },
        # oversized label
        {
            "p": {
                "label": "x" * 65,
                "kind": "deepseek",
                "model": "m",
                "enabled": True,
                "base_url": "",
                "hermes_label": "",
            }
        },
    ],
)
def test_v4_invalid_profile_data_falls_back(config_dir: Path, records: dict[str, Any]) -> None:
    _write(config_dir, _v3_payload(provider_profiles=records))
    settings = load_settings()
    assert settings.version == 4
    assert settings.provider_profiles == ()


def test_v4_non_object_profiles_falls_back(config_dir: Path) -> None:
    _write(config_dir, _v3_payload(provider_profiles=[]))
    assert load_settings().provider_profiles == ()
    _write(config_dir, _v3_payload(provider_profiles="deepseek"))
    assert load_settings().provider_profiles == ()


def test_v4_too_many_profiles_falls_back(config_dir: Path) -> None:
    profiles = {
        f"p-{i}": {
            "label": f"P{i}",
            "kind": "deepseek",
            "model": "m",
            "enabled": True,
            "base_url": "",
            "hermes_label": "",
        }
        for i in range(MAX_PROFILES + 1)
    }
    _write(config_dir, _v3_payload(provider_profiles=profiles))
    assert load_settings().provider_profiles == ()


def test_save_writes_slug_keyed_records_deterministically(config_dir: Path) -> None:
    settings = Settings(
        provider_profiles=(
            _profile("z-local", kind=ProviderKind.LOCAL, enabled=False),
            _profile("a-deepseek", model="deepseek-reasoner"),
        )
    )
    save_settings(settings)
    payload = json.loads((config_dir / "config.json").read_text(encoding="utf-8"))
    assert payload["version"] == 4
    records = payload["provider_profiles"]
    assert list(records) == ["a-deepseek", "z-local"]
    assert "slug" not in records["a-deepseek"]  # the key IS the slug
    assert records["a-deepseek"]["kind"] == "deepseek"
    assert records["a-deepseek"]["model"] == "deepseek-reasoner"
    assert records["z-local"]["enabled"] is False
    # Deterministic: a second save produces identical bytes.
    save_settings(settings)
    assert (config_dir / "config.json").read_text(encoding="utf-8") == json.dumps(
        payload, indent=2, sort_keys=True
    ) + "\n"


def test_profile_round_trip_through_disk(config_dir: Path) -> None:
    settings = Settings(
        provider_profiles=(
            _profile(
                "openrouter-main",
                kind=ProviderKind.OPENROUTER,
                base_url="https://openrouter.ai/api/v1",
            ),
        )
    )
    save_settings(settings)
    loaded = load_settings()
    assert loaded.provider_profiles == settings.provider_profiles
    assert loaded.version == 4
    assert loaded.provider_profiles[0].base_url == "https://openrouter.ai/api/v1"


def test_settings_validate_rejects_bad_profile_collections() -> None:
    with pytest.raises(ValueError):
        Settings(provider_profiles="not-a-tuple").validate()  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Settings(provider_profiles=(_profile("p"), _profile("p"))).validate()  # duplicate slugs
    with pytest.raises(ValueError):
        Settings(provider_profiles=(_profile("claude"),)).validate()  # reserved slug
    with pytest.raises(ValueError):
        Settings(
            provider_profiles=tuple(_profile(f"p-{i}") for i in range(MAX_PROFILES + 1))
        ).validate()
    # validate() enforces deterministic ordering.
    settings = Settings(provider_profiles=(_profile("z"), _profile("a")))
    settings.validate()
    assert [p.slug for p in settings.provider_profiles] == ["a", "z"]


def test_explicit_v4_with_no_profiles_key_is_empty(config_dir: Path) -> None:
    payload = _v3_payload()
    payload.pop("provider_profiles", None)
    _write(config_dir, payload)
    settings = load_settings()
    assert settings.version == 4
    assert settings.provider_profiles == ()
