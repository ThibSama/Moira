"""Package 7a/7b — provider-neutral integration registry and Hermes inventory.

GTK-free domain: immutable types for runtime integrations, provider
identities, model assignments, capability states and the integration
snapshot, plus the bounded Hermes CLI inventory probe (version/help first,
then ``config get`` with a genuinely bounded subprocess reader and strict
JSON decoding) and the bounded newest-wins ``IntegrationCoordinator``.

Privacy contract: the probe decodes only the documented ``default`` and
``provider`` fields of ``hermes config get model --json`` and the
provider slugs plus scalar ``default_model``/``model`` of
``hermes config get providers --json``. Base URLs, credentials, paths,
raw output and exceptions are never carried, stored or rendered.

Package 7b corrections: the subprocess boundary enforces independent
hard caps on stdout and stderr while the child runs (terminate and reap
the process group on overflow or timeout; failures carry no output at
all), the Codex exact-token badge is derived from the history-backed
``TokenStatusView`` (latest typed availability per service through the
existing History query path) instead of a hardcoded value, and the
coordinator never invokes injected submitters or publishers while
holding the state lock.
"""

from __future__ import annotations

import json
import os
import re
import select
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Generic, TypeVar

from .activity import AgentRuntime
from .agent_integration import CapabilityReport
from .models import (
    HistoryStatus,
    QuotaReading,
    Service,
    TokenAvailabilityRecord,
    utc_now,
)

#: Bounded timeout for each Hermes probe subprocess (seconds).
PROBE_TIMEOUT = 10
#: Hard independent cap on one probe's stdout and on its stderr (bytes).
MAX_OUTPUT_BYTES = 64 * 1024
#: Bounded provider/version slug length.
MAX_SLUG_LENGTH = 64
#: Bounded model identifier length.
MAX_MODEL_LENGTH = 128
#: Bounded sanitized detail length.
MAX_DETAIL_LENGTH = 200
#: Grace between SIGTERM and SIGKILL when terminating a probe group.
_TERM_GRACE = 0.5
#: Bounded wait after SIGKILL.
_KILL_WAIT = 2.0
#: Bounded drain slice for the bounded reader.
_READ_SLICE = 0.05
#: Bounded read chunk per stream.
_READ_CHUNK = 65536

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


@dataclass(frozen=True, slots=True)
class TokenStatusView:
    """Immutable history-backed exact-token availability view.

    ``latest`` holds the most recent ``TokenAvailabilityRecord`` per
    service in canonical order; ``codex_has_exact_data`` is True when
    stored exact token rows or the official Codex summary exist. Missing
    or corrupt History yields the fixed sanitized empty view — the badge
    then reports TEMPORARILY_UNAVAILABLE without hiding the runtime/model
    inventory.
    """

    latest: tuple[TokenAvailabilityRecord, ...] = ()
    codex_has_exact_data: bool = False
    source: str = "history"

    def __post_init__(self) -> None:
        if not isinstance(self.latest, tuple):
            raise ValueError("latest must be a tuple")
        if not isinstance(self.codex_has_exact_data, bool):
            raise ValueError("codex_has_exact_data must be a boolean")
        if not self.source.strip():
            raise ValueError("source must not be empty")
        seen: set[Service] = set()
        for record in self.latest:
            if not isinstance(record, TokenAvailabilityRecord):
                raise ValueError("latest must contain TokenAvailabilityRecord values")
            if record.service in seen:
                raise ValueError("latest must hold at most one record per service")
            seen.add(record.service)

    def latest_for(self, service: Service) -> TokenAvailabilityRecord | None:
        """Return the newest availability record for one service, if any."""
        for record in self.latest:
            if record.service is service:
                return record
        return None


@dataclass(frozen=True, slots=True)
class IntegrationProbe:
    """One off-GTK integration probe result.

    The Hermes inventory and the history-backed exact-token status view
    are read together under one coordinator generation, so a published
    probe is internally consistent: the token badge never lags or leads
    the inventory it is rendered with.
    """

    inventory: HermesInventory
    token_status: TokenStatusView

    def __post_init__(self) -> None:
        if not isinstance(self.inventory, HermesInventory):
            raise ValueError("inventory must be a HermesInventory value")
        if not isinstance(self.token_status, TokenStatusView):
            raise ValueError("token_status must be a TokenStatusView value")


# ── Bounded subprocess reader (hard caps while the child runs) ──────────────


class ProbeOutcome(StrEnum):
    """Closed outcome set of one bounded subprocess run."""

    OK = "ok"
    TIMEOUT = "timeout"
    STDOUT_OVERFLOW = "stdout_overflow"
    STDERR_OVERFLOW = "stderr_overflow"


@dataclass(frozen=True, slots=True)
class BoundedResult:
    """Result of one bounded subprocess run.

    Failure results (timeout/overflow) carry no output at all — the caps
    are enforced while the child runs, so retained memory is bounded and
    raw output never leaks into the caller.
    """

    stdout: str
    stderr: str
    returncode: int | None
    outcome: ProbeOutcome

    @property
    def ok(self) -> bool:
        return self.outcome is ProbeOutcome.OK


def _terminate_group(process: subprocess.Popen[bytes]) -> None:
    """SIGTERM the child's process group, then SIGKILL after a short grace.

    Always reaps with bounded waits; a child that ignores SIGTERM is
    SIGKILLed and reaped, and so are any group members (grandchildren)
    that inherited the pipes. The SIGKILL escalation is unconditional
    after the grace: a quick SIGTERM death of the direct child must not
    leave a SIGTERM-ignoring group member alive.
    """
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.wait(timeout=_TERM_GRACE)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.wait(timeout=_KILL_WAIT)
    except subprocess.TimeoutExpired:
        pass


def run_bounded(
    args: list[str],
    *,
    timeout: float,
    max_bytes: int = MAX_OUTPUT_BYTES,
) -> BoundedResult | None:
    """Run one subprocess with independent hard caps on stdout and stderr.

    The child runs in its own process group. The caps are enforced while
    the child runs — output is read in bounded chunks and the group is
    terminated and reaped on overflow or timeout, so neither wall time
    nor retained memory grows with the child's output volume. Returns
    None only on spawn failure (transport). Failure results carry no
    output at all.
    """
    try:
        process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except (OSError, ValueError):
        return None

    def fail(outcome: ProbeOutcome) -> BoundedResult:
        _terminate_group(process)
        return BoundedResult("", "", None, outcome)

    stdout_buf = bytearray()
    stderr_buf = bytearray()
    stdout_eof = False
    stderr_eof = False
    deadline = time.monotonic() + timeout

    def consume(stream: Any, target: bytearray) -> ProbeOutcome | None:
        """Read one chunk; return an overflow outcome or None."""
        nonlocal stdout_eof, stderr_eof
        try:
            data = os.read(stream.fileno(), _READ_CHUNK)
        except OSError:
            return None
        if not data:
            if stream is process.stdout:
                stdout_eof = True
            else:
                stderr_eof = True
            return None
        target.extend(data)
        if len(target) > max_bytes:
            if stream is process.stdout:
                return ProbeOutcome.STDOUT_OVERFLOW
            return ProbeOutcome.STDERR_OVERFLOW
        return None

    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return fail(ProbeOutcome.TIMEOUT)
            streams: list[Any] = []
            if not stdout_eof:
                streams.append(process.stdout)
            if not stderr_eof:
                streams.append(process.stderr)
            if not streams:
                break
            try:
                ready, _, _ = select.select(streams, [], [], min(_READ_SLICE, remaining))
            except OSError:
                break
            if not ready:
                if process.poll() is not None:
                    break
                continue
            for stream in ready:
                outcome = consume(stream, stdout_buf if stream is process.stdout else stderr_buf)
                if outcome is not None:
                    return fail(outcome)
        process.wait(timeout=_KILL_WAIT)
        return BoundedResult(
            stdout_buf.decode("utf-8", "replace"),
            stderr_buf.decode("utf-8", "replace"),
            process.returncode,
            ProbeOutcome.OK,
        )
    finally:
        if process.poll() is None:
            _terminate_group(process)


def _decode_object(raw: str) -> dict[str, Any] | None:
    """Strict, size-bounded JSON object decode; None on any failure."""
    if len(raw) > MAX_OUTPUT_BYTES:
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
    auxiliary surface yields no named entries (truthful, with a fixed
    sanitized detail only) without invalidating the main inventory.
    """
    providers_probe = run_bounded(
        [executable, "config", "get", "providers", "--json"], timeout=timeout
    )
    if providers_probe is None or not providers_probe.ok or providers_probe.returncode != 0:
        return ()
    parsed = _decode_object(providers_probe.stdout)
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
    3. ``hermes config get model --json`` — bounded strict JSON, only
       ``default`` and ``provider`` are decoded;
    4. ``hermes config get providers --json`` — only when the model probe
       succeeded, and only provider slugs plus scalar default models.

    Mappings (Package 7b): an absent binary maps to NOT_INSTALLED;
    unparseable version/help or a rejected documented command maps to
    UNSUPPORTED; malformed or oversized required JSON maps to INVALID;
    timeout/transport/overflow-of-diagnostics maps to
    TEMPORARILY_UNAVAILABLE. Every detail is a fixed sanitized reason —
    never raw output, paths, endpoints or secrets.
    """
    executable = shutil.which(binary)
    if not executable:
        return HermesInventory(IntegrationState.NOT_INSTALLED, detail="hermes CLI not found")
    version_probe = run_bounded([executable, "--version"], timeout=timeout)
    if version_probe is None or not version_probe.ok:
        return HermesInventory(
            IntegrationState.TEMPORARILY_UNAVAILABLE, detail="version probe failed"
        )
    match = _VERSION_RE.search(version_probe.stdout)
    if not match:
        return HermesInventory(IntegrationState.UNSUPPORTED, detail="version unknown")
    version = _bounded(".".join(match.groups()), MAX_SLUG_LENGTH)
    if not version:
        return HermesInventory(IntegrationState.UNSUPPORTED, detail="version unknown")

    help_probe = run_bounded([executable, "--help"], timeout=timeout)
    if help_probe is None or not help_probe.ok:
        return HermesInventory(
            IntegrationState.TEMPORARILY_UNAVAILABLE, version=version, detail="help probe failed"
        )
    help_text = help_probe.stdout
    if help_probe.returncode != 0 or "config" not in help_text:
        return HermesInventory(
            IntegrationState.UNSUPPORTED, version=version, detail="config surface unsupported"
        )

    model_probe = run_bounded([executable, "config", "get", "model", "--json"], timeout=timeout)
    if model_probe is None:
        return HermesInventory(
            IntegrationState.TEMPORARILY_UNAVAILABLE, version=version, detail="config probe failed"
        )
    if model_probe.outcome is ProbeOutcome.STDOUT_OVERFLOW:
        return HermesInventory(
            IntegrationState.INVALID, version=version, detail="config output oversized"
        )
    if not model_probe.ok:
        return HermesInventory(
            IntegrationState.TEMPORARILY_UNAVAILABLE, version=version, detail="config probe failed"
        )
    if model_probe.returncode != 0:
        return HermesInventory(
            IntegrationState.UNSUPPORTED, version=version, detail="config get unsupported"
        )
    payload = _decode_object(model_probe.stdout)
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


def read_token_status_view(
    *,
    db_path: Path | None = None,
    now: datetime | None = None,
) -> TokenStatusView:
    """Read the latest exact-token availability through the existing
    History query path (bounded SQLite reads, off GTK).

    Returns one ``TokenAvailabilityRecord`` per service (newest wins)
    within the 90-day retention window, plus whether stored exact token
    rows or the official Codex summary exist. Never raises: a missing
    database is reported without creating one, and missing/corrupt
    History yields the fixed sanitized empty view — the runtime/model
    inventory is never hidden by a History failure.
    """
    from .history_db import (
        _connect,
        history_path,
        init_schema,
        query_codex_summaries,
        query_token,
        query_token_availability,
    )

    path = db_path or history_path()
    if not path.exists():
        return TokenStatusView((), False)
    clock = now or utc_now()
    since = clock - timedelta(days=90)
    try:
        conn = _connect(path, timeout=5.0)
        try:
            init_schema(conn)
            records = query_token_availability(conn, since=since)
            codex_tokens = query_token(conn, since=since, service=Service.CODEX)
            codex_summaries = query_codex_summaries(conn, since=since, service=Service.CODEX)
        finally:
            conn.close()
    except Exception:
        return TokenStatusView((), False)
    latest: dict[Service, TokenAvailabilityRecord] = {}
    for record in records:
        latest.setdefault(record.service, record)  # newest observed_at first
    ordered = tuple(
        latest[service] for service in (Service.CLAUDE, Service.CODEX) if service in latest
    )
    return TokenStatusView(ordered, bool(codex_tokens) or bool(codex_summaries))


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


def _codex_token_state(
    collect_codex: bool, view: TokenStatusView | None
) -> tuple[IntegrationState, str]:
    """Codex exact-token badge from the history-backed availability view.

    Precedence (Package 7c): the latest typed provider attempt is
    authoritative for every status — only the AVAILABLE_EXACT attempt
    requires stored exact data or an official summary to become AVAILABLE.
    Collection disabled → NOT_CONFIGURED; no availability record (or a
    latest AVAILABLE_EXACT without stored data) → TEMPORARILY_UNAVAILABLE
    with the ``no exact token data yet`` detail; TEMPORARILY_UNAVAILABLE,
    INVALID and UNSUPPORTED attempts map exactly. Missing data is never
    converted to zero, and old exact totals in History are never touched
    by the badge.
    """
    if not collect_codex:
        return IntegrationState.NOT_CONFIGURED, "collection disabled"
    if view is None:
        return IntegrationState.TEMPORARILY_UNAVAILABLE, "no exact token data yet"
    record = view.latest_for(Service.CODEX)
    if record is None:
        return IntegrationState.TEMPORARILY_UNAVAILABLE, "no exact token data yet"
    if record.status is HistoryStatus.AVAILABLE_EXACT:
        if view.codex_has_exact_data:
            return IntegrationState.AVAILABLE, ""
        return IntegrationState.TEMPORARILY_UNAVAILABLE, "no exact token data yet"
    if record.status is HistoryStatus.TEMPORARILY_UNAVAILABLE:
        return IntegrationState.TEMPORARILY_UNAVAILABLE, ""
    if record.status is HistoryStatus.INVALID:
        return IntegrationState.INVALID, ""
    return IntegrationState.UNSUPPORTED, ""


def build_snapshot(
    *,
    hermes: HermesInventory,
    capabilities: Mapping[AgentRuntime, CapabilityReport],
    quota_readings: Sequence[QuotaReading],
    token_status: TokenStatusView | None = None,
    collect_claude: bool = True,
    collect_codex: bool = True,
    now: datetime | None = None,
) -> IntegrationSnapshot:
    """Assemble the immutable integration snapshot from accepted sources.

    Sources: ``agent_integration`` capability reports (activity), the
    collector/history quota readings (Claude/Codex quota capabilities),
    the history-backed ``TokenStatusView`` (Codex exact-token capability,
    derived from the latest typed availability record and stored exact
    data/summaries through the existing History query path), and the
    decoded Hermes inventory (provider/model assignments). Discovered
    assignments are collapsed deterministically: the main provider wins
    over a same-slug named entry, named entries colliding with the
    reserved runtime slugs are dropped, and everything is sorted by
    (role, slug) / slug. Capability badges are independent and truthful:
    Claude stays percentage-only, Codex's badge reflects the latest
    provider attempt while old exact totals stay untouched, and unknown
    cost/balance is a state, never a zero value.
    """
    if not isinstance(hermes, HermesInventory):
        raise ValueError("hermes must be a HermesInventory value")
    if not isinstance(capabilities, Mapping):
        raise ValueError("capabilities must be a mapping")
    if not isinstance(quota_readings, Sequence):
        raise ValueError("quota_readings must be a sequence")
    if token_status is not None and not isinstance(token_status, TokenStatusView):
        raise ValueError("token_status must be a TokenStatusView value")
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
            CapabilityState(
                "codex",
                "exact_tokens",
                *_codex_token_state(collect_codex, token_status),
            ),
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

_T = TypeVar("_T")


class IntegrationCoordinator(Generic[_T]):
    """Bounded newest-wins coordinator for off-GTK integration probes.

    Capacity is exactly one in-flight probe plus one pending request:

    - nothing in flight → the request starts immediately (returns True);
    - a probe in flight but no request pending → the request is parked
      (returns True);
    - a request already pending → the newest request replaces it and the
      call returns False (newest-wins; the caller sees saturation).

    Generation policy: a result publishes only when its generation is the
    newest accepted generation at completion time, no newer request was
    pending (a pending successor supersedes the result), and the
    lifecycle is RUNNING. Results that arrive after shutdown are dropped.

    Hardening (Package 7b): injected submitters and publishers are never
    invoked while holding the state lock — a synchronous submitter runs
    the probe inline without deadlocking, a rejecting submitter cannot
    leave the in-flight slot latched (the slot is cleared and the pending
    request promoted), and a publisher may re-enter the coordinator.
    Publication order is retained: at most one probe runs at a time, a
    result with a pending successor never publishes, and the newest
    accepted generation claims the publication watermark under the state
    lock immediately before its publish call.

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
        probe: Callable[[], _T],
        publish: Callable[[_T], None],
        fallback: Callable[[], _T],
    ) -> None:
        self._submit = submit
        self._probe = probe
        self._publish = publish
        self._fallback = fallback
        self._lock = threading.Lock()
        self._generation = 0
        self._in_flight = 0
        self._pending = 0
        self._latest = 0
        self._published_gen = 0
        self._lifecycle = "new"

    def start(self) -> None:
        """Transition NEW → RUNNING. No-op otherwise (restart rejected)."""
        with self._lock:
            if self._lifecycle == "new":
                self._lifecycle = "running"

    def request_refresh(self) -> bool:
        """Request one probe. Never blocks. Returns False when the pending
        slot was occupied (newest-wins replacement) or after shutdown.

        The off-thread dispatch happens outside the state lock so a
        synchronous submitter cannot deadlock against it.
        """
        with self._lock:
            if self._lifecycle != "running":
                return False
            self._generation += 1
            generation = self._generation
            self._latest = generation
            if self._in_flight == 0:
                self._in_flight = generation
            elif self._pending == 0:
                self._pending = generation
                return True
            else:
                self._pending = generation
                return False
        self._dispatch(generation)
        return True

    def shutdown(self) -> None:
        """Reject future work and discard pending/in-flight publications.
        Idempotent and bounded (no joins)."""
        with self._lock:
            self._lifecycle = "terminated"
            self._pending = 0

    def _dispatch(self, generation: int) -> None:
        """Run one probe off-thread via the injected submitter.

        A rejecting submitter never leaves the in-flight slot latched:
        the slot is cleared and the pending request (at most one) is
        promoted and dispatched — the recursion is bounded to one level.
        """
        try:
            self._submit(lambda: self._run_probe(generation))
        except Exception:
            with self._lock:
                if self._in_flight == generation:
                    self._in_flight = 0
                next_generation = 0
                if self._pending != 0:
                    next_generation = self._pending
                    self._pending = 0
                    self._in_flight = next_generation
            if next_generation:
                self._dispatch(next_generation)

    def _run_probe(self, generation: int) -> None:
        try:
            result = self._probe()
        except Exception:
            result = self._fallback()
        with self._lock:
            if self._lifecycle != "running":
                return
            if self._in_flight == generation:
                self._in_flight = 0
            superseded = self._pending != 0
            next_generation = 0
            if superseded:
                # A newer request superseded this one: promote it without
                # publishing the stale result.
                next_generation = self._pending
                self._pending = 0
                self._in_flight = next_generation
            publish_now = (
                not superseded and generation == self._latest and generation > self._published_gen
            )
            if publish_now:
                self._published_gen = generation
        if next_generation:
            self._dispatch(next_generation)
        if publish_now:
            # Outside the state lock: publishers may re-enter the
            # coordinator. Ordering is retained by the watermark claim
            # above and the single-in-flight probe sequencing.
            self._publish(result)
