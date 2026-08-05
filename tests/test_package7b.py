"""Package 7b — bounded probe reader, history-derived token state and
coordinator hardening.

Deterministic child-process tests prove the bounded subprocess boundary
(continuous output, stdout/stderr overflow, partial JSON followed by a
hang, ignored SIGTERM, near-timeout exit, bounded wall time, bounded
retained bytes, process-group reaping and no raw output leakage). The
exact-token badge is derived from the history-backed
``TokenStatusView`` through the existing History query path, and the
coordinator is hardened against synchronous/raising adapters and
shutdown races.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from moira.activity import AgentRuntime
from moira.agent_integration import CapabilityReport
from moira.integrations import (
    IntegrationCoordinator,
    IntegrationState,
    ProbeOutcome,
    TokenStatusView,
    build_snapshot,
    probe_hermes_inventory,
    read_token_status_view,
    run_bounded,
)
from moira.models import HistoryStatus, Service, TokenAvailabilityRecord

NOW = datetime(2026, 8, 6, 10, 0, 0, tzinfo=UTC)
RESET = NOW + timedelta(days=5)

MODEL_JSON = '{"default": "deepseek-v4-flash", "provider": "deepseek"}'

# ── Fake hermes binary with injectable branch bodies ────────────────────────


def _fake_hermes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    version: str = "0.20.0",
    version_body: str = 'echo "Hermes Agent v$MOIRA_FAKE_VERSION (2026.8.3)"\nexit 0',
    help_body: str = 'echo "$MOIRA_FAKE_HELP"\nexit 0',
    model_body: str = 'printf "%s" "$MOIRA_FAKE_MODEL"\nexit 0',
    providers_body: str = 'printf "%s" "$MOIRA_FAKE_PROVIDERS"\nexit 0',
    pidfile: str = "",
    gpidfile: str = "",
) -> Path:
    binary = tmp_path / "hermes"
    script = "#!/bin/sh\n"
    script += f'if [ "$1" = "--version" ]; then\n{version_body}\nfi\n'
    script += f'if [ "$1" = "--help" ]; then\n{help_body}\nfi\n'
    script += 'if [ "$1" = "config" ] && [ "$2" = "get" ] && [ "$3" = "model" ]; then\n'
    if pidfile:
        script += '  echo "$$" > "$MOIRA_FAKE_PIDFILE"\n'
    script += f"{model_body}\nfi\n"
    script += 'if [ "$1" = "config" ] && [ "$2" = "get" ] && [ "$3" = "providers" ]; then\n'
    if gpidfile:
        script += '  echo "$$" > "$MOIRA_FAKE_GPIDFILE"\n'
    script += f"{providers_body}\nfi\n"
    script += "exit 1\n"
    binary.write_text(script, encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("MOIRA_FAKE_VERSION", version)
    monkeypatch.setenv("MOIRA_FAKE_HELP", "usage: hermes {chat,model,config,hooks,doctor} ...")
    monkeypatch.setenv("MOIRA_FAKE_MODEL", MODEL_JSON)
    monkeypatch.setenv("MOIRA_FAKE_MODEL_RC", "0")
    monkeypatch.setenv("MOIRA_FAKE_PROVIDERS", "{}")
    monkeypatch.setenv("MOIRA_FAKE_PROVIDERS_RC", "0")
    if pidfile:
        monkeypatch.setenv("MOIRA_FAKE_PIDFILE", str(tmp_path / pidfile))
    if gpidfile:
        monkeypatch.setenv("MOIRA_FAKE_GPIDFILE", str(tmp_path / gpidfile))
    return binary


def _pid(pidfile: Path) -> int:
    return int(pidfile.read_text(encoding="utf-8").strip())


def _assert_reaped(pid: int) -> None:
    """Prove the process was reaped: kill(pid, 0) must raise (zombies too)."""
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


# ── Bounded reader: deterministic child-process contract ────────────────────


def test_run_bounded_normal_result() -> None:
    result = run_bounded(["/bin/sh", "-c", "printf 'hello'; printf 'err' >&2; exit 3"], timeout=5)
    assert result is not None
    assert result.ok
    assert result.stdout == "hello"
    assert result.stderr == "err"
    assert result.returncode == 3


def test_run_bounded_spawn_failure_is_none() -> None:
    result = run_bounded(["/nonexistent/binary-7b"], timeout=1)
    assert result is None


def test_run_bounded_continuous_stdout_overflow_is_bounded(tmp_path: Path) -> None:
    """A child that emits forever is cut at the cap: bounded wall time,
    bounded retained bytes (empty result) and reaped."""
    started = time.monotonic()
    result = run_bounded(["/bin/sh", "-c", "while :; do printf 'x'; done"], timeout=2)
    elapsed = time.monotonic() - started
    assert result is not None
    assert result.outcome is ProbeOutcome.STDOUT_OVERFLOW
    assert result.stdout == "" and result.stderr == ""  # no retained output
    assert result.returncode is None
    assert elapsed < 5  # bounded wall time


def test_run_bounded_one_shot_stdout_overflow(tmp_path: Path) -> None:
    result = run_bounded(["/bin/sh", "-c", "printf 'x%.0s' $(seq 1 200000)"], timeout=5)
    assert result is not None
    assert result.outcome is ProbeOutcome.STDOUT_OVERFLOW
    assert result.stdout == ""


def test_run_bounded_stderr_overflow(tmp_path: Path) -> None:
    result = run_bounded(["/bin/sh", "-c", "while :; do printf 'y' >&2; done"], timeout=2)
    assert result is not None
    assert result.outcome is ProbeOutcome.STDERR_OVERFLOW
    assert result.stderr == ""


def test_run_bounded_partial_output_then_hang_is_timeout(tmp_path: Path) -> None:
    """Partial JSON followed by a hang never leaks the partial bytes."""
    result = run_bounded(["/bin/sh", "-c", 'printf \'{"default": "m\'; sleep 30'], timeout=0.5)
    assert result is not None
    assert result.outcome is ProbeOutcome.TIMEOUT
    assert result.stdout == ""  # partial output is never retained


def test_run_bounded_ignored_sigterm_escalates_to_sigkill_and_reaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pidfile = tmp_path / "pid"
    monkeypatch.setenv("MOIRA_PIDFILE", str(pidfile))
    script = 'echo "$$" > "$MOIRA_PIDFILE"\ntrap \'\' TERM\nwhile :; do sleep 1; done\n'
    started = time.monotonic()
    result = run_bounded(["/bin/sh", "-c", script], timeout=0.6)
    elapsed = time.monotonic() - started
    assert result is not None
    assert result.outcome is ProbeOutcome.TIMEOUT
    assert elapsed < 5  # bounded wall time despite the ignored SIGTERM
    # The stubborn child ignored SIGTERM, was SIGKILLed and fully reaped.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(_pid(pidfile), 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    _assert_reaped(_pid(pidfile))


def test_run_bounded_near_timeout_exit_succeeds() -> None:
    """A child that exits just before the deadline with valid output is OK."""
    result = run_bounded(
        ["/bin/sh", "-c", "sleep 0.4; printf 'ok'"],
        timeout=0.8,
    )
    assert result is not None
    assert result.outcome is ProbeOutcome.OK
    assert result.returncode == 0
    assert result.stdout == "ok"


def test_run_bounded_reaps_grandchild_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The process group is killed and reaped, including a grandchild that
    ignores SIGTERM and inherited the pipes."""
    pidfile = tmp_path / "pid"
    gpidfile = tmp_path / "gpid"
    monkeypatch.setenv("MOIRA_PIDFILE", str(pidfile))
    monkeypatch.setenv("MOIRA_GPIDFILE", str(gpidfile))
    script = (
        'echo "$$" > "$MOIRA_PIDFILE"\n'
        "( trap '' TERM; while :; do sleep 1; done ) &\n"
        'echo "$!" > "$MOIRA_GPIDFILE"\n'
        "sleep 30\n"
    )
    result = run_bounded(["/bin/sh", "-c", script], timeout=0.5)
    assert result is not None
    assert result.outcome is ProbeOutcome.TIMEOUT
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(_pid(pidfile), 0)
            gone = False
        except ProcessLookupError:
            gone = True
        try:
            os.kill(_pid(gpidfile), 0)
            grand_gone = False
        except ProcessLookupError:
            grand_gone = True
        if gone and grand_gone:
            break
        time.sleep(0.05)
    _assert_reaped(_pid(pidfile))
    _assert_reaped(_pid(gpidfile))


# ── Probe-level bounds and mappings ─────────────────────────────────────────


def test_probe_continuous_model_output_is_invalid_not_unbounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED (7a): a model surface that never stops emitting maps to INVALID
    (oversized required JSON), bounded in time, never TEMPORARILY_UNAVAILABLE."""
    _fake_hermes(tmp_path, monkeypatch, model_body="while :; do printf 'x'; done")
    started = time.monotonic()
    inventory = probe_hermes_inventory(timeout=0.5)
    elapsed = time.monotonic() - started
    assert inventory.state is IntegrationState.INVALID
    assert inventory.detail == "config output oversized"
    assert elapsed < 5


def test_probe_stderr_flood_is_temporarily_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_hermes(tmp_path, monkeypatch, model_body="while :; do printf 'y' >&2; done")
    inventory = probe_hermes_inventory(timeout=0.5)
    assert inventory.state is IntegrationState.TEMPORARILY_UNAVAILABLE
    assert inventory.detail == "config probe failed"


def test_probe_partial_json_then_hang_leaks_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_hermes(tmp_path, monkeypatch, model_body='printf \'{"default": "m\'; sleep 30')
    inventory = probe_hermes_inventory(timeout=0.5)
    assert inventory.state is IntegrationState.TEMPORARILY_UNAVAILABLE
    assert inventory.detail == "config probe failed"
    assert "m" not in inventory.detail and "{" not in inventory.detail


def test_probe_near_timeout_exit_still_parses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_hermes(
        tmp_path,
        monkeypatch,
        model_body='sleep 0.4; printf "%s" "$MOIRA_FAKE_MODEL"\nexit 0',
    )
    inventory = probe_hermes_inventory(timeout=0.8)
    assert inventory.state is IntegrationState.AVAILABLE
    assert inventory.main_model == "deepseek-v4-flash"


# ── Token state derived from the history view ───────────────────────────────


def _capabilities() -> dict[AgentRuntime, CapabilityReport]:
    return {
        AgentRuntime.CLAUDE: CapabilityReport("full", ""),
        AgentRuntime.CODEX: CapabilityReport("session_owned", ""),
        AgentRuntime.HERMES: CapabilityReport("full", ""),
    }


def _inventory() -> Any:
    from moira.integrations import HermesInventory

    return HermesInventory(
        IntegrationState.AVAILABLE, main_provider="deepseek", main_model="deepseek-v4-flash"
    )


def _record(status: HistoryStatus, *, service: Service = Service.CODEX) -> TokenAvailabilityRecord:
    return TokenAvailabilityRecord(service, NOW, "codex-app-server:account/usage/read", status)


def _snapshot(token_status: TokenStatusView | None, *, collect_codex: bool = True) -> Any:
    return build_snapshot(
        hermes=_inventory(),
        capabilities=_capabilities(),
        quota_readings=(),
        token_status=token_status,
        collect_claude=True,
        collect_codex=collect_codex,
        now=NOW,
    )


def _codex_badge(snapshot: Any) -> tuple[IntegrationState, str]:
    for capability in snapshot.capabilities:
        if capability.provider == "codex" and capability.capability == "exact_tokens":
            return capability.state, capability.detail
    raise AssertionError("codex exact_tokens badge missing")


@pytest.mark.parametrize(
    "status, has_data, collect, expected_state, expected_detail",
    [
        # collection disabled → NOT_CONFIGURED regardless of history
        (
            HistoryStatus.AVAILABLE_EXACT,
            True,
            False,
            IntegrationState.NOT_CONFIGURED,
            "collection disabled",
        ),
        # latest AVAILABLE_EXACT + stored data/summary → AVAILABLE
        (HistoryStatus.AVAILABLE_EXACT, True, True, IntegrationState.AVAILABLE, ""),
        # latest AVAILABLE_EXACT without stored data → not available yet
        (
            HistoryStatus.AVAILABLE_EXACT,
            False,
            True,
            IntegrationState.TEMPORARILY_UNAVAILABLE,
            "no exact token data yet",
        ),
        # the latest provider attempt is authoritative, with or without data
        (
            HistoryStatus.TEMPORARILY_UNAVAILABLE,
            True,
            True,
            IntegrationState.TEMPORARILY_UNAVAILABLE,
            "",
        ),
        (
            HistoryStatus.TEMPORARILY_UNAVAILABLE,
            False,
            True,
            IntegrationState.TEMPORARILY_UNAVAILABLE,
            "",
        ),
        (HistoryStatus.INVALID, True, True, IntegrationState.INVALID, ""),
        (HistoryStatus.INVALID, False, True, IntegrationState.INVALID, ""),
        (HistoryStatus.UNSUPPORTED, True, True, IntegrationState.UNSUPPORTED, ""),
        (HistoryStatus.UNSUPPORTED, False, True, IntegrationState.UNSUPPORTED, ""),
    ],
)
def test_codex_exact_token_mapping(
    status: HistoryStatus,
    has_data: bool,
    collect: bool,
    expected_state: IntegrationState,
    expected_detail: str,
) -> None:
    """Package 7c: the latest typed availability attempt governs the badge
    for every status; only AVAILABLE_EXACT requires stored exact data to
    become AVAILABLE. A latest INVALID or UNSUPPORTED attempt must never
    degrade to TEMPORARILY_UNAVAILABLE when no exact rows/summary exist."""
    view = TokenStatusView((_record(status),), codex_has_exact_data=has_data)
    state, detail = _codex_badge(_snapshot(view, collect_codex=collect))
    assert state is expected_state
    assert detail == expected_detail


def test_codex_no_availability_or_data_yet_is_temporarily_unavailable() -> None:
    state, detail = _codex_badge(_snapshot(TokenStatusView((), False)))
    assert state is IntegrationState.TEMPORARILY_UNAVAILABLE
    assert detail == "no exact token data yet"
    # The empty default (no view) behaves identically.
    state2, _ = _codex_badge(_snapshot(None))
    assert state2 is IntegrationState.TEMPORARILY_UNAVAILABLE


def test_codex_disabled_collection_detail() -> None:
    state, detail = _codex_badge(
        _snapshot(
            TokenStatusView((_record(HistoryStatus.AVAILABLE_EXACT),), True), collect_codex=False
        )
    )
    assert state is IntegrationState.NOT_CONFIGURED
    assert detail == "collection disabled"


def test_claude_stays_unsupported_even_with_injected_exact_rows() -> None:
    """Impossible exact Claude rows never flip the Claude badge."""
    view = TokenStatusView(
        (_record(HistoryStatus.AVAILABLE_EXACT, service=Service.CLAUDE),),
        codex_has_exact_data=True,
    )
    snapshot = _snapshot(view)
    for capability in snapshot.capabilities:
        if capability.provider == "claude" and capability.capability == "exact_tokens":
            assert capability.state is IntegrationState.UNSUPPORTED
            assert capability.detail == "Claude remains percentage-only"
            break
    else:
        raise AssertionError("claude exact_tokens badge missing")


def test_codex_has_data_via_summary_only() -> None:
    """An official summary alone counts as exact stored data."""
    view = TokenStatusView((_record(HistoryStatus.AVAILABLE_EXACT),), codex_has_exact_data=True)
    state, _ = _codex_badge(_snapshot(view))
    assert state is IntegrationState.AVAILABLE


def test_old_exact_totals_preserved_while_badge_tracks_latest_attempt() -> None:
    """The badge reflects the latest typed availability; the snapshot never
    carries token values, so old exact totals in History stay untouched."""
    view = TokenStatusView((_record(HistoryStatus.INVALID),), codex_has_exact_data=True)
    snapshot = _snapshot(view)
    state, _ = _codex_badge(snapshot)
    assert state is IntegrationState.INVALID
    # No numeric token/cost/balance value exists anywhere in the snapshot.
    blob = repr(snapshot)
    for capability in snapshot.capabilities:
        for field in (capability.provider, capability.capability, capability.detail):
            assert not isinstance(field, (int, float))
    assert "tokens=" not in blob


def test_token_status_view_fail_closed() -> None:
    with pytest.raises(ValueError):
        TokenStatusView(
            (_record(HistoryStatus.AVAILABLE_EXACT), _record(HistoryStatus.INVALID))
        )  # duplicate service
    with pytest.raises(ValueError):
        TokenStatusView((), codex_has_exact_data="yes")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        TokenStatusView([], False)  # type: ignore[arg-type]
    view = TokenStatusView((_record(HistoryStatus.INVALID),), codex_has_exact_data=True)
    assert view.latest_for(Service.CODEX) is not None
    assert view.latest_for(Service.CLAUDE) is None
    assert view.source == "history"


# ── read_token_status_view: existing History query path ─────────────────────


def _seed_history(db_path: Path) -> sqlite3.Connection:
    from moira.history_db import _connect, init_schema

    conn = _connect(db_path, timeout=5.0)
    init_schema(conn)
    return conn


def test_view_missing_database_is_empty_view(tmp_path: Path) -> None:
    missing = tmp_path / "nope" / "history.sqlite3"
    view = read_token_status_view(db_path=missing)
    assert view.latest == () and view.codex_has_exact_data is False
    assert not missing.exists()  # reads never create the database


def test_view_empty_database_is_empty_view(tmp_path: Path) -> None:
    db_path = tmp_path / "history.sqlite3"
    conn = _seed_history(db_path)
    conn.close()
    view = read_token_status_view(db_path=db_path)
    assert view.latest == () and view.codex_has_exact_data is False


def test_view_latest_record_per_service_wins(tmp_path: Path) -> None:
    from moira.history_db import record_token_availability

    db_path = tmp_path / "history.sqlite3"
    conn = _seed_history(db_path)
    older = TokenAvailabilityRecord(
        Service.CODEX, NOW - timedelta(hours=2), "source-a", HistoryStatus.AVAILABLE_EXACT
    )
    newer = TokenAvailabilityRecord(Service.CODEX, NOW, "source-b", HistoryStatus.INVALID)
    claude = TokenAvailabilityRecord(Service.CLAUDE, NOW, "source-c", HistoryStatus.UNSUPPORTED)
    record_token_availability(conn, older)
    record_token_availability(conn, newer)
    record_token_availability(conn, claude)
    conn.close()
    view = read_token_status_view(db_path=db_path, now=NOW)
    assert view.latest_for(Service.CODEX) == newer  # newest wins
    assert view.latest_for(Service.CLAUDE) == claude
    assert view.codex_has_exact_data is False


def test_view_codex_has_data_from_token_rows_and_summaries(tmp_path: Path) -> None:
    from moira.history import TokenObservation
    from moira.history_db import (
        record_codex_summary,
        record_token,
        record_token_availability,
    )
    from moira.models import CodexSummary

    db_path = tmp_path / "history.sqlite3"
    conn = _seed_history(db_path)
    record_token_availability(
        conn,
        TokenAvailabilityRecord(Service.CODEX, NOW, "src", HistoryStatus.AVAILABLE_EXACT),
    )
    record_token(
        conn,
        TokenObservation(
            Service.CODEX,
            NOW.replace(hour=0, minute=0, second=0, microsecond=0),
            "day",
            NOW,
            "src",
            HistoryStatus.AVAILABLE_EXACT,
            tokens=1234,
        ),
    )
    record_codex_summary(
        conn,
        CodexSummary(Service.CODEX, "src", NOW, lifetime_tokens=9999),
    )
    conn.close()
    view = read_token_status_view(db_path=db_path, now=NOW)
    assert view.codex_has_exact_data is True
    latest = view.latest_for(Service.CODEX)
    assert latest is not None
    assert latest.status is HistoryStatus.AVAILABLE_EXACT


def test_view_corrupt_database_is_fixed_sanitized_state(tmp_path: Path) -> None:
    db_path = tmp_path / "history.sqlite3"
    db_path.write_bytes(b"not a sqlite database at all" * 10)
    view = read_token_status_view(db_path=db_path)
    assert view.latest == () and view.codex_has_exact_data is False
    assert view.source == "history"


def test_view_schema_mismatch_is_fixed_sanitized_state(tmp_path: Path) -> None:
    db_path = tmp_path / "history.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE schema_meta (version INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO schema_meta (version) VALUES (999)")
    conn.commit()
    conn.close()
    view = read_token_status_view(db_path=db_path)
    assert view.latest == () and view.codex_has_exact_data is False


@pytest.mark.parametrize(
    "latest_status, expected_state",
    [
        (HistoryStatus.INVALID, IntegrationState.INVALID),
        (HistoryStatus.UNSUPPORTED, IntegrationState.UNSUPPORTED),
    ],
)
def test_integration_path_preserves_latest_state_without_stored_data(
    tmp_path: Path, latest_status: HistoryStatus, expected_state: IntegrationState
) -> None:
    """Package 7c: through a real temporary History v4 database with a
    latest INVALID/UNSUPPORTED Codex availability and no exact rows or
    summary, the view → snapshot path preserves that authoritative state
    instead of degrading to TEMPORARILY_UNAVAILABLE."""
    from moira.history_db import record_token_availability

    db_path = tmp_path / "history.sqlite3"
    conn = _seed_history(db_path)
    record_token_availability(
        conn,
        TokenAvailabilityRecord(
            Service.CODEX, NOW, "codex-app-server:account/usage/read", latest_status
        ),
    )
    conn.close()
    view = read_token_status_view(db_path=db_path, now=NOW)
    assert view.codex_has_exact_data is False
    state, detail = _codex_badge(_snapshot(view))
    assert state is expected_state
    assert detail == ""


# ── Coordinator hardening ────────────────────────────────────────────────────


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


def _fallback() -> Any:
    from moira.integrations import HermesInventory

    return HermesInventory(IntegrationState.TEMPORARILY_UNAVAILABLE)


def test_coordinator_synchronous_submitter_runs_inline_without_deadlock() -> None:
    published: list[Any] = []

    def sync_submit(fn: Any) -> None:
        fn()  # inline, on the caller's thread

    coordinator = IntegrationCoordinator(
        submit=sync_submit,
        probe=lambda: _fallback(),
        publish=published.append,
        fallback=_fallback,
    )
    coordinator.start()
    # Must not deadlock: dispatch happens outside the state lock.
    assert coordinator.request_refresh() is True
    assert len(published) == 1


def test_coordinator_raising_submitter_does_not_latch() -> None:
    """RED (7a): a rejecting submitter must not leave the in-flight slot
    latched and must not propagate; the next request still publishes."""
    published: list[Any] = []
    calls: list[Any] = []

    def flaky_submit(fn: Any) -> None:
        calls.append(fn)
        if len(calls) == 1:
            raise RuntimeError("executor rejected")
        _run_in_thread(fn)

    coordinator = IntegrationCoordinator(
        submit=flaky_submit,
        probe=lambda: _fallback(),
        publish=published.append,
        fallback=_fallback,
    )
    coordinator.start()
    assert coordinator.request_refresh() is True  # no exception, no latch
    assert coordinator.request_refresh() is True
    assert _wait_for(lambda: len(published) == 1)
    assert published[0] is not None


def test_coordinator_reentrant_publish_does_not_deadlock() -> None:
    """Publishers are invoked outside the state lock: a publish that
    requests another refresh cannot deadlock."""
    published: list[Any] = []
    holder: dict[str, Any] = {}

    def reentrant_publish(result: Any) -> None:
        published.append(result)
        holder["coordinator"].request_refresh()

    coordinator = IntegrationCoordinator(
        submit=_run_in_thread,
        probe=lambda: _fallback(),
        publish=reentrant_publish,
        fallback=_fallback,
    )
    holder["coordinator"] = coordinator
    coordinator.start()
    assert coordinator.request_refresh() is True
    assert _wait_for(lambda: len(published) >= 2)


def test_coordinator_barrier_replacement_publishes_newest_only() -> None:
    published: list[Any] = []
    release_first = threading.Event()
    started_first = threading.Event()

    def probe_first() -> Any:
        started_first.set()
        assert release_first.wait(timeout=5)
        return _fallback()

    def probe_second() -> Any:
        return _fallback()

    probes = iter([probe_first, probe_second])
    coordinator = IntegrationCoordinator(
        submit=_run_in_thread,
        probe=lambda: next(probes)(),
        publish=published.append,
        fallback=_fallback,
    )
    coordinator.start()
    assert coordinator.request_refresh() is True
    assert started_first.wait(timeout=5)
    assert coordinator.request_refresh() is True  # parked as pending
    release_first.set()
    assert _wait_for(lambda: len(published) == 1)
    assert len(published) == 1  # the stale first result never published


def test_coordinator_shutdown_while_request_races() -> None:
    """request_refresh and shutdown are serialized: after shutdown returns,
    no request can start and no result can publish."""
    published: list[Any] = []
    release_first = threading.Event()
    started_first = threading.Event()

    def blocked_probe() -> Any:
        started_first.set()
        assert release_first.wait(timeout=5)
        return _fallback()

    coordinator = IntegrationCoordinator(
        submit=_run_in_thread,
        probe=blocked_probe,
        publish=published.append,
        fallback=_fallback,
    )
    coordinator.start()
    assert coordinator.request_refresh() is True
    assert started_first.wait(timeout=5)
    coordinator.shutdown()
    assert coordinator.request_refresh() is False
    coordinator.shutdown()  # idempotent
    release_first.set()
    time.sleep(0.2)
    assert published == []
