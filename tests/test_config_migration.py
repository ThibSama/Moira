"""Deterministic tests for config v1→v2→v3 migration, per-service rules,
collection toggles, and fail-closed validation."""

import json
from pathlib import Path
from unittest.mock import patch

from moira.models import Service
from moira.persistence import (
    CONFIG_VERSION,
    DEFAULT_REFRESH_MINUTES,
    DEFAULT_REPO,
    VALID_REFRESH_MINUTES,
    ProviderRules,
    Settings,
    _migrate_v1_to_v2,
    _migrate_v2_to_v3,
    load_settings,
    save_settings,
)


def test_config_version_is_3() -> None:
    assert CONFIG_VERSION == 3


def test_default_refresh_is_2_minutes() -> None:
    assert DEFAULT_REFRESH_MINUTES == 2


def test_valid_refresh_choices() -> None:
    assert VALID_REFRESH_MINUTES == (1, 2, 5, 10, 15, 30)


def test_v1_10_mins_preserved() -> None:
    """Every valid value including 10 must be preserved exactly."""
    v1 = {"version": 1, "refresh_minutes": 10}
    v2 = _migrate_v1_to_v2(v1)
    assert v2["version"] == 2
    assert v2["refresh_minutes"] == 10


def test_all_valid_v1_refresh_values_preserved() -> None:
    """Every value in VALID_REFRESH_MINUTES, including 10, survives migration."""
    for val in VALID_REFRESH_MINUTES:
        v2 = _migrate_v1_to_v2({"version": 1, "refresh_minutes": val})
        assert v2["refresh_minutes"] == val, f"{val} was not preserved"


def test_v1_missing_refresh_minutes_becomes_default() -> None:
    v2 = _migrate_v1_to_v2({"version": 1})
    assert v2["refresh_minutes"] == DEFAULT_REFRESH_MINUTES


def test_v1_invalid_refresh_minutes_becomes_default() -> None:
    v2 = _migrate_v1_to_v2({"version": 1, "refresh_minutes": 7})
    assert v2["refresh_minutes"] == DEFAULT_REFRESH_MINUTES


def test_v2_to_v3_copies_global_rules_to_both_providers() -> None:
    v2 = {
        "version": 2,
        "refresh_minutes": 5,
        "thresholds": [60, 80],
        "reset_alerts": False,
        "error_alerts": True,
    }
    v3 = _migrate_v2_to_v3(v2)
    assert v3["version"] == 3
    assert v3["rules"]["claude"] == {
        "thresholds": [60, 80],
        "reset_alerts": False,
        "error_alerts": True,
    }
    assert v3["rules"]["codex"] == {
        "thresholds": [60, 80],
        "reset_alerts": False,
        "error_alerts": True,
    }
    assert v3["refresh_minutes"] == 5


def test_v2_to_v3_preserves_prior_settings() -> None:
    v2 = {
        "version": 2,
        "refresh_minutes": 10,
        "ntfy_server": "https://custom.example",
        "ntfy_topic": "mytopic",
        "ntfy_enabled": True,
        "thresholds": [50, 75, 90],
        "reset_alerts": True,
        "error_alerts": False,
        "autostart": True,
    }
    v3 = _migrate_v2_to_v3(v2)
    assert v3["ntfy_server"] == "https://custom.example"
    assert v3["ntfy_topic"] == "mytopic"
    assert v3["ntfy_enabled"] is True
    assert v3["autostart"] is True
    assert v3["refresh_minutes"] == 10
    # Legacy fields preserved for backward compatibility
    assert v3["thresholds"] == [50, 75, 90]
    assert v3["error_alerts"] is False


def test_v2_to_v3_new_fields_get_defaults() -> None:
    v3 = _migrate_v2_to_v3({"version": 2})
    assert v3["collect_claude"] is True
    assert v3["collect_codex"] is True
    assert v3["native_notifications"] is False
    assert v3["compact_mode"] is False
    assert v3["window_width"] is None
    assert v3["window_height"] is None
    assert v3["window_maximized"] is False
    assert v3["repo"] == DEFAULT_REPO


def test_v2_to_v3_invalid_legacy_thresholds_fail_closed_to_defaults() -> None:
    v3 = _migrate_v2_to_v3({"version": 2, "thresholds": [0, 500, "x"]})
    assert v3["rules"]["claude"]["thresholds"] == [50, 75, 90]
    assert v3["rules"]["codex"]["thresholds"] == [50, 75, 90]


def test_load_settings_migrates_v1_to_v3(tmp_path: Path) -> None:
    config = tmp_path / "moira" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "refresh_minutes": 10,
                "ntfy_topic": "test",
                "ntfy_enabled": True,
                "thresholds": [50, 75, 90],
                "reset_alerts": True,
                "error_alerts": True,
                "autostart": False,
                "ntfy_server": "https://ntfy.sh",
            }
        )
    )
    with patch.dict("os.environ", {"XDG_CONFIG_HOME": str(tmp_path)}):
        settings = load_settings()
    assert settings.version == 3
    assert settings.refresh_minutes == 10
    assert settings.ntfy_topic == "test"
    assert settings.ntfy_enabled is True
    # Global rules copied to both providers
    assert settings.rules_for(Service.CLAUDE).thresholds == [50, 75, 90]
    assert settings.rules_for(Service.CODEX).thresholds == [50, 75, 90]


def test_load_settings_migrates_v2_to_v3(tmp_path: Path) -> None:
    config = tmp_path / "moira" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps(
            {
                "version": 2,
                "refresh_minutes": 5,
                "ntfy_topic": "roundtrip",
                "thresholds": [60, 80],
                "reset_alerts": False,
                "error_alerts": True,
                "collect_claude": False,
            }
        )
    )
    with patch.dict("os.environ", {"XDG_CONFIG_HOME": str(tmp_path)}):
        settings = load_settings()
    assert settings.version == 3
    assert settings.refresh_minutes == 5
    assert settings.ntfy_topic == "roundtrip"
    assert settings.collect_claude is False
    assert settings.rules_for(Service.CLAUDE).thresholds == [60, 80]
    assert settings.rules_for(Service.CLAUDE).reset_alerts is False
    assert settings.rules_for(Service.CODEX).thresholds == [60, 80]


def test_save_and_load_v3_settings(tmp_path: Path) -> None:
    settings = Settings(
        refresh_minutes=5,
        ntfy_topic="roundtrip",
        rules={
            "claude": ProviderRules([30, 60], False, True),
            "codex": ProviderRules([40, 80], True, False),
        },
    )
    with patch.dict("os.environ", {"XDG_CONFIG_HOME": str(tmp_path)}):
        save_settings(settings)
        loaded = load_settings()
    assert loaded.version == 3
    assert loaded.refresh_minutes == 5
    assert loaded.rules_for(Service.CLAUDE).thresholds == [30, 60]
    assert loaded.rules_for(Service.CLAUDE).reset_alerts is False
    assert loaded.rules_for(Service.CODEX).thresholds == [40, 80]
    assert loaded.rules_for(Service.CODEX).error_alerts is False


def test_invalid_refresh_rejected() -> None:
    import pytest

    with pytest.raises(ValueError):
        Settings(refresh_minutes=7).validate()


def test_refresh_minutes_in_valid_set() -> None:
    for val in VALID_REFRESH_MINUTES:
        Settings(refresh_minutes=val).validate()


def test_defaults_normalize_rules_for_both_providers() -> None:
    settings = Settings()
    settings.validate()
    assert set(settings.rules) == {"claude", "codex"}
    assert settings.rules_for(Service.CLAUDE).thresholds == [50, 75, 90]
    assert settings.rules_for(Service.CODEX).thresholds == [50, 75, 90]


def test_rules_for_falls_back_to_legacy_without_validate() -> None:
    """Directly-constructed Settings (tests) fall back to legacy fields."""
    settings = Settings(thresholds=[25, 50], reset_alerts=False)
    rules = settings.rules_for(Service.CLAUDE)
    assert rules.thresholds == [25, 50]
    assert rules.reset_alerts is False


def test_unknown_provider_in_rules_fails_closed() -> None:
    import pytest

    settings = Settings(rules={"claude": ProviderRules(), "unknown": ProviderRules()})
    with pytest.raises(ValueError):
        settings.validate()


def test_missing_provider_in_rules_fails_closed() -> None:
    import pytest

    settings = Settings(rules={"claude": ProviderRules()})
    with pytest.raises(ValueError):
        settings.validate()


def test_invalid_thresholds_in_rules_fail_closed() -> None:
    import pytest

    settings = Settings(rules={"claude": ProviderRules([0]), "codex": ProviderRules()})
    with pytest.raises(ValueError):
        settings.validate()


def test_invalid_repo_fails_closed() -> None:
    import pytest

    for bad in ("", "no-slash", "owner/name/extra", "owner@x/name", "owner/name with space"):
        settings = Settings(repo=bad)
        with pytest.raises(ValueError):
            settings.validate()


def test_invalid_config_file_fails_closed_to_defaults(tmp_path: Path) -> None:
    config = tmp_path / "moira" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"version": 3, "thresholds": [999]}))
    with patch.dict("os.environ", {"XDG_CONFIG_HOME": str(tmp_path)}):
        settings = load_settings()
    assert settings.version == 3
    assert settings.thresholds == [50, 75, 90]


def test_enabled_services() -> None:
    settings = Settings(collect_claude=True, collect_codex=True)
    assert settings.enabled_services() == [Service.CLAUDE, Service.CODEX]
    settings = Settings(collect_claude=True, collect_codex=False)
    assert settings.enabled_services() == [Service.CLAUDE]
    settings = Settings(collect_claude=False, collect_codex=True)
    assert settings.enabled_services() == [Service.CODEX]
    settings = Settings(collect_claude=False, collect_codex=False)
    assert settings.enabled_services() == []
