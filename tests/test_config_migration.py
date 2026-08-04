"""Deterministic tests for config v1→v2→v3 migration, per-service rules,
collection toggles, and fail-closed validation."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from moira.models import Service
from moira.persistence import (
    CONFIG_VERSION,
    DEFAULT_REFRESH_MINUTES,
    DEFAULT_REPO,
    VALID_REFRESH_MINUTES,
    ProviderRules,
    Settings,
    _coerce_rules,
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

    with pytest.raises(ValueError):
        Settings(refresh_minutes=7).validate()


def test_refresh_minutes_in_valid_set() -> None:
    for val in VALID_REFRESH_MINUTES:
        Settings(refresh_minutes=val).validate()


def test_defaults_normalize_rules_for_both_providers() -> None:
    settings = Settings()
    settings.validate()
    assert set(settings.rules) == {"claude", "codex"}  # type: ignore[arg-type]
    assert settings.rules_for(Service.CLAUDE).thresholds == [50, 75, 90]
    assert settings.rules_for(Service.CODEX).thresholds == [50, 75, 90]


def test_rules_for_falls_back_to_legacy_without_validate() -> None:
    """Directly-constructed Settings (tests) fall back to legacy fields."""
    settings = Settings(thresholds=[25, 50], reset_alerts=False)
    rules = settings.rules_for(Service.CLAUDE)
    assert rules.thresholds == [25, 50]
    assert rules.reset_alerts is False


def test_unknown_provider_in_rules_fails_closed() -> None:

    settings = Settings(rules={"claude": ProviderRules(), "unknown": ProviderRules()})
    with pytest.raises(ValueError):
        settings.validate()


def test_missing_provider_in_rules_fails_closed() -> None:

    settings = Settings(rules={"claude": ProviderRules()})
    with pytest.raises(ValueError):
        settings.validate()


def test_invalid_thresholds_in_rules_fail_closed() -> None:

    settings = Settings(rules={"claude": ProviderRules([0]), "codex": ProviderRules()})
    with pytest.raises(ValueError):
        settings.validate()


def test_invalid_repo_fails_closed() -> None:

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


# ── Package 5b: exact-type v3 validation (no bool/int conflation) ──


def test_boolean_thresholds_rejected() -> None:
    """bool is a subclass of int — True must NOT pass the 1..100 threshold
    checks as the integer 1. Values are routed through untyped variables
    (exactly as JSON parses them) so the runtime validation is the subject."""
    bad_values: list[Any] = [[True], [True, 50], [50, False], [False]]
    for bad in bad_values:
        rules = ProviderRules()
        rules.thresholds = bad
        with pytest.raises(ValueError):
            rules.validate()
    settings = Settings()
    bad_settings_thresholds: Any = [True, 50]
    settings.thresholds = bad_settings_thresholds
    with pytest.raises(ValueError):
        settings.validate()


def test_string_switches_rejected() -> None:
    for name in (
        "ntfy_enabled",
        "native_notifications",
        "reset_alerts",
        "error_alerts",
        "collect_claude",
        "collect_codex",
        "compact_mode",
        "window_maximized",
        "autostart",
    ):
        for bad in ("false", 1, None):
            settings = Settings()
            setattr(settings, name, bad)
            with pytest.raises(ValueError):
                settings.validate()
    # Real booleans pass.
    Settings(collect_claude=False, ntfy_enabled=True).validate()


def test_rule_switch_types_rejected() -> None:
    bad_reset: Any = "false"
    rules = ProviderRules()
    rules.reset_alerts = bad_reset
    with pytest.raises(ValueError):
        rules.validate()
    bad_error: Any = 1
    rules2 = ProviderRules()
    rules2.error_alerts = bad_error
    with pytest.raises(ValueError):
        rules2.validate()
    # JSON-shaped rules with a string switch fail closed through Settings too.
    settings = Settings(rules={"claude": ProviderRules(), "codex": ProviderRules()})
    settings.rules["claude"].reset_alerts = bad_reset  # type: ignore[index]
    with pytest.raises(ValueError):
        settings.validate()


def test_malformed_geometry_rejected() -> None:
    for kwargs in (
        {"window_width": "800", "window_height": 600},
        {"window_width": 800, "window_height": "600"},
        {"window_width": True, "window_height": 600},
        {"window_width": 800, "window_height": True},
        {"window_width": 0, "window_height": 600},
        {"window_width": 800, "window_height": -5},
        {"window_width": 100_000, "window_height": 600},
        {"window_width": 800},  # only one edge set
        {"window_height": 600},  # only one edge set
        {"window_width": 800.0, "window_height": 600},
    ):
        settings = Settings()
        for key, value in kwargs.items():
            setattr(settings, key, value)
        with pytest.raises(ValueError):
            settings.validate()
    # Valid shapes pass.
    Settings().validate()
    Settings(window_width=None, window_height=None).validate()
    Settings(window_width=800, window_height=600).validate()
    Settings(window_width=1, window_height=65535).validate()


def test_refresh_minutes_bool_and_float_rejected() -> None:
    bad_values: list[Any] = [True, 2.0, "2"]
    for bad in bad_values:
        settings = Settings()
        settings.refresh_minutes = bad
        with pytest.raises(ValueError):
            settings.validate()


def test_string_fields_exact_type() -> None:
    bad_server: Any = 123
    bad_topic: Any = True
    bad_repo: Any = 123
    settings = Settings()
    settings.ntfy_server = bad_server
    with pytest.raises(ValueError):
        settings.validate()
    settings2 = Settings()
    settings2.ntfy_topic = bad_topic
    with pytest.raises(ValueError):
        settings2.validate()
    settings3 = Settings()
    settings3.repo = bad_repo
    with pytest.raises(ValueError):
        settings3.validate()


def test_invalid_v3_json_fails_closed(tmp_path: Path) -> None:
    """Exact-type violations in a v3 file fall back to defaults."""
    cases = (
        {"version": 3, "collect_claude": "yes"},
        {"version": 3, "ntfy_enabled": "false"},
        {"version": 3, "thresholds": [True, 75]},
        {"version": 3, "window_width": "800", "window_height": 600},
        {"version": 3, "window_maximized": 1},
        {
            "version": 3,
            "rules": {"claude": {"thresholds": [50], "reset_alerts": "yes"}, "codex": {}},
        },
    )
    for payload in cases:
        config = tmp_path / "moira" / "config.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps(payload))
        with patch.dict("os.environ", {"XDG_CONFIG_HOME": str(tmp_path)}):
            settings = load_settings()
        assert settings.version == 3
        assert settings.collect_claude is True
        assert settings.ntfy_enabled is False
        assert settings.thresholds == [50, 75, 90]
        assert settings.window_width is None
        assert settings.window_maximized is False
        config.unlink()


# ── Package 5b: migration never coerces by truthiness ──


def test_v2_migration_string_false_is_not_enabled() -> None:
    """bool(\"false\") is True — the migration must never coerce by truthiness."""
    v3 = _migrate_v2_to_v3({"version": 2, "reset_alerts": "false", "error_alerts": "false"})
    assert v3["rules"]["claude"]["reset_alerts"] is True  # fail closed to default
    assert v3["rules"]["claude"]["error_alerts"] is True
    assert v3["rules"]["codex"]["reset_alerts"] is True


def test_v2_migration_preserves_real_booleans() -> None:
    v3 = _migrate_v2_to_v3({"version": 2, "reset_alerts": False, "error_alerts": True})
    assert v3["rules"]["claude"]["reset_alerts"] is False
    assert v3["rules"]["claude"]["error_alerts"] is True
    assert v3["rules"]["codex"]["reset_alerts"] is False


def test_v2_migration_numeric_and_none_switches_fail_closed() -> None:
    for bad in (1, 0, None, "true", 2):
        v3 = _migrate_v2_to_v3({"version": 2, "reset_alerts": bad})
        assert v3["rules"]["claude"]["reset_alerts"] is True, bad


def test_v1_migration_boolean_refresh_minutes_not_preserved() -> None:
    """True == 1 must not survive as a refresh interval."""
    v2 = _migrate_v1_to_v2({"version": 1, "refresh_minutes": True})
    assert v2["refresh_minutes"] == DEFAULT_REFRESH_MINUTES
    v2 = _migrate_v1_to_v2({"version": 1, "refresh_minutes": 5.0})
    assert v2["refresh_minutes"] == DEFAULT_REFRESH_MINUTES
    v2 = _migrate_v1_to_v2({"version": 1, "refresh_minutes": "10"})
    assert v2["refresh_minutes"] == DEFAULT_REFRESH_MINUTES


def test_load_settings_v2_string_false_fails_closed(tmp_path: Path) -> None:
    config = tmp_path / "moira" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"version": 2, "reset_alerts": "false"}))
    with patch.dict("os.environ", {"XDG_CONFIG_HOME": str(tmp_path)}):
        settings = load_settings()
    assert settings.rules_for(Service.CLAUDE).reset_alerts is True
    assert settings.rules_for(Service.CODEX).reset_alerts is True


# ── Package 5c: persisted v3 `rules` is a strict exact-shape contract ──


def _valid_rules() -> dict[str, Any]:
    return {
        "claude": {"thresholds": [50, 75], "reset_alerts": True, "error_alerts": True},
        "codex": {"thresholds": [90], "reset_alerts": True, "error_alerts": True},
    }


def test_coerce_rules_strict_shapes() -> None:
    """Persisted v3 `rules` must be an object with exactly claude+codex."""
    with pytest.raises(ValueError):
        _coerce_rules({})  # missing key
    bad_values: list[Any] = [[], False, 0, "", None, "rules"]
    for bad in bad_values:
        with pytest.raises(ValueError):
            _coerce_rules({"rules": bad})
    with pytest.raises(ValueError):
        _coerce_rules({"rules": {}})  # explicit empty object
    with pytest.raises(ValueError):
        _coerce_rules({"rules": {"claude": {"thresholds": [50]}}})  # one provider
    with pytest.raises(ValueError):
        _coerce_rules(  # extra provider
            {"rules": {**_valid_rules(), "extra": {"thresholds": [50]}}}
        )
    with pytest.raises(ValueError):
        _coerce_rules({"rules": {"claude": [50], "codex": {"thresholds": [50]}}})
    coerced = _coerce_rules({"rules": _valid_rules()})
    assert set(coerced) == {"claude", "codex"}
    assert coerced["claude"].thresholds == [50, 75]


def test_persisted_rules_matrix_fails_closed_to_complete_defaults(tmp_path: Path) -> None:
    """Every malformed persisted `rules` shape falls back to a COMPLETE
    default Settings instance — never partial preservation with rules
    silently derived from the legacy globals."""
    cases = (
        {"version": 3, "ntfy_topic": "keep", "rules": {}},
        {"version": 3, "ntfy_topic": "keep", "rules": []},
        {"version": 3, "ntfy_topic": "keep", "rules": False},
        {"version": 3, "ntfy_topic": "keep", "rules": 0},
        {"version": 3, "ntfy_topic": "keep", "rules": ""},
        {"version": 3, "ntfy_topic": "keep"},  # missing rules key
        {"version": 3, "ntfy_topic": "keep", "rules": {"claude": {"thresholds": [50]}}},
        {
            "version": 3,
            "ntfy_topic": "keep",
            "rules": {**_valid_rules(), "extra": {"thresholds": [50]}},
        },
        {
            "version": 3,
            "ntfy_topic": "keep",
            "rules": {"claude": {"thresholds": [True, 50]}, "codex": {"thresholds": [90]}},
        },
    )
    for payload in cases:
        config = tmp_path / "moira" / "config.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps(payload))
        with patch.dict("os.environ", {"XDG_CONFIG_HOME": str(tmp_path)}):
            settings = load_settings()
        # COMPLETE default instance: not even ntfy_topic is preserved.
        assert settings.version == 3
        assert settings.ntfy_topic == "", payload["rules"] if "rules" in payload else "missing"
        assert settings.thresholds == [50, 75, 90]
        assert settings.collect_claude is True
        assert settings.rules_for(Service.CLAUDE).thresholds == [50, 75, 90]
        assert settings.rules_for(Service.CODEX).thresholds == [50, 75, 90]
        config.unlink()


def test_valid_persisted_rules_still_load(tmp_path: Path) -> None:
    config = tmp_path / "moira" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"version": 3, "ntfy_topic": "keep", "rules": _valid_rules()}))
    with patch.dict("os.environ", {"XDG_CONFIG_HOME": str(tmp_path)}):
        settings = load_settings()
    assert settings.ntfy_topic == "keep"
    assert settings.rules_for(Service.CLAUDE).thresholds == [50, 75]
    assert settings.rules_for(Service.CODEX).thresholds == [90]


def test_direct_settings_construction_stays_ergonomic() -> None:
    """Settings() without rules derives both providers from legacy globals
    (in-memory path); the sentinel never reaches persisted validation."""
    settings = Settings()
    settings.validate()
    assert set(settings.rules) == {"claude", "codex"}  # type: ignore[arg-type]
    assert settings.rules_for(Service.CLAUDE).thresholds == [50, 75, 90]
    assert settings.rules_for(Service.CODEX).reset_alerts is True
    # Explicit empty dict is NOT the sentinel: it fails closed.
    explicit = Settings(rules={})
    with pytest.raises(ValueError):
        explicit.validate()


def test_saved_default_settings_round_trip(tmp_path: Path) -> None:
    """A fresh default Settings, once saved, carries the full rules object
    and reloads cleanly (no sentinel ever written to disk)."""
    with patch.dict("os.environ", {"XDG_CONFIG_HOME": str(tmp_path)}):
        save_settings(Settings())
        raw = (tmp_path / "moira" / "config.json").read_text()
        assert "rules" in raw
        loaded = load_settings()
    assert loaded.rules_for(Service.CLAUDE).thresholds == [50, 75, 90]
    assert loaded.rules_for(Service.CODEX).thresholds == [50, 75, 90]
