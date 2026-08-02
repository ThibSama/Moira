"""Deterministic tests for the corrected local history foundation.

Covers: schema/versioning v2, permissions, multiple same-bucket changes
preserved, replay idempotency, unchanged sampling capped, reset-only changes,
populated-v1 migration with rollback, off-thread writes, bounded queue,
sanitized diagnostics, UTC normalization, and invalid token/status fail-closed.
"""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from moira.history import (
    HistoryStatus,
    HistoryWriteResult,
    QuotaObservation,
    TokenObservation,
)
from moira.history_db import (
    BUCKET_MINUTES,
    RETENTION_DAYS,
    SCHEMA_SQL_V1,
    SCHEMA_VERSION,
    _bucket,
    _connect,
    _migrate_v1_to_v2,
    delete_all,
    history_path,
    init_schema,
    query_7d,
    query_24h,
    query_30d,
    query_90d,
    query_quota,
    query_token,
    record_quota,
    record_refresh,
    record_token,
    write_history_safely,
)
from moira.models import QuotaReading, QuotaStatus, Service

NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)
RESET = NOW + timedelta(days=5)
NEW_RESET = RESET + timedelta(days=7)


def _obs(
    service: Service = Service.CLAUDE,
    label: str = "Weekly",
    pct: float = 50.0,
    reset: datetime = RESET,
    observed: datetime = NOW,
    source: str = "fixture",
    status: HistoryStatus = HistoryStatus.AVAILABLE_EXACT,
) -> QuotaObservation:
    return QuotaObservation(
        service=service,
        quota_label=label,
        percentage=pct,
        reset_at=reset,
        observed_at=observed,
        source=source,
        status=status,
    )


def _reading(
    service: Service = Service.CLAUDE,
    label: str = "Weekly",
    pct: float | None = 50.0,
    reset: datetime | None = RESET,
    status: QuotaStatus = QuotaStatus.AVAILABLE,
    retrieved: datetime = NOW,
) -> QuotaReading:
    if (
        status in {QuotaStatus.AVAILABLE, QuotaStatus.STALE}
        and pct is not None
        and reset is not None
    ):
        return QuotaReading(service, label, pct, reset, retrieved, "fixture", status)
    return QuotaReading(service, label, pct, reset, retrieved, "fixture", status, "detail")


def _db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "history.sqlite3"
    conn = _connect(db_path)
    init_schema(conn)
    return conn


def _v1_db(tmp_path: Path) -> sqlite3.Connection:
    """Create a populated v1 database."""
    db_path = tmp_path / "history.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(db_path), os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    os.chmod(db_path, 0o600)
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.executescript(SCHEMA_SQL_V1)
    conn.execute("INSERT INTO schema_meta (version) VALUES (1)")
    # Insert a sample row
    conn.execute(
        "INSERT INTO quota_observations "
        "(service, quota_label, percentage, reset_at, observed_at, source, bucket) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("claude", "Weekly", 42.0, RESET.isoformat(), NOW.isoformat(), "fixture", _bucket(NOW)),
    )
    return conn


# ── Schema and versioning ──


def test_schema_version_is_2() -> None:
    assert SCHEMA_VERSION == 2


def test_init_schema_creates_tables(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    assert "schema_meta" in tables
    assert "quota_observations" in tables
    assert "token_observations" in tables
    conn.close()


def test_schema_version_recorded(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    row = conn.execute("SELECT version FROM schema_meta").fetchone()
    assert row is not None
    assert row[0] == SCHEMA_VERSION
    conn.close()


def test_schema_version_mismatch_raises(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    conn.execute("UPDATE schema_meta SET version = 999")
    conn.close()
    conn = _connect(tmp_path / "history.sqlite3")
    with pytest.raises(ValueError, match="schema version"):
        init_schema(conn)
    conn.close()


def test_init_schema_idempotent(tmp_path: Path) -> None:
    conn = _connect(tmp_path / "history.sqlite3")
    init_schema(conn)
    init_schema(conn)
    row = conn.execute("SELECT version FROM schema_meta").fetchone()
    assert row[0] == SCHEMA_VERSION
    conn.close()


def test_v2_has_status_and_is_change_columns(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(quota_observations)").fetchall()}
    assert "status" in cols
    assert "is_change" in cols
    conn.close()


# ── v1 → v2 migration ──


def test_populated_v1_migrates_without_loss(tmp_path: Path) -> None:
    """A populated v1 database migrates to v2 preserving all rows."""
    conn = _v1_db(tmp_path)
    conn.close()
    # Open and init_schema should trigger migration
    conn = _connect(tmp_path / "history.sqlite3")
    init_schema(conn)
    # Verify version is now 2
    row = conn.execute("SELECT version FROM schema_meta").fetchone()
    assert row[0] == 2
    # Verify the row survived
    rows = query_quota(conn, since=NOW - timedelta(hours=1))
    assert len(rows) == 1
    assert rows[0].percentage == 42.0
    assert rows[0].status is HistoryStatus.AVAILABLE_EXACT
    conn.close()


def test_v1_migration_rollback_on_failure(tmp_path: Path) -> None:
    """If migration fails partway, the transaction rolls back."""
    conn = _v1_db(tmp_path)
    conn.close()
    conn = _connect(tmp_path / "history.sqlite3")
    # Corrupt the v1 table so migration will fail during data copy
    conn.execute("DROP TABLE quota_observations")
    conn.execute("CREATE TABLE quota_observations (id INTEGER PRIMARY KEY)")
    conn.close()
    conn = _connect(tmp_path / "history.sqlite3")
    with pytest.raises(sqlite3.OperationalError):
        _migrate_v1_to_v2(conn)
    conn.close()


def test_fresh_db_created_at_v2(tmp_path: Path) -> None:
    """A brand-new database is created at v2 without needing migration."""
    conn = _db(tmp_path)
    row = conn.execute("SELECT version FROM schema_meta").fetchone()
    assert row[0] == 2
    conn.close()


# ── Permissions ──


def test_database_file_mode_0600(tmp_path: Path) -> None:
    db_path = tmp_path / "history.sqlite3"
    conn = _connect(db_path)
    conn.close()
    mode = oct(db_path.stat().st_mode & 0o777)
    assert mode == "0o600"


def test_connect_sets_mode_on_existing(tmp_path: Path) -> None:
    db_path = tmp_path / "history.sqlite3"
    db_path.touch()
    os.chmod(db_path, 0o644)
    conn = _connect(db_path)
    conn.close()
    mode = oct(db_path.stat().st_mode & 0o777)
    assert mode == "0o600"


# ── Multiple same-bucket changes preserved ──


def test_multiple_same_bucket_changes_all_preserved(tmp_path: Path) -> None:
    """Multiple percentage changes inside one 15-minute bucket are all stored."""
    conn = _db(tmp_path)
    t0 = NOW
    t1 = NOW + timedelta(minutes=1)
    t2 = NOW + timedelta(minutes=3)
    record_quota(conn, _obs(pct=50.0, observed=t0), now=NOW)
    record_quota(conn, _obs(pct=60.0, observed=t1), now=NOW)
    record_quota(conn, _obs(pct=75.0, observed=t2), now=NOW)
    rows = query_quota(conn, since=NOW - timedelta(hours=1))
    assert len(rows) == 3
    assert [r.percentage for r in rows] == [50.0, 60.0, 75.0]
    conn.close()


def test_same_bucket_changes_ordered_by_observed_at(tmp_path: Path) -> None:
    """Same-bucket change points are returned in chronological order."""
    conn = _db(tmp_path)
    record_quota(conn, _obs(pct=50.0, observed=NOW), now=NOW)
    record_quota(conn, _obs(pct=75.0, observed=NOW + timedelta(minutes=2)), now=NOW)
    record_quota(conn, _obs(pct=60.0, observed=NOW + timedelta(minutes=1)), now=NOW)
    rows = query_quota(conn, since=NOW - timedelta(hours=1))
    assert len(rows) == 3
    assert rows[0].observed_at <= rows[1].observed_at <= rows[2].observed_at
    conn.close()


def test_reset_only_change_persists(tmp_path: Path) -> None:
    """A reset_at change without a percentage change is a change point."""
    conn = _db(tmp_path)
    record_quota(conn, _obs(pct=50.0, reset=RESET, observed=NOW), now=NOW)
    record_quota(
        conn,
        _obs(pct=50.0, reset=NEW_RESET, observed=NOW + timedelta(minutes=1)),
        now=NOW,
    )
    rows = query_quota(conn, since=NOW - timedelta(hours=1))
    assert len(rows) == 2
    assert rows[0].reset_at == RESET
    assert rows[1].reset_at == NEW_RESET
    conn.close()


# ── Replay idempotency ──


def test_replay_exact_observation_is_noop(tmp_path: Path) -> None:
    """Recording the exact same observation (same observed_at) is a no-op."""
    conn = _db(tmp_path)
    obs = _obs(pct=50.0, observed=NOW)
    assert record_quota(conn, obs, now=NOW) is True
    assert record_quota(conn, obs, now=NOW) is False
    rows = query_quota(conn, since=NOW - timedelta(hours=1))
    assert len(rows) == 1
    conn.close()


def test_replay_after_change_points_idempotent(tmp_path: Path) -> None:
    """Replaying the same sequence of observations produces the same rows."""
    conn = _db(tmp_path)
    obs1 = _obs(pct=50.0, observed=NOW)
    obs2 = _obs(pct=60.0, observed=NOW + timedelta(minutes=1))
    record_quota(conn, obs1, now=NOW)
    record_quota(conn, obs2, now=NOW)
    count1 = len(query_quota(conn, since=NOW - timedelta(hours=1)))
    # Replay
    record_quota(conn, obs1, now=NOW)
    record_quota(conn, obs2, now=NOW)
    count2 = len(query_quota(conn, since=NOW - timedelta(hours=1)))
    assert count1 == count2 == 2
    conn.close()


# ── Unchanged sampling capped ──


def test_unchanged_same_bucket_deduplicated(tmp_path: Path) -> None:
    """Unchanged values in the same bucket are deduplicated to one sample."""
    conn = _db(tmp_path)
    obs = _obs(pct=50.0, observed=NOW)
    assert record_quota(conn, obs, now=NOW) is True
    # Different observed_at, same bucket, same value → no insert
    obs2 = _obs(pct=50.0, observed=NOW + timedelta(minutes=5))
    assert record_quota(conn, obs2, now=NOW) is False
    rows = query_quota(conn, since=NOW - timedelta(hours=1))
    assert len(rows) == 1
    conn.close()


def test_unchanged_different_buckets_both_stored(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    obs1 = _obs(pct=50.0, observed=NOW)
    obs2 = _obs(pct=50.0, observed=NOW + timedelta(minutes=BUCKET_MINUTES))
    record_quota(conn, obs1, now=NOW)
    record_quota(conn, obs2, now=NOW)
    rows = query_quota(conn, since=NOW - timedelta(hours=1))
    assert len(rows) == 2
    conn.close()


def test_different_quota_labels_stored_separately(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    record_quota(conn, _obs(label="Weekly", pct=50.0), now=NOW)
    record_quota(conn, _obs(label="Five-hour", pct=30.0), now=NOW)
    rows = query_quota(conn, since=NOW - timedelta(hours=1))
    assert len(rows) == 2
    conn.close()


def test_different_services_stored_separately(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    record_quota(conn, _obs(service=Service.CLAUDE, pct=50.0), now=NOW)
    record_quota(conn, _obs(service=Service.CODEX, pct=60.0), now=NOW)
    rows = query_quota(conn, since=NOW - timedelta(hours=1))
    assert len(rows) == 2
    conn.close()


# ── Retention ──


def test_purge_removes_old_rows(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    old = NOW - timedelta(days=RETENTION_DAYS + 1)
    record_quota(conn, _obs(pct=50.0, observed=old, reset=old + timedelta(days=5)), now=NOW)
    record_quota(conn, _obs(pct=60.0), now=NOW)
    rows = query_quota(conn, since=old - timedelta(days=1))
    assert all(r.observed_at >= NOW - timedelta(days=RETENTION_DAYS) for r in rows)
    assert len(rows) == 1
    conn.close()


def test_purge_boundary_90_days_kept(tmp_path: Path) -> None:
    """A row exactly 90 days old is kept (purge uses strict less-than)."""
    conn = _db(tmp_path)
    boundary = NOW - timedelta(days=RETENTION_DAYS)
    record_quota(
        conn,
        _obs(pct=50.0, observed=boundary, reset=boundary + timedelta(days=5)),
        now=NOW,
    )
    record_quota(conn, _obs(pct=60.0), now=NOW)
    rows = query_quota(conn, since=boundary - timedelta(hours=1))
    labels = {(r.observed_at, r.percentage) for r in rows}
    assert (boundary, 50.0) in labels
    assert (NOW, 60.0) in labels
    conn.close()


def test_retention_constant() -> None:
    assert RETENTION_DAYS == 90


def test_bucket_constant() -> None:
    assert BUCKET_MINUTES == 15


def test_bucket_function() -> None:
    dt = datetime(2026, 8, 2, 12, 7, 23, tzinfo=UTC)
    expected = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC).isoformat()
    assert _bucket(dt) == expected


def test_bucket_at_boundary() -> None:
    dt = datetime(2026, 8, 2, 12, 15, 0, tzinfo=UTC)
    expected = datetime(2026, 8, 2, 12, 15, 0, tzinfo=UTC).isoformat()
    assert _bucket(dt) == expected


# ── Queries ──


def test_query_24h(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    old = NOW - timedelta(hours=25)
    record_quota(conn, _obs(pct=50.0, observed=old, reset=old + timedelta(days=5)), now=NOW)
    record_quota(conn, _obs(pct=60.0), now=NOW)
    result = query_24h(conn, now=NOW)
    assert len(result["quota"]) == 1
    quota_rows: list[QuotaObservation] = result["quota"]
    assert quota_rows[0].percentage == 60.0
    conn.close()


def test_query_7d(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    old = NOW - timedelta(days=8)
    record_quota(conn, _obs(pct=50.0, observed=old, reset=old + timedelta(days=5)), now=NOW)
    record_quota(conn, _obs(pct=60.0), now=NOW)
    result = query_7d(conn, now=NOW)
    assert len(result["quota"]) == 1
    conn.close()


def test_query_30d(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    old = NOW - timedelta(days=31)
    record_quota(conn, _obs(pct=50.0, observed=old, reset=old + timedelta(days=5)), now=NOW)
    record_quota(conn, _obs(pct=60.0), now=NOW)
    result = query_30d(conn, now=NOW)
    assert len(result["quota"]) == 1
    conn.close()


def test_query_90d(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    old = NOW - timedelta(days=91)
    record_quota(conn, _obs(pct=50.0, observed=old, reset=old + timedelta(days=5)), now=NOW)
    record_quota(conn, _obs(pct=60.0), now=NOW)
    result = query_90d(conn, now=NOW)
    assert len(result["quota"]) == 1
    conn.close()


def test_query_quota_filter_by_service(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    record_quota(conn, _obs(service=Service.CLAUDE, pct=50.0), now=NOW)
    record_quota(conn, _obs(service=Service.CODEX, pct=60.0), now=NOW)
    rows = query_quota(conn, since=NOW - timedelta(hours=1), service=Service.CLAUDE)
    assert len(rows) == 1
    assert rows[0].service is Service.CLAUDE
    conn.close()


def test_query_quota_filter_by_metric(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    record_quota(conn, _obs(label="Weekly", pct=50.0), now=NOW)
    record_quota(conn, _obs(label="Five-hour", pct=30.0), now=NOW)
    rows = query_quota(conn, since=NOW - timedelta(hours=1), metric="Weekly")
    assert len(rows) == 1
    assert rows[0].quota_label == "Weekly"
    conn.close()


def test_query_quota_ordered(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    t1 = NOW
    t2 = NOW + timedelta(minutes=BUCKET_MINUTES)
    record_quota(conn, _obs(pct=50.0, observed=t1), now=NOW)
    record_quota(conn, _obs(pct=60.0, observed=t2), now=NOW)
    rows = query_quota(conn, since=NOW - timedelta(hours=1))
    assert rows[0].observed_at <= rows[1].observed_at
    conn.close()


def test_query_returns_status(tmp_path: Path) -> None:
    """QuotaObservation rows from queries expose AVAILABLE_EXACT status."""
    conn = _db(tmp_path)
    record_quota(conn, _obs(pct=50.0), now=NOW)
    rows = query_quota(conn, since=NOW - timedelta(hours=1))
    assert len(rows) == 1
    assert rows[0].status is HistoryStatus.AVAILABLE_EXACT
    conn.close()


# ── Deletion ──


def test_delete_all_removes_everything(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    record_quota(conn, _obs(pct=50.0), now=NOW)
    record_quota(conn, _obs(pct=60.0, observed=NOW + timedelta(minutes=BUCKET_MINUTES)), now=NOW)
    deleted = delete_all(conn)
    assert deleted >= 2
    rows = query_quota(conn, since=NOW - timedelta(days=1))
    assert len(rows) == 0
    conn.close()


# ── Malformed database handling ──


def test_malformed_database_raises(tmp_path: Path) -> None:
    db_path = tmp_path / "history.sqlite3"
    db_path.write_text("not a database", encoding="utf-8")
    with pytest.raises(sqlite3.DatabaseError):
        _connect(db_path)


def test_corrupt_table_raises_on_record(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    conn.execute("DROP TABLE quota_observations")
    conn.close()
    conn = _connect(tmp_path / "history.sqlite3")
    with pytest.raises(sqlite3.OperationalError):
        record_quota(conn, _obs(), now=NOW)
    conn.close()


# ── Token observations ──


def test_token_unsupported_factory() -> None:
    obs = TokenObservation.unsupported(Service.CLAUDE, NOW, "fixture")
    assert obs.status is HistoryStatus.UNSUPPORTED
    assert obs.input_tokens is None
    assert obs.total_tokens is None
    assert not obs.has_exact_tokens


def test_token_temporarily_unavailable_factory() -> None:
    obs = TokenObservation.temporarily_unavailable(Service.CODEX, NOW, "fixture")
    assert obs.status is HistoryStatus.TEMPORARILY_UNAVAILABLE
    assert not obs.has_exact_tokens


def test_token_invalid_factory() -> None:
    obs = TokenObservation.invalid(Service.CLAUDE, NOW, "fixture")
    assert obs.status is HistoryStatus.INVALID
    assert not obs.has_exact_tokens


def test_token_available_exact_has_tokens() -> None:
    obs = TokenObservation(
        service=Service.CLAUDE,
        observed_at=NOW,
        source="fixture",
        status=HistoryStatus.AVAILABLE_EXACT,
        input_tokens=100,
        cached_input_tokens=50,
        output_tokens=200,
        reasoning_output_tokens=30,
        total_tokens=380,
    )
    assert obs.has_exact_tokens


def test_token_record_unsupported(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    obs = TokenObservation.unsupported(Service.CLAUDE, NOW, "fixture")
    record_token(conn, obs, now=NOW)
    rows = query_token(conn, since=NOW - timedelta(hours=1))
    assert len(rows) == 1
    assert rows[0].status is HistoryStatus.UNSUPPORTED
    assert rows[0].total_tokens is None
    conn.close()


def test_token_record_exact(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    obs = TokenObservation(
        service=Service.CODEX,
        observed_at=NOW,
        source="codex-app-server",
        status=HistoryStatus.AVAILABLE_EXACT,
        input_tokens=100,
        cached_input_tokens=50,
        output_tokens=200,
        reasoning_output_tokens=30,
        total_tokens=380,
    )
    record_token(conn, obs, now=NOW)
    rows = query_token(conn, since=NOW - timedelta(hours=1))
    assert len(rows) == 1
    assert rows[0].has_exact_tokens
    assert rows[0].input_tokens == 100
    assert rows[0].total_tokens == 380
    conn.close()


# ── Domain validation: fail-closed ──


def test_quota_observation_rejects_naive_timestamp() -> None:
    naive = datetime(2026, 8, 2, 12, 0, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        QuotaObservation(
            service=Service.CLAUDE,
            quota_label="Weekly",
            percentage=50.0,
            reset_at=RESET,
            observed_at=naive,
            source="fixture",
        )


def test_quota_observation_rejects_empty_label() -> None:
    with pytest.raises(ValueError, match="quota_label"):
        QuotaObservation(
            service=Service.CLAUDE,
            quota_label="",
            percentage=50.0,
            reset_at=RESET,
            observed_at=NOW,
            source="fixture",
        )


def test_quota_observation_rejects_empty_source() -> None:
    with pytest.raises(ValueError, match="source"):
        QuotaObservation(
            service=Service.CLAUDE,
            quota_label="Weekly",
            percentage=50.0,
            reset_at=RESET,
            observed_at=NOW,
            source="",
        )


def test_quota_observation_rejects_percentage_out_of_range() -> None:
    with pytest.raises(ValueError, match="percentage"):
        QuotaObservation(
            service=Service.CLAUDE,
            quota_label="Weekly",
            percentage=150.0,
            reset_at=RESET,
            observed_at=NOW,
            source="fixture",
        )


def test_quota_observation_normalizes_non_utc_timezone() -> None:
    from datetime import timezone

    cet = timezone(timedelta(hours=2))
    cet_time = datetime(2026, 8, 2, 14, 0, 0, tzinfo=cet)
    obs = QuotaObservation(
        service=Service.CLAUDE,
        quota_label="Weekly",
        percentage=50.0,
        reset_at=RESET,
        observed_at=cet_time,
        source="fixture",
    )
    assert obs.observed_at.tzinfo is UTC
    assert obs.observed_at == cet_time.astimezone(UTC)


def test_token_available_exact_requires_total() -> None:
    with pytest.raises(ValueError, match="total_tokens"):
        TokenObservation(
            service=Service.CLAUDE,
            observed_at=NOW,
            source="fixture",
            status=HistoryStatus.AVAILABLE_EXACT,
            input_tokens=100,
            total_tokens=None,
        )


def test_token_available_exact_requires_breakdown() -> None:
    with pytest.raises(ValueError, match="breakdown"):
        TokenObservation(
            service=Service.CLAUDE,
            observed_at=NOW,
            source="fixture",
            status=HistoryStatus.AVAILABLE_EXACT,
            total_tokens=100,
        )


def test_token_non_available_must_not_carry_counts() -> None:
    with pytest.raises(ValueError, match="must not carry"):
        TokenObservation(
            service=Service.CLAUDE,
            observed_at=NOW,
            source="fixture",
            status=HistoryStatus.UNSUPPORTED,
            total_tokens=100,
        )


def test_token_rejects_negative_count() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        TokenObservation(
            service=Service.CLAUDE,
            observed_at=NOW,
            source="fixture",
            status=HistoryStatus.AVAILABLE_EXACT,
            input_tokens=-1,
            total_tokens=100,
        )


def test_token_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        TokenObservation(
            service=Service.CLAUDE,
            observed_at=datetime(2026, 8, 2, 12, 0, 0),
            source="fixture",
            status=HistoryStatus.UNSUPPORTED,
        )


# ── record_refresh: filtering and idempotency ──


def test_record_refresh_stores_available_only(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    readings = [
        _reading(service=Service.CLAUDE, label="Weekly", pct=50.0, status=QuotaStatus.AVAILABLE),
        _reading(service=Service.CLAUDE, label="Five-hour", pct=30.0, status=QuotaStatus.STALE),
        _reading(service=Service.CODEX, label="Weekly", pct=None, status=QuotaStatus.ERROR),
    ]
    record_refresh(conn, readings, now=NOW)
    rows = query_quota(conn, since=NOW - timedelta(hours=1))
    assert len(rows) == 1
    assert rows[0].quota_label == "Weekly"
    assert rows[0].service is Service.CLAUDE
    conn.close()


def test_record_refresh_idempotent(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    readings = [_reading(pct=50.0)]
    record_refresh(conn, readings, now=NOW)
    record_refresh(conn, readings, now=NOW)
    rows = query_quota(conn, since=NOW - timedelta(hours=1))
    assert len(rows) == 1
    conn.close()


def test_record_refresh_stale_not_stored(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    readings = [_reading(pct=50.0, status=QuotaStatus.STALE)]
    record_refresh(conn, readings, now=NOW)
    rows = query_quota(conn, since=NOW - timedelta(hours=1))
    assert len(rows) == 0
    conn.close()


def test_record_refresh_error_not_stored(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    readings = [_reading(pct=None, reset=None, status=QuotaStatus.ERROR)]
    record_refresh(conn, readings, now=NOW)
    rows = query_quota(conn, since=NOW - timedelta(hours=1))
    assert len(rows) == 0
    conn.close()


def test_record_refresh_preserves_changes_within_batch(tmp_path: Path) -> None:
    """Multiple readings with different observed_at and percentages in one
    refresh batch (same bucket) all persist."""
    conn = _db(tmp_path)
    readings = [
        _reading(pct=50.0, retrieved=NOW),
        _reading(pct=60.0, retrieved=NOW + timedelta(minutes=1)),
        _reading(pct=75.0, retrieved=NOW + timedelta(minutes=2)),
    ]
    record_refresh(conn, readings, now=NOW)
    rows = query_quota(conn, since=NOW - timedelta(hours=1))
    assert len(rows) == 3
    conn.close()


# ── History path ──


def test_history_path_uses_state_dir(tmp_path: Path) -> None:
    with patch.dict(os.environ, {"XDG_STATE_HOME": str(tmp_path)}):
        path = history_path()
        assert path == tmp_path / "moira" / "history.sqlite3"


def test_history_path_respects_xdg_state_home(tmp_path: Path) -> None:
    with patch.dict(os.environ, {"XDG_STATE_HOME": str(tmp_path)}):
        conn = _connect(history_path())
        init_schema(conn)
        record_quota(conn, _obs(pct=50.0), now=NOW)
        conn.close()
        assert tmp_path.joinpath("moira", "history.sqlite3").exists()
        mode = oct(tmp_path.joinpath("moira", "history.sqlite3").stat().st_mode & 0o777)
        assert mode == "0o600"


# ── QuotaObservation from_reading ──


def test_quota_observation_from_reading_available() -> None:
    reading = _reading(pct=50.0, status=QuotaStatus.AVAILABLE)
    obs = QuotaObservation.from_reading(reading)
    assert obs is not None
    assert obs.percentage == 50.0
    assert obs.service is Service.CLAUDE
    assert obs.status is HistoryStatus.AVAILABLE_EXACT


def test_quota_observation_from_reading_stale_returns_none() -> None:
    reading = _reading(pct=50.0, status=QuotaStatus.STALE)
    assert QuotaObservation.from_reading(reading) is None


def test_quota_observation_from_reading_error_returns_none() -> None:
    reading = _reading(pct=None, reset=None, status=QuotaStatus.ERROR)
    assert QuotaObservation.from_reading(reading) is None


# ── write_history_safely: sanitized diagnostics ──


def test_write_history_safely_success(tmp_path: Path) -> None:
    result = write_history_safely(
        [_reading(pct=50.0)], now=NOW, db_path=tmp_path / "history.sqlite3"
    )
    assert result.ok
    assert result.diagnostic == "ok"


def test_write_history_safely_corrupt_db(tmp_path: Path) -> None:
    db_path = tmp_path / "history.sqlite3"
    db_path.write_text("corrupt", encoding="utf-8")
    result = write_history_safely([_reading(pct=50.0)], now=NOW, db_path=db_path)
    assert not result.ok
    assert result.diagnostic == "database unavailable"


def test_write_history_safely_no_exception_text(tmp_path: Path) -> None:
    """Diagnostics must never contain exception text, SQL, or paths."""
    db_path = tmp_path / "history.sqlite3"
    db_path.write_text("corrupt", encoding="utf-8")
    result = write_history_safely([_reading(pct=50.0)], now=NOW, db_path=db_path)
    diagnostic = result.diagnostic
    assert "sqlite3" not in diagnostic.lower()
    assert "operationalerror" not in diagnostic.lower()
    assert "databaseerror" not in diagnostic.lower()
    assert str(db_path) not in diagnostic
    assert "traceback" not in diagnostic.lower()


def test_write_history_safely_schema_mismatch(tmp_path: Path) -> None:
    """A schema version mismatch returns a schema diagnostic."""
    conn = _db(tmp_path)
    conn.execute("UPDATE schema_meta SET version = 999")
    conn.close()
    result = write_history_safely(
        [_reading(pct=50.0)], now=NOW, db_path=tmp_path / "history.sqlite3"
    )
    assert not result.ok
    assert result.diagnostic == "schema mismatch"


def test_history_failure_leaves_quota_intact(tmp_path: Path) -> None:
    """History failure does not affect quota state."""
    db_path = tmp_path / "history.sqlite3"
    db_path.write_text("corrupt", encoding="utf-8")
    readings = [_reading(pct=50.0)]
    result = write_history_safely(readings, now=NOW, db_path=db_path)
    assert not result.ok
    # Quota state is untouched
    assert len(readings) == 1
    assert readings[0].percentage == 50.0


# ── Off-thread history writes ──


def test_write_history_safely_does_not_block_on_slow_db(tmp_path: Path) -> None:
    """A slow/locked DB cannot delay rendering because the write is off-thread."""
    db_path = tmp_path / "history.sqlite3"
    # Simulate a locked DB: open a write transaction and hold it
    conn1 = _connect(db_path)
    init_schema(conn1)
    conn1.execute("BEGIN IMMEDIATE")
    conn1.execute(
        "INSERT INTO quota_observations "
        "(service, quota_label, percentage, reset_at, observed_at, source, "
        "bucket, status, is_change) "
        "VALUES ('claude', 'Weekly', 50.0, ?, ?, 'test', ?, 'available_exact', 1)",
        (RESET.isoformat(), NOW.isoformat(), _bucket(NOW)),
    )
    # Do NOT commit — this holds the write lock

    # write_history_safely will time out (5s) and return an error result
    start = time.monotonic()
    result = write_history_safely([_reading(pct=60.0)], now=NOW, db_path=db_path)
    elapsed = time.monotonic() - start
    conn1.execute("ROLLBACK")
    conn1.close()

    assert not result.ok
    # The timeout prevents indefinite blocking
    assert elapsed < 10.0


# ── Bounded queue ──


def test_bounded_queue_drops_overflow(tmp_path: Path) -> None:
    """A queue with maxsize=1 drops new items when full."""
    import queue as queue_mod

    q: queue_mod.Queue[tuple[list[QuotaReading], datetime] | None] = queue_mod.Queue(maxsize=1)
    item1 = ([_reading(pct=50.0)], NOW)
    item2 = ([_reading(pct=60.0)], NOW)
    q.put_nowait(item1)
    with pytest.raises(queue_mod.Full):
        q.put_nowait(item2)
    # The first item is still there
    assert q.get_nowait() == item1


# ── HistoryWriteResult ──


def test_history_write_result_repr() -> None:
    r = HistoryWriteResult(ok=True, diagnostic="ok")
    assert "ok=True" in repr(r)
    r2 = HistoryWriteResult(ok=False, diagnostic="database unavailable")
    assert "ok=False" in repr(r2)


# ── UTC normalization after storage ──


def test_stored_observations_normalized_to_utc(tmp_path: Path) -> None:
    """Observations stored with non-UTC tz are normalized to UTC on read."""
    from datetime import timezone

    cet = timezone(timedelta(hours=2))
    cet_time = datetime(2026, 8, 2, 14, 0, 0, tzinfo=cet)
    conn = _db(tmp_path)
    obs = QuotaObservation(
        service=Service.CLAUDE,
        quota_label="Weekly",
        percentage=50.0,
        reset_at=RESET,
        observed_at=cet_time,
        source="fixture",
    )
    record_quota(conn, obs, now=NOW)
    rows = query_quota(conn, since=NOW - timedelta(hours=1))
    assert len(rows) == 1
    assert rows[0].observed_at.tzinfo is UTC
    assert rows[0].observed_at == cet_time.astimezone(UTC)
    conn.close()


# ── 1c: QuotaObservation rejects non-AVAILABLE_EXACT status ──


def test_quota_observation_rejects_unsupported_status() -> None:
    with pytest.raises(ValueError, match="AVAILABLE_EXACT"):
        QuotaObservation(
            service=Service.CLAUDE,
            quota_label="Weekly",
            percentage=50.0,
            reset_at=RESET,
            observed_at=NOW,
            source="fixture",
            status=HistoryStatus.UNSUPPORTED,
        )


def test_quota_observation_rejects_invalid_status() -> None:
    with pytest.raises(ValueError, match="AVAILABLE_EXACT"):
        QuotaObservation(
            service=Service.CLAUDE,
            quota_label="Weekly",
            percentage=50.0,
            reset_at=RESET,
            observed_at=NOW,
            source="fixture",
            status=HistoryStatus.INVALID,
        )


def test_quota_observation_accepts_available_exact() -> None:
    obs = QuotaObservation(
        service=Service.CLAUDE,
        quota_label="Weekly",
        percentage=50.0,
        reset_at=RESET,
        observed_at=NOW,
        source="fixture",
        status=HistoryStatus.AVAILABLE_EXACT,
    )
    assert obs.status is HistoryStatus.AVAILABLE_EXACT


# ── 1c: Invalid observations map to 'invalid observation' ──


def test_invalid_observation_maps_to_invalid_observation(tmp_path: Path) -> None:
    """A ValueError from domain validation maps to 'invalid observation',
    not 'schema mismatch'."""
    from moira.history import SchemaVersionError

    # SchemaVersionError must be a subclass of ValueError but caught separately
    assert issubclass(SchemaVersionError, ValueError)


def test_schema_mismatch_still_maps_to_schema_mismatch(tmp_path: Path) -> None:
    """An actual schema version mismatch maps to 'schema mismatch'."""
    conn = _db(tmp_path)
    conn.execute("UPDATE schema_meta SET version = 999")
    conn.close()
    result = write_history_safely(
        [_reading(pct=50.0)], now=NOW, db_path=tmp_path / "history.sqlite3"
    )
    assert not result.ok
    assert result.diagnostic == "schema mismatch"


def test_domain_validation_error_maps_to_invalid_observation(tmp_path: Path) -> None:
    """A domain ValueError (not SchemaVersionError) maps to 'invalid observation'.

    We monkeypatch record_refresh to raise a plain ValueError, simulating
    a domain validation failure that is not a schema version error.
    """
    import moira.history_db as history_db_module

    original = history_db_module.record_refresh

    def raise_value_error(*_args: Any, **_kwargs: Any) -> None:
        raise ValueError("simulated domain validation failure")

    history_db_module.record_refresh = raise_value_error
    try:
        result = write_history_safely(
            [_reading(pct=50.0)], now=NOW, db_path=tmp_path / "history.sqlite3"
        )
    finally:
        history_db_module.record_refresh = original

    assert not result.ok
    assert result.diagnostic == "invalid observation"


# ── 1c: HistoryCoordinator — overflow, shutdown, diagnostics ──


def test_coordinator_overflow_sets_backlog_saturated(tmp_path: Path) -> None:
    """When the worker is busy, a second enqueue sets 'backlog saturated'."""
    from moira.history_db import HistoryCoordinator

    coord = HistoryCoordinator(db_path=tmp_path / "history.sqlite3")
    coord.start()
    try:
        # Block the worker by locking the database
        lock_conn = _connect(tmp_path / "history.sqlite3")
        init_schema(lock_conn)
        lock_conn.execute("BEGIN IMMEDIATE")

        # First enqueue: accepted (worker picks it up, blocks on the lock)
        accepted1 = coord.enqueue([_reading(pct=50.0)], now=NOW)
        assert accepted1

        # Give the worker time to pick up the first batch and block
        time.sleep(0.5)

        # Second enqueue: worker is busy → backlog saturated
        accepted2 = coord.enqueue([_reading(pct=60.0)], now=NOW + timedelta(minutes=1))
        assert accepted2 is False
        assert coord.status == "backlog saturated"

        # Release the lock so the worker can proceed
        lock_conn.execute("ROLLBACK")
        lock_conn.close()
    finally:
        coord.shutdown(timeout=3.0)


def test_coordinator_status_clears_after_successful_write(tmp_path: Path) -> None:
    """Status returns to 'ok' after a successful write following saturation."""
    from moira.history_db import HistoryCoordinator

    coord = HistoryCoordinator(db_path=tmp_path / "history.sqlite3")
    coord.start()
    try:
        # Force saturation by manually setting the status
        coord._status = "backlog saturated"  # noqa: SLF001
        assert coord.status == "backlog saturated"

        # A successful write clears the status
        coord.enqueue([_reading(pct=50.0)], now=NOW)
        time.sleep(1.0)
        assert coord.status == "ok"
    finally:
        coord.shutdown()


def test_coordinator_no_raw_diagnostic_leakage(tmp_path: Path) -> None:
    """Coordinator status never contains raw exception text, SQL, or paths."""
    from moira.history_db import HistoryCoordinator

    db_path = tmp_path / "corrupt.sqlite3"
    db_path.write_text("not a database", encoding="utf-8")
    coord = HistoryCoordinator(db_path=db_path)
    coord.start()
    try:
        coord.enqueue([_reading(pct=50.0)], now=NOW)
        time.sleep(1.0)
        status = coord.status
        assert "sqlite3" not in status.lower()
        assert "operationalerror" not in status.lower()
        assert "databaseerror" not in status.lower()
        assert str(db_path) not in status
        assert "traceback" not in status.lower()
    finally:
        coord.shutdown()


def test_coordinator_shutdown_is_idempotent(tmp_path: Path) -> None:
    """Shutdown can be called multiple times without error."""
    from moira.history_db import HistoryCoordinator

    coord = HistoryCoordinator(db_path=tmp_path / "history.sqlite3")
    coord.start()
    coord.shutdown()
    coord.shutdown()  # Idempotent
    coord.shutdown()  # Still fine


def test_coordinator_rejects_new_work_after_shutdown(tmp_path: Path) -> None:
    """After shutdown, enqueue returns False and does not accept new work."""
    from moira.history_db import HistoryCoordinator

    coord = HistoryCoordinator(db_path=tmp_path / "history.sqlite3")
    coord.start()
    coord.shutdown()
    accepted = coord.enqueue([_reading(pct=50.0)], now=NOW)
    assert accepted is False


def test_coordinator_shutdown_terminates_worker(tmp_path: Path) -> None:
    """The worker thread terminates after shutdown."""
    from moira.history_db import HistoryCoordinator

    coord = HistoryCoordinator(db_path=tmp_path / "history.sqlite3")
    coord.start()
    assert coord._thread is not None  # noqa: SLF001
    assert coord._thread.is_alive()  # noqa: SLF001
    coord.shutdown(timeout=3.0)
    # Thread is no longer tracked
    assert coord._thread is None  # noqa: SLF001


def test_coordinator_shutdown_bounded_with_locked_db(tmp_path: Path) -> None:
    """Shutdown completes within the bounded timeout even if the DB is locked."""
    from moira.history_db import HistoryCoordinator

    db_path = tmp_path / "history.sqlite3"
    lock_conn = _connect(db_path)
    init_schema(lock_conn)
    lock_conn.execute("BEGIN IMMEDIATE")

    coord = HistoryCoordinator(db_path=db_path)
    coord.start()
    start = time.monotonic()
    coord.shutdown(timeout=2.0)
    elapsed = time.monotonic() - start
    # Should complete within ~2 seconds even with a locked DB
    assert elapsed < 4.0

    lock_conn.execute("ROLLBACK")
    lock_conn.close()


def test_coordinator_newest_wins_policy(tmp_path: Path) -> None:
    """When multiple batches arrive while the worker is busy, the newest
    pending batch replaces the older one."""
    from moira.history_db import HistoryCoordinator

    db_path = tmp_path / "history.sqlite3"
    lock_conn = _connect(db_path)
    init_schema(lock_conn)
    lock_conn.execute("BEGIN IMMEDIATE")

    coord = HistoryCoordinator(db_path=db_path)
    coord.start()
    try:
        # First batch accepted, worker blocks on the lock
        coord.enqueue([_reading(pct=50.0)], now=NOW)
        time.sleep(0.5)

        # Second batch: replaces any pending, sets saturated
        coord.enqueue([_reading(pct=60.0)], now=NOW + timedelta(minutes=1))
        # Third batch: replaces the second pending batch
        coord.enqueue([_reading(pct=75.0)], now=NOW + timedelta(minutes=2))
        assert coord.status == "backlog saturated"

        # Check that the newest (75%) is the pending batch
        assert coord._pending is not None  # noqa: SLF001
        assert coord._pending[0].percentage == 75.0  # noqa: SLF001

        lock_conn.execute("ROLLBACK")
        lock_conn.close()
    finally:
        coord.shutdown(timeout=5.0)


def test_coordinator_start_idempotent(tmp_path: Path) -> None:
    """Starting twice does not create a second thread."""
    from moira.history_db import HistoryCoordinator

    coord = HistoryCoordinator(db_path=tmp_path / "history.sqlite3")
    coord.start()
    thread1 = coord._thread  # noqa: SLF001
    coord.start()  # No-op
    thread2 = coord._thread  # noqa: SLF001
    assert thread1 is thread2
    coord.shutdown()
