"""SQLite-backed local history store at $XDG_STATE_HOME/moira/history.sqlite3.

Stores validated quota and optional token observations for at most 90 days.
Uses stdlib sqlite3 with a transactional schema version. The database file
is created with mode 0600. Config and current-state JSON are unchanged.

Schema v2: Removes UNIQUE(service, quota_label, bucket) to preserve every
distinct percentage or reset transition, including multiple changes inside
one bucket. Unchanged values retain at most one periodic sample per
service/quota/bucket via INSERT OR IGNORE on a periodic-sample index.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .history import (
    HistoryStatus,
    HistoryWriteResult,
    QuotaObservation,
    SchemaVersionError,
    TokenObservation,
)
from .models import Service
from .persistence import state_dir

SCHEMA_VERSION = 2
RETENTION_DAYS = 90
BUCKET_MINUTES = 15

# Sanitized diagnostic strings — never contain exception text, SQL, or paths.
_DIAG_OK = "ok"
_DIAG_DB_ERROR = "database unavailable"
_DIAG_SCHEMA_ERROR = "schema mismatch"
_DIAG_VALIDATION_ERROR = "invalid observation"
_DIAG_BACKLOG = "backlog saturated"


def history_path() -> Path:
    """Return the path to the history SQLite database."""
    return state_dir() / "history.sqlite3"


def _connect(path: Path | None = None, *, timeout: float = 5.0) -> sqlite3.Connection:
    """Open a SQLite connection, creating the database with mode 0600."""
    db_path = path or history_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if not db_path.exists():
        fd = os.open(str(db_path), os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
    os.chmod(db_path, 0o600)
    conn = sqlite3.connect(
        str(db_path),
        isolation_level=None,  # autocommit; we manage transactions explicitly
        timeout=timeout,
    )
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


SCHEMA_SQL_V2 = """\
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
    status          TEXT    NOT NULL DEFAULT 'available_exact',
    is_change       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (service, quota_label, bucket, observed_at)
);
CREATE INDEX IF NOT EXISTS idx_quota_obs_time ON quota_observations (observed_at);
CREATE INDEX IF NOT EXISTS idx_quota_obs_service ON quota_observations (service, quota_label);
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
CREATE INDEX IF NOT EXISTS idx_token_obs_time ON token_observations (observed_at);
CREATE INDEX IF NOT EXISTS idx_token_obs_service ON token_observations (service);
"""


SCHEMA_SQL_V1 = """\
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


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """Transactionally migrate a v1 schema to v2.

    Preserves all existing rows. Drops the old UNIQUE(service, quota_label,
    bucket) constraint and adds status/is_change columns with defaults.
    Rolls back on any failure.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Recreate quota_observations without the v1 UNIQUE constraint
        conn.execute("ALTER TABLE quota_observations RENAME TO quota_observations_v1")
        conn.execute(
            """\
CREATE TABLE quota_observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    service         TEXT    NOT NULL,
    quota_label     TEXT    NOT NULL,
    percentage      REAL    NOT NULL,
    reset_at        TEXT    NOT NULL,
    observed_at     TEXT    NOT NULL,
    source          TEXT    NOT NULL,
    bucket          TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'available_exact',
    is_change       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (service, quota_label, bucket, observed_at)
)"""
        )
        conn.execute(
            """\
INSERT INTO quota_observations
    (id, service, quota_label, percentage, reset_at, observed_at, source, bucket, status, is_change)
SELECT id, service, quota_label, percentage, reset_at, observed_at, source, bucket,
       'available_exact', 1
FROM quota_observations_v1"""
        )
        conn.execute("DROP TABLE quota_observations_v1")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_quota_obs_time ON quota_observations (observed_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_quota_obs_service "
            "ON quota_observations (service, quota_label)"
        )
        conn.execute("UPDATE schema_meta SET version = 2 WHERE version = 1")
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise


def init_schema(conn: sqlite3.Connection) -> None:
    """Initialize or migrate the schema. Throws on irrecoverable mismatch."""
    # For a fresh database, create v2 directly
    conn.executescript(SCHEMA_SQL_V2)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT version FROM schema_meta").fetchone()
        if row is None:
            conn.execute("INSERT INTO schema_meta (version) VALUES (?)", (SCHEMA_VERSION,))
        elif row[0] == 1:
            # Need to migrate v1 → v2. Rollback the current transaction first,
            # then run the migration.
            conn.execute("ROLLBACK")
            _migrate_v1_to_v2(conn)
            return
        elif row[0] != SCHEMA_VERSION:
            raise SchemaVersionError(
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
    """Record a quota observation, preserving every distinct change point.

    A new row is inserted when:
    - No row exists for this service/quota_label/bucket, OR
    - The latest row in this bucket has a different percentage or reset_at
      (a change point). Earlier change points are never overwritten.

    Unchanged values (same percentage and reset_at as the latest row in the
    bucket) are deduplicated: at most one periodic sample per bucket.

    Returns True if a row was inserted. Purges rows older than 90 days after
    a successful write using the injected clock.
    """
    clock = now or datetime.now(UTC)
    bucket = _bucket(obs.observed_at)
    reset_iso = obs.reset_at.isoformat()
    observed_iso = obs.observed_at.isoformat()

    conn.execute("BEGIN IMMEDIATE")
    try:
        # Find the latest row in this bucket for this service/label
        existing = conn.execute(
            "SELECT percentage, reset_at FROM quota_observations "
            "WHERE service = ? AND quota_label = ? AND bucket = ? "
            "ORDER BY observed_at DESC LIMIT 1",
            (obs.service.value, obs.quota_label, bucket),
        ).fetchone()

        inserted = False
        if existing is None:
            # First sample in this bucket
            conn.execute(
                "INSERT OR IGNORE INTO quota_observations "
                "(service, quota_label, percentage, reset_at, observed_at, source, bucket, "
                "status, is_change) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (
                    obs.service.value,
                    obs.quota_label,
                    obs.percentage,
                    reset_iso,
                    observed_iso,
                    obs.source,
                    bucket,
                    obs.status.value,
                ),
            )
            inserted = conn.total_changes > 0
        elif existing[0] != obs.percentage or existing[1] != reset_iso:
            # Change point: always insert (or ignore if exact replay)
            conn.execute(
                "INSERT OR IGNORE INTO quota_observations "
                "(service, quota_label, percentage, reset_at, observed_at, source, bucket, "
                "status, is_change) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (
                    obs.service.value,
                    obs.quota_label,
                    obs.percentage,
                    reset_iso,
                    observed_iso,
                    obs.source,
                    bucket,
                    obs.status.value,
                ),
            )
            inserted = conn.total_changes > 0
        # else: unchanged → deduplicate (no insert)

        _purge(conn, clock)
        conn.execute("COMMIT")
        return inserted
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
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
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
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
        f"SELECT service, quota_label, percentage, reset_at, observed_at, source, status "
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
            status=HistoryStatus(row[6]),
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
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
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


def write_history_safely(
    readings: list[Any],
    *,
    now: datetime | None = None,
    db_path: Path | None = None,
    db_timeout: float = 5.0,
) -> HistoryWriteResult:
    """Write quota observations to the history database and return a sanitized result.

    This is the entry point for off-thread history writes. It performs
    connect/schema/write/purge in one call and returns only a bounded
    HistoryWriteResult. Never exposes exception text, SQL, payloads, paths,
    account data, or secrets.
    """
    clock = now or datetime.now(UTC)
    try:
        conn = _connect(db_path, timeout=db_timeout)
        try:
            init_schema(conn)
            record_refresh(conn, readings, now=clock)
        finally:
            conn.close()
    except SchemaVersionError:
        return HistoryWriteResult(ok=False, diagnostic=_DIAG_SCHEMA_ERROR)
    except sqlite3.DatabaseError:
        return HistoryWriteResult(ok=False, diagnostic=_DIAG_DB_ERROR)
    except ValueError:
        return HistoryWriteResult(ok=False, diagnostic=_DIAG_VALIDATION_ERROR)
    except Exception:
        return HistoryWriteResult(ok=False, diagnostic=_DIAG_VALIDATION_ERROR)
    return HistoryWriteResult(ok=True, diagnostic=_DIAG_OK)


class HistoryCoordinator:
    """Bounded, testable, race-free coordinator for off-thread history writes.

    Capacity is exactly one in-flight batch plus one pending batch:
      - if neither exists, accept the batch and return True;
      - if a batch is in-flight but pending is empty, accept the new pending
        batch, return True, and do not report saturation;
      - if pending is already occupied, replace it with the newest generation,
        return False, and latch ``backlog saturated``.

    The saturation latch is separate from the latest write diagnostic.
    While saturation remains unresolved, public status stays
    ``backlog saturated`` even when the retained write fails. The sanitized
    write diagnostic is stored internally for the future Diagnostic view.

    Generation policy: an old in-flight result cannot resolve a newer
    saturation. Only successful completion of the retained generation,
    or a later successful generation, may clear the saturation latch.

    Lifecycle states (all transitions under the Condition):
      - NEW: constructed but not started. enqueue() is accepted;
        shutdown() atomically rejects future work and disposes pending.
      - RUNNING: worker thread is active. Normal operation.
      - SHUTTING_DOWN: shutdown() called while running. New work rejected;
        worker drains or times out.
      - TERMINATED: worker has exited or shutdown-before-start completed.
        start() is a documented no-op (restart is rejected). enqueue()
        returns False. shutdown() is idempotent.

    Defaults: ``db_timeout=1.0``, ``shutdown_timeout=3.0`` (2-second margin).
    The constructor validates ``0 < db_timeout < shutdown_timeout``.
    ``shutdown(timeout=...)`` validates the effective timeout against
    ``db_timeout`` before mutating lifecycle state. ``_thread`` is never
    set to None while the captured thread is still alive.
    """

    def __init__(
        self,
        *,
        db_path: Path | None = None,
        db_timeout: float = 1.0,
        shutdown_timeout: float = 3.0,
    ) -> None:
        if not (0 < db_timeout < shutdown_timeout):
            raise ValueError(
                f"db_timeout ({db_timeout}) must be strictly positive and "
                f"strictly less than shutdown_timeout ({shutdown_timeout})"
            )
        self._db_path = db_path or history_path()
        self._db_timeout = db_timeout
        self._shutdown_timeout = shutdown_timeout
        self._cond = threading.Condition()
        self._thread: threading.Thread | None = None
        self._in_flight: list[Any] | None = None
        self._in_flight_gen: int = 0
        self._pending: list[Any] | None = None
        self._pending_time: datetime | None = None
        self._pending_gen: int = 0
        self._generation = 0
        self._saturation_gen: int = 0
        self._status = _DIAG_OK
        self._last_write_diagnostic = _DIAG_OK
        # Lifecycle: "new", "running", "shutting_down", "terminated"
        self._lifecycle = "new"
        # Write-success callback for History refresh
        self._write_success_callback: Any = None

    @property
    def status(self) -> str:
        """Return the current sanitized history status string.

        While saturation is active, returns ``backlog saturated``
        regardless of the latest write diagnostic.
        """
        with self._cond:
            if self._saturation_gen > 0:
                return _DIAG_BACKLOG
            return self._status

    @property
    def last_write_diagnostic(self) -> str:
        """Return the sanitized diagnostic from the latest write attempt.

        This is internal and for the future Diagnostic view. The public
        ``status`` property always reflects saturation state first.
        """
        with self._cond:
            return self._last_write_diagnostic

    @property
    def lifecycle_state(self) -> str:
        """Return the current lifecycle state string."""
        with self._cond:
            return self._lifecycle

    def set_write_success_callback(self, callback: Any) -> None:
        """Set a callback fired after a successful history write.

        The callback is called outside the condition lock. It receives no
        arguments. Use this to trigger a History tab refresh.
        """
        self._write_success_callback = callback

    def start(self) -> None:
        """Start the worker thread if in the NEW state.

        After terminal shutdown, start() is a no-op (restart is rejected).
        Does not create an immediately dying untracked thread.
        """
        with self._cond:
            if self._lifecycle != "new":
                return
            self._lifecycle = "running"
            self._thread = threading.Thread(target=self._run, name="moira-history", daemon=True)
            self._thread.start()

    def enqueue(self, readings: list[Any], now: datetime) -> bool:
        """Submit a batch for asynchronous writing.

        Returns True if the batch was accepted (either as the first entry
        or as a new pending batch while the worker is in-flight but pending
        is empty). Returns False (and latches ``backlog saturated``) only
        when the pending slot was already occupied — the newest batch
        replaces the older one. Never blocks.

        After shutdown (from any state), returns False.
        """
        snapshot = list(readings)
        with self._cond:
            if self._lifecycle in ("shutting_down", "terminated"):
                return False
            if self._pending is not None:
                # Pending slot occupied — newest-wins, report saturation
                self._generation += 1
                self._pending = snapshot
                self._pending_time = now
                self._pending_gen = self._generation
                if self._saturation_gen < self._generation:
                    self._saturation_gen = self._generation
                self._cond.notify_all()
                return False
            # Pending slot is empty — accept the batch
            self._generation += 1
            self._pending = snapshot
            self._pending_time = now
            self._pending_gen = self._generation
            self._cond.notify_all()
            return True

    def _run(self) -> None:
        """Worker loop: drain pending batches until shutdown."""
        while True:
            with self._cond:
                while self._pending is None and self._lifecycle == "running":
                    self._cond.wait()
                if self._lifecycle in ("shutting_down", "terminated"):
                    return
                # Pick up the pending batch atomically — no race window
                batch = self._pending
                when = self._pending_time
                gen = self._pending_gen
                self._pending = None
                self._pending_time = None
                self._in_flight = batch
                self._in_flight_gen = gen
            # batch is guaranteed non-None here (checked by the while loop)
            assert batch is not None and when is not None
            # Write outside the lock so enqueue can still proceed
            result = write_history_safely(
                batch, now=when, db_path=self._db_path, db_timeout=self._db_timeout
            )
            with self._cond:
                self._in_flight = None
                self._in_flight_gen = 0
                self._last_write_diagnostic = result.diagnostic
                # Only resolve saturation if this generation is at or past
                # the saturation generation.
                if gen >= self._saturation_gen:
                    if result.ok:
                        # Successful completion of the retained (or later)
                        # generation clears the saturation latch.
                        self._saturation_gen = 0
                        self._status = _DIAG_OK
                    else:
                        # Failure of the retained generation does NOT clear
                        # saturation. Keep backlog saturated as the public
                        # status. The diagnostic is stored internally.
                        self._status = _DIAG_BACKLOG
                # else: an older write completed — preserve current status
                fire_callback = result.ok and self._write_success_callback is not None
                callback = self._write_success_callback
            # Fire write-success callback outside the lock
            if fire_callback and callback is not None:
                try:
                    callback()
                except Exception:
                    pass

    def shutdown(self, *, timeout: float | None = None) -> None:
        """Idempotent shutdown.

        Validates the effective timeout before mutating lifecycle state.
        From NEW: atomically transitions to TERMINATED, rejects future work,
        and disposes pending with the saturation policy. From RUNNING:
        transitions to SHUTTING_DOWN, signals the worker, and joins.
        ``_thread`` is never set to None while the captured thread is
        still alive. From TERMINATED: no-op.
        """
        join_timeout = timeout if timeout is not None else self._shutdown_timeout
        if not (0 < join_timeout <= self._shutdown_timeout) or join_timeout <= self._db_timeout:
            raise ValueError(
                f"shutdown timeout ({join_timeout}) must be positive, at most "
                f"shutdown_timeout ({self._shutdown_timeout}), and strictly "
                f"greater than db_timeout ({self._db_timeout})"
            )
        with self._cond:
            if self._lifecycle == "terminated":
                return
            if self._lifecycle == "new":
                # Shutdown before start — terminate immediately
                if self._pending is not None:
                    if self._saturation_gen < self._pending_gen:
                        self._saturation_gen = self._pending_gen
                    if self._saturation_gen > 0:
                        self._status = _DIAG_BACKLOG
                self._pending = None
                self._pending_time = None
                self._pending_gen = 0
                self._lifecycle = "terminated"
                return
            # RUNNING → SHUTTING_DOWN
            self._lifecycle = "shutting_down"
            if self._pending is not None:
                if self._saturation_gen < self._pending_gen:
                    self._saturation_gen = self._pending_gen
                if self._saturation_gen > 0:
                    self._status = _DIAG_BACKLOG
            self._pending = None
            self._pending_time = None
            self._pending_gen = 0
            self._cond.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=join_timeout)
            if thread.is_alive():
                # Do not clear _thread while it is still alive
                return
        with self._cond:
            self._thread = None
            self._lifecycle = "terminated"
