"""Versioned JSON configuration (v3) and a small last-known-state cache.

Config v3 adds typed per-service alert rules (``ProviderRules``), separate
Claude/Codex collection toggles, native-desktop-notification enablement,
compact mode, persisted window geometry, and the update-check repository.
The v1 → v2 → v3 chain is additive: every prior user setting is preserved
exactly, and the legacy global rule fields remain the source that v2→v3
copies into both providers' rules. Invalid configuration fails closed:
``load_settings`` falls back to defaults on any validation error.

Secrets (NTFY token) never enter JSON — they live in GNOME Keyring only.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .models import QuotaReading, Service

CONFIG_VERSION = 3

VALID_REFRESH_MINUTES = (1, 2, 5, 10, 15, 30)
DEFAULT_REFRESH_MINUTES = 2

#: Default GitHub repository used for the manual update check.
DEFAULT_REPO = "ThibSama/moira"

DEFAULT_THRESHOLDS = [50, 75, 90]

#: Valid provider rule keys (the two collection services).
VALID_RULE_KEYS = ("claude", "codex")

#: Exact field set of one persisted provider rules object. Persisted v3
#: rules must contain exactly these three fields — missing fields must not
#: silently fall back to ProviderRules defaults.
PROVIDER_RULE_FIELDS = ("thresholds", "reset_alerts", "error_alerts")

#: Bounded geometry edge: window sizes are stored as 16-bit-ish positive
#: integers; anything outside 1..MAX_WINDOW_EDGE is rejected (fail closed).
MAX_WINDOW_EDGE = 65535

#: Boolean configuration switches (must be actual JSON booleans, never
#: truthy values like strings, numbers, or None).
_SWITCH_FIELDS = (
    "ntfy_enabled",
    "native_notifications",
    "reset_alerts",
    "error_alerts",
    "collect_claude",
    "collect_codex",
    "compact_mode",
    "window_maximized",
    "autostart",
)


def _is_exact_bool(value: Any) -> bool:
    """Return True only for actual ``bool`` values (``type(x) is bool``).

    ``bool`` is a subclass of ``int`` in Python, so ``isinstance``-based
    checks would accept ``1``/``0`` and strings like ``"false"``; exact type
    comparison is the only check that keeps JSON switches honest.
    """
    return type(value) is bool


def _valid_thresholds(value: Any) -> bool:
    """Exact-type thresholds contract: a list of non-bool ints in 1..100."""
    if not isinstance(value, list):
        return False
    return all(type(item) is int and 1 <= item <= 100 for item in value)


def _valid_geometry(width: Any, height: Any) -> bool:
    """Geometry contract: both None, or two positive bounded ints."""
    if width is None and height is None:
        return True
    return (
        type(width) is int
        and type(height) is int
        and 1 <= width <= MAX_WINDOW_EDGE
        and 1 <= height <= MAX_WINDOW_EDGE
    )


def config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "moira"


def state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "moira"


@dataclass(slots=True)
class ProviderRules:
    """Typed per-service alert rules (thresholds, reset, error)."""

    thresholds: list[int] = field(default_factory=lambda: list(DEFAULT_THRESHOLDS))
    reset_alerts: bool = True
    error_alerts: bool = True

    def validate(self) -> None:
        if not _valid_thresholds(self.thresholds):
            raise ValueError("thresholds must be a list of integers between 1 and 100")
        if not _is_exact_bool(self.reset_alerts) or not _is_exact_bool(self.error_alerts):
            raise ValueError("reset/error alert switches must be booleans")
        self.thresholds = sorted(set(self.thresholds))


class _UnsetRules:
    """Sentinel type for ``rules`` omitted on direct in-memory ``Settings()``
    construction.

    Persisted v3 JSON never carries the sentinel: the strict decode layer
    (``_coerce_rules``) either produces the exact ``{claude, codex}`` shape
    or raises, so an invalid persisted file fails closed to a complete
    default ``Settings`` instance. The sentinel exists only so that
    ``Settings()`` (no rules argument) stays ergonomic: ``validate()`` then
    derives both providers' rules from the legacy global fields.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "<unset-rules>"


_RULES_UNSET = _UnsetRules()


@dataclass(slots=True)
class Settings:
    version: int = CONFIG_VERSION
    refresh_minutes: int = DEFAULT_REFRESH_MINUTES
    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    ntfy_enabled: bool = False
    native_notifications: bool = False
    # Legacy global rule fields — kept for backward compatibility and as the
    # migration source. Per-service rules are authoritative after v3.
    thresholds: list[int] = field(default_factory=lambda: list(DEFAULT_THRESHOLDS))
    reset_alerts: bool = True
    error_alerts: bool = True
    # Typed per-service alert rules (v3). The sentinel default keeps direct
    # ``Settings()`` construction ergonomic: ``validate()`` derives both
    # providers' rules from the legacy global fields. Persisted v3 JSON
    # always carries an explicit exact ``{claude, codex}`` object, enforced
    # by ``_coerce_rules`` (any other shape fails closed to defaults).
    rules: dict[str, ProviderRules] | _UnsetRules = _RULES_UNSET
    collect_claude: bool = True
    collect_codex: bool = True
    compact_mode: bool = False
    window_width: int | None = None
    window_height: int | None = None
    window_maximized: bool = False
    repo: str = DEFAULT_REPO
    autostart: bool = False

    def validate(self) -> None:
        if not isinstance(self.version, int) or isinstance(self.version, bool):
            raise ValueError("configuration version must be an integer")
        if self.version != CONFIG_VERSION:
            raise ValueError("unsupported configuration version")
        if (
            type(self.refresh_minutes) is not int
            or self.refresh_minutes not in VALID_REFRESH_MINUTES
        ):
            raise ValueError(
                f"refresh interval must be one of: {', '.join(map(str, VALID_REFRESH_MINUTES))}"
            )
        if type(self.ntfy_server) is not str or type(self.ntfy_topic) is not str:
            raise ValueError("NTFY server and topic must be strings")
        for name in _SWITCH_FIELDS:
            if not _is_exact_bool(getattr(self, name)):
                raise ValueError(f"{name} must be a boolean")
        if not _valid_thresholds(self.thresholds):
            raise ValueError("thresholds must be a list of integers between 1 and 100")
        self.thresholds = sorted(set(self.thresholds))
        if not _valid_geometry(self.window_width, self.window_height):
            raise ValueError("window geometry must be two positive integers or both null")
        if not _valid_repo(self.repo):
            raise ValueError("update-check repository must be owner/name without separators")
        if isinstance(self.rules, _UnsetRules):
            # In-memory default construction without explicit rules: derive
            # both providers' rules from the legacy global fields. Valid by
            # construction (thresholds and switches were checked above).
            self.rules = {
                key: ProviderRules(list(self.thresholds), self.reset_alerts, self.error_alerts)
                for key in VALID_RULE_KEYS
            }
            return
        if not isinstance(self.rules, dict):
            raise ValueError("rules must be an object mapping providers to ProviderRules")
        # Typed per-service contract: exactly the two providers, each valid.
        for key in VALID_RULE_KEYS:
            rules = self.rules.get(key)
            if not isinstance(rules, ProviderRules):
                raise ValueError(f"missing or invalid rules for provider {key!r}")
            rules.validate()
        for key in self.rules:
            if key not in VALID_RULE_KEYS:
                raise ValueError(f"unknown provider in rules: {key!r}")

    def rules_for(self, service: Service | str) -> ProviderRules:
        """Return the typed alert rules for one provider.

        Falls back to the legacy global fields when the rules dict is unset
        (direct ``Settings()`` construction before ``validate()``) or when
        the provider is absent. After ``validate()`` the rules dict is
        always populated for both providers.
        """
        key = service.value if isinstance(service, Service) else str(service)
        if isinstance(self.rules, dict):
            rules = self.rules.get(key)
            if rules is not None:
                return rules
        return ProviderRules(list(self.thresholds), self.reset_alerts, self.error_alerts)

    def enabled_services(self) -> list[Service]:
        """Return the services whose collectors should run, in fixed order."""
        services: list[Service] = []
        if self.collect_claude:
            services.append(Service.CLAUDE)
        if self.collect_codex:
            services.append(Service.CODEX)
        return services


def _valid_repo(repo: str) -> bool:
    """Validate an ``owner/name`` GitHub repository reference (fail closed)."""
    if not isinstance(repo, str):
        return False
    if "/" not in repo:
        return False
    owner, _, name = repo.partition("/")
    if not owner or not name or "/" in name:
        return False
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-")
    return all(ch in allowed for ch in owner) and all(ch in allowed for ch in name)


def _migrate_v1_to_v2(data: dict[str, Any]) -> dict[str, Any]:
    """Additively migrate a v1 config dict to v2.

    Preserves all user settings. Every refresh_minutes value in the v2 allowed
    set — including 10 — is preserved exactly. The new default of 2 minutes is
    applied only when refresh_minutes is absent or holds an invalid value.
    """
    migrated = dict(data)
    old = migrated.get("refresh_minutes")
    if type(old) is int and old in VALID_REFRESH_MINUTES:
        # Valid existing value → preserve as-is
        pass
    else:
        # Absent or invalid (including booleans, floats, strings) → new default
        migrated["refresh_minutes"] = DEFAULT_REFRESH_MINUTES
    migrated["version"] = 2
    return migrated


def _legacy_bool(data: dict[str, Any], key: str, default: bool) -> bool:
    """Read a legacy switch with exact-type semantics (never truthiness).

    Only an actual JSON boolean is preserved; strings like ``"false"``,
    numbers, and None all fail closed to the default. ``bool(value)``
    coercion is forbidden because ``bool("false")`` is True.
    """
    value = data.get(key, default)
    return value if _is_exact_bool(value) else default


def _legacy_thresholds(data: dict[str, Any]) -> list[int]:
    """Return the legacy thresholds value, or the default when invalid."""
    value = data.get("thresholds")
    if (
        isinstance(value, list)
        and all(isinstance(item, int) and not isinstance(item, bool) for item in value)
        and all(1 <= item <= 100 for item in value)
    ):
        return sorted(set(value))
    return list(DEFAULT_THRESHOLDS)


def _migrate_v2_to_v3(data: dict[str, Any]) -> dict[str, Any]:
    """Additively migrate a v2 config dict to v3.

    Copies the existing global thresholds/reset/error rules into BOTH
    providers' typed ``rules`` and preserves every prior setting. New
    fields take their defaults only when absent. Invalid legacy rule
    values fail closed to the defaults (the migration must never produce
    an invalid configuration).
    """
    migrated = dict(data)
    thresholds = _legacy_thresholds(migrated)
    reset_alerts = _legacy_bool(migrated, "reset_alerts", True)
    error_alerts = _legacy_bool(migrated, "error_alerts", True)
    migrated["rules"] = {
        "claude": {
            "thresholds": list(thresholds),
            "reset_alerts": reset_alerts,
            "error_alerts": error_alerts,
        },
        "codex": {
            "thresholds": list(thresholds),
            "reset_alerts": reset_alerts,
            "error_alerts": error_alerts,
        },
    }
    migrated.setdefault("collect_claude", True)
    migrated.setdefault("collect_codex", True)
    migrated.setdefault("native_notifications", False)
    migrated.setdefault("compact_mode", False)
    migrated.setdefault("window_width", None)
    migrated.setdefault("window_height", None)
    migrated.setdefault("window_maximized", False)
    migrated.setdefault("repo", DEFAULT_REPO)
    migrated["version"] = CONFIG_VERSION
    return migrated


def _coerce_rules(data: dict[str, Any]) -> dict[str, ProviderRules]:
    """Strictly decode persisted v3 ``rules`` (fail closed).

    A persisted v3 file MUST carry ``rules`` as an object containing exactly
    ``claude`` and ``codex``, each being an object with EXACTLY the fields
    ``thresholds``, ``reset_alerts`` and ``error_alerts`` (no missing field
    may silently fall back to a ProviderRules default, no extra field is
    tolerated). Missing, falsy non-object shapes (``[]``, ``false``, ``0``,
    ``""``), empty, incomplete or extra shapes at either level all raise
    ValueError, so ``load_settings`` falls back to a COMPLETE default
    ``Settings`` instance — never to partial preservation with rules
    silently derived from defaults.
    """
    if "rules" not in data:
        raise ValueError("persisted v3 configuration must contain rules")
    raw = data["rules"]
    if not isinstance(raw, dict):
        raise ValueError("rules must be an object")
    if set(raw) != set(VALID_RULE_KEYS):
        raise ValueError("rules must contain exactly the claude and codex providers")
    rules: dict[str, ProviderRules] = {}
    for key in VALID_RULE_KEYS:
        value = raw[key]
        if not isinstance(value, dict):
            raise ValueError(f"rules for {key!r} must be an object")
        if set(value) != set(PROVIDER_RULE_FIELDS):
            raise ValueError(
                f"rules for {key!r} must contain exactly thresholds, reset_alerts and error_alerts"
            )
        rules[key] = ProviderRules(**value)
    return rules


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


def _decode_version(data: dict[str, Any]) -> int:
    """Decode the persisted configuration version with exact semantics.

    Only a non-bool integer ``1``, ``2`` or ``3`` is accepted. The sole
    documented deterministic legacy rule: an ABSENT ``version`` key means a
    versionless v1 file and is migrated. Every other explicit value —
    boolean, float, string, zero, negative, unsupported — raises ValueError
    so ``load_settings`` fails closed to complete defaults WITHOUT any
    partial preservation (an explicit malformed ``version`` is never
    reinterpreted as a legacy version).
    """
    if "version" not in data:
        return 1  # documented legacy rule: versionless files are v1
    version = data["version"]
    if type(version) is not int:
        raise ValueError("configuration version must be a non-bool integer")
    if version not in (1, 2, 3):
        raise ValueError("unsupported configuration version")
    return version


def load_settings() -> Settings:
    path = config_dir() / "config.json"
    if not path.exists():
        return Settings()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return Settings()
        version = _decode_version(data)
        if version == 1:
            data = _migrate_v1_to_v2(data)
        if version <= 2:
            data = _migrate_v2_to_v3(data)
        settings = Settings(**{**data, "rules": _coerce_rules(data)})
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
