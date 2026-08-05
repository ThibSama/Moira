"""Package 7a — integration registry and Hermes inventory tests.

Covers the immutable GTK-free types, the bounded Hermes CLI inventory
probe (fake binaries; strict decoding, malformed/oversized JSON,
unsupported commands, secret/base-URL isolation), the pure snapshot
builder (capability matrix, duplicate collapse, deterministic order,
capability independence, never-zero balance/cost), the bounded
newest-wins coordinator (generations, stale publication, saturation,
shutdown races) and the GTK Integrations page (structure, EN/FR states,
refresh routing, moved agent controls).
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from moira.activity import AgentRuntime
from moira.agent_integration import CapabilityReport
from moira.i18n import tr
from moira.integrations import (
    CAPABILITY_SLUGS,
    MAX_OUTPUT_CHARS,
    CapabilityState,
    HermesInventory,
    IntegrationCoordinator,
    IntegrationSnapshot,
    IntegrationState,
    ModelAssignment,
    ProviderIdentity,
    RuntimeIntegration,
    build_snapshot,
    probe_hermes_inventory,
)
from moira.models import QuotaReading, QuotaStatus, Service

NOW = datetime(2026, 8, 5, 10, 0, 0, tzinfo=UTC)
RESET = NOW + timedelta(days=5)

MODEL_JSON = json.dumps(
    {
        "default": "deepseek-v4-flash",
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com/v1",
    }
)
PROVIDERS_JSON = json.dumps(
    {
        "openrouter": {"base_url": "https://openrouter.ai/api/v1", "default_model": "o3-mini"},
        "local-lab": {
            "base_url": "http://10.0.0.5:8080/v1",
            "api_key": "sk-super-secret",
            "model": "llama-3.1-8b",
        },
    }
)


# ── Fake hermes binary ──────────────────────────────────────────────────────


def _fake_hermes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    version: str = "0.20.0",
    help_text: str = "usage: hermes {chat,model,config,hooks,doctor} ...",
    model_json: str = MODEL_JSON,
    model_rc: int = 0,
    providers_json: str = PROVIDERS_JSON,
    providers_rc: int = 0,
    version_ok: bool = True,
    version_sleep: str = "",
    help_sleep: str = "",
    model_sleep: str = "",
) -> Path:
    binary = tmp_path / "hermes"
    body = "#!/bin/sh\n"
    if version_ok:
        body += (
            'if [ "$1" = "--version" ]; then\n'
            '  if [ -n "$MOIRA_FAKE_VERSION_SLEEP" ]; then sleep "$MOIRA_FAKE_VERSION_SLEEP"; fi\n'
            '  echo "Hermes Agent v$MOIRA_FAKE_VERSION (2026.8.3)"\n'
            "  exit 0\n"
            "fi\n"
        )
    body += (
        'if [ "$1" = "--help" ]; then\n'
        '  if [ -n "$MOIRA_FAKE_HELP_SLEEP" ]; then sleep "$MOIRA_FAKE_HELP_SLEEP"; fi\n'
        '  echo "$MOIRA_FAKE_HELP"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "config" ] && [ "$2" = "get" ] && [ "$3" = "model" ]; then\n'
        '  if [ -n "$MOIRA_FAKE_MODEL_SLEEP" ]; then sleep "$MOIRA_FAKE_MODEL_SLEEP"; fi\n'
        "  printf '%s' \"$MOIRA_FAKE_MODEL\"\n"
        '  exit "${MOIRA_FAKE_MODEL_RC:-0}"\n'
        "fi\n"
        'if [ "$1" = "config" ] && [ "$2" = "get" ] && [ "$3" = "providers" ]; then\n'
        "  printf '%s' \"$MOIRA_FAKE_PROVIDERS\"\n"
        '  exit "${MOIRA_FAKE_PROVIDERS_RC:-0}"\n'
        "fi\n"
        "exit 1\n"
    )
    binary.write_text(body, encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("MOIRA_FAKE_VERSION", version)
    monkeypatch.setenv("MOIRA_FAKE_HELP", help_text)
    monkeypatch.setenv("MOIRA_FAKE_MODEL", model_json)
    monkeypatch.setenv("MOIRA_FAKE_MODEL_RC", str(model_rc))
    monkeypatch.setenv("MOIRA_FAKE_PROVIDERS", providers_json)
    monkeypatch.setenv("MOIRA_FAKE_PROVIDERS_RC", str(providers_rc))
    if version_sleep:
        monkeypatch.setenv("MOIRA_FAKE_VERSION_SLEEP", version_sleep)
    if help_sleep:
        monkeypatch.setenv("MOIRA_FAKE_HELP_SLEEP", help_sleep)
    if model_sleep:
        monkeypatch.setenv("MOIRA_FAKE_MODEL_SLEEP", model_sleep)
    return binary


def _capabilities(**levels: str) -> dict[AgentRuntime, CapabilityReport]:
    return {
        AgentRuntime.CLAUDE: CapabilityReport(levels.get("claude", "full"), ""),
        AgentRuntime.CODEX: CapabilityReport(levels.get("codex", "session_owned"), ""),
        AgentRuntime.HERMES: CapabilityReport(levels.get("hermes", "full"), ""),
    }


def _full_inventory() -> HermesInventory:
    return HermesInventory(
        IntegrationState.AVAILABLE,
        version="0.20.0",
        main_provider="deepseek",
        main_model="deepseek-v4-flash",
        named=(("openrouter", "o3-mini"),),
    )


def _reading(service: Service) -> QuotaReading:
    return QuotaReading(service, "Weekly", 40.0, RESET, NOW, "fixture", QuotaStatus.AVAILABLE)


# ── Types: exact states and fail-closed validation ──────────────────────────


def test_integration_state_exact_values() -> None:
    assert [state.value for state in IntegrationState] == [
        "available",
        "not_configured",
        "not_installed",
        "unsupported",
        "temporarily_unavailable",
        "invalid",
    ]


def test_capability_slugs_closed_set() -> None:
    assert CAPABILITY_SLUGS == ("activity", "quota_percentage", "exact_tokens", "balance", "cost")


def test_types_are_frozen() -> None:
    integration = RuntimeIntegration("hermes", "Hermes", IntegrationState.AVAILABLE)
    provider = ProviderIdentity("deepseek", "deepseek")
    assignment = ModelAssignment(provider, "m", "main", IntegrationState.AVAILABLE)
    capability = CapabilityState("deepseek", "balance", IntegrationState.NOT_CONFIGURED)
    for target in (integration, provider, assignment, capability):
        # frozen + slots raises FrozenInstanceError (an AttributeError) on
        # some Python versions and TypeError on others — both prove immutability.
        with pytest.raises((AttributeError, TypeError)):
            target.slug = "changed"  # type: ignore[misc, union-attr]


@pytest.mark.parametrize(
    "factory",
    [
        lambda: RuntimeIntegration("", "label", IntegrationState.AVAILABLE),
        lambda: RuntimeIntegration("x" * 65, "label", IntegrationState.AVAILABLE),
        lambda: RuntimeIntegration("slug", "", IntegrationState.AVAILABLE),
        lambda: RuntimeIntegration("slug", "label", "available"),  # type: ignore[arg-type]
        lambda: RuntimeIntegration("slug", "label", IntegrationState.AVAILABLE, "d" * 201),
        lambda: ProviderIdentity("", "label"),
        lambda: ProviderIdentity("slug", ""),
        lambda: ProviderIdentity("slug", "x" * 65),
    ],
)
def test_identity_types_fail_closed(factory: Any) -> None:
    with pytest.raises(ValueError):
        factory()


def test_model_assignment_fail_closed() -> None:
    provider = ProviderIdentity("deepseek", "deepseek")
    with pytest.raises(ValueError):
        ModelAssignment(provider, "m", "primary", IntegrationState.AVAILABLE)
    with pytest.raises(ValueError):
        ModelAssignment(provider, "", "main", IntegrationState.AVAILABLE)
    with pytest.raises(ValueError):
        ModelAssignment(provider, "m" * 129, "named", IntegrationState.AVAILABLE)
    with pytest.raises(ValueError):
        ModelAssignment("not-a-provider", "m", "named", IntegrationState.AVAILABLE)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ModelAssignment(provider, "m", "named", "available")  # type: ignore[arg-type]
    # A named entry without a disclosed default model is valid but NOT_CONFIGURED.
    assignment = ModelAssignment(
        provider, "", "named", IntegrationState.NOT_CONFIGURED, "no default model"
    )
    assert assignment.model == ""


def test_capability_state_fail_closed() -> None:
    with pytest.raises(ValueError):
        CapabilityState("deepseek", "tokens", IntegrationState.AVAILABLE)
    with pytest.raises(ValueError):
        CapabilityState("deepseek", "balance", "not_configured")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        CapabilityState("deepseek", "cost", IntegrationState.AVAILABLE, "d" * 201)
    # Capability badges carry no numeric value by construction.
    capability = CapabilityState("deepseek", "balance", IntegrationState.NOT_CONFIGURED)
    for field in (capability.provider, capability.capability, capability.detail):
        assert not isinstance(field, (int, float))
    assert capability.state is IntegrationState.NOT_CONFIGURED


def test_snapshot_fail_closed() -> None:
    with pytest.raises(ValueError):
        IntegrationSnapshot((), (), (), (), datetime.now())  # naive observed_at
    with pytest.raises(ValueError):
        IntegrationSnapshot((), (), (), (), NOW, source="  ")
    with pytest.raises(ValueError):
        IntegrationSnapshot([], (), (), (), NOW)  # type: ignore[arg-type]  # non-tuple collection
    snapshot = IntegrationSnapshot((), (), (), (), NOW)
    assert snapshot.source == "integrations"


def test_hermes_inventory_fail_closed() -> None:
    with pytest.raises(ValueError):
        HermesInventory("available")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        HermesInventory(IntegrationState.AVAILABLE, main_provider="x" * 65)
    with pytest.raises(ValueError):
        HermesInventory(IntegrationState.AVAILABLE, named=(("", "m"),))
    with pytest.raises(ValueError):
        HermesInventory(IntegrationState.AVAILABLE, named=[("a", "m")])  # type: ignore[arg-type]


# ── Hermes inventory probe (fake binaries) ──────────────────────────────────


def test_probe_full_inventory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_hermes(tmp_path, monkeypatch)
    inventory = probe_hermes_inventory()
    assert inventory.state is IntegrationState.AVAILABLE
    assert inventory.version == "0.20.0"
    assert inventory.main_provider == "deepseek"
    assert inventory.main_model == "deepseek-v4-flash"
    assert inventory.named == (
        ("local-lab", "llama-3.1-8b"),
        ("openrouter", "o3-mini"),
    )  # sorted deterministically
    assert inventory.detail == ""


def test_probe_base_url_and_secrets_never_carried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_hermes(tmp_path, monkeypatch)  # MODEL_JSON/PROVIDERS_JSON embed base_url + api_key
    inventory = probe_hermes_inventory()
    blob = " ".join(
        [inventory.version, inventory.main_provider, inventory.main_model, inventory.detail]
        + [f"{slug} {model}" for slug, model in inventory.named]
    )
    for forbidden in (
        "https://",
        "http://",
        "api.deepseek.com",
        "openrouter.ai",
        "10.0.0.5",
        "sk-super-secret",
        "base_url",
        "api_key",
        "extra_headers",
        "ssl_ca_cert",
        "auth.json",
        ".env",
        "/home/",
    ):
        assert forbidden not in blob, forbidden


def test_probe_absent_binary_is_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))  # empty PATH: no hermes
    inventory = probe_hermes_inventory()
    assert inventory.state is IntegrationState.NOT_INSTALLED
    assert inventory.detail == "hermes CLI not found"


def test_probe_unknown_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_hermes(tmp_path, monkeypatch, version="??")
    inventory = probe_hermes_inventory()
    assert inventory.state is IntegrationState.UNSUPPORTED
    assert inventory.detail == "version unknown"


def test_probe_version_probe_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_hermes(tmp_path, monkeypatch, version_sleep="5")
    inventory = probe_hermes_inventory(timeout=0.5)
    assert inventory.state is IntegrationState.TEMPORARILY_UNAVAILABLE
    assert inventory.detail == "version probe failed"


def test_probe_help_without_config_surface(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_hermes(tmp_path, monkeypatch, help_text="usage: hermes {chat,hooks} ...")
    inventory = probe_hermes_inventory()
    assert inventory.state is IntegrationState.UNSUPPORTED
    assert inventory.detail == "config surface unsupported"


def test_probe_help_probe_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_hermes(tmp_path, monkeypatch, help_sleep="5")
    inventory = probe_hermes_inventory(timeout=0.5)
    assert inventory.state is IntegrationState.TEMPORARILY_UNAVAILABLE
    assert inventory.detail == "help probe failed"


def test_probe_config_get_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_hermes(tmp_path, monkeypatch, model_rc=2)
    inventory = probe_hermes_inventory()
    assert inventory.state is IntegrationState.UNSUPPORTED
    assert inventory.detail == "config get unsupported"


def test_probe_config_probe_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_hermes(tmp_path, monkeypatch, model_sleep="5")
    inventory = probe_hermes_inventory(timeout=0.5)
    assert inventory.state is IntegrationState.TEMPORARILY_UNAVAILABLE
    assert inventory.detail == "config probe failed"


def test_probe_malformed_model_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_hermes(tmp_path, monkeypatch, model_json="{not json")
    inventory = probe_hermes_inventory()
    assert inventory.state is IntegrationState.INVALID
    assert inventory.detail == "config output malformed"


def test_probe_oversized_model_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_hermes(tmp_path, monkeypatch, model_json="x" * (MAX_OUTPUT_CHARS + 1))
    inventory = probe_hermes_inventory()
    assert inventory.state is IntegrationState.INVALID
    assert inventory.detail == "config output oversized"


def test_probe_non_dict_model_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_hermes(tmp_path, monkeypatch, model_json='["a", "b"]')
    inventory = probe_hermes_inventory()
    assert inventory.state is IntegrationState.INVALID
    assert inventory.detail == "config output malformed"


def test_probe_incomplete_model_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_hermes(tmp_path, monkeypatch, model_json=json.dumps({"default": "m"}))
    inventory = probe_hermes_inventory()
    assert inventory.state is IntegrationState.INVALID
    assert inventory.detail == "config output incomplete"


def test_probe_non_string_model_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_hermes(tmp_path, monkeypatch, model_json=json.dumps({"default": 42, "provider": "x"}))
    inventory = probe_hermes_inventory()
    assert inventory.state is IntegrationState.INVALID
    assert inventory.detail == "config output incomplete"


def test_probe_oversized_model_dropped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_hermes(
        tmp_path,
        monkeypatch,
        model_json=json.dumps({"default": "m" * 129, "provider": "deepseek"}),
    )
    inventory = probe_hermes_inventory()
    assert inventory.state is IntegrationState.INVALID
    assert inventory.detail == "config output incomplete"


def test_probe_named_providers_rejected_keeps_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_hermes(tmp_path, monkeypatch, providers_rc=2)
    inventory = probe_hermes_inventory()
    assert inventory.state is IntegrationState.AVAILABLE
    assert inventory.main_model == "deepseek-v4-flash"
    assert inventory.named == ()


def test_probe_named_providers_malformed_keeps_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_hermes(tmp_path, monkeypatch, providers_json="{broken")
    inventory = probe_hermes_inventory()
    assert inventory.state is IntegrationState.AVAILABLE
    assert inventory.named == ()


def test_probe_named_entry_without_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_hermes(
        tmp_path,
        monkeypatch,
        providers_json=json.dumps({"bare": {"base_url": "https://example.invalid"}}),
    )
    inventory = probe_hermes_inventory()
    assert inventory.named == (("bare", ""),)


def test_probe_named_entry_uses_default_model_over_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_hermes(
        tmp_path,
        monkeypatch,
        providers_json=json.dumps(
            {"p1": {"model": "fallback-model", "default_model": "default-model"}}
        ),
    )
    inventory = probe_hermes_inventory()
    assert inventory.named == (("p1", "default-model"),)


def test_probe_named_oversized_slugs_and_models_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_hermes(
        tmp_path,
        monkeypatch,
        providers_json=json.dumps(
            {
                "x" * 65: {"default_model": "m"},
                "ok": {"default_model": "m" * 129},
                "good": {"default_model": "fine"},
                "": {"default_model": "empty"},
                42: {"default_model": "number"},
            }
        ),
    )
    inventory = probe_hermes_inventory()
    # Oversized slugs/models are dropped; an entry whose model is dropped
    # stays as a provider with no disclosed default model ("").
    assert inventory.named == (("42", "number"), ("good", "fine"), ("ok", ""))


def test_probe_models_list_never_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_hermes(
        tmp_path,
        monkeypatch,
        providers_json=json.dumps(
            {"multi": {"models": ["a", "b", "c"], "default_model": "chosen"}}
        ),
    )
    inventory = probe_hermes_inventory()
    assert inventory.named == (("multi", "chosen"),)


def test_live_probe_real_hermes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Live probe: skip only when the real hermes binary is absent."""
    import shutil

    if shutil.which("hermes") is None:
        pytest.skip("hermes binary absent")
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    inventory = probe_hermes_inventory()
    assert inventory.state is IntegrationState.AVAILABLE
    assert inventory.version != ""
    assert inventory.main_provider != "" and inventory.main_model != ""
    blob = " ".join(
        [inventory.version, inventory.main_provider, inventory.main_model, inventory.detail]
        + [f"{slug} {model}" for slug, model in inventory.named]
    )
    for forbidden in ("https://", "base_url", "api_key", "sk-", "auth.json", ".env", "/home/"):
        assert forbidden not in blob, forbidden


# ── Snapshot builder ────────────────────────────────────────────────────────


def _matrix(snapshot: IntegrationSnapshot) -> dict[tuple[str, str], Any]:
    return {(c.provider, c.capability): c for c in snapshot.capabilities}


def test_snapshot_full_capability_matrix() -> None:
    snapshot = build_snapshot(
        hermes=_full_inventory(),
        capabilities=_capabilities(),
        quota_readings=[_reading(Service.CLAUDE), _reading(Service.CODEX)],
        collect_claude=True,
        collect_codex=True,
    )
    caps = _matrix(snapshot)
    # Claude: percentage-only contract.
    assert caps[("claude", "activity")].state is IntegrationState.AVAILABLE
    assert caps[("claude", "quota_percentage")].state is IntegrationState.AVAILABLE
    assert caps[("claude", "exact_tokens")].state is IntegrationState.UNSUPPORTED
    assert caps[("claude", "exact_tokens")].detail == "Claude remains percentage-only"
    assert caps[("claude", "balance")].state is IntegrationState.UNSUPPORTED
    assert caps[("claude", "cost")].state is IntegrationState.UNSUPPORTED
    # Codex: exact account-usage contract kept.
    assert caps[("codex", "activity")].state is IntegrationState.AVAILABLE
    assert caps[("codex", "quota_percentage")].state is IntegrationState.AVAILABLE
    assert caps[("codex", "exact_tokens")].state is IntegrationState.AVAILABLE
    assert caps[("codex", "balance")].state is IntegrationState.UNSUPPORTED
    assert caps[("codex", "cost")].state is IntegrationState.UNSUPPORTED
    # Hermes runtime: activity only; no quota/token surface.
    assert caps[("hermes", "activity")].state is IntegrationState.AVAILABLE
    assert caps[("hermes", "quota_percentage")].state is IntegrationState.UNSUPPORTED
    assert caps[("hermes", "exact_tokens")].state is IntegrationState.UNSUPPORTED
    assert caps[("hermes", "balance")].state is IntegrationState.UNSUPPORTED
    # Discovered providers: balance/cost NOT_CONFIGURED and deferred — the
    # /user/balance endpoint is never called in 7a.
    for slug in ("deepseek", "openrouter"):
        assert caps[(slug, "activity")].state is IntegrationState.UNSUPPORTED
        assert caps[(slug, "quota_percentage")].state is IntegrationState.UNSUPPORTED
        assert caps[(slug, "exact_tokens")].state is IntegrationState.UNSUPPORTED
        assert caps[(slug, "balance")].state is IntegrationState.NOT_CONFIGURED
        assert caps[(slug, "balance")].detail == "deferred"
        assert caps[(slug, "cost")].state is IntegrationState.NOT_CONFIGURED
        assert caps[(slug, "cost")].detail == "deferred"
    assert [p.slug for p in snapshot.providers] == [
        "claude",
        "codex",
        "hermes",
        "deepseek",
        "openrouter",
    ]


def test_snapshot_activity_mapping() -> None:
    cases = {
        "full": IntegrationState.AVAILABLE,
        "session_owned": IntegrationState.AVAILABLE,
        "completion_only": IntegrationState.TEMPORARILY_UNAVAILABLE,
        "not_installed": IntegrationState.NOT_INSTALLED,
        "unsupported": IntegrationState.UNSUPPORTED,
    }
    for level, expected in cases.items():
        snapshot = build_snapshot(
            hermes=_full_inventory(),
            capabilities=_capabilities(claude=level),
            quota_readings=(),
        )
        caps = _matrix(snapshot)
        assert caps[("claude", "activity")].state is expected, level
        runtime = next(r for r in snapshot.runtimes if r.slug == "claude")
        assert runtime.state is expected, level


def test_snapshot_quota_states_follow_toggles_and_readings() -> None:
    # Disabled collection → NOT_CONFIGURED with a sanitized detail.
    snapshot = build_snapshot(
        hermes=_full_inventory(),
        capabilities=_capabilities(),
        quota_readings=(),
        collect_claude=False,
        collect_codex=False,
    )
    caps = _matrix(snapshot)
    assert caps[("claude", "quota_percentage")].state is IntegrationState.NOT_CONFIGURED
    assert caps[("claude", "quota_percentage")].detail == "collection disabled"
    assert caps[("codex", "quota_percentage")].state is IntegrationState.NOT_CONFIGURED
    # Enabled but no reading yet → TEMPORARILY_UNAVAILABLE.
    snapshot = build_snapshot(
        hermes=_full_inventory(),
        capabilities=_capabilities(),
        quota_readings=(),
        collect_claude=True,
        collect_codex=True,
    )
    caps = _matrix(snapshot)
    assert caps[("claude", "quota_percentage")].state is IntegrationState.TEMPORARILY_UNAVAILABLE
    assert caps[("claude", "quota_percentage")].detail == "no reading yet"
    # Enabled with collector readings → AVAILABLE.
    snapshot = build_snapshot(
        hermes=_full_inventory(),
        capabilities=_capabilities(),
        quota_readings=[_reading(Service.CLAUDE), _reading(Service.CODEX)],
        collect_claude=True,
        collect_codex=True,
    )
    caps = _matrix(snapshot)
    assert caps[("claude", "quota_percentage")].state is IntegrationState.AVAILABLE
    assert caps[("claude", "quota_percentage")].detail == ""
    # Independence: Claude disabled never affects Codex.
    snapshot = build_snapshot(
        hermes=_full_inventory(),
        capabilities=_capabilities(),
        quota_readings=[_reading(Service.CODEX)],
        collect_claude=False,
        collect_codex=True,
    )
    caps = _matrix(snapshot)
    assert caps[("claude", "quota_percentage")].state is IntegrationState.NOT_CONFIGURED
    assert caps[("codex", "quota_percentage")].state is IntegrationState.AVAILABLE


def test_snapshot_assignments_main_and_named() -> None:
    snapshot = build_snapshot(
        hermes=_full_inventory(), capabilities=_capabilities(), quota_readings=()
    )
    assigns = [(a.provider.slug, a.model, a.role, a.state) for a in snapshot.assignments]
    assert assigns == [
        ("deepseek", "deepseek-v4-flash", "main", IntegrationState.AVAILABLE),
        ("openrouter", "o3-mini", "named", IntegrationState.AVAILABLE),
    ]


def test_snapshot_named_without_model_is_not_configured() -> None:
    inventory = HermesInventory(
        IntegrationState.AVAILABLE,
        main_provider="deepseek",
        main_model="deepseek-v4-flash",
        named=(("bare", ""),),
    )
    snapshot = build_snapshot(hermes=inventory, capabilities=_capabilities(), quota_readings=())
    assignment = next(a for a in snapshot.assignments if a.provider.slug == "bare")
    assert assignment.role == "named"
    assert assignment.model == ""
    assert assignment.state is IntegrationState.NOT_CONFIGURED
    assert assignment.detail == "no default model"


def test_snapshot_collapses_duplicates_deterministically() -> None:
    inventory = HermesInventory(
        IntegrationState.AVAILABLE,
        main_provider="deepseek",
        main_model="m-main",
        named=(
            ("deepseek", "m-named"),  # same provider as main → collapses to main
            ("claude", "m-other"),  # reserved runtime slug → dropped
            ("hermes", "m-hermes"),  # reserved runtime slug → dropped
        ),
    )
    snapshot = build_snapshot(hermes=inventory, capabilities=_capabilities(), quota_readings=())
    assigns = [(a.provider.slug, a.model, a.role) for a in snapshot.assignments]
    assert assigns == [("deepseek", "m-main", "main")]
    assert [p.slug for p in snapshot.providers] == ["claude", "codex", "hermes", "deepseek"]


def test_snapshot_unavailable_inventory_has_no_assignments() -> None:
    for state, detail in (
        (IntegrationState.NOT_INSTALLED, "hermes CLI not found"),
        (IntegrationState.UNSUPPORTED, "version unknown"),
        (IntegrationState.INVALID, "config output malformed"),
        (IntegrationState.TEMPORARILY_UNAVAILABLE, "config probe failed"),
    ):
        inventory = HermesInventory(state, detail=detail)
        snapshot = build_snapshot(hermes=inventory, capabilities=_capabilities(), quota_readings=())
        assert snapshot.assignments == ()
        assert [p.slug for p in snapshot.providers] == ["claude", "codex", "hermes"]
        hermes_runtime = next(r for r in snapshot.runtimes if r.slug == "hermes")
        assert hermes_runtime.state is IntegrationState.AVAILABLE  # activity is independent


def test_snapshot_missing_capability_reports_checking() -> None:
    snapshot = build_snapshot(hermes=_full_inventory(), capabilities={}, quota_readings=())
    for runtime in snapshot.runtimes:
        assert runtime.state is IntegrationState.TEMPORARILY_UNAVAILABLE
        assert runtime.detail == "checking"
    caps = _matrix(snapshot)
    for slug in ("claude", "codex", "hermes"):
        assert caps[(slug, "activity")].state is IntegrationState.TEMPORARILY_UNAVAILABLE
        assert caps[(slug, "activity")].detail == "checking"


def test_snapshot_balance_cost_never_available_or_zero() -> None:
    snapshot = build_snapshot(
        hermes=_full_inventory(), capabilities=_capabilities(), quota_readings=()
    )
    for capability in snapshot.capabilities:
        if capability.capability in ("balance", "cost"):
            assert capability.state in (
                IntegrationState.NOT_CONFIGURED,
                IntegrationState.UNSUPPORTED,
            )
            assert not any(ch.isdigit() for ch in capability.detail)
    # No numeric field exists on any badge by construction.
    for capability in snapshot.capabilities:
        for field in (capability.provider, capability.capability, capability.detail):
            assert not isinstance(field, (int, float))
        assert isinstance(capability.state, IntegrationState)


def test_snapshot_end_to_end_no_secret_leakage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_hermes(tmp_path, monkeypatch)
    inventory = probe_hermes_inventory()
    snapshot = build_snapshot(hermes=inventory, capabilities=_capabilities(), quota_readings=())
    blob = repr(snapshot) + repr(inventory)
    for forbidden in (
        "https://",
        "http://",
        "api.deepseek.com",
        "openrouter.ai",
        "10.0.0.5",
        "sk-super-secret",
        "base_url",
        "api_key",
        "extra_headers",
        "ssl_ca_cert",
        "auth.json",
        ".env",
        "/home/",
    ):
        assert forbidden not in blob, forbidden


def test_snapshot_observed_at_and_validation() -> None:
    snapshot = build_snapshot(
        hermes=_full_inventory(), capabilities=_capabilities(), quota_readings=(), now=NOW
    )
    assert snapshot.observed_at == NOW
    assert snapshot.source == "integrations"
    with pytest.raises(ValueError):
        build_snapshot(hermes="not-an-inventory", capabilities={}, quota_readings=())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        build_snapshot(
            hermes=_full_inventory(),
            capabilities={},
            quota_readings=(),
            now=datetime.now(),  # naive
        )


# ── Coordinator: bounded newest-wins with generations ───────────────────────


def _run_in_thread(fn: Any) -> None:
    thread = threading.Thread(target=fn, daemon=True)
    thread.start()


def _wait_for(predicate: Any, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _make_coordinator(probe: Any, published: list[HermesInventory]) -> IntegrationCoordinator:
    coordinator = IntegrationCoordinator(
        submit=_run_in_thread, probe=probe, publish=published.append
    )
    coordinator.start()
    return coordinator


def test_coordinator_publishes_newest_result() -> None:
    published: list[HermesInventory] = []
    coordinator = _make_coordinator(
        lambda: HermesInventory(IntegrationState.AVAILABLE, main_provider="p", main_model="m"),
        published,
    )
    assert coordinator.request_refresh() is True
    assert _wait_for(lambda: len(published) == 1)
    assert published[0].main_model == "m"


def test_coordinator_stale_result_never_publishes() -> None:
    published: list[HermesInventory] = []
    release_first = threading.Event()
    started_first = threading.Event()
    done_first = threading.Event()

    def probe_first() -> HermesInventory:
        started_first.set()
        try:
            assert release_first.wait(timeout=5)
            return HermesInventory(IntegrationState.AVAILABLE, main_provider="gen1")
        finally:
            done_first.set()

    def probe_second() -> HermesInventory:
        return HermesInventory(IntegrationState.AVAILABLE, main_provider="gen2")

    probes = iter([probe_first, probe_second])
    coordinator = _make_coordinator(lambda: next(probes)(), published)
    assert coordinator.request_refresh() is True
    assert started_first.wait(timeout=5)
    assert coordinator.request_refresh() is True  # gen2 parked as pending
    release_first.set()
    assert done_first.wait(timeout=5)
    # gen1 completed while gen2 was pending → stale, never published; gen2
    # is promoted, runs and publishes.
    assert _wait_for(lambda: len(published) == 1)
    assert published[0].main_provider == "gen2"


def test_coordinator_saturation_replaces_pending_newest_wins() -> None:
    published: list[HermesInventory] = []
    release_first = threading.Event()
    started_first = threading.Event()

    def probe_first() -> HermesInventory:
        started_first.set()
        assert release_first.wait(timeout=5)
        return HermesInventory(IntegrationState.AVAILABLE, main_provider="gen1")

    def probe_third() -> HermesInventory:
        return HermesInventory(IntegrationState.AVAILABLE, main_provider="gen3")

    probes = iter([probe_first, probe_third])
    coordinator = _make_coordinator(lambda: next(probes)(), published)
    assert coordinator.request_refresh() is True  # gen1 in flight
    assert started_first.wait(timeout=5)
    assert coordinator.request_refresh() is True  # gen2 parked
    assert coordinator.request_refresh() is False  # gen3 replaces gen2 (saturated)
    release_first.set()
    assert _wait_for(lambda: len(published) == 1)
    assert published[0].main_provider == "gen3"


def test_coordinator_rejects_after_shutdown() -> None:
    published: list[HermesInventory] = []
    coordinator = _make_coordinator(lambda: HermesInventory(IntegrationState.AVAILABLE), published)
    coordinator.shutdown()
    assert coordinator.request_refresh() is False
    coordinator.shutdown()  # idempotent
    assert coordinator.request_refresh() is False


def test_coordinator_request_before_start_rejected() -> None:
    coordinator = IntegrationCoordinator(
        submit=_run_in_thread,
        probe=lambda: HermesInventory(IntegrationState.AVAILABLE),
        publish=lambda _inventory: None,
    )
    assert coordinator.request_refresh() is False
    coordinator.start()
    assert coordinator.request_refresh() is True


def test_coordinator_in_flight_result_never_publishes_after_shutdown() -> None:
    published: list[HermesInventory] = []
    release_first = threading.Event()
    started_first = threading.Event()
    done_first = threading.Event()

    def probe_first() -> HermesInventory:
        started_first.set()
        try:
            assert release_first.wait(timeout=5)
            return HermesInventory(IntegrationState.AVAILABLE, main_provider="gen1")
        finally:
            done_first.set()

    coordinator = _make_coordinator(probe_first, published)
    assert coordinator.request_refresh() is True
    assert started_first.wait(timeout=5)
    coordinator.shutdown()
    release_first.set()
    assert done_first.wait(timeout=5)
    assert published == []


def test_coordinator_pending_discarded_on_shutdown() -> None:
    published: list[HermesInventory] = []
    release_first = threading.Event()
    started_first = threading.Event()

    def probe_first() -> HermesInventory:
        started_first.set()
        assert release_first.wait(timeout=5)
        return HermesInventory(IntegrationState.AVAILABLE, main_provider="gen1")

    def probe_never() -> HermesInventory:
        raise AssertionError("pending probe must never run")

    probes = iter([probe_first, probe_never])
    coordinator = _make_coordinator(lambda: next(probes)(), published)
    assert coordinator.request_refresh() is True
    assert started_first.wait(timeout=5)
    assert coordinator.request_refresh() is True  # parked
    coordinator.shutdown()  # pending discarded
    release_first.set()
    assert _wait_for(lambda: len(published) == 0, timeout=2.0)
    assert published == []


def test_coordinator_probe_exception_is_sanitized() -> None:
    published: list[HermesInventory] = []

    def bad_probe() -> HermesInventory:
        raise RuntimeError("boom super-secret")

    coordinator = _make_coordinator(bad_probe, published)
    assert coordinator.request_refresh() is True
    assert _wait_for(lambda: len(published) == 1)
    inventory = published[0]
    assert inventory.state is IntegrationState.TEMPORARILY_UNAVAILABLE
    assert inventory.detail == "inventory probe failed"
    assert "boom" not in inventory.detail and "secret" not in inventory.detail


# ── GTK Integrations page (skip-guarded) ────────────────────────────────────


def _page(**kwargs: Any) -> Any:
    from moira.integrations_page import IntegrationsPage

    try:
        return IntegrationsPage(**kwargs)
    except Exception as exc:  # headless environments
        pytest.skip(f"GTK display unavailable: {exc}")


def _all_texts(widget: Any) -> list[str]:
    texts: list[str] = []
    stack = [widget]
    while stack:
        current = stack.pop()
        if hasattr(current, "get_text"):
            text = current.get_text()
            if text:
                texts.append(text)
        child = current.get_first_child()
        while child is not None:
            stack.append(child)
            child = child.get_next_sibling()
    return texts


def _render_snapshot() -> IntegrationSnapshot:
    return build_snapshot(
        hermes=_full_inventory(),
        capabilities=_capabilities(),
        quota_readings=[_reading(Service.CLAUDE), _reading(Service.CODEX)],
        collect_claude=True,
        collect_codex=True,
        now=NOW,
    )


def _english_locale() -> Any:
    return patch.dict(
        os.environ, {"LANG": "en_US.UTF-8", "LC_ALL": "", "LC_MESSAGES": ""}, clear=False
    )


def test_page_structure_and_snapshot_rendering_en() -> None:
    with _english_locale():
        page = _page()
        page.render_snapshot(_render_snapshot())
        texts = _all_texts(page)
        assert tr("Agents") in texts
        assert tr("Providers and models") in texts
        assert tr("Refresh") in texts
        # Provider rows and independent badges.
        assert "Claude Code" in texts
        assert "Codex CLI" in texts
        assert "Hermes" in texts
        assert "deepseek" in texts
        assert "openrouter" in texts
        assert f"deepseek-v4-flash ({tr('Main')})" in texts
        assert f"o3-mini ({tr('Named')})" in texts
        assert f"{tr('Quota percentage')}: {tr('Available')}" in texts
        assert (
            f"{tr('Exact tokens')}: {tr('Unsupported')} ({tr('Claude remains percentage-only')})"
            in texts
        )
        assert f"{tr('Balance')}: {tr('Not configured')} ({tr('deferred')})" in texts
        # Unknown cost/balance is a state, never a zero value.
        assert "0" not in [t for t in texts if "Balance" in t or "Cost" in t]
        local = NOW.astimezone()
        assert f"{tr('Last refresh: ')}{local:%H:%M:%S} · integrations" in texts


def test_page_structure_and_snapshot_rendering_fr() -> None:
    with patch.dict(
        os.environ,
        {"LANG": "fr_FR.UTF-8", "LC_ALL": "", "LC_MESSAGES": ""},
        clear=False,
    ):
        page = _page()
        page.render_snapshot(_render_snapshot())
        texts = _all_texts(page)
        assert "Fournisseurs et modèles" in texts
        assert tr("Integrations") == "Intégrations"
        assert f"deepseek-v4-flash ({tr('Principal')})" in texts
        assert "Solde: Non configuré (différé)" in texts
        assert "Pourcentage de quota: Disponible" in texts
        assert "Jetons exacts: Non pris en charge (Claude reste en pourcentage uniquement)" in texts


def test_page_refresh_routing_visibility_and_shutdown() -> None:
    calls: list[str] = []
    page = _page(on_visible_refresh=lambda: calls.append("refresh"))
    assert not page.is_visible_page()
    page.on_hidden()
    assert calls == []
    page.on_visible()
    assert page.is_visible_page()
    assert calls == ["refresh"]
    page.refresh_button.emit("clicked")
    assert calls == ["refresh", "refresh"]
    page.on_hidden()
    assert not page.is_visible_page()
    page.shutdown()
    page.on_visible()
    page.refresh_button.emit("clicked")
    assert calls == ["refresh", "refresh"]


def test_page_no_assignments_note() -> None:
    snapshot = build_snapshot(
        hermes=HermesInventory(IntegrationState.NOT_INSTALLED, detail="hermes CLI not found"),
        capabilities=_capabilities(),
        quota_readings=(),
        now=NOW,
    )
    page = _page()
    page.render_snapshot(snapshot)
    texts = _all_texts(page)
    assert tr("No model assignments discovered.") in texts
    # Runtime rows still render with an em-dash placeholder.
    assert "Claude Code" in texts and "Codex CLI" in texts and "Hermes" in texts


def test_agents_controls_moved_from_settings_to_integrations(
    tmp_path: Path,
) -> None:
    """The Set up / Remove / Test controls now live in the Integrations
    page's Agents section and no longer appear in the Settings view."""
    from moira.persistence import load_settings
    from moira.ui import MainWindow

    try:
        with patch.dict(
            os.environ,
            {"XDG_CONFIG_HOME": str(tmp_path), "XDG_STATE_HOME": str(tmp_path)},
            clear=False,
        ):
            window = MainWindow.__new__(MainWindow)
            window.settings = load_settings()
            settings_page = window._settings_page()
            page = window._integrations_page_content()
    except Exception as exc:  # headless environments
        pytest.skip(f"GTK display unavailable: {exc}")

    settings_texts = _all_texts(settings_page)
    assert tr("Set up") not in settings_texts
    assert tr("Remove") not in settings_texts
    assert tr("Test") not in settings_texts
    assert tr("Agent integrations") not in settings_texts

    page_texts = _all_texts(page)
    assert page_texts.count(tr("Set up")) == 3
    assert page_texts.count(tr("Remove")) == 3
    assert page_texts.count(tr("Test")) == 3
    assert {str(runtime.value) for runtime in window._integration_status} == {
        "claude",
        "codex",
        "hermes",
    }
    # The desktop shortcut controls stay in Settings (untouched behavior).
    assert tr("Create desktop shortcut") in settings_texts
