"""Package 3g: close provider identity and schema semantics — deterministic fixtures and tests.

Covers: per-future provider identity in MainWindow (both completion orders),
sanitized Claude/Codex exception fallbacks, CollectorResult ownership
enforcement, PRAGMA-proven quota UNIQUE key (autoindex, not DDL substrings),
exact ordered columns, index uniqueness/table validation, transactional
repair of every secondary index, UNIQUE-slot fail-closed, v3 migration
rollback on wrong keys/table shapes, and production-path fresh-init
rollback via an injected contract.

All fallback tests use real ``concurrent.futures.Future`` objects resolved
in a deterministic order — no sleeps.
"""

from __future__ import annotations

import concurrent.futures
import os
import sqlite3
import threading
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from moira.history import HistoryStatus, SchemaVersionError
from moira.history_db import (
    SCHEMA_SQL_V3_3B,
    V4_CONTRACT,
    V4ColumnSpec,
    V4Contract,
    V4TableSpec,
    _connect,
    _validate_v4_semantics,
    init_schema,
)
from moira.models import (
    CodexSummary,
    CollectorResult,
    QuotaReading,
    QuotaStatus,
    Service,
    TokenAvailabilityRecord,
    TokenReading,
)

NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)


def _db(tmp_path: Path) -> sqlite3.Connection:
    """Create a fresh v4 database for testing."""
    db_path = tmp_path / "history.sqlite3"
    conn = _connect(db_path)
    init_schema(conn)
    return conn


def _connect_v3b_with_rows(tmp_path: Path) -> sqlite3.Connection:
    """Create a Package 3b v3 database with one quota row and one token row."""
    db_path = tmp_path / "history_v3b.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(db_path), os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    os.chmod(db_path, 0o600)
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.executescript(SCHEMA_SQL_V3_3B)
    conn.execute("INSERT INTO schema_meta (version) VALUES (3)")
    conn.execute(
        "INSERT INTO quota_observations "
        "(service, quota_label, percentage, reset_at, observed_at, source, bucket, "
        "status, is_change) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
        (
            "codex",
            "Weekly",
            42.0,
            "2026-08-09T12:00:00+00:00",
            "2026-08-02T12:00:00+00:00",
            "codex-app-server",
            "2026-08-02T12:00:00+00:00",
            "available_exact",
        ),
    )
    conn.execute(
        "INSERT INTO token_events "
        "(event_key, service, period_start, period_kind, observed_at, source, status, "
        "total_tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "codex:d:2026-08-02",
            "codex",
            "2026-08-02",
            "day",
            "2026-08-02T12:00:00+00:00",
            "codex-app-server",
            "available_exact",
            1234,
        ),
    )
    conn.commit()
    return conn


# ── Criterion 4: quota UNIQUE key proven through PRAGMA, never DDL ──────────


def test_misleading_unique_service_does_not_satisfy_quota_key(tmp_path: Path) -> None:
    """UNIQUE(service) plus ordinary declarations of the other columns fails.

    The DDL contains the word UNIQUE and every key column as an ordinary
    declaration — a substring search would falsely accept it. PRAGMA
    index_list/index_info proves the autoindex covers only (service).
    """
    conn = _db(tmp_path)
    conn.execute("ALTER TABLE quota_observations RENAME TO quota_observations_old")
    conn.execute(
        "CREATE TABLE quota_observations ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "service TEXT NOT NULL UNIQUE,"
        "quota_label TEXT NOT NULL, percentage REAL NOT NULL,"
        "reset_at TEXT NOT NULL, observed_at TEXT NOT NULL,"
        "source TEXT NOT NULL, bucket TEXT NOT NULL,"
        "status TEXT NOT NULL DEFAULT 'available_exact',"
        "is_change INTEGER NOT NULL DEFAULT 1)"
    )
    conn.execute(
        "INSERT INTO quota_observations (id, service, quota_label, percentage, "
        "reset_at, observed_at, source, bucket, status, is_change) "
        "SELECT id, service, quota_label, percentage, reset_at, observed_at, source, "
        "bucket, status, is_change FROM quota_observations_old"
    )
    conn.execute("DROP TABLE quota_observations_old")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_quota_obs_time ON quota_observations (observed_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_quota_obs_service "
        "ON quota_observations (service, quota_label)"
    )
    conn.close()

    conn = _connect(tmp_path / "history.sqlite3")
    violations = _validate_v4_semantics(conn)
    assert any("UNIQUE constraint" in v for v in violations), f"got {violations}"
    conn.close()


def test_clean_v4_quota_unique_key_proven_via_autoindex(tmp_path: Path) -> None:
    """The clean v4 schema satisfies the key via one exact autoindex (origin 'u')."""
    conn = _db(tmp_path)
    index_list = conn.execute("PRAGMA index_list(quota_observations)").fetchall()
    uniques = [row for row in index_list if row[3] == "u" and bool(row[2])]
    assert len(uniques) == 1
    cols = tuple(
        row[2]
        for row in sorted(
            conn.execute(f"PRAGMA index_info({uniques[0][1]})").fetchall(),
            key=lambda r: r[0],
        )
    )
    assert cols == ("service", "quota_label", "bucket", "observed_at")
    violations = _validate_v4_semantics(conn)
    assert violations == [], f"unexpected violations on clean v4: {violations}"
    conn.close()


# ── Criterion 5: index uniqueness, missing indexes, exact columns ───────────


def test_semantic_validation_rejects_wrong_index_uniqueness(tmp_path: Path) -> None:
    """A UNIQUE index where a plain secondary index is required fails validation."""
    conn = _db(tmp_path)
    conn.execute("DROP INDEX IF EXISTS idx_token_avail_service")
    conn.execute("CREATE UNIQUE INDEX idx_token_avail_service ON token_availability (service)")
    conn.close()

    conn = _connect(tmp_path / "history.sqlite3")
    violations = _validate_v4_semantics(conn)
    assert any("idx_token_avail_service" in v and "unique=True" in v for v in violations), (
        f"expected uniqueness violation, got {violations}"
    )
    conn.close()


def test_semantic_validation_rejects_missing_index(tmp_path: Path) -> None:
    """A required secondary index that does not exist fails validation."""
    conn = _db(tmp_path)
    conn.execute("DROP INDEX idx_token_events_period")
    conn.close()

    conn = _connect(tmp_path / "history.sqlite3")
    violations = _validate_v4_semantics(conn)
    assert any("idx_token_events_period missing" in v for v in violations), (
        f"expected missing-index violation, got {violations}"
    )
    conn.close()


def test_semantic_validation_rejects_extra_column(tmp_path: Path) -> None:
    """An extra column beyond the contract fails closed."""
    conn = _db(tmp_path)
    conn.execute("ALTER TABLE token_availability RENAME TO token_availability_old")
    conn.execute(
        "CREATE TABLE token_availability ("
        "service TEXT NOT NULL, observed_at TEXT NOT NULL,"
        "source TEXT NOT NULL, status TEXT NOT NULL,"
        "detail TEXT NOT NULL DEFAULT '', extra TEXT,"
        "PRIMARY KEY (service, observed_at))"
    )
    conn.execute(
        "INSERT INTO token_availability (service, observed_at, source, status, detail) "
        "SELECT service, observed_at, source, status, detail FROM token_availability_old"
    )
    conn.execute("DROP TABLE token_availability_old")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_token_avail_service ON token_availability (service)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_token_avail_time ON token_availability (observed_at)"
    )
    conn.close()

    conn = _connect(tmp_path / "history.sqlite3")
    violations = _validate_v4_semantics(conn)
    assert any("column set/order" in v for v in violations), f"got {violations}"
    conn.close()


def test_semantic_validation_rejects_reordered_columns(tmp_path: Path) -> None:
    """The same column set declared in a different order fails closed."""
    conn = _db(tmp_path)
    conn.execute("ALTER TABLE token_events RENAME TO token_events_old")
    conn.execute(
        "CREATE TABLE token_events ("
        "event_key TEXT PRIMARY KEY,"
        "service TEXT NOT NULL, period_start TEXT NOT NULL,"
        "period_kind TEXT NOT NULL, observed_at TEXT NOT NULL,"
        "status TEXT NOT NULL, source TEXT NOT NULL,"
        "input_tokens INTEGER, cached_input_tokens INTEGER,"
        "output_tokens INTEGER, reasoning_output_tokens INTEGER,"
        "total_tokens INTEGER)"
    )
    conn.execute(
        "INSERT INTO token_events (event_key, service, period_start, period_kind, "
        "observed_at, source, status, input_tokens, cached_input_tokens, output_tokens, "
        "reasoning_output_tokens, total_tokens) "
        "SELECT event_key, service, period_start, period_kind, observed_at, source, status, "
        "input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens, "
        "total_tokens FROM token_events_old"
    )
    conn.execute("DROP TABLE token_events_old")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_token_events_time ON token_events (observed_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_token_events_service ON token_events (service)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_token_events_period ON token_events (period_start)"
    )
    conn.close()

    conn = _connect(tmp_path / "history.sqlite3")
    violations = _validate_v4_semantics(conn)
    assert any("column set/order" in v for v in violations), f"got {violations}"
    conn.close()


# ── Criterion 6: transactional repair of every secondary index ──────────────


def test_repairs_every_secondary_index(tmp_path: Path) -> None:
    """All eight required non-unique secondary indexes are rebuilt when missing."""
    conn = _db(tmp_path)
    for name in (
        "idx_quota_obs_time",
        "idx_quota_obs_service",
        "idx_token_events_time",
        "idx_token_events_service",
        "idx_token_events_period",
        "idx_codex_summaries_time",
        "idx_token_avail_service",
        "idx_token_avail_time",
    ):
        conn.execute(f"DROP INDEX IF EXISTS {name}")
    # One index is also corrupted with wrong columns
    conn.execute("CREATE INDEX idx_quota_obs_service ON quota_observations (quota_label, service)")
    conn.close()

    conn = _connect(tmp_path / "history.sqlite3")
    init_schema(conn)
    violations = _validate_v4_semantics(conn)
    assert violations == [], f"unexpected violations after repair: {violations}"
    cols = tuple(
        row[2]
        for row in sorted(
            conn.execute("PRAGMA index_info(idx_quota_obs_service)").fetchall(),
            key=lambda r: r[0],
        )
    )
    assert cols == ("service", "quota_label")
    conn.close()


def test_repairs_wrong_quota_index(tmp_path: Path) -> None:
    """A quota index on the wrong columns is rebuilt on the exact columns."""
    conn = _db(tmp_path)
    conn.execute("DROP INDEX IF EXISTS idx_quota_obs_time")
    conn.execute("CREATE INDEX idx_quota_obs_time ON quota_observations (bucket)")
    conn.close()

    conn = _connect(tmp_path / "history.sqlite3")
    init_schema(conn)
    cols = tuple(
        row[2]
        for row in sorted(
            conn.execute("PRAGMA index_info(idx_quota_obs_time)").fetchall(),
            key=lambda r: r[0],
        )
    )
    assert cols == ("observed_at",)
    conn.close()


def test_repairs_wrong_token_index(tmp_path: Path) -> None:
    """A token_events index with extra/reordered columns is rebuilt."""
    conn = _db(tmp_path)
    conn.execute("DROP INDEX IF EXISTS idx_token_events_period")
    conn.execute("CREATE INDEX idx_token_events_period ON token_events (observed_at, service)")
    conn.close()

    conn = _connect(tmp_path / "history.sqlite3")
    init_schema(conn)
    cols = tuple(
        row[2]
        for row in sorted(
            conn.execute("PRAGMA index_info(idx_token_events_period)").fetchall(),
            key=lambda r: r[0],
        )
    )
    assert cols == ("period_start",)
    conn.close()


def test_unique_index_at_required_non_unique_slot_fails_closed(tmp_path: Path) -> None:
    """A UNIQUE index occupying a required non-unique slot is never dropped.

    init_schema fails closed inside the transaction; the UNIQUE index and
    the v4 version row survive untouched.
    """
    conn = _db(tmp_path)
    conn.execute("DROP INDEX IF EXISTS idx_quota_obs_time")
    conn.execute("CREATE UNIQUE INDEX idx_quota_obs_time ON quota_observations (observed_at)")
    conn.close()

    conn = _connect(tmp_path / "history.sqlite3")
    with pytest.raises(SchemaVersionError):
        init_schema(conn)

    rows = [
        row
        for row in conn.execute("PRAGMA index_list(quota_observations)").fetchall()
        if row[1] == "idx_quota_obs_time"
    ]
    assert rows and bool(rows[0][2]), "the UNIQUE index must never be dropped"
    assert conn.execute("SELECT version FROM schema_meta").fetchone()[0] == 4
    conn.close()


# ── Criterion 7: v3 migration validates the full semantic contract ──────────


def test_v3_migration_rollback_on_wrong_unique_key(tmp_path: Path) -> None:
    """A v3 DB with the wrong quota UNIQUE key rolls back ALL v4 DDL."""
    conn = _connect_v3b_with_rows(tmp_path)
    conn.execute("ALTER TABLE quota_observations RENAME TO quota_observations_old")
    conn.execute(
        "CREATE TABLE quota_observations ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "service TEXT NOT NULL, quota_label TEXT NOT NULL,"
        "percentage REAL NOT NULL, reset_at TEXT NOT NULL,"
        "observed_at TEXT NOT NULL, source TEXT NOT NULL,"
        "bucket TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'available_exact',"
        "is_change INTEGER NOT NULL DEFAULT 1,"
        "UNIQUE (service, quota_label, bucket))"
    )
    conn.execute(
        "INSERT INTO quota_observations (id, service, quota_label, percentage, "
        "reset_at, observed_at, source, bucket, status, is_change) "
        "SELECT id, service, quota_label, percentage, reset_at, observed_at, source, "
        "bucket, status, is_change FROM quota_observations_old"
    )
    conn.execute("DROP TABLE quota_observations_old")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_quota_obs_time ON quota_observations (observed_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_quota_obs_service "
        "ON quota_observations (service, quota_label)"
    )
    conn.commit()
    token_rows_before = conn.execute("SELECT COUNT(*) FROM token_events").fetchone()[0]
    quota_rows_before = conn.execute("SELECT COUNT(*) FROM quota_observations").fetchone()[0]
    conn.close()

    conn = _connect(tmp_path / "history_v3b.sqlite3")
    with pytest.raises(SchemaVersionError):
        init_schema(conn)

    assert conn.execute("SELECT version FROM schema_meta").fetchone()[0] == 3
    for table in ("codex_summaries", "token_availability"):
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            is None
        ), f"v4 table {table} must be rolled back"
    assert conn.execute("SELECT COUNT(*) FROM token_events").fetchone()[0] == token_rows_before
    assert (
        conn.execute("SELECT COUNT(*) FROM quota_observations").fetchone()[0] == quota_rows_before
    )
    conn.close()


def test_v3_migration_rollback_on_extra_column(tmp_path: Path) -> None:
    """A v3 DB whose token_events has an extra column rolls back all v4 DDL."""
    conn = _connect_v3b_with_rows(tmp_path)
    conn.execute("ALTER TABLE token_events RENAME TO token_events_old")
    conn.execute(
        "CREATE TABLE token_events ("
        "event_key TEXT PRIMARY KEY,"
        "service TEXT NOT NULL, period_start TEXT NOT NULL,"
        "period_kind TEXT NOT NULL, observed_at TEXT NOT NULL,"
        "source TEXT NOT NULL, status TEXT NOT NULL,"
        "extra TEXT,"
        "input_tokens INTEGER, cached_input_tokens INTEGER,"
        "output_tokens INTEGER, reasoning_output_tokens INTEGER,"
        "total_tokens INTEGER)"
    )
    conn.execute(
        "INSERT INTO token_events (event_key, service, period_start, period_kind, "
        "observed_at, source, status, input_tokens, cached_input_tokens, output_tokens, "
        "reasoning_output_tokens, total_tokens) "
        "SELECT event_key, service, period_start, period_kind, observed_at, source, status, "
        "input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens, "
        "total_tokens FROM token_events_old"
    )
    conn.execute("DROP TABLE token_events_old")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_token_events_time ON token_events (observed_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_token_events_service ON token_events (service)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_token_events_period ON token_events (period_start)"
    )
    conn.commit()
    token_rows_before = conn.execute("SELECT COUNT(*) FROM token_events").fetchone()[0]
    conn.close()

    conn = _connect(tmp_path / "history_v3b.sqlite3")
    with pytest.raises(SchemaVersionError):
        init_schema(conn)

    assert conn.execute("SELECT version FROM schema_meta").fetchone()[0] == 3
    for table in ("codex_summaries", "token_availability"):
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            is None
        ), f"v4 table {table} must be rolled back"
    assert conn.execute("SELECT COUNT(*) FROM token_events").fetchone()[0] == token_rows_before
    conn.close()


# ── Criterion 8: production-path fresh-init rollback via injected contract ──


def test_fresh_init_injected_contract_rolls_back_everything(tmp_path: Path) -> None:
    """A semantic mismatch forced through the production init_schema path leaves
    no tables, no indexes and no version row.

    The broken contract demands NOT NULL on schema_meta.version while the
    DDL declares a plain INTEGER PRIMARY KEY (nullable). DDL runs first,
    the mismatch is detected before the version insert, and the whole
    transaction rolls back.
    """
    db_path = tmp_path / "fresh_contract.sqlite3"
    conn = _connect(db_path)

    broken = V4Contract(
        tables=(
            V4TableSpec(
                name="schema_meta",
                columns=(
                    V4ColumnSpec(
                        name="version",
                        affinity="INTEGER",
                        not_null=True,  # DDL: version INTEGER PRIMARY KEY (nullable)
                        default=None,
                        pk_order=1,
                    ),
                ),
                indexes=(),
            ),
            *V4_CONTRACT.tables[1:],
        )
    )

    with pytest.raises(SchemaVersionError, match="semantic validation"):
        init_schema(conn, contract=broken)

    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    indexes = conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    assert tables == [], f"tables left behind: {tables}"
    assert indexes == [], f"indexes left behind: {indexes}"
    conn.close()

    # The default contract still initializes the same empty file cleanly.
    conn2 = _connect(db_path)
    init_schema(conn2)
    assert conn2.execute("SELECT version FROM schema_meta").fetchone()[0] == 4
    violations = _validate_v4_semantics(conn2)
    assert violations == []
    conn2.close()


def test_init_schema_explicit_default_contract(tmp_path: Path) -> None:
    """Passing the default contract explicitly is equivalent to the default."""
    conn = _connect(tmp_path / "explicit.sqlite3")
    init_schema(conn, contract=V4_CONTRACT)
    assert conn.execute("SELECT version FROM schema_meta").fetchone()[0] == 4
    violations = _validate_v4_semantics(conn)
    assert violations == [], f"unexpected violations: {violations}"
    conn.close()


# ── Criterion 3: CollectorResult explicit ownership ──────────────────────────


def test_collector_result_rejects_availability_service_mismatch() -> None:
    """The single availability record must belong to the owner service."""
    avail = TokenAvailabilityRecord(
        service=Service.CODEX,
        observed_at=NOW,
        source="codex-app-server",
        status=HistoryStatus.AVAILABLE_EXACT,
    )
    with pytest.raises(ValueError, match="availability record service"):
        CollectorResult(
            service=Service.CLAUDE,
            quota_readings=(),
            token_readings=(),
            token_availability_records=(avail,),
        )


def test_collector_result_rejects_quota_service_mismatch() -> None:
    """Every quota reading must belong to the owner service."""
    reading = QuotaReading(
        Service.CODEX,
        "Weekly",
        50.0,
        NOW + timedelta(days=5),
        NOW,
        "codex-app-server",
        QuotaStatus.AVAILABLE,
    )
    avail = TokenAvailabilityRecord(
        service=Service.CLAUDE,
        observed_at=NOW,
        source="claude-statusline",
        status=HistoryStatus.UNSUPPORTED,
    )
    with pytest.raises(ValueError, match="quota reading service"):
        CollectorResult(
            service=Service.CLAUDE,
            quota_readings=(reading,),
            token_readings=(),
            token_availability_records=(avail,),
        )


def test_collector_result_rejects_token_service_mismatch() -> None:
    """Every token reading must belong to the owner service."""
    reading = TokenReading(
        service=Service.CODEX,
        day=date(2026, 8, 2),
        retrieved_at=NOW,
        source="codex-app-server",
        status=HistoryStatus.AVAILABLE_EXACT,
        tokens=1234,
    )
    avail = TokenAvailabilityRecord(
        service=Service.CLAUDE,
        observed_at=NOW,
        source="claude-statusline",
        status=HistoryStatus.UNSUPPORTED,
    )
    with pytest.raises(ValueError, match="token reading service"):
        CollectorResult(
            service=Service.CLAUDE,
            quota_readings=(),
            token_readings=(reading,),
            token_availability_records=(avail,),
        )


def test_collector_result_rejects_summary_service_mismatch() -> None:
    """A codex_summary must belong to the owner service."""
    summary = CodexSummary(
        service=Service.CODEX,
        source="codex-app-server",
        observed_at=NOW,
        lifetime_tokens=1000,
    )
    avail = TokenAvailabilityRecord(
        service=Service.CLAUDE,
        observed_at=NOW,
        source="claude-statusline",
        status=HistoryStatus.UNSUPPORTED,
    )
    with pytest.raises(ValueError, match="summary service"):
        CollectorResult(
            service=Service.CLAUDE,
            quota_readings=(),
            token_readings=(),
            codex_summary=summary,
            token_availability_records=(avail,),
        )


def test_collector_result_accepts_matching_owner() -> None:
    """A result whose payloads all belong to the owner is accepted."""
    quota = QuotaReading(
        Service.CODEX,
        "Weekly",
        50.0,
        NOW + timedelta(days=5),
        NOW,
        "codex-app-server",
        QuotaStatus.AVAILABLE,
    )
    tokens = TokenReading(
        service=Service.CODEX,
        day=date(2026, 8, 2),
        retrieved_at=NOW,
        source="codex-app-server",
        status=HistoryStatus.AVAILABLE_EXACT,
        tokens=1234,
    )
    summary = CodexSummary(
        service=Service.CODEX,
        source="codex-app-server",
        observed_at=NOW,
        lifetime_tokens=1000,
    )
    avail = TokenAvailabilityRecord(
        service=Service.CODEX,
        observed_at=NOW,
        source="codex-app-server",
        status=HistoryStatus.AVAILABLE_EXACT,
    )
    result = CollectorResult(
        service=Service.CODEX,
        quota_readings=(quota,),
        token_readings=(tokens,),
        codex_summary=summary,
        token_availability_records=(avail,),
    )
    assert result.service is Service.CODEX
    assert len(result.token_availability_records) == 1


# ── Criteria 1–2: per-future provider identity and sanitized fallbacks ───────


class RecordingExecutor:
    """Executor stub that records submitted calls without running them."""

    def __init__(self) -> None:
        self.submitted: list[
            tuple[
                object,
                tuple[object, ...],
                dict[str, object],
                concurrent.futures.Future[CollectorResult],
            ]
        ] = []

    def submit(
        self, fn: object, /, *args: object, **kwargs: object
    ) -> concurrent.futures.Future[CollectorResult]:
        future: concurrent.futures.Future[CollectorResult] = concurrent.futures.Future()
        self.submitted.append((fn, args, kwargs, future))
        return future


def _window_harness() -> tuple[Any, RecordingExecutor]:
    """Build an uninitialized MainWindow exposing only the refresh-accumulator state."""
    from moira.persistence import Settings
    from moira.ui import MainWindow

    window = MainWindow.__new__(MainWindow)
    window.settings = Settings()
    window.settings.validate()
    window.pending = []
    window.pending_tokens = []
    window.pending_summary = None
    window.pending_availability = []
    window.pending_lock = threading.Lock()
    window.completed = 0
    window._enabled_services = []
    executor = RecordingExecutor()
    window.executor = executor  # type: ignore[assignment]
    return window, executor


def test_fallback_codex_first_then_claude() -> None:
    """Codex fails first: its record is CODEX/TEMPORARILY_UNAVAILABLE; Claude
    resolves second. Identity follows the bound future, not completion order."""
    window, executor = _window_harness()
    with patch("moira.ui.GLib.idle_add", return_value=0) as idle:
        window._submit_collectors()
        assert len(executor.submitted) == 2
        claude_future = executor.submitted[0][3]
        codex_future = executor.submitted[1][3]
        codex_future.set_exception(RuntimeError("codex exploded"))
        claude_future.set_exception(RuntimeError("claude exploded"))

    assert window.completed == 2
    assert idle.call_count == 1
    services = [r.service for r in window.pending_availability]
    assert services == [Service.CODEX, Service.CLAUDE]
    for record in window.pending_availability:
        assert record.status is HistoryStatus.TEMPORARILY_UNAVAILABLE
        assert record.source == "moira"
        assert record.detail == ""  # no exception text can be stored
    assert window.pending == []
    assert window.pending_tokens == []
    assert window.pending_summary is None


def test_fallback_claude_first_then_codex() -> None:
    """Claude fails first: its record is CLAUDE/TEMPORARILY_UNAVAILABLE; the
    reversed completion order still yields the correct identities."""
    window, executor = _window_harness()
    with patch("moira.ui.GLib.idle_add", return_value=0) as idle:
        window._submit_collectors()
        claude_future = executor.submitted[0][3]
        codex_future = executor.submitted[1][3]
        claude_future.set_exception(RuntimeError("claude exploded"))
        codex_future.set_exception(RuntimeError("codex exploded"))

    assert window.completed == 2
    assert idle.call_count == 1
    services = [r.service for r in window.pending_availability]
    assert services == [Service.CLAUDE, Service.CODEX]
    for record in window.pending_availability:
        assert record.status is HistoryStatus.TEMPORARILY_UNAVAILABLE
        assert record.detail == ""
    assert window.pending_summary is None


def test_mixed_success_and_failure_keeps_identities() -> None:
    """Codex failure and Claude success in one refresh: the Codex slot is the
    sanitized fallback, the Claude slot keeps its real UNSUPPORTED record."""
    window, executor = _window_harness()
    with patch("moira.ui.GLib.idle_add", return_value=0) as idle:
        window._submit_collectors()
        claude_future = executor.submitted[0][3]
        codex_future = executor.submitted[1][3]
        codex_future.set_exception(RuntimeError("codex exploded"))
        claude_future.set_result(
            CollectorResult(
                service=Service.CLAUDE,
                quota_readings=(),
                token_readings=(),
                token_availability_records=(
                    TokenAvailabilityRecord(
                        service=Service.CLAUDE,
                        observed_at=NOW,
                        source="claude-statusline",
                        status=HistoryStatus.UNSUPPORTED,
                    ),
                ),
            )
        )

    assert window.completed == 2
    assert idle.call_count == 1
    codex_rec = next(r for r in window.pending_availability if r.service is Service.CODEX)
    claude_rec = next(r for r in window.pending_availability if r.service is Service.CLAUDE)
    assert codex_rec.status is HistoryStatus.TEMPORARILY_UNAVAILABLE
    assert codex_rec.source == "moira"
    assert codex_rec.detail == ""
    assert claude_rec.status is HistoryStatus.UNSUPPORTED
    assert claude_rec.source == "claude-statusline"
    assert [r.service for r in window.pending_availability] == [
        Service.CODEX,
        Service.CLAUDE,
    ]


def test_collector_done_merges_successful_result() -> None:
    """A successful Codex result is merged under its bound identity."""
    window, _ = _window_harness()
    reading = QuotaReading(
        Service.CODEX,
        "Weekly",
        50.0,
        NOW + timedelta(days=5),
        NOW,
        "codex-app-server",
        QuotaStatus.AVAILABLE,
    )
    avail = TokenAvailabilityRecord(
        service=Service.CODEX,
        observed_at=NOW,
        source="codex-app-server",
        status=HistoryStatus.AVAILABLE_EXACT,
    )
    result = CollectorResult(
        service=Service.CODEX,
        quota_readings=(reading,),
        token_readings=(),
        token_availability_records=(avail,),
    )
    future: concurrent.futures.Future[CollectorResult] = concurrent.futures.Future()
    future.set_result(result)

    with patch("moira.ui.GLib.idle_add", return_value=0):
        window._collector_done(future, Service.CODEX)

    assert window.completed == 1
    assert len(window.pending) == 1
    assert window.pending[0].service is Service.CODEX
    assert window.pending_availability[0].service is Service.CODEX
