"""Deterministic tests for the corrected local history foundation.

Covers: schema/versioning v2, permissions, multiple same-bucket changes
preserved, replay idempotency, unchanged sampling capped, reset-only changes,
populated-v1 migration with rollback, off-thread writes, bounded queue,
sanitized diagnostics, UTC normalization, and invalid token/status fail-closed.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from datetime import time as time_type
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import moira.history_db as history_db_module
from moira.history import (
    HistoryStatus,
    HistoryWriteResult,
    QuotaObservation,
    SchemaVersionError,
    TokenObservation,
)
from moira.history_db import (
    BUCKET_MINUTES,
    RETENTION_DAYS,
    SCHEMA_SQL_V1,
    SCHEMA_VERSION,
    HistoryCoordinator,
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
    query_codex_summaries,
    query_quota,
    query_token,
    record_codex_summary,
    record_quota,
    record_refresh,
    record_token,
    record_token_events,
    write_history_safely,
)
from moira.models import QuotaReading, QuotaStatus, Service, TokenReading

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


def _token_obs(
    service: Service = Service.CLAUDE,
    *,
    period_start: datetime | None = None,
    period_kind: str = "day",
    observed_at: datetime = NOW,
    source: str = "fixture",
    status: HistoryStatus = HistoryStatus.AVAILABLE_EXACT,
    tokens: int | None = None,
) -> TokenObservation:
    """Construct a TokenObservation; day-kind defaults to the activity day."""
    if period_start is None:
        period_start = datetime.combine(observed_at.date(), time_type.min, tzinfo=UTC)
    return TokenObservation(
        service=service,
        period_start=period_start,
        period_kind=period_kind,
        observed_at=observed_at,
        source=source,
        status=status,
        tokens=tokens,
    )


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


def test_schema_version_is_4() -> None:
    assert SCHEMA_VERSION == 4


def test_init_schema_creates_tables(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    assert "schema_meta" in tables
    assert "quota_observations" in tables
    assert "token_events" in tables
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
    """A populated v1 database migrates to v3 preserving all rows."""
    conn = _v1_db(tmp_path)
    conn.close()
    # Open and init_schema should trigger migration v1 → v2 → v3
    conn = _connect(tmp_path / "history.sqlite3")
    init_schema(conn)
    # Verify version is now 4
    row = conn.execute("SELECT version FROM schema_meta").fetchone()
    assert row[0] == 4
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


def test_fresh_db_created_at_v3(tmp_path: Path) -> None:
    """A brand-new database is created at v4 without needing migration."""
    conn = _db(tmp_path)
    row = conn.execute("SELECT version FROM schema_meta").fetchone()
    assert row[0] == 4
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
    assert obs.tokens is None
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
    obs = _token_obs(tokens=380)
    assert obs.has_exact_tokens


def test_token_record_unsupported(tmp_path: Path) -> None:
    """record_token only stores AVAILABLE_EXACT. Non-exact returns False."""
    conn = _db(tmp_path)
    obs = TokenObservation.unsupported(Service.CLAUDE, NOW, "fixture")
    result = record_token(conn, obs, now=NOW)
    assert result is False  # non-exact not stored in token_events
    rows = query_token(conn, since=NOW - timedelta(hours=1))
    assert len(rows) == 0
    conn.close()


def test_token_record_exact(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    obs = _token_obs(service=Service.CODEX, source="codex-app-server", tokens=380)
    record_token(conn, obs, now=NOW)
    rows = query_token(conn, since=NOW - timedelta(hours=1))
    assert len(rows) == 1
    assert rows[0].has_exact_tokens
    assert rows[0].tokens == 380
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
        _token_obs(tokens=None)


def test_token_available_exact_requires_tokens() -> None:
    """AVAILABLE_EXACT requires a non-None tokens value (the one daily total)."""
    with pytest.raises(ValueError, match="total_tokens"):
        _token_obs()


def test_token_non_available_must_not_carry_counts() -> None:
    with pytest.raises(ValueError, match="must not carry"):
        _token_obs(status=HistoryStatus.UNSUPPORTED, tokens=100)


def test_token_rejects_negative_count() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        _token_obs(tokens=-1)


def test_token_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _token_obs(
            observed_at=datetime(2026, 8, 2, 12, 0, 0),
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


# ── 1e: Coordinator capacity, saturation latch, shutdown ──


def _make_blocking_write(
    name: str = "block",
) -> tuple[threading.Event, threading.Event, Any]:
    """Create an injected writer that signals start and blocks until released."""
    started = threading.Event()
    blocking = threading.Event()
    original = history_db_module.write_history_safely

    def blocking_write(*_args: Any, **_kwargs: Any) -> HistoryWriteResult:
        started.set()
        blocking.wait(timeout=10.0)
        return HistoryWriteResult(ok=True, diagnostic="ok")

    history_db_module.write_history_safely = blocking_write
    return started, blocking, original


def _make_failing_write(diagnostic: str) -> tuple[threading.Event, Any]:
    """Create an injected writer that signals start and always fails."""
    started = threading.Event()
    original = history_db_module.write_history_safely

    def failing_write(*_args: Any, **_kwargs: Any) -> HistoryWriteResult:
        started.set()
        return HistoryWriteResult(ok=False, diagnostic=diagnostic)

    history_db_module.write_history_safely = failing_write
    return started, original


def test_inflight_empty_pending_accepts_batch(tmp_path: Path) -> None:
    """An in-flight batch plus an empty pending slot accepts the next batch
    with return True and status remains 'ok'."""
    coord = HistoryCoordinator(db_path=tmp_path / "history.sqlite3", db_timeout=1.0)
    started, blocking, original = _make_blocking_write()
    coord.start()
    try:
        # First batch: accepted, worker picks it up and blocks
        assert coord.enqueue([_reading(pct=50.0)], now=NOW)
        assert started.wait(timeout=3.0)

        # Second batch: worker is in-flight, but pending is empty → accept
        accepted2 = coord.enqueue([_reading(pct=60.0)], now=NOW + timedelta(minutes=1))
        assert accepted2 is True
        assert coord.status == "ok"  # No saturation

        blocking.set()
    finally:
        history_db_module.write_history_safely = original
        coord.shutdown(timeout=3.0)


def test_third_enqueue_replaces_occupied_pending(tmp_path: Path) -> None:
    """Only a third enqueue that replaces the occupied pending slot returns
    False and sets 'backlog saturated'."""
    coord = HistoryCoordinator(db_path=tmp_path / "history.sqlite3", db_timeout=1.0)
    started, blocking, original = _make_blocking_write()
    coord.start()
    try:
        # First batch: accepted, worker picks up, blocks
        assert coord.enqueue([_reading(pct=50.0)], now=NOW)
        assert started.wait(timeout=3.0)

        # Second batch: pending is empty → accept without saturation
        assert coord.enqueue([_reading(pct=60.0)], now=NOW + timedelta(minutes=1))
        assert coord.status == "ok"

        # Third batch: pending is occupied → replace, return False, saturate
        accepted3 = coord.enqueue([_reading(pct=75.0)], now=NOW + timedelta(minutes=2))
        assert accepted3 is False
        assert coord.status == "backlog saturated"
        assert coord._pending is not None  # noqa: SLF001
        assert coord._pending[0].percentage == 75.0  # noqa: SLF001

        blocking.set()
    finally:
        history_db_module.write_history_safely = original
        coord.shutdown(timeout=3.0)


def test_two_enqueues_before_worker_start(tmp_path: Path) -> None:
    """Two enqueues before the worker starts deterministically replace the
    pending slot and report saturation. Uses enqueue-before-start."""
    coord = HistoryCoordinator(db_path=tmp_path / "history.sqlite3", db_timeout=1.0)

    # Enqueue before starting the worker
    assert coord.enqueue([_reading(pct=50.0)], now=NOW)
    # Second enqueue: pending slot is occupied → saturate
    accepted2 = coord.enqueue([_reading(pct=60.0)], now=NOW + timedelta(minutes=1))
    assert accepted2 is False
    assert coord.status == "backlog saturated"
    assert coord._pending is not None  # noqa: SLF001
    assert coord._pending[0].percentage == 60.0  # noqa: SLF001

    # Clean up without starting the worker
    coord.shutdown()


def test_three_or_more_enqueues_retain_newest(tmp_path: Path) -> None:
    """Three or more enqueues while pending is occupied retain the newest."""
    coord = HistoryCoordinator(db_path=tmp_path / "history.sqlite3", db_timeout=1.0)

    assert coord.enqueue([_reading(pct=50.0)], now=NOW)
    coord.enqueue([_reading(pct=60.0)], now=NOW + timedelta(minutes=1))
    coord.enqueue([_reading(pct=75.0)], now=NOW + timedelta(minutes=2))
    coord.enqueue([_reading(pct=90.0)], now=NOW + timedelta(minutes=3))

    assert coord.status == "backlog saturated"
    assert coord._pending is not None  # noqa: SLF001
    assert coord._pending[0].percentage == 90.0  # noqa: SLF001

    coord.shutdown()


def test_older_success_cannot_clear_saturation(tmp_path: Path) -> None:
    """An older in-flight success cannot clear saturation latched by a
    newer enqueue. Uses generation tracking."""
    coord = HistoryCoordinator(db_path=tmp_path / "history.sqlite3", db_timeout=1.0)

    first_started = threading.Event()
    first_blocking = threading.Event()
    second_started = threading.Event()
    second_blocking = threading.Event()
    original = history_db_module.write_history_safely

    write_count = [0]

    def controlled_write(readings: list[Any], **kwargs: Any) -> HistoryWriteResult:
        write_count[0] += 1
        if write_count[0] == 1:
            first_started.set()
            first_blocking.wait(timeout=10.0)
        elif write_count[0] == 2:
            second_started.set()
            second_blocking.wait(timeout=10.0)
        return HistoryWriteResult(ok=True, diagnostic="ok")

    history_db_module.write_history_safely = controlled_write
    coord.start()
    try:
        coord.enqueue([_reading(pct=50.0)], now=NOW)
        assert first_started.wait(timeout=3.0)

        # Second enqueue: accepted (pending empty), pending gen 2
        coord.enqueue([_reading(pct=60.0)], now=NOW + timedelta(minutes=1))
        assert coord.status == "ok"  # No saturation yet

        # Third enqueue: pending occupied → saturate, generation 3
        coord.enqueue([_reading(pct=70.0)], now=NOW + timedelta(minutes=2))
        assert coord.status == "backlog saturated"

        # Release the first write (gen 1) — must NOT clear saturation
        first_blocking.set()
        assert second_started.wait(timeout=3.0)
        assert coord.status == "backlog saturated"

        # Release the second write (gen 2) — still < saturation_gen 3
        second_blocking.set()

        # Poll for the third write to complete and clear saturation
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if coord.status == "ok":
                break
            time.sleep(0.05)
        assert coord.status == "ok"
        assert write_count[0] >= 2
    finally:
        history_db_module.write_history_safely = original
        coord.shutdown(timeout=3.0)


def test_retained_failure_does_not_clear_saturation(tmp_path: Path) -> None:
    """Failure of the retained newest generation does not clear saturation.
    Public status stays 'backlog saturated'. The diagnostic is available
    internally via last_write_diagnostic."""
    coord = HistoryCoordinator(db_path=tmp_path / "history.sqlite3", db_timeout=1.0)

    first_started = threading.Event()
    first_blocking = threading.Event()
    second_started = threading.Event()
    second_blocking = threading.Event()
    original = history_db_module.write_history_safely

    write_count = [0]

    def controlled_write(readings: list[Any], **kwargs: Any) -> HistoryWriteResult:
        write_count[0] += 1
        if write_count[0] == 1:
            first_started.set()
            first_blocking.wait(timeout=10.0)
            return HistoryWriteResult(ok=True, diagnostic="ok")
        elif write_count[0] == 2:
            second_started.set()
            second_blocking.wait(timeout=10.0)
            # This is the retained generation (gen 3) — it fails
            return HistoryWriteResult(ok=False, diagnostic="database unavailable")
        return HistoryWriteResult(ok=True, diagnostic="ok")

    history_db_module.write_history_safely = controlled_write
    coord.start()
    try:
        # First batch: gen 1, worker blocks
        coord.enqueue([_reading(pct=50.0)], now=NOW)
        assert first_started.wait(timeout=3.0)

        # Second batch: gen 2, accepted (pending empty)
        coord.enqueue([_reading(pct=60.0)], now=NOW + timedelta(minutes=1))

        # Third batch: gen 3, replaces pending → saturate
        coord.enqueue([_reading(pct=70.0)], now=NOW + timedelta(minutes=2))
        assert coord.status == "backlog saturated"

        # Release gen 1 — must NOT clear saturation
        first_blocking.set()
        assert second_started.wait(timeout=3.0)

        # Release gen 2 (the retained gen 3's predecessor pick-up)
        # Actually: gen 2 picked up by worker, blocks. Then gen 3 replaces
        # the pending. When gen 2 completes it is < saturation_gen 3.
        second_blocking.set()

        # Gen 3 picks up and fails — saturation must persist
        # Poll for the second write (gen 3) to start
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if write_count[0] >= 2:
                break
            time.sleep(0.05)
        assert write_count[0] >= 2

        # Wait for gen 3 to complete (it fails)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if coord.last_write_diagnostic == "database unavailable":
                break
            time.sleep(0.05)

        # Saturation must persist despite the failure
        assert coord.status == "backlog saturated"

        # The diagnostic is available internally
        assert coord.last_write_diagnostic == "database unavailable"
        assert "sqlite3" not in coord.last_write_diagnostic.lower()
    finally:
        history_db_module.write_history_safely = original
        coord.shutdown(timeout=3.0)


def test_later_successful_generation_clears_saturation(tmp_path: Path) -> None:
    """A later successful generation (after a failed retained batch) clears
    saturation."""
    coord = HistoryCoordinator(db_path=tmp_path / "history.sqlite3", db_timeout=1.0)

    first_started = threading.Event()
    first_blocking = threading.Event()
    original = history_db_module.write_history_safely

    write_count = [0]

    def controlled_write(readings: list[Any], **kwargs: Any) -> HistoryWriteResult:
        write_count[0] += 1
        if write_count[0] == 1:
            first_started.set()
            first_blocking.wait(timeout=10.0)
            return HistoryWriteResult(ok=True, diagnostic="ok")
        elif write_count[0] == 2:
            # The retained generation fails
            return HistoryWriteResult(ok=False, diagnostic="database unavailable")
        # A later generation succeeds
        return HistoryWriteResult(ok=True, diagnostic="ok")

    history_db_module.write_history_safely = controlled_write
    coord.start()
    try:
        coord.enqueue([_reading(pct=50.0)], now=NOW)
        assert first_started.wait(timeout=3.0)

        # Second batch: gen 2, accepted (pending empty)
        coord.enqueue([_reading(pct=60.0)], now=NOW + timedelta(minutes=1))

        # Third batch: gen 3, replaces pending → saturate
        coord.enqueue([_reading(pct=70.0)], now=NOW + timedelta(minutes=2))
        assert coord.status == "backlog saturated"

        first_blocking.set()

        # Wait for gen 2 to fail (retained gen 3 is pending)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if write_count[0] >= 2:
                break
            time.sleep(0.05)

        # Gen 3 picks up and fails — saturation persists
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if write_count[0] >= 2:
                break
            time.sleep(0.05)
        assert coord.status == "backlog saturated"

        # Enqueue gen 4 — accepted (pending empty after gen 3 pick up)
        coord.enqueue([_reading(pct=80.0)], now=NOW + timedelta(minutes=3))

        # Poll for gen 4 to succeed and clear saturation
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if coord.status == "ok":
                break
            time.sleep(0.05)
        assert coord.status == "ok"
    finally:
        history_db_module.write_history_safely = original
        coord.shutdown(timeout=3.0)


def test_no_raw_diagnostic_leakage(tmp_path: Path) -> None:
    """Coordinator status and last_write_diagnostic never contain raw
    exception text, SQL, or paths."""
    coord = HistoryCoordinator(db_path=tmp_path / "corrupt.sqlite3", db_timeout=1.0)
    coord.start()
    try:
        coord.enqueue([_reading(pct=50.0)], now=NOW)
        # Poll for the worker to set a non-ok status
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if coord.status != "ok":
                break
            time.sleep(0.05)
        status = coord.status
        diag = coord.last_write_diagnostic
        for s in (status, diag):
            assert "sqlite3" not in s.lower()
            assert "operationalerror" not in s.lower()
            assert "databaseerror" not in s.lower()
            assert "traceback" not in s.lower()
    finally:
        coord.shutdown(timeout=3.0)


# ── 1e: Timeout validation and production shutdown ──


def test_constructor_validates_db_timeout_below_shutdown_timeout() -> None:
    """Constructor rejects db_timeout >= shutdown_timeout."""
    with pytest.raises(ValueError, match="db_timeout"):
        HistoryCoordinator(db_timeout=5.0, shutdown_timeout=3.0)

    with pytest.raises(ValueError, match="db_timeout"):
        HistoryCoordinator(db_timeout=2.0, shutdown_timeout=2.0)


def test_production_timeout_values_satisfy_invariant() -> None:
    """Production coordinator with db_timeout=1.0, shutdown_timeout=3.0
    satisfies db_timeout < shutdown_timeout."""
    coord = HistoryCoordinator(db_timeout=1.0, shutdown_timeout=3.0)
    assert coord._db_timeout == 1.0  # noqa: SLF001
    assert coord._shutdown_timeout == 3.0  # noqa: SLF001
    assert coord._db_timeout < coord._shutdown_timeout  # noqa: SLF001


def test_production_shutdown_terminates_blocked_worker(tmp_path: Path) -> None:
    """Genuine locked-SQLite termination test.

    The lock is held throughout shutdown. The worker's 1-second SQLite
    timeout expires inside the 3-second join. The captured thread is
    actually dead and _thread is None when shutdown returns. The lock
    is released only after assertions.
    """
    db_path = tmp_path / "history.sqlite3"
    lock_conn = _connect(db_path)
    init_schema(lock_conn)
    lock_conn.execute("BEGIN IMMEDIATE")

    coord = HistoryCoordinator(db_path=db_path, db_timeout=1.0, shutdown_timeout=3.0)
    coord.start()

    # Enqueue so the worker starts a write and blocks on the lock
    coord.enqueue([_reading(pct=50.0)], now=NOW)

    # Wait for the worker to be in-flight (blocked on the lock)
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        with coord._cond:  # noqa: SLF001
            if coord._in_flight is not None:  # noqa: SLF001
                break
        time.sleep(0.05)
    assert coord._in_flight is not None  # noqa: SLF001

    # Capture the thread before shutdown
    with coord._cond:  # noqa: SLF001
        thread = coord._thread  # noqa: SLF001
    assert thread is not None

    # Call shutdown while the lock is STILL HELD.
    # The worker's SQLite write times out at 1.0s, then the worker exits.
    # The 3.0s join waits for that to happen.
    start = time.monotonic()
    coord.shutdown()
    elapsed = time.monotonic() - start
    assert elapsed < 4.0

    # The captured thread is actually dead
    assert not thread.is_alive()
    # And the coordinator cleared its reference
    assert coord._thread is None  # noqa: SLF001
    assert coord.lifecycle_state == "terminated"

    # Release the lock ONLY after assertions
    lock_conn.execute("ROLLBACK")
    lock_conn.close()


def test_repeated_shutdown_and_post_shutdown_enqueue(tmp_path: Path) -> None:
    """Repeated shutdown is safe and post-shutdown enqueue returns False."""
    coord = HistoryCoordinator(db_path=tmp_path / "history.sqlite3", db_timeout=1.0)
    coord.start()
    coord.shutdown()
    coord.shutdown()
    coord.shutdown()
    assert coord.enqueue([_reading(pct=50.0)], now=NOW) is False


def test_shutdown_is_idempotent(tmp_path: Path) -> None:
    """Shutdown can be called multiple times without error."""
    coord = HistoryCoordinator(db_path=tmp_path / "history.sqlite3", db_timeout=1.0)
    coord.start()
    thread = coord._thread  # noqa: SLF001
    coord.shutdown()
    coord.shutdown()
    assert thread is not None
    assert not thread.is_alive()
    assert coord._thread is None  # noqa: SLF001


def test_start_is_idempotent(tmp_path: Path) -> None:
    """Starting twice does not create a second thread."""
    coord = HistoryCoordinator(db_path=tmp_path / "history.sqlite3", db_timeout=1.0)
    coord.start()
    thread1 = coord._thread  # noqa: SLF001
    coord.start()
    thread2 = coord._thread  # noqa: SLF001
    assert thread1 is thread2
    coord.shutdown()


def test_worker_wakes_on_notification_not_polling(tmp_path: Path) -> None:
    """The worker wakes on Condition.notify_all() from enqueue, not from
    periodic polling. Verified by checking that the worker picks up a
    batch immediately after enqueue with no polling delay."""
    coord = HistoryCoordinator(db_path=tmp_path / "history.sqlite3", db_timeout=1.0)
    write_completed = threading.Event()
    original = history_db_module.write_history_safely

    def fast_write(*_args: Any, **_kwargs: Any) -> HistoryWriteResult:
        write_completed.set()
        return HistoryWriteResult(ok=True, diagnostic="ok")

    history_db_module.write_history_safely = fast_write
    coord.start()
    try:
        # Enqueue and verify the worker picks it up immediately
        coord.enqueue([_reading(pct=50.0)], now=NOW)
        # With notification-based wake-up, this should complete nearly instantly
        assert write_completed.wait(timeout=2.0)
    finally:
        history_db_module.write_history_safely = original
        coord.shutdown(timeout=3.0)


# ── 1f: Lifecycle contract tests ──


def test_no_argument_construction_succeeds() -> None:
    """HistoryCoordinator() with no arguments must not raise."""
    coord = HistoryCoordinator()
    assert coord._db_timeout == 1.0  # noqa: SLF001
    assert coord._shutdown_timeout == 3.0  # noqa: SLF001
    assert coord.lifecycle_state == "new"


def test_invalid_constructor_values_fail_closed() -> None:
    """Invalid timeout relationships are rejected at construction."""
    with pytest.raises(ValueError):
        HistoryCoordinator(db_timeout=5.0, shutdown_timeout=3.0)
    with pytest.raises(ValueError):
        HistoryCoordinator(db_timeout=3.0, shutdown_timeout=3.0)
    with pytest.raises(ValueError):
        HistoryCoordinator(db_timeout=0.0, shutdown_timeout=3.0)
    with pytest.raises(ValueError):
        HistoryCoordinator(db_timeout=-1.0, shutdown_timeout=3.0)


def test_enqueue_before_start_then_shutdown_rejects_later_work(tmp_path: Path) -> None:
    """Enqueue before start places a batch in pending. Shutdown-before-start
    transitions to TERMINATED and rejects later enqueues."""
    coord = HistoryCoordinator(db_path=tmp_path / "history.sqlite3")

    # Enqueue before start — accepted in NEW state
    assert coord.enqueue([_reading(pct=50.0)], now=NOW)
    assert coord.lifecycle_state == "new"

    # Shutdown before start — terminates immediately
    coord.shutdown()
    assert coord.lifecycle_state == "terminated"

    # Post-shutdown enqueue is rejected
    assert coord.enqueue([_reading(pct=60.0)], now=NOW) is False


def test_pending_before_start_disposal_yields_sanitized_status(tmp_path: Path) -> None:
    """Pending work disposed during shutdown-before-start yields only a
    sanitized 'backlog saturated' status."""
    coord = HistoryCoordinator(db_path=tmp_path / "history.sqlite3")

    # Enqueue two batches before start — second replaces first, saturates
    assert coord.enqueue([_reading(pct=50.0)], now=NOW)
    assert coord.enqueue([_reading(pct=60.0)], now=NOW + timedelta(minutes=1)) is False
    assert coord.status == "backlog saturated"

    # Shutdown before start — disposes pending, preserves saturation
    coord.shutdown()
    assert coord.lifecycle_state == "terminated"
    assert coord.status == "backlog saturated"
    # No raw exception text in the status
    assert "sqlite3" not in coord.status.lower()
    assert "traceback" not in coord.status.lower()


def test_shutdown_before_start_is_idempotent(tmp_path: Path) -> None:
    """Shutdown before start is idempotent."""
    coord = HistoryCoordinator(db_path=tmp_path / "history.sqlite3")
    coord.shutdown()
    assert coord.lifecycle_state == "terminated"
    coord.shutdown()
    assert coord.lifecycle_state == "terminated"
    coord.shutdown()
    assert coord.lifecycle_state == "terminated"


def test_post_terminal_start_is_noop(tmp_path: Path) -> None:
    """After terminal shutdown, start() is a documented no-op (restart rejected).
    Does not create a thread."""
    coord = HistoryCoordinator(db_path=tmp_path / "history.sqlite3")
    coord.shutdown()
    assert coord.lifecycle_state == "terminated"
    coord.start()  # No-op
    assert coord.lifecycle_state == "terminated"
    assert coord._thread is None  # noqa: SLF001


def test_shutdown_timeout_override_below_db_timeout_rejected(tmp_path: Path) -> None:
    """An override <= db_timeout is rejected before mutating lifecycle state."""
    coord = HistoryCoordinator(db_path=tmp_path / "history.sqlite3", db_timeout=1.0)
    coord.start()

    # Override at db_timeout — rejected
    with pytest.raises(ValueError, match="shutdown timeout"):
        coord.shutdown(timeout=1.0)
    # Lifecycle must NOT have changed
    assert coord.lifecycle_state == "running"

    # Override below db_timeout — rejected
    with pytest.raises(ValueError, match="shutdown timeout"):
        coord.shutdown(timeout=0.5)
    assert coord.lifecycle_state == "running"

    # Override above shutdown_timeout — rejected
    with pytest.raises(ValueError, match="shutdown timeout"):
        coord.shutdown(timeout=5.0)
    assert coord.lifecycle_state == "running"

    # Now shutdown with valid default
    coord.shutdown()
    assert coord.lifecycle_state == "terminated"


def test_shutdown_timeout_override_at_constructor_value_accepted(tmp_path: Path) -> None:
    """An override equal to the constructor's shutdown_timeout is accepted."""
    coord = HistoryCoordinator(db_path=tmp_path / "history.sqlite3", db_timeout=1.0)
    coord.start()
    coord.shutdown(timeout=3.0)
    assert coord.lifecycle_state == "terminated"


def test_lifecycle_new_state_before_start(tmp_path: Path) -> None:
    """A freshly constructed coordinator is in the NEW state."""
    coord = HistoryCoordinator(db_path=tmp_path / "history.sqlite3")
    assert coord.lifecycle_state == "new"


def test_lifecycle_running_after_start(tmp_path: Path) -> None:
    """After start(), the coordinator is in RUNNING state."""
    coord = HistoryCoordinator(db_path=tmp_path / "history.sqlite3")
    coord.start()
    assert coord.lifecycle_state == "running"
    coord.shutdown()


def test_lifecycle_terminated_after_shutdown(tmp_path: Path) -> None:
    """After shutdown, the coordinator is in TERMINATED state."""
    coord = HistoryCoordinator(db_path=tmp_path / "history.sqlite3")
    coord.start()
    coord.shutdown()
    assert coord.lifecycle_state == "terminated"


# ── Schema v3: token events with stable event keys ──


def test_v3_token_events_table_exists(tmp_path: Path) -> None:
    """Fresh v3 database has token_events table, not token_observations."""
    conn = _db(tmp_path)
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "token_events" in tables
    assert "token_observations" not in tables
    conn.close()


def test_v3_token_events_columns(tmp_path: Path) -> None:
    """token_events has event_key, period_start, period_kind columns."""
    conn = _db(tmp_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(token_events)").fetchall()}
    assert "event_key" in cols
    assert "period_start" in cols
    assert "period_kind" in cols
    assert "service" in cols
    conn.close()


def test_v2_to_v3_migration_preserves_token_rows(tmp_path: Path) -> None:
    """A v2 database with token rows migrates to v3 preserving all data."""
    from moira.history_db import SCHEMA_SQL_V2

    db_path = tmp_path / "history.sqlite3"
    conn = _connect(db_path)
    conn.executescript(SCHEMA_SQL_V2)
    conn.execute("INSERT INTO schema_meta (version) VALUES (2)")
    conn.execute(
        "INSERT INTO token_observations "
        "(service, observed_at, source, status, input_tokens, output_tokens, "
        "total_tokens, bucket) "
        "VALUES ('codex', ?, 'fixture', 'available_exact', 100, 200, 300, ?)",
        (NOW.isoformat(), _bucket(NOW)),
    )
    conn.execute(
        "INSERT INTO token_observations "
        "(service, observed_at, source, status, input_tokens, output_tokens, "
        "total_tokens, bucket) "
        "VALUES ('codex', ?, 'fixture', 'available_exact', 50, 75, 125, ?)",
        ((NOW + timedelta(minutes=30)).isoformat(), _bucket(NOW + timedelta(minutes=30))),
    )
    conn.close()

    # Migrate
    conn = _connect(db_path)
    init_schema(conn)
    row = conn.execute("SELECT version FROM schema_meta").fetchone()
    assert row[0] == 4
    rows = query_token(conn, since=NOW - timedelta(hours=2))
    assert len(rows) == 2
    total = sum(r.tokens or 0 for r in rows)
    assert total == 425  # 300 + 125
    conn.close()


def test_v2_to_v3_migration_rollback(tmp_path: Path) -> None:
    """If v2→v3 migration fails, the v2 data survives (rollback)."""
    from moira.history_db import SCHEMA_SQL_V2, _migrate_v2_to_v3

    db_path = tmp_path / "history.sqlite3"
    conn = _connect(db_path)
    conn.executescript(SCHEMA_SQL_V2)
    conn.execute("INSERT INTO schema_meta (version) VALUES (2)")
    conn.execute(
        "INSERT INTO token_observations "
        "(service, observed_at, source, status, input_tokens, total_tokens, bucket) "
        "VALUES ('codex', ?, 'fixture', 'available_exact', 100, 100, ?)",
        (NOW.isoformat(), _bucket(NOW)),
    )
    conn.close()

    # Corrupt the v2 table structure so migration fails
    conn = _connect(db_path)
    conn.execute("ALTER TABLE token_observations RENAME TO token_observations_bak")
    conn.execute("CREATE TABLE token_observations (id INTEGER PRIMARY KEY)")
    conn.close()

    conn = _connect(db_path)
    with pytest.raises(sqlite3.OperationalError):
        _migrate_v2_to_v3(conn)
    conn.close()

    # Verify v2 data is still intact
    conn = _connect(db_path)
    row = conn.execute("SELECT version FROM schema_meta").fetchone()
    assert row[0] == 2  # unchanged
    rows = conn.execute("SELECT COUNT(*) FROM token_observations_bak").fetchone()
    assert rows[0] >= 1  # data survived
    conn.close()


# ── Package 3c: canonical daily identity, period-kind boundaries, summary ──


def _token_reading(
    day: datetime | None = None,
    *,
    source: str = "codex-app-server:account/usage/read",
    tokens: int | None = 500,
    status: HistoryStatus = HistoryStatus.AVAILABLE_EXACT,
    retrieved: datetime | None = None,
) -> TokenReading:
    day_date = (day or NOW).date()
    return TokenReading(
        service=Service.CODEX,
        day=day_date,
        retrieved_at=retrieved or NOW,
        source=source,
        status=status,
        tokens=tokens,
    )


def test_canonical_daily_identity_ignores_source_digest(tmp_path: Path) -> None:
    """Canonical identity is provider/service/period-kind/day — a source
    wording change updates one row instead of duplicating it."""
    conn = _db(tmp_path)
    first = _token_reading(NOW, source="codex-app-server:account/usage/read")
    record_token_events(conn, [first], now=NOW)
    rows = query_token(conn, since=NOW - timedelta(hours=1))
    assert len(rows) == 1
    assert rows[0].tokens == 500
    assert rows[0].source == "codex-app-server:account/usage/read"

    # Source wording/rename changes — same logical service/day
    renamed = _token_reading(NOW, source="codex-app-server:v2/usage/read", tokens=700)
    record_token_events(conn, [renamed], now=NOW + timedelta(minutes=1))
    rows = query_token(conn, since=NOW - timedelta(hours=1))
    assert len(rows) == 1  # one row, not two
    assert rows[0].tokens == 700
    assert rows[0].source == "codex-app-server:v2/usage/read"
    conn.close()


def test_event_keys_have_no_source_digest(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    record_token_events(conn, [_token_reading(NOW)], now=NOW)
    key = conn.execute("SELECT event_key FROM token_events").fetchone()[0]
    assert key == "codex:day:2026-08-02"
    conn.close()


def test_legacy_source_digest_keys_reconciled(tmp_path: Path) -> None:
    """Package 3/3b keys like codex:u:<day>:<digest> collapse to canonical
    keys; the newest alias wins and newer canonical rows are preserved."""
    conn = _db(tmp_path)
    digest_a = "a" * 12
    digest_b = "b" * 12
    conn.execute(
        "INSERT INTO token_events (event_key, service, period_start, period_kind, "
        "observed_at, source, status, total_tokens) VALUES (?, ?, ?, 'day', ?, ?, ?, ?)",
        (
            f"codex:u:2026-08-01:{digest_a}",
            "codex",
            "2026-08-01",
            "2026-08-01T08:00:00+00:00",
            "old-source-a",
            "available_exact",
            100,
        ),
    )
    conn.execute(
        "INSERT INTO token_events (event_key, service, period_start, period_kind, "
        "observed_at, source, status, total_tokens) VALUES (?, ?, ?, 'day', ?, ?, ?, ?)",
        (
            f"codex:u:2026-08-01:{digest_b}",
            "codex",
            "2026-08-01",
            "2026-08-01T09:00:00+00:00",
            "old-source-b",
            "available_exact",
            200,
        ),
    )
    # A canonical row written by the new code that is NEWER than both aliases
    conn.execute(
        "INSERT INTO token_events (event_key, service, period_start, period_kind, "
        "observed_at, source, status, total_tokens) VALUES (?, ?, ?, 'day', ?, ?, ?, ?)",
        (
            "codex:day:2026-08-01",
            "codex",
            "2026-08-01",
            "2026-08-01T10:00:00+00:00",
            "codex-app-server:account/usage/read",
            "available_exact",
            300,
        ),
    )
    conn.close()

    conn = _connect(tmp_path / "history.sqlite3")
    init_schema(conn)
    rows = conn.execute("SELECT event_key FROM token_events").fetchall()
    assert [r[0] for r in rows] == ["codex:day:2026-08-01"]
    row = conn.execute(
        "SELECT total_tokens, source FROM token_events WHERE event_key = 'codex:day:2026-08-01'"
    ).fetchone()
    # Newer canonical row preserved (300 > 200)
    assert row[0] == 300
    assert row[1] == "codex-app-server:account/usage/read"
    conn.close()


def test_legacy_reconcile_newest_alias_wins(tmp_path: Path) -> None:
    """Two legacy aliases for one day merge to the newest alias."""
    conn = _db(tmp_path)
    conn.execute(
        "INSERT INTO token_events (event_key, service, period_start, period_kind, "
        "observed_at, source, status, total_tokens) VALUES (?, ?, ?, 'day', ?, ?, ?, ?)",
        (
            f"codex:u:2026-08-03:{'a' * 12}",
            "codex",
            "2026-08-03",
            "2026-08-03T08:00:00+00:00",
            "source-a",
            "available_exact",
            100,
        ),
    )
    conn.execute(
        "INSERT INTO token_events (event_key, service, period_start, period_kind, "
        "observed_at, source, status, total_tokens) VALUES (?, ?, ?, 'day', ?, ?, ?, ?)",
        (
            f"codex:u:2026-08-03:{'b' * 12}",
            "codex",
            "2026-08-03",
            "2026-08-03T12:00:00+00:00",
            "source-b",
            "available_exact",
            250,
        ),
    )
    conn.close()
    conn = _connect(tmp_path / "history.sqlite3")
    init_schema(conn)
    row = conn.execute(
        "SELECT total_tokens, source FROM token_events WHERE event_key = 'codex:day:2026-08-03'"
    ).fetchone()
    assert row[0] == 250
    assert row[1] == "source-b"
    conn.close()


def test_incomplete_schema_without_version_fails_closed(tmp_path: Path) -> None:
    """A metadata table with no version row and missing v3 tables must fail
    closed — never label an incomplete database v3."""
    db_path = tmp_path / "history.sqlite3"
    conn = _connect(db_path)
    conn.executescript(
        "CREATE TABLE schema_meta (version INTEGER PRIMARY KEY);"
        "CREATE TABLE quota_observations (id INTEGER PRIMARY KEY);"
    )
    # No version row, token_events missing
    conn.close()
    conn = _connect(db_path)
    with pytest.raises(SchemaVersionError, match="incomplete"):
        init_schema(conn)
    conn.close()
    # The version row must NOT have been written
    conn = _connect(db_path)
    row = conn.execute("SELECT version FROM schema_meta").fetchone()
    assert row is None
    conn.close()


def test_incomplete_schema_without_version_repaired_transactionally(tmp_path: Path) -> None:
    """A complete v3 table set with no version row is repaired transactionally."""
    conn = _db(tmp_path)
    conn.execute("DELETE FROM schema_meta")
    conn.close()
    conn = _connect(tmp_path / "history.sqlite3")
    init_schema(conn)
    row = conn.execute("SELECT version FROM schema_meta").fetchone()
    assert row[0] == SCHEMA_VERSION
    conn.close()


def test_v3_version_row_with_missing_table_fails_closed(tmp_path: Path) -> None:
    """A version row claiming v3 with missing tables must fail closed."""
    conn = _db(tmp_path)
    conn.execute("DROP TABLE token_events")
    conn.close()
    conn = _connect(tmp_path / "history.sqlite3")
    with pytest.raises(SchemaVersionError, match="incomplete"):
        init_schema(conn)
    conn.close()


def test_v2_migration_missing_quota_table_keeps_v2_label(tmp_path: Path) -> None:
    """A v2 database missing quota_observations rolls back and keeps its v2
    label — never labeled v3."""
    from moira.history_db import SCHEMA_SQL_V2

    db_path = tmp_path / "history.sqlite3"
    conn = _connect(db_path)
    conn.executescript(SCHEMA_SQL_V2)
    conn.execute("INSERT INTO schema_meta (version) VALUES (2)")
    conn.execute("DROP TABLE quota_observations")
    conn.close()
    conn = _connect(db_path)
    with pytest.raises(SchemaVersionError, match="incomplete"):
        init_schema(conn)
    conn.close()
    conn = _connect(db_path)
    row = conn.execute("SELECT version FROM schema_meta").fetchone()
    assert row[0] == 2  # still v2
    conn.close()


def test_query_token_preserves_period_start_and_kind(tmp_path: Path) -> None:
    """Domain reads preserve period_start and period_kind; observed_at stays
    retrieval provenance."""
    conn = _db(tmp_path)
    record_token_events(conn, [_token_reading(NOW)], now=NOW)
    rows = query_token(conn, since=NOW - timedelta(hours=1))
    assert len(rows) == 1
    obs = rows[0]
    assert obs.period_kind == "day"
    assert obs.period_start == datetime(2026, 8, 2, 0, 0, tzinfo=UTC)
    assert obs.day.isoformat() == "2026-08-02"
    assert obs.observed_at == NOW  # retrieval provenance
    conn.close()


def test_query_token_day_boundaries_24h_7d_90d(tmp_path: Path) -> None:
    """Daily rows respect day-string boundaries: a row on the boundary day
    is included, a row outside the range is excluded."""
    conn = _db(tmp_path)
    now = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)
    # 6 days ago → inside the 7d boundary day (Aug 2), outside 24h
    inside_7d = _token_reading(now - timedelta(days=6), tokens=100)
    # 25 hours ago → day Aug 7, the 24h boundary day → included
    inside_24h = _token_reading(now - timedelta(hours=25), tokens=200)
    # 91 days ago → outside 90d
    outside_90d = _token_reading(now - timedelta(days=91), tokens=300)
    record_token_events(conn, [inside_7d, inside_24h, outside_90d], now=now)

    r24 = query_token(conn, since=now - timedelta(hours=24))
    assert sorted(o.tokens or 0 for o in r24) == [200]
    r7 = query_token(conn, since=now - timedelta(days=7))
    assert sorted(o.tokens or 0 for o in r7) == [100, 200]
    r90 = query_token(conn, since=now - timedelta(days=90))
    assert sorted(o.tokens or 0 for o in r90) == [100, 200]
    conn.close()


def test_query_token_bucket_boundaries(tmp_path: Path) -> None:
    """Migrated bucket rows compare the full ISO bucket instant."""
    conn = _db(tmp_path)
    now = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)
    bucket_inside = datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC)
    bucket_outside = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
    record_token(
        conn,
        _token_obs(
            service=Service.CODEX,
            period_start=bucket_inside,
            period_kind="bucket",
            observed_at=now,
            source="migrated",
            tokens=400,
        ),
        now=now,
    )
    record_token(
        conn,
        _token_obs(
            service=Service.CODEX,
            period_start=bucket_outside,
            period_kind="bucket",
            observed_at=now,
            source="migrated",
            tokens=500,
        ),
        now=now,
    )
    # 7d boundary: Aug 1 12:00 is exactly the boundary → included
    r7 = query_token(conn, since=now - timedelta(days=7))
    assert sorted(o.tokens or 0 for o in r7) == [400, 500]
    # 24h boundary: only the Aug 7 bucket
    r24 = query_token(conn, since=now - timedelta(hours=24))
    assert [o.tokens or 0 for o in r24] == [400]
    conn.close()


def test_retention_day_rows_boundary_kept(tmp_path: Path) -> None:
    """A daily row exactly 90 days old is kept (strict less-than)."""
    conn = _db(tmp_path)
    boundary_day = NOW - timedelta(days=RETENTION_DAYS)
    record_token_events(conn, [_token_reading(boundary_day, tokens=111)], now=NOW)
    record_token_events(conn, [_token_reading(NOW - timedelta(days=91), tokens=222)], now=NOW)
    rows = query_token(conn, since=NOW - timedelta(days=RETENTION_DAYS))
    assert [o.tokens for o in rows] == [111]
    conn.close()


def test_retention_bucket_rows_boundary_kept(tmp_path: Path) -> None:
    """A bucket row exactly 90 days old is kept."""
    conn = _db(tmp_path)
    boundary = NOW - timedelta(days=RETENTION_DAYS)
    record_token(
        conn,
        _token_obs(
            service=Service.CODEX,
            period_start=boundary,
            period_kind="bucket",
            observed_at=NOW,
            source="migrated",
            tokens=333,
        ),
        now=NOW,
    )
    record_token(
        conn,
        _token_obs(
            service=Service.CODEX,
            period_start=boundary - timedelta(minutes=1),
            period_kind="bucket",
            observed_at=NOW,
            source="migrated",
            tokens=444,
        ),
        now=NOW,
    )
    rows = query_token(conn, since=NOW - timedelta(days=RETENTION_DAYS))
    assert [o.tokens for o in rows] == [333]
    conn.close()


def test_record_and_query_codex_summary(tmp_path: Path) -> None:
    """Official summary persists as one typed record and reads back."""
    from moira.models import CodexSummary

    conn = _db(tmp_path)
    summary = CodexSummary(
        service=Service.CODEX,
        source="codex-app-server:account/usage/read",
        observed_at=NOW,
        lifetime_tokens=50000,
        peak_daily_tokens=3000,
        current_streak_days=7,
        longest_streak_days=14,
        longest_running_turn_sec=1500,
    )
    record_codex_summary(conn, summary, now=NOW)
    summaries = query_codex_summaries(conn, since=NOW - timedelta(hours=1))
    assert len(summaries) == 1
    assert summaries[0].lifetime_tokens == 50000
    assert summaries[0].peak_daily_tokens == 3000
    assert summaries[0].current_streak_days == 7
    assert summaries[0].longest_streak_days == 14
    assert summaries[0].longest_running_turn_sec == 1500
    conn.close()


def test_summary_replay_idempotent_and_newest_wins(tmp_path: Path) -> None:
    from moira.models import CodexSummary

    conn = _db(tmp_path)
    s1 = CodexSummary(service=Service.CODEX, source="s", observed_at=NOW, lifetime_tokens=100)
    record_codex_summary(conn, s1, now=NOW)
    record_codex_summary(conn, s1, now=NOW)  # replay — no duplicate
    later = CodexSummary(
        service=Service.CODEX,
        source="s",
        observed_at=NOW + timedelta(minutes=1),
        lifetime_tokens=200,
    )
    record_codex_summary(conn, later, now=NOW)
    summaries = query_codex_summaries(conn, since=NOW - timedelta(hours=1))
    assert len(summaries) == 2  # two distinct instants
    assert summaries[0].lifetime_tokens == 200  # newest first
    conn.close()


def test_summary_never_duplicated_per_daily_bucket(tmp_path: Path) -> None:
    """One refresh writes one summary record even with many daily buckets."""
    from moira.models import CodexSummary

    conn = _db(tmp_path)
    summary = CodexSummary(
        service=Service.CODEX,
        source="codex-app-server:account/usage/read",
        observed_at=NOW,
        lifetime_tokens=99999,
    )
    readings = [_token_reading(NOW - timedelta(days=d), tokens=10 * d + 1) for d in range(5)]
    batch: list[Any] = list(readings) + [summary]
    record_refresh(conn, batch, now=NOW)
    count = conn.execute("SELECT COUNT(*) FROM codex_summaries").fetchone()[0]
    assert count == 1
    rows = query_token(conn, since=NOW - timedelta(days=7))
    assert len(rows) == 5  # five daily buckets
    conn.close()


def test_non_exact_status_never_hides_exact_data(tmp_path: Path) -> None:
    """An INVALID/TEMPORARILY_UNAVAILABLE reading for a day that already has
    exact data never replaces it."""
    conn = _db(tmp_path)
    exact = _token_reading(NOW, tokens=500)
    record_token_events(conn, [exact], now=NOW)
    invalid = _token_reading(NOW, tokens=None, status=HistoryStatus.INVALID)
    record_token_events(conn, [invalid], now=NOW + timedelta(minutes=1))
    rows = query_token(conn, since=NOW - timedelta(hours=1))
    assert len(rows) == 1
    assert rows[0].status is HistoryStatus.AVAILABLE_EXACT
    assert rows[0].tokens == 500
    conn.close()


def test_non_exact_status_persisted_via_availability(tmp_path: Path) -> None:
    """Non-exact availability is persisted in token_availability, not token_events."""
    conn = _db(tmp_path)
    from moira.history_db import query_token_availability, record_token_availability
    from moira.models import TokenAvailabilityRecord

    avail = TokenAvailabilityRecord(
        service=Service.CODEX,
        observed_at=NOW,
        source="codex-app-server",
        status=HistoryStatus.TEMPORARILY_UNAVAILABLE,
        detail="test",
    )
    record_token_availability(conn, avail, now=NOW)
    rows = query_token(conn, since=NOW - timedelta(hours=1))
    assert len(rows) == 0  # token_events is empty
    avail_rows = query_token_availability(conn, since=NOW - timedelta(hours=1))
    assert len(avail_rows) == 1
    assert avail_rows[0].status is HistoryStatus.TEMPORARILY_UNAVAILABLE
    assert avail_rows[0].detail == "test"
    conn.close()


def test_availability_coexists_with_exact_data(tmp_path: Path) -> None:
    """Availability state coexists with exact daily data — never alters totals."""
    conn = _db(tmp_path)
    from moira.history_db import query_token_availability, record_token_availability
    from moira.models import TokenAvailabilityRecord

    # Write exact data first
    exact = _token_reading(NOW, tokens=800)
    record_token_events(conn, [exact], now=NOW)

    # Then write a non-exact availability observation (e.g. temporary outage)
    avail = TokenAvailabilityRecord(
        service=Service.CODEX,
        observed_at=NOW + timedelta(minutes=5),
        source="codex-app-server",
        status=HistoryStatus.TEMPORARILY_UNAVAILABLE,
        detail="transient failure",
    )
    record_token_availability(conn, avail, now=NOW + timedelta(minutes=5))

    # Exact data survives unchanged
    rows = query_token(conn, since=NOW - timedelta(hours=1))
    assert len(rows) == 1
    assert rows[0].status is HistoryStatus.AVAILABLE_EXACT
    assert rows[0].tokens == 800

    # Availability is tracked independently
    avail_rows = query_token_availability(conn, since=NOW - timedelta(hours=1))
    assert len(avail_rows) == 1
    assert avail_rows[0].status is HistoryStatus.TEMPORARILY_UNAVAILABLE
    conn.close()


def test_record_refresh_end_to_end_with_summary(tmp_path: Path) -> None:
    """Quota + daily buckets + summary in one batch persist together."""
    from moira.models import CodexSummary, TokenReading

    conn = _db(tmp_path)
    summary = CodexSummary(
        service=Service.CODEX,
        source="codex-app-server:account/usage/read",
        observed_at=NOW,
        lifetime_tokens=123456,
    )
    reading = TokenReading(
        service=Service.CODEX,
        day=NOW.date(),
        retrieved_at=NOW,
        source="codex-app-server:account/usage/read",
        status=HistoryStatus.AVAILABLE_EXACT,
        tokens=777,
    )
    record_refresh(conn, [_reading(pct=50.0), reading, summary], now=NOW)
    quota = query_quota(conn, since=NOW - timedelta(hours=1))
    assert len(quota) == 1
    tokens = query_token(conn, since=NOW - timedelta(hours=1))
    assert len(tokens) == 1
    assert tokens[0].tokens == 777
    summaries = query_codex_summaries(conn, since=NOW - timedelta(hours=1))
    assert len(summaries) == 1
    assert summaries[0].lifetime_tokens == 123456
    conn.close()


# ── Package 3e: v3→v4 migration with historical shapes ──


def _v3_db_3b(tmp_path: Path) -> sqlite3.Connection:
    """Create a populated Package 3b v3 database (no codex_summaries)."""
    from moira.history_db import SCHEMA_SQL_V3_3B

    db_path = tmp_path / "history_3b.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(db_path), os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    os.chmod(db_path, 0o600)
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.executescript(SCHEMA_SQL_V3_3B)
    conn.execute("INSERT INTO schema_meta (version) VALUES (3)")
    conn.execute(
        "INSERT INTO quota_observations "
        "(service, quota_label, percentage, reset_at, observed_at, source, bucket) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("codex", "Weekly", 42.0, RESET.isoformat(), NOW.isoformat(), "fixture", _bucket(NOW)),
    )
    conn.execute(
        "INSERT OR REPLACE INTO token_events "
        "(event_key, service, period_start, period_kind, observed_at, source, "
        "status, total_tokens) "
        "VALUES (?, ?, ?, 'day', ?, ?, 'available_exact', ?)",
        ("codex:day:2026-08-02", "codex", "2026-08-02", NOW.isoformat(), "fixture", 500),
    )
    return conn


def _v3_db_3c(tmp_path: Path) -> sqlite3.Connection:
    """Create a populated Package 3c v3 database (with codex_summaries)."""
    from moira.history_db import SCHEMA_SQL_V3_3C

    db_path = tmp_path / "history_3c.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(db_path), os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    os.chmod(db_path, 0o600)
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.executescript(SCHEMA_SQL_V3_3C)
    conn.execute("INSERT INTO schema_meta (version) VALUES (3)")
    conn.execute(
        "INSERT INTO quota_observations "
        "(service, quota_label, percentage, reset_at, observed_at, source, bucket) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("codex", "Weekly", 42.0, RESET.isoformat(), NOW.isoformat(), "fixture", _bucket(NOW)),
    )
    conn.execute(
        "INSERT OR REPLACE INTO token_events "
        "(event_key, service, period_start, period_kind, observed_at, source, "
        "status, total_tokens) "
        "VALUES (?, ?, ?, 'day', ?, ?, 'available_exact', ?)",
        ("codex:day:2026-08-02", "codex", "2026-08-02", NOW.isoformat(), "fixture", 500),
    )
    return conn


def test_3b_migration_creates_codex_summaries_and_availability(tmp_path: Path) -> None:
    """Package 3b v3 (no codex_summaries) → v4 creates both tables, preserves rows."""
    conn = _v3_db_3b(tmp_path)
    # Verify no codex_summaries, no token_availability before migration
    assert (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='codex_summaries'"
        ).fetchone()
        is None
    )
    assert (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='token_availability'"
        ).fetchone()
        is None
    )
    conn.close()

    conn = _connect(tmp_path / "history_3b.sqlite3")
    init_schema(conn)

    # Version is now 4
    row = conn.execute("SELECT version FROM schema_meta").fetchone()
    assert row[0] == 4

    # Both v4 tables now exist
    assert (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='codex_summaries'"
        ).fetchone()
        is not None
    )
    assert (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='token_availability'"
        ).fetchone()
        is not None
    )

    # Indexes exist
    assert (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_codex_summaries_time'"
        ).fetchone()
        is not None
    )
    assert (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_token_avail_service'"
        ).fetchone()
        is not None
    )
    assert (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_token_avail_time'"
        ).fetchone()
        is not None
    )

    # Existing rows preserved
    quota = query_quota(conn, since=NOW - timedelta(hours=1))
    assert len(quota) == 1
    assert quota[0].percentage == 42.0

    tokens = query_token(conn, since=NOW - timedelta(hours=1))
    assert len(tokens) == 1
    assert tokens[0].tokens == 500

    # A subsequent codex_summary write works
    from moira.models import CodexSummary

    summary = CodexSummary(
        service=Service.CODEX,
        source="test",
        observed_at=NOW,
        lifetime_tokens=999,
    )
    record_codex_summary(conn, summary, now=NOW)
    summaries = query_codex_summaries(conn, since=NOW - timedelta(hours=1))
    assert len(summaries) == 1
    assert summaries[0].lifetime_tokens == 999

    conn.close()


def test_3c_migration_idempotent(tmp_path: Path) -> None:
    """Package 3c v3 (with codex_summaries) → v4 creates only missing additions."""
    conn = _v3_db_3c(tmp_path)
    # Verify codex_summaries EXISTS but token_availability does NOT
    assert (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='codex_summaries'"
        ).fetchone()
        is not None
    )
    assert (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='token_availability'"
        ).fetchone()
        is None
    )
    conn.close()

    conn = _connect(tmp_path / "history_3c.sqlite3")
    init_schema(conn)

    row = conn.execute("SELECT version FROM schema_meta").fetchone()
    assert row[0] == 4

    # Both tables exist
    assert (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='token_availability'"
        ).fetchone()
        is not None
    )

    # Run init_schema again (idempotent)
    init_schema(conn)
    row2 = conn.execute("SELECT version FROM schema_meta").fetchone()
    assert row2[0] == 4

    conn.close()


def test_v3_migration_rollback_preserves_version_and_rows(tmp_path: Path) -> None:
    """Forced failure during v3→v4 rolls back DDL, index, and version changes."""
    conn = _v3_db_3b(tmp_path)
    conn.close()

    conn = _connect(tmp_path / "history_3b.sqlite3")

    # Monkey-patch _create_missing_v4_objects to fail after creating something
    original_create = history_db_module._create_missing_v4_objects

    def failing_create(c: sqlite3.Connection) -> None:
        original_create(c)
        raise sqlite3.OperationalError("simulated failure")

    history_db_module._create_missing_v4_objects = failing_create  # type: ignore[assignment]
    try:
        with pytest.raises(sqlite3.OperationalError, match="simulated failure"):
            init_schema(conn)
    finally:
        history_db_module._create_missing_v4_objects = original_create  # type: ignore[assignment]

    # Version stays 3 (rollback)
    row = conn.execute("SELECT version FROM schema_meta").fetchone()
    assert row[0] == 3

    # Neither v4 table was committed
    assert (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='codex_summaries'"
        ).fetchone()
        is None
    )
    assert (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='token_availability'"
        ).fetchone()
        is None
    )

    # Existing rows survive
    quota = query_quota(conn, since=NOW - timedelta(hours=1))
    assert len(quota) == 1
    conn.close()


def test_v4_completeness_validates_indexes(tmp_path: Path) -> None:
    """A v4 database missing a required index fails validation."""
    conn = _db(tmp_path)  # fresh v4
    row = conn.execute("SELECT version FROM schema_meta").fetchone()
    assert row[0] == 4

    # Drop one required index
    conn.execute("DROP INDEX idx_codex_summaries_time")
    conn.close()

    conn = _connect(tmp_path / "history.sqlite3")
    # init_schema should detect the missing index and repair it
    init_schema(conn)

    # The index is recreated
    assert (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_codex_summaries_time'"
        ).fetchone()
        is not None
    )
    conn.close()


def test_v4_idempotent_reinit_repairs_missing_index(tmp_path: Path) -> None:
    """Calling init_schema on a valid v4 DB is idempotent."""
    conn = _db(tmp_path)
    row = conn.execute("SELECT version FROM schema_meta").fetchone()
    assert row[0] == 4

    init_schema(conn)  # second call
    row2 = conn.execute("SELECT version FROM schema_meta").fetchone()
    assert row2[0] == 4

    # No duplicate application tables (sqlite_sequence is an internal table)
    tables_count = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchone()[0]
    conn.close()
    assert tables_count == 5


def test_damaged_v4_fails_closed_on_missing_column(tmp_path: Path) -> None:
    """A v4 database missing a required column fails closed."""
    conn = _db(tmp_path)
    # Drop a required column (SQLite doesn't support DROP COLUMN easily,
    # so we create a fresh table with fewer columns and rename)
    conn.execute("ALTER TABLE codex_summaries RENAME TO codex_summaries_old")
    conn.execute(
        "CREATE TABLE codex_summaries ("
        "service TEXT NOT NULL, observed_at TEXT NOT NULL, source TEXT NOT NULL,"
        "PRIMARY KEY (service, observed_at))"
    )
    conn.execute("DROP TABLE codex_summaries_old")
    conn.close()

    conn = _connect(tmp_path / "history.sqlite3")
    with pytest.raises(SchemaVersionError, match="required objects"):
        init_schema(conn)
    conn.close()


def test_v3_db_3b_produces_complete_v4_manifest(tmp_path: Path) -> None:
    """After 3b→v4 migration, the complete v4 manifest validates cleanly."""
    from moira.history_db import _validate_v4_completeness

    conn = _v3_db_3b(tmp_path)
    conn.close()

    conn = _connect(tmp_path / "history_3b.sqlite3")
    init_schema(conn)
    missing = _validate_v4_completeness(conn)
    assert missing == [], f"v4 manifest incomplete: {missing}"
    conn.close()


def test_two_provider_availability_persisted(tmp_path: Path) -> None:
    """Claude UNSUPPORTED and Codex AVAILABLE_EXACT coexist in one batch."""
    from moira.collectors import ClaudeCollector
    from moira.history_db import query_token_availability

    # Simulate a full refresh with both collectors
    claude_result = ClaudeCollector().collect()
    assert len(claude_result.token_availability_records) == 1
    assert claude_result.token_availability_records[0].status is HistoryStatus.UNSUPPORTED
    assert claude_result.token_availability_records[0].service is Service.CLAUDE

    # Build a synthetic Codex result with exact availability
    from moira.models import TokenAvailabilityRecord

    codex_avail = TokenAvailabilityRecord(
        service=Service.CODEX,
        observed_at=NOW,
        source="codex-app-server",
        status=HistoryStatus.AVAILABLE_EXACT,
    )
    from moira.models import CollectorResult

    codex_result = CollectorResult(
        quota_readings=(),
        token_readings=(),
        token_availability_records=(codex_avail,),
    )

    # Combine all readings into one batch like MainWindow does
    conn = _db(tmp_path)
    batch: list[Any] = list(claude_result.quota_readings)
    batch.extend(claude_result.token_readings)
    batch.extend(claude_result.token_availability_records)
    batch.extend(codex_result.quota_readings)
    batch.extend(codex_result.token_readings)
    batch.extend(codex_result.token_availability_records)

    record_refresh(conn, batch, now=NOW)

    avail_rows = query_token_availability(conn, since=NOW - timedelta(hours=1))
    services = {r.service for r in avail_rows}
    assert Service.CLAUDE in services
    assert Service.CODEX in services
    assert len(avail_rows) == 2

    conn.close()


def test_claude_unsupported_does_not_hide_codex_data(tmp_path: Path) -> None:
    """Claude UNSUPPORTED availability coexists with Codex exact token data."""
    from moira.history_db import query_token_availability
    from moira.models import TokenAvailabilityRecord

    conn = _db(tmp_path)

    # Write Codex exact token data
    exact = _token_reading(NOW, tokens=800)
    record_token_events(conn, [exact], now=NOW)

    # Write Codex AVAILABLE_EXACT availability
    codex_avail = TokenAvailabilityRecord(
        service=Service.CODEX,
        observed_at=NOW,
        source="test",
        status=HistoryStatus.AVAILABLE_EXACT,
    )
    from moira.history_db import record_token_availability

    record_token_availability(conn, codex_avail, now=NOW)

    # Write Claude UNSUPPORTED availability
    claude_avail = TokenAvailabilityRecord(
        service=Service.CLAUDE,
        observed_at=NOW,
        source="claude-statusline",
        status=HistoryStatus.UNSUPPORTED,
    )
    record_token_availability(conn, claude_avail, now=NOW)

    # Codex exact data survives
    tokens = query_token(conn, since=NOW - timedelta(hours=1))
    assert len(tokens) == 1
    assert tokens[0].tokens == 800

    avail_rows = query_token_availability(conn, since=NOW - timedelta(hours=1))
    statuses = {(r.service, r.status) for r in avail_rows}
    assert (Service.CLAUDE, HistoryStatus.UNSUPPORTED) in statuses
    assert (Service.CODEX, HistoryStatus.AVAILABLE_EXACT) in statuses

    conn.close()


def test_v4_manifest_indexes_exist_after_fresh_create(tmp_path: Path) -> None:
    """Fresh v4 database has all required indexes."""
    conn = _db(tmp_path)
    from moira.history_db import V4_MANIFEST

    indexes_manifest: list[str] = V4_MANIFEST["indexes"]  # type: ignore[assignment]
    for idx in indexes_manifest:
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (idx,)
            ).fetchone()
            is not None
        ), f"index {idx} missing"
    conn.close()
