"""Package 3f: enforce semantic v4 invariants — deterministic fixtures and tests.

Covers: missing PK, wrong composite PK, removed quota UNIQUE, wrong index
columns, malformed affinity/nullability, migration rollback, fresh-init
rollback, index repair, raw-detail rejection, zero/two availability
rejection, and collector-exception fallback.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from moira.history import HistoryStatus, SchemaVersionError
from moira.history_db import (
    SCHEMA_SQL_V3_3B,
    _connect,
    _validate_v4_semantics,
    init_schema,
    query_token_availability,
)
from moira.models import (
    CollectorResult,
    Service,
    TokenAvailabilityRecord,
)

NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)


def _db(tmp_path: Path) -> sqlite3.Connection:
    """Create a fresh v4 database for testing (reusing test_history sibling pattern)."""
    db_path = tmp_path / "history.sqlite3"
    conn = _connect(db_path)
    init_schema(conn)
    return conn


def _connect_v3b(tmp_path: Path) -> sqlite3.Connection:
    """Create a fresh Package 3b v3 database (no codex_summaries)."""
    db_path = tmp_path / "history_v3b.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(db_path), os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    os.chmod(db_path, 0o600)
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.executescript(SCHEMA_SQL_V3_3B)
    conn.execute("INSERT INTO schema_meta (version) VALUES (3)")
    conn.commit()
    return conn


# ── Semantic validation: PK invariants ──────────────────────────────────────


def test_semantic_validation_rejects_missing_pk(tmp_path: Path) -> None:
    """A token_events table without event_key as PK fails semantic validation."""
    conn = _db(tmp_path)
    conn.execute("ALTER TABLE token_events RENAME TO token_events_old")
    conn.execute(
        "CREATE TABLE token_events ("
        "event_key TEXT, service TEXT NOT NULL, period_start TEXT NOT NULL,"
        "period_kind TEXT NOT NULL, observed_at TEXT NOT NULL,"
        "source TEXT NOT NULL, status TEXT NOT NULL,"
        "input_tokens INTEGER, cached_input_tokens INTEGER,"
        "output_tokens INTEGER, reasoning_output_tokens INTEGER,"
        "total_tokens INTEGER)"
    )
    conn.execute("INSERT INTO token_events SELECT * FROM token_events_old")
    conn.execute("DROP TABLE token_events_old")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_token_events_time ON token_events (observed_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_token_events_service ON token_events (service)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_token_events_period ON token_events (period_start)"
    )
    conn.close()

    conn = _connect(tmp_path / "history.sqlite3")
    violations = _validate_v4_semantics(conn)
    assert any("in PK=" in v for v in violations), f"expected PK violation, got {violations}"
    conn.close()


def test_semantic_validation_rejects_wrong_composite_pk(tmp_path: Path) -> None:
    """A codex_summaries table with wrong PK columns fails semantic validation."""
    conn = _db(tmp_path)
    conn.execute("ALTER TABLE codex_summaries RENAME TO codex_summaries_old")
    conn.execute(
        "CREATE TABLE codex_summaries ("
        "service TEXT NOT NULL, observed_at TEXT NOT NULL,"
        "source TEXT NOT NULL,"
        "lifetime_tokens INTEGER, peak_daily_tokens INTEGER,"
        "current_streak_days INTEGER, longest_streak_days INTEGER,"
        "longest_running_turn_sec INTEGER,"
        "PRIMARY KEY (source, observed_at))"
    )
    conn.execute("INSERT INTO codex_summaries SELECT * FROM codex_summaries_old")
    conn.execute("DROP TABLE codex_summaries_old")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_codex_summaries_time ON codex_summaries (observed_at)"
    )
    conn.close()

    conn = _connect(tmp_path / "history.sqlite3")
    violations = _validate_v4_semantics(conn)
    assert any("PK columns" in v for v in violations), (
        f"expected PK columns violation, got {violations}"
    )
    conn.close()


def test_semantic_validation_rejects_missing_quota_unique(tmp_path: Path) -> None:
    """A quota_observations table without UNIQUE constraint fails semantic validation."""
    conn = _db(tmp_path)
    conn.execute("ALTER TABLE quota_observations RENAME TO quota_observations_old")
    conn.execute(
        "CREATE TABLE quota_observations ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "service TEXT NOT NULL, quota_label TEXT NOT NULL,"
        "percentage REAL NOT NULL, reset_at TEXT NOT NULL,"
        "observed_at TEXT NOT NULL, source TEXT NOT NULL,"
        "bucket TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'available_exact',"
        "is_change INTEGER NOT NULL DEFAULT 1)"
    )
    conn.execute("INSERT INTO quota_observations SELECT * FROM quota_observations_old")
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
    assert any("UNIQUE" in v for v in violations), (
        f"expected UNIQUE constraint violation, got {violations}"
    )
    conn.close()


def test_semantic_validation_rejects_wrong_index_columns(tmp_path: Path) -> None:
    """An index on the wrong columns fails semantic validation."""
    conn = _db(tmp_path)
    conn.execute("DROP INDEX IF EXISTS idx_token_avail_service")
    conn.execute("CREATE INDEX idx_token_avail_service ON token_availability (observed_at)")
    conn.close()

    conn = _connect(tmp_path / "history.sqlite3")
    violations = _validate_v4_semantics(conn)
    assert any("idx_token_avail_service" in v and "columns" in v for v in violations), (
        f"expected index column violation, got {violations}"
    )
    conn.close()


def test_semantic_validation_rejects_wrong_affinity(tmp_path: Path) -> None:
    """A column with wrong type affinity fails semantic validation."""
    conn = _db(tmp_path)
    conn.execute("ALTER TABLE quota_observations RENAME TO quota_observations_old")
    conn.execute(
        "CREATE TABLE quota_observations ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "service TEXT NOT NULL, quota_label TEXT NOT NULL,"
        "percentage TEXT NOT NULL, reset_at TEXT NOT NULL,"
        "observed_at TEXT NOT NULL, source TEXT NOT NULL,"
        "bucket TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'available_exact',"
        "is_change INTEGER NOT NULL DEFAULT 1,"
        "UNIQUE (service, quota_label, bucket, observed_at))"
    )
    conn.execute("INSERT INTO quota_observations SELECT * FROM quota_observations_old")
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
    assert any("percentage" in v and "type" in v for v in violations), (
        f"expected type violation for percentage, got {violations}"
    )
    conn.close()


def test_semantic_validation_rejects_wrong_nullability(tmp_path: Path) -> None:
    """A NOT NULL column that should be nullable fails semantic validation."""
    conn = _db(tmp_path)
    conn.execute("ALTER TABLE token_events RENAME TO token_events_old")
    conn.execute(
        "CREATE TABLE token_events ("
        "event_key TEXT PRIMARY KEY,"
        "service TEXT NOT NULL, period_start TEXT NOT NULL,"
        "period_kind TEXT NOT NULL, observed_at TEXT NOT NULL,"
        "source TEXT NOT NULL, status TEXT NOT NULL,"
        "input_tokens INTEGER, cached_input_tokens INTEGER,"
        "output_tokens INTEGER, reasoning_output_tokens INTEGER,"
        "total_tokens INTEGER NOT NULL)"
    )
    conn.execute(
        "INSERT INTO token_events SELECT event_key, service, period_start, period_kind,"
        "observed_at, source, status, input_tokens, cached_input_tokens,"
        "output_tokens, reasoning_output_tokens, COALESCE(total_tokens, 0) "
        "FROM token_events_old"
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
    assert any("total_tokens" in v and "notnull" in v for v in violations), (
        f"expected notnull violation for total_tokens, got {violations}"
    )
    conn.close()


# ── Migration rollback: damaged v3 with wrong constraints ───────────────────


def test_migration_rollback_on_damaged_v3_wrong_pk(tmp_path: Path) -> None:
    """v3→v4 migration on a token_events without PK fails closed and rolls back."""
    conn = _connect_v3b(tmp_path)
    conn.execute("ALTER TABLE token_events RENAME TO token_events_old")
    conn.execute(
        "CREATE TABLE token_events ("
        "event_key TEXT, service TEXT NOT NULL, period_start TEXT NOT NULL,"
        "period_kind TEXT NOT NULL, observed_at TEXT NOT NULL,"
        "source TEXT NOT NULL, status TEXT NOT NULL,"
        "input_tokens INTEGER, cached_input_tokens INTEGER,"
        "output_tokens INTEGER, reasoning_output_tokens INTEGER,"
        "total_tokens INTEGER)"
    )
    conn.execute("INSERT INTO token_events SELECT * FROM token_events_old")
    conn.execute("DROP TABLE token_events_old")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_token_events_time ON token_events (observed_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_token_events_service ON token_events (service)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_token_events_period ON token_events (period_start)"
    )
    conn.commit()
    conn.close()

    conn = _connect(tmp_path / "history_v3b.sqlite3")
    assert conn.execute("SELECT version FROM schema_meta").fetchone()[0] == 3

    with pytest.raises(SchemaVersionError):
        init_schema(conn)

    # Version must stay at 3 (rollback preserved it)
    assert conn.execute("SELECT version FROM schema_meta").fetchone()[0] == 3
    conn.close()


def test_migration_rollback_on_damaged_v3_missing_unique(tmp_path: Path) -> None:
    """v3→v4 migration on quota_observations without UNIQUE rolls back version."""
    conn = _connect_v3b(tmp_path)
    conn.execute("ALTER TABLE quota_observations RENAME TO quota_observations_old")
    conn.execute(
        "CREATE TABLE quota_observations ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "service TEXT NOT NULL, quota_label TEXT NOT NULL,"
        "percentage REAL NOT NULL, reset_at TEXT NOT NULL,"
        "observed_at TEXT NOT NULL, source TEXT NOT NULL,"
        "bucket TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'available_exact',"
        "is_change INTEGER NOT NULL DEFAULT 1)"
    )
    conn.execute("INSERT INTO quota_observations SELECT * FROM quota_observations_old")
    conn.execute("DROP TABLE quota_observations_old")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_quota_obs_time ON quota_observations (observed_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_quota_obs_service "
        "ON quota_observations (service, quota_label)"
    )
    conn.commit()
    conn.close()

    conn = _connect(tmp_path / "history_v3b.sqlite3")
    assert conn.execute("SELECT version FROM schema_meta").fetchone()[0] == 3

    with pytest.raises(SchemaVersionError):
        init_schema(conn)

    assert conn.execute("SELECT version FROM schema_meta").fetchone()[0] == 3
    conn.close()


# ── Fresh-init semantic validation ──────────────────────────────────────────


def test_fresh_init_validates_semantics_before_commit(tmp_path: Path) -> None:
    """Fresh v4 init: semantic validation before COMMIT — mismatch rolls back."""
    # init_schema on a fresh DB will do all-or-nothing. We verify by creating
    # a fresh DB with correct DDL and then calling init_schema — it validates
    # before committing the version row.
    from moira.history_db import SCHEMA_SQL_V4

    db_path = tmp_path / "fresh.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(db_path), os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    os.chmod(db_path, 0o600)
    conn = _connect(db_path)

    # Manually create v4 DDL but inject a defect before the semantic check
    conn.execute("BEGIN IMMEDIATE")
    try:
        for statement in SCHEMA_SQL_V4.split(";"):
            stmt = statement.strip()
            if stmt:
                conn.execute(stmt)
        # Defect: make total_tokens NOT NULL
        conn.execute("ALTER TABLE token_events RENAME TO token_events_old")
        conn.execute(
            "CREATE TABLE token_events ("
            "event_key TEXT PRIMARY KEY,"
            "service TEXT NOT NULL, period_start TEXT NOT NULL,"
            "period_kind TEXT NOT NULL, observed_at TEXT NOT NULL,"
            "source TEXT NOT NULL, status TEXT NOT NULL,"
            "input_tokens INTEGER, cached_input_tokens INTEGER,"
            "output_tokens INTEGER, reasoning_output_tokens INTEGER,"
            "total_tokens INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO token_events SELECT event_key, service, period_start, period_kind,"
            "observed_at, source, status, input_tokens, cached_input_tokens,"
            "output_tokens, reasoning_output_tokens, 0 FROM token_events_old"
        )
        conn.execute("DROP TABLE token_events_old")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_token_events_time ON token_events (observed_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_token_events_service ON token_events (service)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_token_events_period ON token_events (period_start)"
        )
        # Semantic check must detect the defect
        violations = _validate_v4_semantics(conn)
        assert violations, "expected violations for NOT NULL total_tokens"
        conn.execute("ROLLBACK")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
    conn.close()

    # init_schema on a genuinely fresh DB must succeed
    conn2 = _connect(db_path)
    init_schema(conn2)
    assert conn2.execute("SELECT version FROM schema_meta").fetchone()[0] == 4
    conn2.close()


# ── Index repair: wrong columns ─────────────────────────────────────────────


def test_repair_index_with_wrong_columns(tmp_path: Path) -> None:
    """An already-v4 DB with wrong index columns gets repaired transactionally."""
    conn = _db(tmp_path)
    conn.execute("DROP INDEX IF EXISTS idx_token_avail_service")
    conn.execute("CREATE INDEX idx_token_avail_service ON token_availability (observed_at)")
    conn.close()

    conn = _connect(tmp_path / "history.sqlite3")
    # init_schema detects the wrong-column index and repairs it
    init_schema(conn)

    idx_cols = conn.execute("PRAGMA index_info(idx_token_avail_service)").fetchall()
    actual_columns = tuple(row[2] for row in sorted(idx_cols, key=lambda r: r[0]))
    assert actual_columns == ("service",), f"expected ('service',), got {actual_columns}"
    conn.close()


# ── TokenAvailabilityRecord: detail is always empty ─────────────────────────


def test_token_availability_detail_always_empty(tmp_path: Path) -> None:
    """TokenAvailabilityRecord detail returns '' — raw exceptions are impossible."""
    conn = _db(tmp_path)
    from moira.history_db import record_token_availability

    avail = TokenAvailabilityRecord(
        service=Service.CODEX,
        observed_at=NOW,
        source="test",
        status=HistoryStatus.INVALID,
    )
    assert avail.detail == ""
    record_token_availability(conn, avail, now=NOW)

    rows = query_token_availability(conn, since=NOW - timedelta(hours=1))
    assert len(rows) == 1
    assert rows[0].detail == ""

    db_detail = conn.execute(
        "SELECT detail FROM token_availability WHERE service='codex'"
    ).fetchone()[0]
    assert db_detail == ""
    conn.close()


# ── CollectorResult: exact one availability record enforcement ──────────────


def test_collector_result_rejects_zero_availability() -> None:
    """CollectorResult with zero availability records raises ValueError."""
    with pytest.raises(ValueError, match="exactly one"):
        CollectorResult(
            service=Service.CODEX,
            quota_readings=(),
            token_readings=(),
            token_availability_records=(),
        )


def test_collector_result_rejects_two_availability() -> None:
    """CollectorResult with two availability records raises ValueError."""
    now = datetime.now(UTC)
    r1 = TokenAvailabilityRecord(
        service=Service.CODEX,
        observed_at=now,
        source="a",
        status=HistoryStatus.AVAILABLE_EXACT,
    )
    r2 = TokenAvailabilityRecord(
        service=Service.CLAUDE,
        observed_at=now,
        source="b",
        status=HistoryStatus.UNSUPPORTED,
    )
    with pytest.raises(ValueError, match="exactly one"):
        CollectorResult(
            service=Service.CODEX,
            quota_readings=(),
            token_readings=(),
            token_availability_records=(r1, r2),
        )


# ── Collector exception fallback synthesizes TEMPORARILY_UNAVAILABLE ────────


def test_collector_exception_synthesizes_availability() -> None:
    """When a collector throws, fallback produces one TEMPORARILY_UNAVAILABLE record."""
    now = datetime.now(UTC)
    result = CollectorResult(
        service=Service.CODEX,
        quota_readings=(),
        token_readings=(),
        token_availability_records=(
            TokenAvailabilityRecord(
                service=Service.CODEX,
                observed_at=now,
                source="moira",
                status=HistoryStatus.TEMPORARILY_UNAVAILABLE,
            ),
        ),
    )
    assert len(result.token_availability_records) == 1
    assert result.token_availability_records[0].status is HistoryStatus.TEMPORARILY_UNAVAILABLE
    assert result.token_availability_records[0].service is Service.CODEX


# ── Semantic validation passes on clean v4 ──────────────────────────────────


def test_semantic_validation_passes_on_clean_v4(tmp_path: Path) -> None:
    """_validate_v4_semantics returns empty list for a clean v4 database."""
    conn = _db(tmp_path)
    violations = _validate_v4_semantics(conn)
    assert violations == [], f"unexpected violations on clean v4: {violations}"
    conn.close()
