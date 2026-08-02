"""SQLite-backed local history store at $XDG_STATE_HOME/moira/history.sqlite3.

Stores validated quota and optional token observations for at most 90 days.
Uses stdlib sqlite3 with a transactional schema version. The database file
is created with mode 0600. Config and current-state JSON are unchanged.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .history import HistoryStatus, QuotaObservation, TokenObservation
from .models import Service
from .persistence import state_dir

SCHEMA_VERSION = 1
RETENTION_DAYS = 90
BUCKET_MINUTES = 15


def history_path() -> Path:
    """Return the path to the history SQLite database."""
    return state_dir() / "history.sqlite3"


def _connect(path: Path | None = None) -> sqlite3.Connection:
    """Open a SQLite connection, creating the database with mode 0600."""
    db_path = path or history_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if not db_path.exists():
        # Create with restrictive permissions before any data is written
        fd = os.open(str(db_path), os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
    os.chmod(db_path, 0o600)
    conn = sqlite3.connect(
        str(db_path),
        isolation_level=None,  # autocommit; we manage transactions explicitly
        timeout=5.0,
    )
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS schema_meta (
    version INTEGER PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS quota_observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    service         TEXT    NOT NULL,
    quota_label     TEXT    NOT NULL,
    percentage      REAL    NOT NULL,
    reset_at        TEXT    NOT NULL,
    observed_at     TEXT    NOT NULL,
    source          TEXT    NOT NULL,
    bucket          TEXT    NOT NULL,
    UNIQUE (service, quota_label, bucket)
);
CREATE TABLE IF NOT EXISTS token_observations (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    service                 TEXT    NOT NULL,
    observed_at             TEXT    NOT NULL,
    source                  TEXT    NOT NULL,
    status                  TEXT    NOT NULL,
    input_tokens            INTEGER,
    cached_input_tokens     INTEGER,
    output_tokens           INTEGER,
    reasoning_output_tokens INTEGER,
    total_tokens            INTEGER,
    bucket                  TEXT    NOT NULL,
    UNIQUE (service, bucket)
);
CREATE INDEX IF NOT EXISTS idx_quota_obs_time ON quota_observations (observed_at);
CREATE INDEX IF NOT EXISTS idx_quota_obs_service ON quota_observations (service, quota_label);
CREATE INDEX IF NOT EXISTS idx_token_obs_time ON token_observations (observed_at);
CREATE INDEX IF NOT EXISTS idx_token_obs_service ON token_observations (service);
"""


def _bucket(dt: datetime) -> str:
    """Compute a 15-minute bucket key for deduplication."""
    bucket_dt = dt.replace(
        minute=(dt.minute // BUCKET_MINUTES) * BUCKET_MINUTES, second=0, microsecond=0
    )
    return bucket_dt.isoformat()


def init_schema(conn: sqlite3.Connection) -> None:
    """Initialize or verify the schema version. Throws on version mismatch."""
    # executescript implicitly commits, so run DDL outside the explicit transaction
    conn.executescript(SCHEMA_SQL)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT version FROM schema_meta").fetchone()
        if row is None:
            conn.execute("INSERT INTO schema_meta (version) VALUES (?)", (SCHEMA_VERSION,))
        elif row[0] != SCHEMA_VERSION:
            raise ValueError(
                f"history database schema version {row[0]} does not match expected {SCHEMA_VERSION}"
            )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise


def record_quota(
    conn: sqlite3.Connection,
    obs: QuotaObservation,
    *,
    now: datetime | None = None,
) -> bool:
    """Record a quota observation, deduplicating within 15-minute buckets.

    Records a change immediately. Otherwise, keeps at most one unchanged sample
    per service/quota_label per 15-minute bucket. Returns True if a row was
    inserted.

    Purges rows older than 90 days after a successful write using the injected
    clock.
    """
    clock = now or datetime.now(UTC)
    bucket = _bucket(obs.observed_at)
    reset_iso = obs.reset_at.isoformat()
    observed_iso = obs.observed_at.isoformat()

    conn.execute("BEGIN IMMEDIATE")
    try:
        # Check for an existing sample in this bucket
        existing = conn.execute(
            "SELECT percentage FROM quota_observations "
            "WHERE service = ? AND quota_label = ? AND bucket = ?",
            (obs.service.value, obs.quota_label, bucket),
        ).fetchone()

        inserted = False
        if existing is None:
            conn.execute(
                "INSERT INTO quota_observations "
                "(service, quota_label, percentage, reset_at, observed_at, source, bucket) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    obs.service.value,
                    obs.quota_label,
                    obs.percentage,
                    reset_iso,
                    observed_iso,
                    obs.source,
                    bucket,
                ),
            )
            inserted = True
        elif existing[0] != obs.percentage:
            # Change point: replace the existing sample in this bucket
            conn.execute(
                "UPDATE quota_observations "
                "SET percentage = ?, reset_at = ?, observed_at = ?, source = ? "
                "WHERE service = ? AND quota_label = ? AND bucket = ?",
                (
                    obs.percentage,
                    reset_iso,
                    observed_iso,
                    obs.source,
                    obs.service.value,
                    obs.quota_label,
                    bucket,
                ),
            )
            inserted = True

        _purge(conn, clock)
        conn.execute("COMMIT")
        return inserted
    except Exception:
        conn.execute("ROLLBACK")
        raise


def record_token(
    conn: sqlite3.Connection,
    obs: TokenObservation,
    *,
    now: datetime | None = None,
) -> bool:
    """Record a token observation, deduplicating within 15-minute buckets.

    Returns True if a row was inserted or updated. Purges old rows after
    a successful write.
    """
    clock = now or datetime.now(UTC)
    bucket = _bucket(obs.observed_at)
    observed_iso = obs.observed_at.isoformat()

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT OR REPLACE INTO token_observations "
            "(service, observed_at, source, status, input_tokens, "
            "cached_input_tokens, output_tokens, reasoning_output_tokens, "
            "total_tokens, bucket) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                obs.service.value,
                observed_iso,
                obs.source,
                obs.status.value,
                obs.input_tokens,
                obs.cached_input_tokens,
                obs.output_tokens,
                obs.reasoning_output_tokens,
                obs.total_tokens,
                bucket,
            ),
        )
        _purge(conn, clock)
        conn.execute("COMMIT")
        return True
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _purge(conn: sqlite3.Connection, now: datetime) -> int:
    """Delete rows older than 90 days. Returns count of deleted rows."""
    boundary = (now - timedelta(days=RETENTION_DAYS)).isoformat()
    quota_deleted = conn.execute(
        "DELETE FROM quota_observations WHERE observed_at < ?", (boundary,)
    ).rowcount
    token_deleted = conn.execute(
        "DELETE FROM token_observations WHERE observed_at < ?", (boundary,)
    ).rowcount
    return quota_deleted + token_deleted


def query_quota(
    conn: sqlite3.Connection,
    *,
    since: datetime,
    service: Service | None = None,
    metric: str | None = None,
) -> list[QuotaObservation]:
    """Return quota observations since a given UTC time, optionally filtered."""
    since_iso = since.isoformat()
    clauses = ["observed_at >= ?"]
    params: list[object] = [since_iso]
    if service is not None:
        clauses.append("service = ?")
        params.append(service.value)
    if metric is not None:
        clauses.append("quota_label = ?")
        params.append(metric)
    where = " AND ".join(clauses)
    rows = conn.execute(
        f"SELECT service, quota_label, percentage, reset_at, observed_at, source "
        f"FROM quota_observations WHERE {where} ORDER BY observed_at ASC",
        params,
    ).fetchall()
    return [
        QuotaObservation(
            service=Service(row[0]),
            quota_label=row[1],
            percentage=row[2],
            reset_at=datetime.fromisoformat(row[3]),
            observed_at=datetime.fromisoformat(row[4]),
            source=row[5],
        )
        for row in rows
    ]


def query_token(
    conn: sqlite3.Connection,
    *,
    since: datetime,
    service: Service | None = None,
    metric: str | None = None,
) -> list[TokenObservation]:
    """Return token observations since a given UTC time, optionally filtered."""
    since_iso = since.isoformat()
    clauses = ["observed_at >= ?"]
    params: list[object] = [since_iso]
    if service is not None:
        clauses.append("service = ?")
        params.append(service.value)
    where = " AND ".join(clauses)
    rows = conn.execute(
        f"SELECT service, observed_at, source, status, input_tokens, "
        f"cached_input_tokens, output_tokens, reasoning_output_tokens, total_tokens "
        f"FROM token_observations WHERE {where} ORDER BY observed_at ASC",
        params,
    ).fetchall()
    return [
        TokenObservation(
            service=Service(row[0]),
            observed_at=datetime.fromisoformat(row[1]),
            source=row[2],
            status=HistoryStatus(row[3]),
            input_tokens=row[4],
            cached_input_tokens=row[5],
            output_tokens=row[6],
            reasoning_output_tokens=row[7],
            total_tokens=row[8],
        )
        for row in rows
    ]


def query_24h(conn: sqlite3.Connection, *, now: datetime | None = None) -> dict[str, list[Any]]:
    """Return quota and token observations from the last 24 hours."""
    clock = now or datetime.now(UTC)
    since = clock - timedelta(hours=24)
    return {
        "quota": query_quota(conn, since=since),
        "tokens": query_token(conn, since=since),
    }


def query_7d(conn: sqlite3.Connection, *, now: datetime | None = None) -> dict[str, list[Any]]:
    """Return quota and token observations from the last 7 days."""
    clock = now or datetime.now(UTC)
    since = clock - timedelta(days=7)
    return {
        "quota": query_quota(conn, since=since),
        "tokens": query_token(conn, since=since),
    }


def query_30d(conn: sqlite3.Connection, *, now: datetime | None = None) -> dict[str, list[Any]]:
    """Return quota and token observations from the last 30 days."""
    clock = now or datetime.now(UTC)
    since = clock - timedelta(days=30)
    return {
        "quota": query_quota(conn, since=since),
        "tokens": query_token(conn, since=since),
    }


def query_90d(conn: sqlite3.Connection, *, now: datetime | None = None) -> dict[str, list[Any]]:
    """Return quota and token observations from the last 90 days."""
    clock = now or datetime.now(UTC)
    since = clock - timedelta(days=90)
    return {
        "quota": query_quota(conn, since=since),
        "tokens": query_token(conn, since=since),
    }


def delete_all(conn: sqlite3.Connection) -> int:
    """Delete all history rows. Returns total count deleted."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        q = conn.execute("DELETE FROM quota_observations").rowcount
        t = conn.execute("DELETE FROM token_observations").rowcount
        conn.execute("COMMIT")
        return q + t
    except Exception:
        conn.execute("ROLLBACK")
        raise


def record_refresh(
    conn: sqlite3.Connection,
    readings: list[Any],
    *,
    now: datetime | None = None,
) -> None:
    """Record fresh validated quota observations from a refresh batch.

    Only AVAILABLE readings with percentage and reset_at are stored.
    STALE/error/unavailable readings are silently skipped. Token observations
    are not recorded here (no structured token surface is wired yet).

    Failure is caught by the caller; this function raises on DB errors so
    the caller can produce a sanitized diagnostic.
    """
    from .models import QuotaReading, QuotaStatus

    clock = now or datetime.now(UTC)
    for reading in readings:
        if not isinstance(reading, QuotaReading):
            continue
        if reading.status is not QuotaStatus.AVAILABLE:
            continue
        if reading.percentage is None or reading.reset_at is None:
            continue
        obs = QuotaObservation(
            service=reading.service,
            quota_label=reading.quota_label,
            percentage=reading.percentage,
            reset_at=reading.reset_at,
            observed_at=reading.retrieved_at,
            source=reading.source,
        )
        record_quota(conn, obs, now=clock)
