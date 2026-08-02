"""Deterministic tests for config v2 migration, refresh choices, and timer replacement."""

import json
from pathlib import Path
from unittest.mock import patch

from moira.persistence import (
    CONFIG_VERSION,
    DEFAULT_REFRESH_MINUTES,
    VALID_REFRESH_MINUTES,
    Settings,
    _migrate_v1_to_v2,
    load_settings,
    save_settings,
)


def test_config_version_is_2() -> None:
    assert CONFIG_VERSION == 2


def test_default_refresh_is_2_minutes() -> None:
    assert DEFAULT_REFRESH_MINUTES == 2


def test_valid_refresh_choices() -> None:
    assert VALID_REFRESH_MINUTES == (1, 2, 5, 10, 15, 30)


def test_v1_default_10_mins_migrates_to_2() -> None:
    v1 = {"version": 1, "refresh_minutes": 10}
    v2 = _migrate_v1_to_v2(v1)
    assert v2["version"] == 2
    assert v2["refresh_minutes"] == 2


def test_v1_custom_5_mins_preserved() -> None:
    v1 = {"version": 1, "refresh_minutes": 5}
    v2 = _migrate_v1_to_v2(v1)
    assert v2["refresh_minutes"] == 5


def test_v1_invalid_value_migrates_to_default() -> None:
    v1 = {"version": 1, "refresh_minutes": 7}
    v2 = _migrate_v1_to_v2(v1)
    assert v2["refresh_minutes"] == DEFAULT_REFRESH_MINUTES


def test_v1_preserves_other_settings() -> None:
    v1 = {
        "version": 1,
        "refresh_minutes": 10,
        "ntfy_server": "https://custom.example",
        "ntfy_topic": "mytopic",
        "ntfy_enabled": True,
        "thresholds": [60, 80],
        "reset_alerts": False,
        "error_alerts": True,
        "autostart": True,
    }
    v2 = _migrate_v1_to_v2(v1)
    assert v2["ntfy_server"] == "https://custom.example"
    assert v2["ntfy_topic"] == "mytopic"
    assert v2["ntfy_enabled"] is True
    assert v2["thresholds"] == [60, 80]
    assert v2["reset_alerts"] is False
    assert v2["error_alerts"] is True
    assert v2["autostart"] is True


def test_v1_missing_refresh_minutes_gets_default() -> None:
    v1 = {"version": 1}
    v2 = _migrate_v1_to_v2(v1)
    assert v2["refresh_minutes"] == DEFAULT_REFRESH_MINUTES


def test_load_settings_migrates_v1(tmp_path: Path) -> None:
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
    assert settings.version == 2
    assert settings.refresh_minutes == 2
    assert settings.ntfy_topic == "test"
    assert settings.ntfy_enabled is True


def test_save_and_load_v2_settings(tmp_path: Path) -> None:
    with patch.dict("os.environ", {"XDG_CONFIG_HOME": str(tmp_path)}):
        save_settings(Settings(refresh_minutes=5, ntfy_topic="roundtrip"))
        loaded = load_settings()
    assert loaded.version == 2
    assert loaded.refresh_minutes == 5
    assert loaded.ntfy_topic == "roundtrip"


def test_invalid_refresh_rejected() -> None:
    import pytest

    with pytest.raises(ValueError):
        Settings(refresh_minutes=7).validate()


def test_refresh_minutes_in_valid_set() -> None:
    for val in VALID_REFRESH_MINUTES:
        Settings(refresh_minutes=val).validate()
