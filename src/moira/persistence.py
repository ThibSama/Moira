from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .models import QuotaReading

CONFIG_VERSION = 1


def config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "moira"


def state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "moira"


@dataclass(slots=True)
class Settings:
    version: int = CONFIG_VERSION
    refresh_minutes: int = 10
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
        if not 1 <= self.refresh_minutes <= 1440:
            raise ValueError("refresh interval must be between 1 and 1440 minutes")
        if any(not 1 <= value <= 100 for value in self.thresholds):
            raise ValueError("thresholds must be between 1 and 100")
        self.thresholds = sorted(set(self.thresholds))


@dataclass(slots=True)
class AppState:
    readings: list[QuotaReading] = field(default_factory=list)
    alert_keys: list[str] = field(default_factory=list)


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
        return AppState(readings, [str(key) for key in data.get("alert_keys", [])][-500:])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return AppState()


def save_state(state: AppState) -> None:
    _atomic_json(
        state_dir() / "state.json",
        {
            "readings": [item.to_dict() for item in state.readings],
            "alert_keys": state.alert_keys[-500:],
        },
    )
