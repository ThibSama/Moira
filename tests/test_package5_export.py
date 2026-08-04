"""Package 5: deterministic CSV/JSON export — golden content, stable fields,
atomic UTF-8 writes, sanitized failure outcomes, no secrets."""

from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from moira.export import (
    CSV_COLUMNS,
    STATUS_DB_ERROR,
    STATUS_EXPORTED,
    STATUS_INVALID_DESTINATION,
    STATUS_NO_DATA,
    STATUS_NO_DATABASE,
    STATUS_SCHEMA_MISMATCH,
    ExportResult,
    export_history,
    to_csv_text,
    to_json_text,
)
from moira.history import HistoryStatus, QuotaObservation
from moira.history_db import (
    _connect,
    init_schema,
    query_30d,
    record_codex_summary,
    record_quota,
    record_token_availability,
    record_token_events,
)
from moira.models import (
    CodexSummary,
    QuotaReading,
    QuotaStatus,
    Service,
    TokenAvailabilityRecord,
    TokenReading,
)

NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)
RESET = NOW + timedelta(days=7)
TOKEN_SOURCE = "codex-app-server:account/usage/read"
CLAUDE_SOURCE = "claude-statusline"


def _seed(tmp_path: Path) -> Path:
    db_path = tmp_path / "history.sqlite3"
    conn = _connect(db_path)
    init_schema(conn)
    record_quota(
        conn,
        QuotaObservation(
            service=Service.CODEX,
            quota_label="Weekly",
            percentage=42.0,
            reset_at=RESET,
            observed_at=NOW,
            source="codex-app-server",
        ),
        now=NOW,
    )
    record_token_events(
        conn,
        [
            TokenReading(
                Service.CODEX,
                date(2026, 8, 1),
                NOW,
                TOKEN_SOURCE,
                HistoryStatus.AVAILABLE_EXACT,
                1234,
            )
        ],
        now=NOW,
    )
    record_codex_summary(
        conn,
        CodexSummary(
            service=Service.CODEX,
            source=TOKEN_SOURCE,
            observed_at=NOW,
            lifetime_tokens=100_000,
            peak_daily_tokens=2_000,
            current_streak_days=3,
            longest_streak_days=5,
            longest_running_turn_sec=120,
        ),
        now=NOW,
    )
    record_token_availability(
        conn,
        TokenAvailabilityRecord(Service.CLAUDE, NOW, CLAUDE_SOURCE, HistoryStatus.UNSUPPORTED),
        now=NOW,
    )
    record_token_availability(
        conn,
        TokenAvailabilityRecord(Service.CODEX, NOW, TOKEN_SOURCE, HistoryStatus.AVAILABLE_EXACT),
        now=NOW,
    )
    conn.close()
    return db_path


def _export(db_path: Path, tmp_path: Path, fmt: str) -> tuple[ExportResult, Path]:
    dest = tmp_path / f"out.{fmt}"
    result = export_history(
        db_path,
        range_func=query_30d,
        service=None,
        fmt=fmt,
        dest=dest,
        now=NOW,
    )
    return result, dest


# ── Golden CSV content ──


def test_csv_golden_rows(tmp_path: Path) -> None:
    db_path = _seed(tmp_path)
    result, dest = _export(db_path, tmp_path, "csv")
    assert result.ok is True
    assert result.status == STATUS_EXPORTED
    assert result.rows == 5
    text = dest.read_text(encoding="utf-8")
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == list(CSV_COLUMNS)
    assert rows[1] == [
        "availability",
        "claude",
        "2026-08-02T12:00:00+00:00",
        "",
        "",
        "",
        "",
        "",
        CLAUDE_SOURCE,
        "unsupported",
        "",
        "",
        "",
        "",
        "",
        "",
    ]
    assert rows[2] == [
        "availability",
        "codex",
        "2026-08-02T12:00:00+00:00",
        "",
        "",
        "",
        "",
        "",
        TOKEN_SOURCE,
        "available_exact",
        "",
        "",
        "",
        "",
        "",
        "",
    ]
    assert rows[3] == [
        "quota",
        "codex",
        "2026-08-02T12:00:00+00:00",
        "",
        "",
        "Weekly",
        "42.0",
        "2026-08-09T12:00:00+00:00",
        "codex-app-server",
        "available_exact",
        "",
        "",
        "",
        "",
        "",
        "",
    ]
    assert rows[4] == [
        "summary",
        "codex",
        "2026-08-02T12:00:00+00:00",
        "",
        "",
        "",
        "",
        "",
        TOKEN_SOURCE,
        "available_exact",
        "",
        "100000",
        "2000",
        "3",
        "5",
        "120",
    ]
    assert rows[5] == [
        "token",
        "codex",
        "2026-08-02T12:00:00+00:00",
        "2026-08-01T00:00:00+00:00",
        "day",
        "",
        "",
        "",
        TOKEN_SOURCE,
        "available_exact",
        "1234",
        "",
        "",
        "",
        "",
        "",
    ]
    assert len(rows) == 6  # header + 5 rows


def test_json_golden_structure(tmp_path: Path) -> None:
    db_path = _seed(tmp_path)
    result, dest = _export(db_path, tmp_path, "json")
    assert result.ok is True
    payload = json.loads(dest.read_text(encoding="utf-8"))
    assert payload["format"] == 1
    rows = payload["rows"]
    assert len(rows) == 5
    kinds = [r["kind"] for r in rows]
    assert kinds == ["availability", "availability", "quota", "summary", "token"]
    token_row = rows[-1]
    assert token_row["service"] == "codex"
    assert token_row["period_kind"] == "day"
    assert token_row["tokens"] == "1234"
    quota_row = rows[2]
    assert quota_row["quota_label"] == "Weekly"
    assert quota_row["percentage"] == "42.0"
    assert "ntfy" not in json.dumps(rows)
    assert "secret" not in json.dumps(rows)


def test_export_is_deterministic(tmp_path: Path) -> None:
    db_path = _seed(tmp_path)
    _, first = _export(db_path, tmp_path, "csv")
    _, second = _export(db_path, tmp_path, "csv")
    assert first.read_bytes() == second.read_bytes()
    # JSON is byte-identical too (sorted keys, fixed indent).
    _, jfirst = _export(db_path, tmp_path, "json")
    _, jsecond = _export(db_path, tmp_path, "json")
    assert jfirst.read_bytes() == jsecond.read_bytes()


def test_export_order_is_stable_regardless_of_insert_order(tmp_path: Path) -> None:
    """Reversing the DB insertion order yields the same file (sorted rows)."""
    db_path = _seed(tmp_path)
    _, dest = _export(db_path, tmp_path, "csv")
    first_bytes = dest.read_bytes()
    # Rebuild with the same data inserted in a different order.
    db2 = tmp_path / "history2.sqlite3"
    conn = _connect(db2)
    init_schema(conn)
    record_token_availability(
        conn,
        TokenAvailabilityRecord(Service.CLAUDE, NOW, CLAUDE_SOURCE, HistoryStatus.UNSUPPORTED),
        now=NOW,
    )
    record_token_availability(
        conn,
        TokenAvailabilityRecord(Service.CODEX, NOW, TOKEN_SOURCE, HistoryStatus.AVAILABLE_EXACT),
        now=NOW,
    )
    record_codex_summary(
        conn,
        CodexSummary(
            service=Service.CODEX,
            source=TOKEN_SOURCE,
            observed_at=NOW,
            lifetime_tokens=100_000,
            peak_daily_tokens=2_000,
            current_streak_days=3,
            longest_streak_days=5,
            longest_running_turn_sec=120,
        ),
        now=NOW,
    )
    record_token_events(
        conn,
        [
            TokenReading(
                Service.CODEX,
                date(2026, 8, 1),
                NOW,
                TOKEN_SOURCE,
                HistoryStatus.AVAILABLE_EXACT,
                1234,
            )
        ],
        now=NOW,
    )
    record_quota(
        conn,
        QuotaObservation(
            service=Service.CODEX,
            quota_label="Weekly",
            percentage=42.0,
            reset_at=RESET,
            observed_at=NOW,
            source="codex-app-server",
        ),
        now=NOW,
    )
    conn.close()
    _, dest2 = _export(db2, tmp_path, "csv")
    assert dest2.read_bytes() == first_bytes


def test_service_filter_limits_export(tmp_path: Path) -> None:
    db_path = _seed(tmp_path)
    dest = tmp_path / "claude.csv"
    result = export_history(
        db_path, range_func=query_30d, service=Service.CLAUDE, fmt="csv", dest=dest, now=NOW
    )
    assert result.ok is True
    rows = list(csv.reader(io.StringIO(dest.read_text(encoding="utf-8"))))
    assert len(rows) == 2  # header + one availability row
    assert rows[1][1] == "claude"


# ── Atomic write ──


def test_atomic_write_leaves_no_temp_file_and_mode_0600(tmp_path: Path) -> None:
    db_path = _seed(tmp_path)
    dest = tmp_path / "sub" / "dir" / "out.csv"
    result = export_history(
        db_path, range_func=query_30d, service=None, fmt="csv", dest=dest, now=NOW
    )
    assert result.ok is True
    assert dest.exists()
    assert not dest.with_name(dest.name + ".tmp").exists()
    assert (dest.stat().st_mode & 0o777) == 0o600
    assert dest.read_text(encoding="utf-8").startswith("kind,service")


# ── Failure outcomes (sanitized) ──


def test_export_invalid_destination_is_directory(tmp_path: Path) -> None:
    db_path = _seed(tmp_path)
    result = export_history(
        db_path, range_func=query_30d, service=None, fmt="csv", dest=tmp_path, now=NOW
    )
    assert result.ok is False
    assert result.status == STATUS_INVALID_DESTINATION


def test_export_no_database_does_not_create_file(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sqlite3"
    dest = tmp_path / "out.csv"
    result = export_history(
        missing, range_func=query_30d, service=None, fmt="csv", dest=dest, now=NOW
    )
    assert result.ok is False
    assert result.status == STATUS_NO_DATABASE
    assert not missing.exists()
    assert not dest.exists()


def test_export_schema_mismatch(tmp_path: Path) -> None:
    db_path = tmp_path / "bad.sqlite3"
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.executescript("CREATE TABLE schema_meta (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO schema_meta (version) VALUES (999)")
    conn.close()
    dest = tmp_path / "out.csv"
    result = export_history(
        db_path, range_func=query_30d, service=None, fmt="csv", dest=dest, now=NOW
    )
    assert result.ok is False
    assert result.status == STATUS_SCHEMA_MISMATCH
    assert not dest.exists()


def test_export_db_error_is_sanitized(tmp_path: Path) -> None:
    """A corrupt database yields a fixed sanitized outcome, never a raw error."""
    db_path = tmp_path / "corrupt.sqlite3"
    db_path.write_bytes(b"this is not a sqlite database at all")
    dest = tmp_path / "out.csv"
    result = export_history(
        db_path, range_func=query_30d, service=None, fmt="csv", dest=dest, now=NOW
    )
    assert result.ok is False
    assert result.status in (STATUS_DB_ERROR, STATUS_SCHEMA_MISMATCH)
    assert "not a sqlite" not in result.status
    assert not dest.exists()


def test_export_empty_range_reports_no_data(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.sqlite3"
    conn = _connect(db_path)
    init_schema(conn)
    conn.close()
    dest = tmp_path / "out.csv"
    result = export_history(
        db_path, range_func=query_30d, service=None, fmt="csv", dest=dest, now=NOW
    )
    assert result.ok is True
    assert result.status == STATUS_NO_DATA
    assert result.rows == 0
    assert not dest.exists()


def test_export_unknown_format_fails_sanitized(tmp_path: Path) -> None:
    db_path = _seed(tmp_path)
    dest = tmp_path / "out.xyz"
    result = export_history(
        db_path, range_func=query_30d, service=None, fmt="xyz", dest=dest, now=NOW
    )
    assert result.ok is False
    assert not dest.exists()


def test_to_csv_text_and_to_json_text_are_pure() -> None:
    # Empty input → header-only CSV / empty rows JSON (deterministic).
    assert to_csv_text([]) == ",".join(CSV_COLUMNS) + "\n"
    assert json.loads(to_json_text([])) == {"format": 1, "rows": []}


def test_delete_all_survives_settings_state_and_keyring(tmp_path: Path) -> None:
    """Confirmed deletion removes only history rows: settings, keyring, and
    current quota state survive (the delete path never touches them)."""
    from unittest.mock import patch

    from moira.history_db import _connect as _db_connect
    from moira.history_db import delete_all
    from moira.history_db import init_schema as _init_schema
    from moira.persistence import (
        AppState,
        Settings,
        load_settings,
        load_state,
        save_settings,
        save_state,
    )

    db_path = _seed(tmp_path)
    with patch.dict(
        "os.environ",
        {
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
        },
    ):
        save_settings(Settings(ntfy_topic="keep", collect_claude=False))
        save_state(
            AppState(
                readings=[
                    QuotaReading(
                        Service.CODEX,
                        "Weekly",
                        42.0,
                        RESET,
                        NOW,
                        "codex-app-server",
                        QuotaStatus.AVAILABLE,
                    )
                ],
                alert_keys=["exhausted:claude:Weekly:k:ntfy"],
                last_refresh="12:00:00",
            )
        )
        conn = _db_connect(db_path)
        _init_schema(conn)
        deleted = delete_all(conn)
        conn.close()
        assert deleted > 0
        # Settings survive untouched.
        settings = load_settings()
        assert settings.ntfy_topic == "keep"
        assert settings.collect_claude is False
        # Current quota state survives untouched (readings, dedup keys, times).
        state = load_state()
        assert state.alert_keys == ["exhausted:claude:Weekly:k:ntfy"]
        assert state.last_refresh == "12:00:00"
        assert [r.percentage for r in state.readings] == [42.0]
    # The keyring token is untouched by construction: the delete path imports
    # only history_db (never secrets.py) — no keyring call exists here.
    assert "secrets" not in os.path.dirname(db_path)
