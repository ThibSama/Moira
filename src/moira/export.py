"""Deterministic CSV/JSON history export with atomic UTF-8 writes.

The export reads the same typed domain objects as the History tab (quota
observations, token events, official summaries, availability records) for
the selected range and service filter, and renders them with stable
fields. Rows are sorted by observed time so identical databases produce
byte-identical files. Writes go to an explicit destination via a
temporary file + atomic replace, off the GTK thread.

Only sanitized typed fields are exported — never secrets, raw payloads,
exceptions, or paths. Failures return a bounded ``ExportResult`` with a
fixed sanitized status string.
"""

from __future__ import annotations

import csv
import io
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .history import HistoryStatus, QuotaObservation, SchemaVersionError, TokenObservation
from .models import CodexSummary, Service, TokenAvailabilityRecord

EXPORT_FORMATS = ("csv", "json")

#: Stable column order shared by CSV and JSON. Empty cells for fields that
#: do not apply to a row kind. Values are deterministic strings (str() of
#: ints, shortest round-trip repr of floats — never rounded).
CSV_COLUMNS = (
    "kind",
    "service",
    "observed_at",
    "period_start",
    "period_kind",
    "quota_label",
    "percentage",
    "reset_at",
    "source",
    "status",
    "tokens",
    "lifetime_tokens",
    "peak_daily_tokens",
    "current_streak_days",
    "longest_streak_days",
    "longest_running_turn_sec",
)

#: Sanitized outcome statuses (fixed set; no free-form strings).
STATUS_EXPORTED = "exported"
STATUS_NO_DATA = "no data"
STATUS_NO_DATABASE = "no database"
STATUS_SCHEMA_MISMATCH = "schema mismatch"
STATUS_DB_ERROR = "database unavailable"
STATUS_INVALID_DESTINATION = "invalid destination"
STATUS_WRITE_FAILED = "export failed"


@dataclass(frozen=True, slots=True)
class ExportResult:
    """Bounded outcome of one export run."""

    ok: bool
    status: str
    rows: int = 0


def _quota_row(obs: QuotaObservation) -> dict[str, Any]:
    return {
        "kind": "quota",
        "service": obs.service.value,
        "observed_at": obs.observed_at.isoformat(),
        "period_start": "",
        "period_kind": "",
        "quota_label": obs.quota_label,
        "percentage": str(obs.percentage),
        "reset_at": obs.reset_at.isoformat(),
        "source": obs.source,
        "status": obs.status.value,
        "tokens": "",
        "lifetime_tokens": "",
        "peak_daily_tokens": "",
        "current_streak_days": "",
        "longest_streak_days": "",
        "longest_running_turn_sec": "",
    }


def _token_row(obs: TokenObservation) -> dict[str, Any]:
    return {
        "kind": "token",
        "service": obs.service.value,
        "observed_at": obs.observed_at.isoformat(),
        "period_start": obs.period_start.isoformat(),
        "period_kind": obs.period_kind,
        "quota_label": "",
        "percentage": "",
        "reset_at": "",
        "source": obs.source,
        "status": obs.status.value,
        "tokens": str(obs.tokens) if obs.tokens is not None else "",
        "lifetime_tokens": "",
        "peak_daily_tokens": "",
        "current_streak_days": "",
        "longest_streak_days": "",
        "longest_running_turn_sec": "",
    }


def _summary_row(summary: CodexSummary) -> dict[str, Any]:
    return {
        "kind": "summary",
        "service": summary.service.value,
        "observed_at": summary.observed_at.isoformat(),
        "period_start": "",
        "period_kind": "",
        "quota_label": "",
        "percentage": "",
        "reset_at": "",
        "source": summary.source,
        "status": HistoryStatus.AVAILABLE_EXACT.value,
        "tokens": "",
        "lifetime_tokens": (
            str(summary.lifetime_tokens) if summary.lifetime_tokens is not None else ""
        ),
        "peak_daily_tokens": (
            str(summary.peak_daily_tokens) if summary.peak_daily_tokens is not None else ""
        ),
        "current_streak_days": (
            str(summary.current_streak_days) if summary.current_streak_days is not None else ""
        ),
        "longest_streak_days": (
            str(summary.longest_streak_days) if summary.longest_streak_days is not None else ""
        ),
        "longest_running_turn_sec": (
            str(summary.longest_running_turn_sec)
            if summary.longest_running_turn_sec is not None
            else ""
        ),
    }


def _availability_row(record: TokenAvailabilityRecord) -> dict[str, Any]:
    return {
        "kind": "availability",
        "service": record.service.value,
        "observed_at": record.observed_at.isoformat(),
        "period_start": "",
        "period_kind": "",
        "quota_label": "",
        "percentage": "",
        "reset_at": "",
        "source": record.source,
        "status": record.status.value,
        "tokens": "",
        "lifetime_tokens": "",
        "peak_daily_tokens": "",
        "current_streak_days": "",
        "longest_streak_days": "",
        "longest_running_turn_sec": "",
    }


def _row_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    """Deterministic ordering: observed time, kind, service, period key."""
    return (
        row["observed_at"],
        row["kind"],
        row["service"],
        row["period_start"],
        row["quota_label"],
    )


def build_export_rows(
    *,
    quota: list[QuotaObservation],
    tokens: list[TokenObservation],
    summaries: list[CodexSummary],
    availability: list[TokenAvailabilityRecord],
) -> list[dict[str, Any]]:
    """Build deterministic export rows from typed domain objects."""
    rows: list[dict[str, Any]] = []
    rows.extend(_quota_row(obs) for obs in quota)
    rows.extend(_token_row(obs) for obs in tokens)
    rows.extend(_summary_row(s) for s in summaries)
    rows.extend(_availability_row(record) for record in availability)
    rows.sort(key=_row_sort_key)
    return rows


def to_csv_text(rows: list[dict[str, Any]]) -> str:
    """Render rows as deterministic CSV (fixed column order, \\n endings)."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(CSV_COLUMNS), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.get(column, "") for column in CSV_COLUMNS})
    return buffer.getvalue()


def to_json_text(rows: list[dict[str, Any]]) -> str:
    """Render rows as deterministic JSON (sorted keys, fixed indent)."""
    payload: dict[str, Any] = {"format": 1, "rows": rows}
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _atomic_write_text(path: Path, content: str) -> None:
    """Write UTF-8 text atomically (temp file + replace, mode 0600)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def export_history(
    db_path: Path,
    *,
    range_func: Callable[..., dict[str, Any]],
    range_delta: timedelta,
    service: Service | None,
    fmt: str,
    dest: Path,
    now: datetime | None = None,
) -> ExportResult:
    """Read the selected range from the history database and write the file.

    ``range_delta`` is the selected 24h/7d/30d/90d boundary: quota, token,
    summary AND availability rows are all read with the same clock and the
    same ``since = now - range_delta`` boundary, so a narrow export never
    contains out-of-range availability rows.

    Runs off the GTK thread. Never raises for DB or write failures and
    never exposes raw exceptions, SQL, payloads, paths, or secrets in the
    returned status. An absent database is reported without creating one.
    """
    if fmt not in EXPORT_FORMATS:
        return ExportResult(False, STATUS_WRITE_FAILED)
    try:
        if dest.exists() and dest.is_dir():
            return ExportResult(False, STATUS_INVALID_DESTINATION)
    except OSError:
        return ExportResult(False, STATUS_INVALID_DESTINATION)

    from .history_db import _connect, init_schema, query_token_availability

    if not db_path.exists():
        return ExportResult(False, STATUS_NO_DATABASE)

    clock = now or datetime.now(UTC)
    try:
        conn = _connect(db_path, timeout=5.0)
        try:
            init_schema(conn)
            result = range_func(conn, now=clock)
            quota: list[QuotaObservation] = list(result.get("quota", []))
            tokens: list[TokenObservation] = list(result.get("tokens", []))
            summaries: list[CodexSummary] = list(result.get("summaries", []))
            # The same clock and boundary as the selected range query.
            since = clock - range_delta
            availability: list[TokenAvailabilityRecord] = query_token_availability(
                conn, since=since
            )
            if service is not None:
                quota = [o for o in quota if o.service is service]
                tokens = [o for o in tokens if o.service is service]
                summaries = [s for s in summaries if s.service is service]
                availability = [a for a in availability if a.service is service]
        finally:
            conn.close()
    except SchemaVersionError:
        return ExportResult(False, STATUS_SCHEMA_MISMATCH)
    except Exception:
        return ExportResult(False, STATUS_DB_ERROR)

    rows = build_export_rows(
        quota=quota, tokens=tokens, summaries=summaries, availability=availability
    )
    if not rows:
        return ExportResult(True, STATUS_NO_DATA, 0)
    content = to_csv_text(rows) if fmt == "csv" else to_json_text(rows)
    try:
        _atomic_write_text(dest, content)
    except OSError:
        return ExportResult(False, STATUS_WRITE_FAILED)
    return ExportResult(True, STATUS_EXPORTED, len(rows))
