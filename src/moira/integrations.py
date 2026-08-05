"""Package 7a — provider-neutral integration registry and Hermes inventory.

GTK-free domain: immutable types for runtime integrations, provider
identities, model assignments, capability states and the integration
snapshot, plus the bounded Hermes CLI inventory probe (version/help first,
then ``config get`` with strict size-bounded JSON decoding) and the
bounded newest-wins ``IntegrationCoordinator``.

Privacy contract: the probe decodes only the documented ``default`` and
``provider`` fields of ``hermes config get model --json`` and the
provider slugs plus scalar ``default_model``/``model`` of
``hermes config get providers --json``. Base URLs, credentials, paths,
raw output and exceptions are never carried, stored or rendered.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from .activity import AgentRuntime
from .agent_integration import CapabilityReport
from .models import QuotaReading, utc_now

#: Bounded timeout for each Hermes probe subprocess (seconds).
PROBE_TIMEOUT = 10
#: Bounded stdout budget for one probe step (characters).
MAX_OUTPUT_CHARS = 64 * 1024
#: Bounded provider/version slug length.
MAX_SLUG_LENGTH = 64
#: Bounded model identifier length.
MAX_MODEL_LENGTH = 128
#: Bounded sanitized detail length.
MAX_DETAIL_LENGTH = 200

_VERSION_RE = re.compile(r"Hermes Agent v(\d+)\.(\d+)\.(\d+)")

#: Closed set of capability slugs carried by the snapshot.
CAPABILITY_SLUGS = ("activity", "quota_percentage", "exact_tokens", "balance", "cost")
#: Closed set of assignment roles.
ASSIGNMENT_ROLES = ("main", "named")
#: Runtime slugs reserved for the three Moira-monitored agents.
_RUNTIME_SLUGS = ("claude", "codex", "hermes")


class IntegrationState(StrEnum):
    """Exact closed availability set for integrations, assignments and badges.

    The enum values are the only strings that may flow through the
    registry, so arbitrary free-form availability strings are impossible.
    """

    AVAILABLE = "available"
    NOT_CONFIGURED = "not_configured"
    NOT_INSTALLED = "not_installed"
    UNSUPPORTED = "unsupported"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
    INVALID = "invalid"


def _bounded(value: str, limit: int) -> str:
    """Strip and bound a label; empty when blank or oversized (fail closed)."""
    value = value.strip()
    if not value or len(value) > limit:
        return ""
    return value


def _require_string(value: object, name: str, limit: int, *, allow_empty: bool = False) -> None:
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{name} must be a string bounded to {limit} characters")
    if not allow_empty and not value.strip():
        raise ValueError(f"{name} must not be empty")


@dataclass(frozen=True, slots=True)
class RuntimeIntegration:
    """One agent runtime (Claude Code, Codex CLI, Hermes) and its state."""

    slug: str
    label: str
    state: IntegrationState
    detail: str = ""

    def __post_init__(self) -> None:
        _require_string(self.slug, "slug", MAX_SLUG_LENGTH)
        _require_string(self.label, "label", MAX_SLUG_LENGTH)
        if not isinstance(self.state, IntegrationState):
            raise ValueError("state must be an IntegrationState value")
        _require_string(self.detail, "detail", MAX_DETAIL_LENGTH, allow_empty=True)


@dataclass(frozen=True, slots=True)
class ProviderIdentity:
    """A provider identity with a stable slug and a bounded label."""

    slug: str
    label: str

    def __post_init__(self) -> None:
        _require_string(self.slug, "slug", MAX_SLUG_LENGTH)
        _require_string(self.label, "label", MAX_SLUG_LENGTH)


@dataclass(frozen=True, slots=True)
class ModelAssignment:
    """A provider→model assignment with a role (main or named)."""

    provider: ProviderIdentity
    model: str
    role: str
    state: IntegrationState
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.provider, ProviderIdentity):
            raise ValueError("provider must be a ProviderIdentity value")
        _require_string(self.model, "model", MAX_MODEL_LENGTH, allow_empty=True)
        if self.role not in ASSIGNMENT_ROLES:
            raise ValueError(f"role must be one of {ASSIGNMENT_ROLES}")
        if self.role == "main" and not self.model.strip():
            raise ValueError("main assignments require a model")
        if not isinstance(self.state, IntegrationState):
            raise ValueError("state must be an IntegrationState value")
        _require_string(self.detail, "detail", MAX_DETAIL_LENGTH, allow_empty=True)


@dataclass(frozen=True, slots=True)
class CapabilityState:
    """One independent capability badge for one provider.

    ``capability`` is a closed slug from ``CAPABILITY_SLUGS``; the state
    is an exact ``IntegrationState``. No numeric value is ever carried:
    unknown cost/balance can never be rendered as zero.
    """

    provider: str
    capability: str
    state: IntegrationState
    detail: str = ""

    def __post_init__(self) -> None:
        _require_string(self.provider, "provider", MAX_SLUG_LENGTH)
        if self.capability not in CAPABILITY_SLUGS:
            raise ValueError(f"capability must be one of {CAPABILITY_SLUGS}")
        if not isinstance(self.state, IntegrationState):
            raise ValueError("state must be an IntegrationState value")
        _require_string(self.detail, "detail", MAX_DETAIL_LENGTH, allow_empty=True)


@dataclass(frozen=True, slots=True)
class IntegrationSnapshot:
    """Immutable render model for the Integrations page.

    Built from the accepted sources only: ``agent_integration`` capability
    reports for activity, collector/history quota readings for Claude and
    Codex quota/token capabilities, and the documented installed Hermes
    CLI surfaces for provider/model inventory. ``observed_at`` is
    timezone-aware; ``source`` is a stable non-empty slug.
    """

    runtimes: tuple[RuntimeIntegration, ...]
    providers: tuple[ProviderIdentity, ...]
    assignments: tuple[ModelAssignment, ...]
    capabilities: tuple[CapabilityState, ...]
    observed_at: datetime
    source: str = "integrations"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.runtimes, tuple)
            or not isinstance(self.providers, tuple)
            or not isinstance(self.assignments, tuple)
            or not isinstance(self.capabilities, tuple)
        ):
            raise ValueError("snapshot collections must be tuples")
        if not self.source.strip():
            raise ValueError("source must not be empty")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class HermesInventory:
    """Decoded, sanitized result of the Hermes CLI inventory probe.

    ``main_provider``/``main_model`` come from ``config get model``;
    ``named`` holds (provider slug, default model or '') pairs from
    ``config get providers``, sorted deterministically. Base URLs,
    credentials, paths and raw output are never carried.
    """

    state: IntegrationState
    version: str = ""
    main_provider: str = ""
    main_model: str = ""
    named: tuple[tuple[str, str], ...] = ()
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.state, IntegrationState):
            raise ValueError("state must be an IntegrationState value")
        _require_string(self.version, "version", MAX_SLUG_LENGTH, allow_empty=True)
        _require_string(self.main_provider, "main_provider", MAX_SLUG_LENGTH, allow_empty=True)
        _require_string(self.main_model, "main_model", MAX_MODEL_LENGTH, allow_empty=True)
        _require_string(self.detail, "detail", MAX_DETAIL_LENGTH, allow_empty=True)
        if not isinstance(self.named, tuple):
            raise ValueError("named must be a tuple")
        for slug, model in self.named:
            _require_string(slug, "named provider slug", MAX_SLUG_LENGTH)
            _require_string(model, "named provider model", MAX_MODEL_LENGTH, allow_empty=True)


# ── Hermes CLI inventory probe (bounded, version/help first) ────────────────


def _run_bounded(args: list[str], timeout: float) -> subprocess.CompletedProcess[str] | None:
    """Run one bounded subprocess; None on transport failures."""
    try:
        return subprocess.run(
            args,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _decode_object(raw: str) -> dict[str, Any] | None:
    """Strict, size-bounded JSON object decode; None on any failure."""
    if len(raw) > MAX_OUTPUT_CHARS:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _probe_named_providers(executable: str, timeout: float) -> tuple[tuple[str, str], ...]:
    """Decode ``config get providers --json`` (auxiliary surface).

    Only the provider slug and the scalar ``default_model``/``model`` are
    extracted. Base URLs, credentials, extra headers, TLS material and the
    ``models`` list are never read into the registry. Any failure of this
    auxiliary surface yields no named entries (truthful) without
    invalidating the main inventory.
    """
    providers_probe = _run_bounded([executable, "config", "get", "providers", "--json"], timeout)
    if providers_probe is None or providers_probe.returncode != 0:
        return ()
    parsed = _decode_object(providers_probe.stdout or "")
    if parsed is None:
        return ()
    named: list[tuple[str, str]] = []
    for slug, entry in parsed.items():
        slug_bounded = _bounded(slug, MAX_SLUG_LENGTH) if isinstance(slug, str) else ""
        if not slug_bounded or not isinstance(entry, dict):
            continue
        model_value = entry.get("default_model")
        if not isinstance(model_value, str) or not model_value.strip():
            model_value = entry.get("model")
        model_bounded = (
            _bounded(model_value, MAX_MODEL_LENGTH) if isinstance(model_value, str) else ""
        )
        named.append((slug_bounded, model_bounded))
    named.sort(key=lambda item: item[0])
    return tuple(named)


def probe_hermes_inventory(
    binary: str = "hermes", *, timeout: float = PROBE_TIMEOUT
) -> HermesInventory:
    """Probe the installed Hermes CLI for the provider/model inventory.

    Protocol (documented installed surfaces, fail closed):
    1. ``hermes --version`` — parse ``Hermes Agent vX.Y.Z``;
    2. ``hermes --help`` — prove the ``config`` subcommand exists before
       choosing any command;
    3. ``hermes config get model --json`` — strict size-bounded JSON, only
       ``default`` and ``provider`` are decoded;
    4. ``hermes config get providers --json`` — only when the model probe
       succeeded, and only provider slugs plus scalar default models.

    An absent binary maps to NOT_INSTALLED; unparseable version/help or a
    rejected command maps to UNSUPPORTED; malformed or oversized JSON maps
    to INVALID; transport failures map to TEMPORARILY_UNAVAILABLE. Every
    detail is a fixed sanitized reason — never raw output, paths,
    endpoints or secrets.
    """
    executable = shutil.which(binary)
    if not executable:
        return HermesInventory(IntegrationState.NOT_INSTALLED, detail="hermes CLI not found")
    version_probe = _run_bounded([executable, "--version"], timeout)
    if version_probe is None:
        return HermesInventory(
            IntegrationState.TEMPORARILY_UNAVAILABLE, detail="version probe failed"
        )
    match = _VERSION_RE.search(version_probe.stdout or "")
    if not match:
        return HermesInventory(IntegrationState.UNSUPPORTED, detail="version unknown")
    version = _bounded(".".join(match.groups()), MAX_SLUG_LENGTH)
    if not version:
        return HermesInventory(IntegrationState.UNSUPPORTED, detail="version unknown")

    help_probe = _run_bounded([executable, "--help"], timeout)
    if help_probe is None:
        return HermesInventory(
            IntegrationState.TEMPORARILY_UNAVAILABLE, version=version, detail="help probe failed"
        )
    help_text = help_probe.stdout or ""
    if help_probe.returncode != 0 or len(help_text) > MAX_OUTPUT_CHARS or "config" not in help_text:
        return HermesInventory(
            IntegrationState.UNSUPPORTED, version=version, detail="config surface unsupported"
        )

    model_probe = _run_bounded([executable, "config", "get", "model", "--json"], timeout)
    if model_probe is None:
        return HermesInventory(
            IntegrationState.TEMPORARILY_UNAVAILABLE, version=version, detail="config probe failed"
        )
    if model_probe.returncode != 0:
        return HermesInventory(
            IntegrationState.UNSUPPORTED, version=version, detail="config get unsupported"
        )
    raw_model = model_probe.stdout or ""
    if len(raw_model) > MAX_OUTPUT_CHARS:
        return HermesInventory(
            IntegrationState.INVALID, version=version, detail="config output oversized"
        )
    payload = _decode_object(raw_model)
    if payload is None:
        return HermesInventory(
            IntegrationState.INVALID, version=version, detail="config output malformed"
        )
    default = payload.get("default")
    provider = payload.get("provider")
    main_model = _bounded(default, MAX_MODEL_LENGTH) if isinstance(default, str) else ""
    main_provider = _bounded(provider, MAX_SLUG_LENGTH) if isinstance(provider, str) else ""
    if not main_model or not main_provider:
        return HermesInventory(
            IntegrationState.INVALID, version=version, detail="config output incomplete"
        )
    named = _probe_named_providers(executable, timeout)
    return HermesInventory(
        IntegrationState.AVAILABLE,
        version=version,
        main_provider=main_provider,
        main_model=main_model,
        named=named,
    )


# ── Snapshot builder (pure, GTK-free) ───────────────────────────────────────


def _activity_state(level: str) -> IntegrationState:
    """Map an ``agent_integration`` capability level to an exact state.

    ``full`` and ``session_owned`` are available; ``not_installed`` maps
    to NOT_INSTALLED; ``completion_only`` (a reduced Codex capability)
    maps to TEMPORARILY_UNAVAILABLE; anything else (including the
    unsupported level) maps to UNSUPPORTED.
    """
    if level in ("full", "session_owned"):
        return IntegrationState.AVAILABLE
    if level == "not_installed":
        return IntegrationState.NOT_INSTALLED
    if level == "completion_only":
        return IntegrationState.TEMPORARILY_UNAVAILABLE
    return IntegrationState.UNSUPPORTED


def _activity_capability(
    capabilities: Mapping[AgentRuntime, CapabilityReport], runtime: AgentRuntime
) -> tuple[IntegrationState, str]:
    """Activity badge from the agent_integration capability report.

    A missing report (probe still running) is TEMPORARILY_UNAVAILABLE with
    the sanitized ``checking`` detail — never claimed unsupported.
    """
    report = capabilities.get(runtime)
    if report is None:
        return IntegrationState.TEMPORARILY_UNAVAILABLE, "checking"
    return _activity_state(report.level), report.detail


def _quota_state(enabled: bool, has_reading: bool) -> IntegrationState:
    """Claude/Codex quota-percentage capability from the collection toggle
    and the collector/history readings. A disabled provider is
    NOT_CONFIGURED; enabled with no reading yet is TEMPORARILY_UNAVAILABLE;
    enabled with readings is AVAILABLE."""
    if not enabled:
        return IntegrationState.NOT_CONFIGURED
    if has_reading:
        return IntegrationState.AVAILABLE
    return IntegrationState.TEMPORARILY_UNAVAILABLE


def build_snapshot(
    *,
    hermes: HermesInventory,
    capabilities: Mapping[AgentRuntime, CapabilityReport],
    quota_readings: Sequence[QuotaReading],
    collect_claude: bool = True,
    collect_codex: bool = True,
    now: datetime | None = None,
) -> IntegrationSnapshot:
    """Assemble the immutable integration snapshot from accepted sources.

    Sources: ``agent_integration`` capability reports (activity), the
    collector/history quota readings (Claude/Codex quota and token
    capabilities), and the decoded Hermes inventory (provider/model
    assignments). Discovered assignments are collapsed deterministically:
    the main provider wins over a same-slug named entry, named entries
    colliding with the reserved runtime slugs are dropped, and everything
    is sorted by (role, slug) / slug. Capability badges are independent
    and truthful: Claude stays percentage-only, Codex keeps its accepted
    exact account-usage contract, and unknown cost/balance is a state,
    never a zero value.
    """
    if not isinstance(hermes, HermesInventory):
        raise ValueError("hermes must be a HermesInventory value")
    if not isinstance(capabilities, Mapping):
        raise ValueError("capabilities must be a mapping")
    if not isinstance(quota_readings, Sequence):
        raise ValueError("quota_readings must be a sequence")
    observed_at = now or utc_now()
    if observed_at.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    # ── Runtime integrations (activity capability per runtime) ──
    runtimes: list[RuntimeIntegration] = []
    runtime_labels = {
        AgentRuntime.CLAUDE: "Claude Code",
        AgentRuntime.CODEX: "Codex CLI",
        AgentRuntime.HERMES: "Hermes",
    }
    for runtime in AgentRuntime:
        report = capabilities.get(runtime)
        if report is None:
            state = IntegrationState.TEMPORARILY_UNAVAILABLE
            detail = "checking"
        else:
            state = _activity_state(report.level)
            detail = report.detail
        runtimes.append(RuntimeIntegration(runtime.value, runtime_labels[runtime], state, detail))

    # ── Provider identities: runtimes first, then discovered, sorted ──
    discovered_slugs = {hermes.main_provider} if hermes.main_provider else set()
    discovered_slugs.update(slug for slug, _model in hermes.named)
    discovered_slugs -= set(_RUNTIME_SLUGS)
    providers: list[ProviderIdentity] = [
        ProviderIdentity(slug, label) for slug, label in runtime_labels.items()
    ]
    for slug in sorted(discovered_slugs):
        providers.append(ProviderIdentity(slug, slug))

    # ── Model assignments (Hermes inventory only), collapsed ──
    assignments: list[ModelAssignment] = []
    if hermes.main_provider:
        assignments.append(
            ModelAssignment(
                ProviderIdentity(hermes.main_provider, hermes.main_provider),
                hermes.main_model,
                "main",
                IntegrationState.AVAILABLE,
            )
        )
    for slug, model in hermes.named:
        if slug in _RUNTIME_SLUGS or slug == hermes.main_provider:
            continue  # reserved runtime slugs and the main provider collapse
        if model:
            assignments.append(
                ModelAssignment(
                    ProviderIdentity(slug, slug), model, "named", IntegrationState.AVAILABLE
                )
            )
        else:
            assignments.append(
                ModelAssignment(
                    ProviderIdentity(slug, slug),
                    "",
                    "named",
                    IntegrationState.NOT_CONFIGURED,
                    "no default model",
                )
            )
    assignments.sort(key=lambda item: (0 if item.role == "main" else 1, item.provider.slug))

    # ── Independent capability badges ──
    readings_by_service = {
        service: [r for r in quota_readings if r.service.value == service]
        for service in ("claude", "codex")
    }
    capabilities_out: list[CapabilityState] = []
    claude_activity, claude_activity_detail = _activity_capability(
        capabilities, AgentRuntime.CLAUDE
    )
    codex_activity, codex_activity_detail = _activity_capability(capabilities, AgentRuntime.CODEX)
    hermes_activity, hermes_activity_detail = _activity_capability(
        capabilities, AgentRuntime.HERMES
    )
    capabilities_out.extend(
        [
            CapabilityState("claude", "activity", claude_activity, claude_activity_detail),
            CapabilityState(
                "claude",
                "quota_percentage",
                _quota_state(collect_claude, bool(readings_by_service["claude"])),
                ""
                if collect_claude and readings_by_service["claude"]
                else "collection disabled"
                if not collect_claude
                else "no reading yet",
            ),
            CapabilityState(
                "claude",
                "exact_tokens",
                IntegrationState.UNSUPPORTED,
                "Claude remains percentage-only",
            ),
            CapabilityState("claude", "balance", IntegrationState.UNSUPPORTED),
            CapabilityState("claude", "cost", IntegrationState.UNSUPPORTED),
            CapabilityState("codex", "activity", codex_activity, codex_activity_detail),
            CapabilityState(
                "codex",
                "quota_percentage",
                _quota_state(collect_codex, bool(readings_by_service["codex"])),
                ""
                if collect_codex and readings_by_service["codex"]
                else "collection disabled"
                if not collect_codex
                else "no reading yet",
            ),
            CapabilityState("codex", "exact_tokens", IntegrationState.AVAILABLE),
            CapabilityState("codex", "balance", IntegrationState.UNSUPPORTED),
            CapabilityState("codex", "cost", IntegrationState.UNSUPPORTED),
            CapabilityState("hermes", "activity", hermes_activity, hermes_activity_detail),
            CapabilityState("hermes", "quota_percentage", IntegrationState.UNSUPPORTED),
            CapabilityState("hermes", "exact_tokens", IntegrationState.UNSUPPORTED),
            CapabilityState("hermes", "balance", IntegrationState.UNSUPPORTED),
            CapabilityState("hermes", "cost", IntegrationState.UNSUPPORTED),
        ]
    )
    for slug in sorted(discovered_slugs):
        capabilities_out.extend(
            [
                CapabilityState(slug, "activity", IntegrationState.UNSUPPORTED),
                CapabilityState(slug, "quota_percentage", IntegrationState.UNSUPPORTED),
                CapabilityState(slug, "exact_tokens", IntegrationState.UNSUPPORTED),
                CapabilityState(slug, "balance", IntegrationState.NOT_CONFIGURED, "deferred"),
                CapabilityState(slug, "cost", IntegrationState.NOT_CONFIGURED, "deferred"),
            ]
        )

    return IntegrationSnapshot(
        runtimes=tuple(runtimes),
        providers=tuple(providers),
        assignments=tuple(assignments),
        capabilities=tuple(capabilities_out),
        observed_at=observed_at,
    )


# ── Bounded newest-wins coordinator ─────────────────────────────────────────


class IntegrationCoordinator:
    """Bounded newest-wins coordinator for off-GTK integration probes.

    Capacity is exactly one in-flight probe plus one pending request:

    - nothing in flight → the request starts immediately (returns True);
    - a probe in flight but no request pending → the request is parked
      (returns True);
    - a request already pending → the newest request replaces it and the
      call returns False (newest-wins; the caller sees saturation).

    Generation policy: a result publishes only when its generation is the
    newest accepted generation at completion time, the lifecycle is
    RUNNING, and no newer request is pending. A stale in-flight result is
    dropped; a result that arrives after shutdown is dropped. Publication
    happens under the lock so publish ordering is serialized — a newer
    result can never be overtaken by an older one.

    Lifecycle: NEW (constructed) → start() → RUNNING → shutdown() →
    TERMINATED. After TERMINATED, requests are rejected and shutdown() is
    an idempotent no-op. Shutdown is bounded: it never joins; the probe
    itself is subprocess-bounded and runs on the injected off-thread
    runner. There is no polling, daemon thread, process detection or
    unbounded future — at most one probe future exists at a time.
    """

    def __init__(
        self,
        *,
        submit: Callable[[Callable[[], None]], None],
        probe: Callable[[], HermesInventory],
        publish: Callable[[HermesInventory], None],
    ) -> None:
        self._submit = submit
        self._probe = probe
        self._publish = publish
        self._lock = threading.Lock()
        self._generation = 0
        self._in_flight = 0
        self._pending = 0
        self._latest = 0
        self._lifecycle = "new"

    def start(self) -> None:
        """Transition NEW → RUNNING. No-op otherwise (restart rejected)."""
        with self._lock:
            if self._lifecycle == "new":
                self._lifecycle = "running"

    def request_refresh(self) -> bool:
        """Request one probe. Never blocks. Returns False when the pending
        slot was occupied (newest-wins replacement) or after shutdown."""
        with self._lock:
            if self._lifecycle != "running":
                return False
            self._generation += 1
            generation = self._generation
            self._latest = generation
            if self._in_flight == 0:
                self._in_flight = generation
                self._submit(lambda: self._run_probe(generation))
                return True
            if self._pending == 0:
                self._pending = generation
                return True
            self._pending = generation
            return False

    def shutdown(self) -> None:
        """Reject future work and discard pending/in-flight publications.
        Idempotent and bounded (no joins)."""
        with self._lock:
            self._lifecycle = "terminated"
            self._pending = 0

    def _run_probe(self, generation: int) -> None:
        try:
            inventory = self._probe()
        except Exception:
            inventory = HermesInventory(
                IntegrationState.TEMPORARILY_UNAVAILABLE,
                detail="inventory probe failed",
            )
        with self._lock:
            if self._lifecycle != "running":
                return
            if self._in_flight == generation:
                self._in_flight = 0
            if self._pending != 0:
                # A newer request superseded this one: promote it without
                # publishing the stale result.
                next_generation = self._pending
                self._pending = 0
                self._in_flight = next_generation
                self._submit(lambda: self._run_probe(next_generation))
                return
            if generation != self._latest:
                return
            # Publish under the lock: serialized order, never overtaken.
            self._publish(inventory)
