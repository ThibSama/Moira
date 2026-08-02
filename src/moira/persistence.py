from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .models import QuotaReading

CONFIG_VERSION = 2

VALID_REFRESH_MINUTES = (1, 2, 5, 10, 15, 30)
DEFAULT_REFRESH_MINUTES = 2


def config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "moira"


def state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "moira"


@dataclass(slots=True)
class Settings:
    version: int = CONFIG_VERSION
    refresh_minutes: int = DEFAULT_REFRESH_MINUTES
    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    ntfy_enabled: bool = False
    thresholds: list[int] = field(default_factory=lambda: [50, 75, 90])
    reset_alerts: bool = True
    error_alerts: bool = True
    autostart: bool = False

    def validate(self) -> None:
        if self.version != CONFIG_VERSION:
            raise ValueError("unsupported configuration version")
        if self.refresh_minutes not in VALID_REFRESH_MINUTES:
            raise ValueError(
                f"refresh interval must be one of: {', '.join(map(str, VALID_REFRESH_MINUTES))}"
            )
        if any(not 1 <= value <= 100 for value in self.thresholds):
            raise ValueError("thresholds must be between 1 and 100")
        self.thresholds = sorted(set(self.thresholds))


def _migrate_v1_to_v2(data: dict[str, Any]) -> dict[str, Any]:
    """Additively migrate a v1 config dict to v2.

    Preserves all user settings. The old default of 10 minutes is mapped to the
    new default of 2 minutes if the user never changed it (i.e. the config still
    has the old default of 10). Valid existing custom values in the new allowed
    set are preserved as-is.
    """
    migrated = dict(data)
    old = migrated.get("refresh_minutes")
    if old is None or old == 10:
        # Old default → new default
        migrated["refresh_minutes"] = DEFAULT_REFRESH_MINUTES
    elif old in VALID_REFRESH_MINUTES:
        # Custom but valid → preserve
        pass
    else:
        # Invalid for v2 → fall back to new default
        migrated["refresh_minutes"] = DEFAULT_REFRESH_MINUTES
    migrated["version"] = CONFIG_VERSION
    return migrated


@dataclass(slots=True)
class AppState:
    readings: list[QuotaReading] = field(default_factory=list)
    alert_keys: list[str] = field(default_factory=list)
    last_refresh: str | None = None
    next_refresh: str | None = None


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def load_settings() -> Settings:
    path = config_dir() / "config.json"
    if not path.exists():
        return Settings()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return Settings()
        version = data.get("version", 1)
        if version == 1:
            data = _migrate_v1_to_v2(data)
        settings = Settings(**data)
        settings.validate()
        return settings
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return Settings()


def save_settings(settings: Settings) -> None:
    settings.validate()
    _atomic_json(config_dir() / "config.json", asdict(settings))


def load_state() -> AppState:
    path = state_dir() / "state.json"
    if not path.exists():
        return AppState()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        readings = [QuotaReading.from_dict(item) for item in data.get("readings", [])]
        last_refresh = data.get("last_refresh")
        next_refresh = data.get("next_refresh")
        return AppState(
            readings,
            [str(key) for key in data.get("alert_keys", [])][-500:],
            str(last_refresh) if last_refresh else None,
            str(next_refresh) if next_refresh else None,
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return AppState()


def save_state(state: AppState) -> None:
    _atomic_json(
        state_dir() / "state.json",
        {
            "readings": [item.to_dict() for item in state.readings],
            "alert_keys": state.alert_keys[-500:],
            "last_refresh": state.last_refresh,
            "next_refresh": state.next_refresh,
        },
    )
