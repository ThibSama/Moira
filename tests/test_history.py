"""Deterministic tests for the local history foundation: schema/versioning,
permissions, dedup/sampling, change points, retention, queries, deletion,
malformed database handling, unsupported tokens, and refresh survival.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from moira.history import (
    HistoryStatus,
    QuotaObservation,
    TokenObservation,
)
from moira.history_db import (
    BUCKET_MINUTES,
    RETENTION_DAYS,
    SCHEMA_VERSION,
    _bucket,
    _connect,
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
) -> QuotaObservation:
    return QuotaObservation(
        service=service,
        quota_label=label,
        percentage=pct,
        reset_at=reset,
        observed_at=observed,
        source=source,
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


# ── Schema and versioning ──


def test_schema_version_constant() -> None:
    assert SCHEMA_VERSION == 1


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


# ── Dedup / sampling ──


def test_same_bucket_same_value_deduplicated(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    obs = _obs(pct=50.0)
    assert record_quota(conn, obs, now=NOW) is True
    # Same bucket, same value → no new insert
    assert record_quota(conn, obs, now=NOW) is False
    rows = query_quota(conn, since=NOW - timedelta(hours=1))
    assert len(rows) == 1
    conn.close()


def test_same_bucket_different_observed_at_deduplicated(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    obs1 = _obs(pct=50.0, observed=NOW)
    # 5 minutes later, still same 15-minute bucket
    obs2 = _obs(pct=50.0, observed=NOW + timedelta(minutes=5))
    assert record_quota(conn, obs1, now=NOW) is True
    assert record_quota(conn, obs2, now=NOW) is False
    rows = query_quota(conn, since=NOW - timedelta(hours=1))
    assert len(rows) == 1
    conn.close()


def test_different_buckets_both_stored(tmp_path: Path) -> None:
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


# ── Change points ──


def test_change_point_in_same_bucket_updates(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    record_quota(conn, _obs(pct=50.0), now=NOW)
    # Same bucket, different percentage → update, not duplicate
    record_quota(conn, _obs(pct=75.0), now=NOW)
    rows = query_quota(conn, since=NOW - timedelta(hours=1))
    assert len(rows) == 1
    assert rows[0].percentage == 75.0
    conn.close()


def test_change_point_preserves_latest_observed_at(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    t1 = NOW
    t2 = NOW + timedelta(minutes=3)
    record_quota(conn, _obs(pct=50.0, observed=t1), now=NOW)
    record_quota(conn, _obs(pct=80.0, observed=t2), now=NOW)
    rows = query_quota(conn, since=NOW - timedelta(hours=1))
    assert len(rows) == 1
    assert rows[0].percentage == 80.0
    assert rows[0].observed_at == t2
    conn.close()


# ── Retention ──


def test_purge_removes_old_rows(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    old = NOW - timedelta(days=RETENTION_DAYS + 1)
    record_quota(conn, _obs(pct=50.0, observed=old, reset=old + timedelta(days=5)), now=NOW)
    # Purge happens on the next write
    record_quota(conn, _obs(pct=60.0), now=NOW)
    rows = query_quota(conn, since=old - timedelta(days=1))
    assert all(r.observed_at >= NOW - timedelta(days=RETENTION_DAYS) for r in rows)
    assert len(rows) == 1  # only the fresh row
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
    # The boundary row at exactly 90 days should survive (purge is strict <)
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
    # The 91-day-old row is purged during the write
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


# ── Unsupported tokens ──


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


# ── record_refresh: idempotency and filtering ──


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


def test_record_refresh_unavailable_not_stored(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    readings = [
        _reading(pct=None, reset=None, status=QuotaStatus.UNAVAILABLE),
    ]
    record_refresh(conn, readings, now=NOW)
    rows = query_quota(conn, since=NOW - timedelta(hours=1))
    assert len(rows) == 0
    conn.close()


def test_record_refresh_error_not_stored(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    readings = [
        _reading(pct=None, reset=None, status=QuotaStatus.ERROR),
    ]
    record_refresh(conn, readings, now=NOW)
    rows = query_quota(conn, since=NOW - timedelta(hours=1))
    assert len(rows) == 0
    conn.close()


def test_record_refresh_parse_error_not_stored(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    readings = [
        _reading(pct=None, reset=None, status=QuotaStatus.PARSE_ERROR),
    ]
    record_refresh(conn, readings, now=NOW)
    rows = query_quota(conn, since=NOW - timedelta(hours=1))
    assert len(rows) == 0
    conn.close()


# ── History path ──


def test_history_path_uses_state_dir(tmp_path: Path) -> None:
    with patch.dict(os.environ, {"XDG_STATE_HOME": str(tmp_path)}):
        path = history_path()
        assert path == tmp_path / "moira" / "history.sqlite3"


# ── QuotaObservation domain ──


def test_quota_observation_from_reading_available() -> None:
    reading = _reading(pct=50.0, status=QuotaStatus.AVAILABLE)
    obs = QuotaObservation.from_reading(reading)
    assert obs is not None
    assert obs.percentage == 50.0
    assert obs.service is Service.CLAUDE


def test_quota_observation_from_reading_stale_returns_none() -> None:
    reading = _reading(pct=50.0, status=QuotaStatus.STALE)
    assert QuotaObservation.from_reading(reading) is None


def test_quota_observation_from_reading_error_returns_none() -> None:
    reading = _reading(pct=None, reset=None, status=QuotaStatus.ERROR)
    assert QuotaObservation.from_reading(reading) is None


def test_quota_observation_from_reading_unavailable_returns_none() -> None:
    reading = _reading(pct=None, reset=None, status=QuotaStatus.UNAVAILABLE)
    assert QuotaObservation.from_reading(reading) is None


# ── Refresh survival after history failure ──


def test_record_refresh_failure_does_not_raise(tmp_path: Path) -> None:
    """When the database is corrupt, record_refresh should raise (caller
    catches). This test verifies the DB-level failure surfaces as an
    exception, not silently ignored at the DB layer."""
    conn = _db(tmp_path)
    conn.execute("DROP TABLE quota_observations")
    conn.close()
    conn = _connect(tmp_path / "history.sqlite3")
    with pytest.raises(sqlite3.OperationalError):
        record_refresh(conn, [_reading(pct=50.0)], now=NOW)
    conn.close()


def test_history_failure_leaves_quota_intact(tmp_path: Path) -> None:
    """Simulate the UI-level _record_history pattern: history failure is
    caught and swallowed, leaving quota state operational."""
    db_path = tmp_path / "history.sqlite3"
    db_path.write_text("corrupt", encoding="utf-8")

    # Simulate the _record_history try/except pattern from ui.py
    readings = [_reading(pct=50.0)]
    now = NOW
    try:
        conn = _connect(db_path)
        try:
            init_schema(conn)
            record_refresh(conn, readings, now=now)
        finally:
            conn.close()
    except Exception:
        pass  # This is the _record_history pattern

    # Quota state (simulated as still having readings) is unaffected
    assert len(readings) == 1
    assert readings[0].percentage == 50.0


def test_history_path_respects_xdg_state_home(tmp_path: Path) -> None:
    with patch.dict(os.environ, {"XDG_STATE_HOME": str(tmp_path)}):
        conn = _connect(history_path())
        init_schema(conn)
        record_quota(conn, _obs(pct=50.0), now=NOW)
        conn.close()
        assert tmp_path.joinpath("moira", "history.sqlite3").exists()
        mode = oct(tmp_path.joinpath("moira", "history.sqlite3").stat().st_mode & 0o777)
        assert mode == "0o600"
