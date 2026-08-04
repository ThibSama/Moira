"""Package 4b: token-only rendering and provider capability — deterministic tests.

Covers: the pure content-state helper (token-only, summary-only,
availability-only, truly empty, full), the centralized exact-token
capability gate (impossible exact Claude rows ignored — never rendered,
summed or relabeled), production-path reads through a real schema-v4
database with the real range query/_safe_read path (24h/30d boundaries,
Claude/Codex service isolation, token-only/summary-only/availability-only
views, exact-plus-temporary-note coexistence), and GTK render tests
proving a token-only Codex day clears the no-data status and creates the
indicator label.

No sleeps, no network: all reads go through the real SQLite path.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from moira.history import HistoryStatus, QuotaObservation, TokenObservation
from moira.history_db import (
    _connect,
    init_schema,
    query_24h,
    query_30d,
    record_codex_summary,
    record_quota,
    record_token_availability,
    record_token_events,
)
from moira.history_view import (
    HistoryContentState,
    HistoryViewResult,
    _safe_read,
    derive_content_state,
    prepare_history_view,
)
from moira.i18n import _FRENCH, tr
from moira.models import (
    CodexSummary,
    Service,
    TokenAvailabilityRecord,
    TokenReading,
)

NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)
DAY_A = NOW.date()  # 2026-08-02 — inside 24h and 30d
DAY_B = (NOW - timedelta(days=2)).date()  # 2026-07-31 — inside 30d only
TOKEN_SOURCE = "codex-app-server:account/usage/read"
CLAUDE_SOURCE = "claude-statusline"


def _token_obs(
    day: datetime,
    *,
    service: Service = Service.CODEX,
    observed: datetime | None = None,
    source: str = TOKEN_SOURCE,
    status: HistoryStatus = HistoryStatus.AVAILABLE_EXACT,
    tokens: int | None = 500,
    period_kind: str = "day",
) -> TokenObservation:
    from datetime import time as time_type

    period_start = datetime.combine(day.date(), time_type.min, tzinfo=UTC)
    return TokenObservation(
        service=service,
        period_start=period_start,
        period_kind=period_kind,
        observed_at=observed or day,
        source=source,
        status=status,
        tokens=tokens,
    )


def _view(token_obs: list[TokenObservation], **extra: Any) -> HistoryViewResult:
    return prepare_history_view(
        [],
        range_label="30d",
        filter_label="All",
        token_observations=token_obs,
        **extra,
    )


# ── Criterion 1–3: pure immutable content-state helper (GTK-free) ───────────


def test_content_state_token_only() -> None:
    """Token summaries + daily statistics are real History content."""
    view = _view([_token_obs(NOW, tokens=1000)])
    state = derive_content_state(view)
    assert isinstance(state, HistoryContentState)
    assert state.has_quota_series is False
    assert state.has_token_summaries is True
    assert state.has_daily_stats is True
    assert state.has_codex_summaries is False
    assert state.has_availability is False
    assert state.has_token_content is True
    assert state.has_any_content is True
    assert state.is_empty is False


def test_content_state_summary_only() -> None:
    """An official summary alone is a non-empty History result."""
    summary = CodexSummary(
        service=Service.CODEX,
        source=TOKEN_SOURCE,
        observed_at=NOW,
        lifetime_tokens=100000,
    )
    view = prepare_history_view(
        [], range_label="30d", filter_label="All", codex_summaries=[summary]
    )
    state = derive_content_state(view)
    assert state.has_codex_summaries is True
    assert state.has_quota_series is False
    assert state.has_token_content is True
    assert state.is_empty is False


def test_content_state_availability_only() -> None:
    """Availability-only results remain visible as sanitized provider states."""
    avail = TokenAvailabilityRecord(
        service=Service.CLAUDE,
        observed_at=NOW,
        source=CLAUDE_SOURCE,
        status=HistoryStatus.UNSUPPORTED,
    )
    view = prepare_history_view(
        [], range_label="30d", filter_label="All", token_availability_records=[avail]
    )
    state = derive_content_state(view)
    assert state.has_availability is True
    assert state.has_token_content is False
    assert state.is_empty is False  # availability alone is not "no history"


def test_content_state_truly_empty() -> None:
    """Only a fully empty result is classified as no-history-at-all."""
    view = prepare_history_view([], range_label="30d", filter_label="All")
    state = derive_content_state(view)
    assert state.has_quota_series is False
    assert state.has_token_content is False
    assert state.has_availability is False
    assert state.is_empty is True


def test_content_state_full() -> None:
    """A full result sets every flag."""
    from moira.history import QuotaObservation

    quota = [
        QuotaObservation(
            service=Service.CODEX,
            quota_label="Weekly",
            percentage=50.0,
            reset_at=NOW + timedelta(days=5),
            observed_at=NOW,
            source=TOKEN_SOURCE,
        )
    ]
    avail = TokenAvailabilityRecord(
        service=Service.CODEX,
        observed_at=NOW,
        source=TOKEN_SOURCE,
        status=HistoryStatus.TEMPORARILY_UNAVAILABLE,
    )
    view = prepare_history_view(
        quota,
        range_label="30d",
        filter_label="All",
        token_observations=[_token_obs(NOW, tokens=1000)],
        token_availability_records=[avail],
        codex_summaries=[
            CodexSummary(
                service=Service.CODEX,
                source=TOKEN_SOURCE,
                observed_at=NOW,
                lifetime_tokens=1,
            )
        ],
    )
    state = derive_content_state(view)
    assert state.has_quota_series is True
    assert state.has_token_summaries is True
    assert state.has_daily_stats is True
    assert state.has_codex_summaries is True
    assert state.has_availability is True
    assert state.is_empty is False


# ── Criterion 4–5: centralized exact-token capability gate ──────────────────


def test_exact_claude_daily_ignored() -> None:
    """An exact Claude daily row is never rendered, summed or relabeled."""
    view = _view([_token_obs(NOW, tokens=5000, service=Service.CLAUDE)])
    assert view.token_summaries == ()
    assert view.daily_token_stats == ()


def test_exact_claude_bucket_ignored() -> None:
    """An exact Claude bucket row is ignored too."""
    view = _view([_token_obs(NOW, tokens=5000, service=Service.CLAUDE, period_kind="bucket")])
    assert view.token_summaries == ()
    assert view.daily_token_stats == ()


def test_exact_claude_duplicates_ignored_without_raise() -> None:
    """Impossible exact Claude duplicates are ignored before the duplicate
    check — they never raise and never double-count anything."""
    view = _view(
        [
            _token_obs(NOW, tokens=1, service=Service.CLAUDE),
            _token_obs(NOW, tokens=2, service=Service.CLAUDE),
        ]
    )
    assert view.daily_token_stats == ()
    assert view.token_summaries == ()


def test_exact_codex_still_supported() -> None:
    """Codex exact daily rows remain fully supported."""
    view = _view([_token_obs(NOW, tokens=500)])
    assert len(view.daily_token_stats) == 1
    assert view.daily_token_stats[0].total_tokens == 500
    assert len(view.token_summaries) == 1


def test_mixed_codex_and_claude_rows() -> None:
    """Codex exact rows aggregate; a same-day exact Claude row is ignored."""
    view = _view(
        [
            _token_obs(NOW, tokens=500),  # codex
            _token_obs(NOW, tokens=9000, service=Service.CLAUDE),  # ignored
            _token_obs(
                NOW,
                service=Service.CLAUDE,
                source=CLAUDE_SOURCE,
                status=HistoryStatus.UNSUPPORTED,
                tokens=None,
            ),
        ]
    )
    assert len(view.daily_token_stats) == 1
    assert view.daily_token_stats[0].total_tokens == 500
    assert view.daily_token_stats[0].reported_days == 1
    assert all(ts.service is Service.CODEX for ts in view.token_summaries)


# ── Criterion 7: production-path reads (real schema-v4 DB + _safe_read) ─────


def _seed(
    tmp_path: Path,
    *,
    codex_days: list[tuple[date, int]] | None = None,
    bucket_tokens: int | None = None,
    quota: bool = False,
    summary: bool = False,
    claude_exact: bool = False,
    claude_avail: HistoryStatus | None = None,
    codex_avail: HistoryStatus | None = None,
) -> Path:
    """Create a real schema-v4 database seeded through production writers."""
    db_path = tmp_path / "history.sqlite3"
    conn = _connect(db_path)
    init_schema(conn)
    for day, tokens in codex_days or []:
        record_token_events(
            conn,
            [
                TokenReading(
                    service=Service.CODEX,
                    day=day,
                    retrieved_at=NOW,
                    source=TOKEN_SOURCE,
                    status=HistoryStatus.AVAILABLE_EXACT,
                    tokens=tokens,
                )
            ],
            now=NOW,
        )
    if bucket_tokens is not None:
        conn.execute(
            "INSERT INTO token_events "
            "(event_key, service, period_start, period_kind, observed_at, source, status, "
            "total_tokens) VALUES (?, ?, ?, 'bucket', ?, ?, 'available_exact', ?)",
            (
                "codex:b:2026-07-31T12:00:00+00:00",
                "codex",
                "2026-07-31T12:00:00+00:00",
                "2026-07-31T12:00:00+00:00",
                TOKEN_SOURCE,
                bucket_tokens,
            ),
        )
    if quota:
        record_quota(
            conn,
            QuotaObservation(
                service=Service.CODEX,
                quota_label="Weekly",
                percentage=50.0,
                reset_at=NOW + timedelta(days=5),
                observed_at=NOW,
                source=TOKEN_SOURCE,
            ),
            now=NOW,
        )
    if summary:
        record_codex_summary(
            conn,
            CodexSummary(
                service=Service.CODEX,
                source=TOKEN_SOURCE,
                observed_at=NOW,
                lifetime_tokens=100000,
            ),
            now=NOW,
        )
    if claude_exact:
        # Impossible by construction (collector emits UNSUPPORTED) — planted
        # to prove the read path ignores it.
        conn.execute(
            "INSERT INTO token_events "
            "(event_key, service, period_start, period_kind, observed_at, source, status, "
            "total_tokens) VALUES (?, ?, ?, 'day', ?, ?, 'available_exact', ?)",
            (
                "claude:d:2026-08-02",
                "claude",
                "2026-08-02",
                "2026-08-02T12:00:00+00:00",
                "moira-test",
                9999,
            ),
        )
    if claude_avail is not None:
        record_token_availability(
            conn,
            TokenAvailabilityRecord(
                service=Service.CLAUDE,
                observed_at=NOW,
                source=CLAUDE_SOURCE,
                status=claude_avail,
            ),
            now=NOW,
        )
    if codex_avail is not None:
        record_token_availability(
            conn,
            TokenAvailabilityRecord(
                service=Service.CODEX,
                observed_at=NOW,
                source=TOKEN_SOURCE,
                status=codex_avail,
            ),
            now=NOW,
        )
    conn.close()
    return db_path


def _read(
    db_path: Path,
    *,
    range_func: Any = query_30d,
    range_label: str = "30d",
    filter_label: str = "All",
    service: Service | None = None,
    req_id: int = 1,
) -> HistoryViewResult:
    result = _safe_read(
        range_func=range_func,
        range_label=range_label,
        filter_label=filter_label,
        service=service,
        now=NOW,
        req_id=req_id,
        db_path=db_path,
    )
    assert result is not None
    _, view = result
    return view


def test_production_24h_vs_30d_boundaries(tmp_path: Path) -> None:
    """Range queries filter persisted rows; indicators follow exactly."""
    db_path = _seed(tmp_path, codex_days=[(DAY_B, 1000), (DAY_A, 3000)])
    view24 = _read(db_path, range_func=query_24h, range_label="24h")
    view30 = _read(db_path, range_func=query_30d, range_label="30d", req_id=2)

    assert view24.daily_token_stats[0].total_tokens == 3000
    assert view24.daily_token_stats[0].reported_days == 1
    assert view30.daily_token_stats[0].total_tokens == 4000
    assert view30.daily_token_stats[0].reported_days == 2
    assert view24.token_summaries[0].total_tokens == 3000
    assert view30.token_summaries[0].total_tokens == 4000


def test_production_service_isolation(tmp_path: Path) -> None:
    """Claude/Codex service filtering affects indicators exactly as rows."""
    db_path = _seed(
        tmp_path,
        codex_days=[(DAY_A, 3000)],
        summary=True,
        claude_avail=HistoryStatus.UNSUPPORTED,
        codex_avail=HistoryStatus.TEMPORARILY_UNAVAILABLE,
    )
    view_codex = _read(db_path, service=Service.CODEX)
    assert len(view_codex.daily_token_stats) == 1
    assert view_codex.daily_token_stats[0].total_tokens == 3000
    assert len(view_codex.codex_summaries) == 1
    assert {s.service for s in view_codex.token_availability} == {Service.CODEX}

    view_claude = _read(db_path, service=Service.CLAUDE, req_id=2)
    assert view_claude.daily_token_stats == ()
    assert view_claude.token_summaries == ()
    assert view_claude.codex_summaries == ()
    assert view_claude.series == ()  # no quota rows seeded
    assert {s.service for s in view_claude.token_availability} == {Service.CLAUDE}
    assert view_claude.token_availability[0].status is HistoryStatus.UNSUPPORTED

    view_all = _read(db_path, service=None, req_id=3)
    assert len(view_all.daily_token_stats) == 1
    assert {s.service for s in view_all.token_availability} == {
        Service.CLAUDE,
        Service.CODEX,
    }


def test_production_exact_claude_corruption_ignored(tmp_path: Path) -> None:
    """A planted exact Claude row in the DB never reaches indicators."""
    db_path = _seed(tmp_path, codex_days=[(DAY_A, 3000)], claude_exact=True)
    view = _read(db_path)
    assert len(view.daily_token_stats) == 1
    assert view.daily_token_stats[0].service is Service.CODEX
    assert view.daily_token_stats[0].total_tokens == 3000
    assert all(ts.service is Service.CODEX for ts in view.token_summaries)


def test_production_bucket_excluded(tmp_path: Path) -> None:
    """A persisted bucket row stays in its summary but never in daily stats."""
    db_path = _seed(tmp_path, codex_days=[(DAY_A, 3000)], bucket_tokens=4000)
    view = _read(db_path)
    stats = view.daily_token_stats[0]
    assert stats.total_tokens == 3000
    assert stats.reported_days == 1
    kinds = {ts.period_kind for ts in view.token_summaries}
    assert kinds == {"day", "bucket"}
    bucket_summary = next(ts for ts in view.token_summaries if ts.period_kind == "bucket")
    assert bucket_summary.total_tokens == 4000


def test_production_token_only_view_is_non_empty(tmp_path: Path) -> None:
    """Token-only data is a non-empty History result with no quota series."""
    db_path = _seed(tmp_path, codex_days=[(DAY_A, 3000)])
    view = _read(db_path)
    assert view.series == ()
    assert view.diagnostic == "ok"
    assert len(view.daily_token_stats) == 1
    state = derive_content_state(view)
    assert state.has_token_content is True
    assert state.has_quota_series is False
    assert state.is_empty is False


def test_production_summary_only_view(tmp_path: Path) -> None:
    """A summary-only database still produces a non-empty view."""
    db_path = _seed(tmp_path, summary=True)
    view = _read(db_path)
    assert view.series == ()
    assert view.daily_token_stats == ()
    assert len(view.codex_summaries) == 1
    assert derive_content_state(view).has_codex_summaries is True
    assert derive_content_state(view).is_empty is False


def test_production_availability_only_view(tmp_path: Path) -> None:
    """Availability-only data remains visible as sanitized states."""
    db_path = _seed(tmp_path, claude_avail=HistoryStatus.UNSUPPORTED)
    view = _read(db_path)
    assert view.series == ()
    assert view.daily_token_stats == ()
    assert len(view.token_availability) == 1
    assert view.token_availability[0].service is Service.CLAUDE
    assert view.token_availability[0].status is HistoryStatus.UNSUPPORTED
    state = derive_content_state(view)
    assert state.has_availability is True
    assert state.is_empty is False


def test_production_truly_empty_database(tmp_path: Path) -> None:
    """An initialized database with no rows at all is 'empty range'."""
    db_path = _seed(tmp_path)
    result = _safe_read(
        range_func=query_30d,
        range_label="30d",
        filter_label="All",
        service=None,
        now=NOW,
        req_id=1,
        db_path=db_path,
    )
    assert result is not None
    _, view = result
    assert view.diagnostic == "empty range"
    assert derive_content_state(view).is_empty is True


def test_production_exact_plus_temporary_note(tmp_path: Path) -> None:
    """Codex exact data coexists with a current temporary unavailability."""
    db_path = _seed(
        tmp_path,
        codex_days=[(DAY_A, 3000)],
        codex_avail=HistoryStatus.TEMPORARILY_UNAVAILABLE,
    )
    view = _read(db_path)
    assert len(view.daily_token_stats) == 1
    assert view.daily_token_stats[0].total_tokens == 3000
    codex_state = next(s for s in view.token_availability if s.service is Service.CODEX)
    assert codex_state.status is HistoryStatus.TEMPORARILY_UNAVAILABLE


# ── Criterion 8: GTK render tests ───────────────────────────────────────────


class _DummyExecutor:
    def submit(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no reader submits expected in render tests")


def _page() -> Any:
    from moira.history_page import HistoryPage

    try:
        return HistoryPage(_DummyExecutor())
    except Exception as exc:  # pragma: no cover - headless environments
        pytest.skip(f"GTK display unavailable: {exc}")


def _stats_box_texts(page: Any) -> list[str]:
    texts: list[str] = []
    child = page._stats_box.get_first_child()
    while child is not None:
        texts.append(child.get_text())
        child = child.get_next_sibling()
    return texts


def test_gtk_render_token_only_clears_no_data_status() -> None:
    """A token-only Codex day never shows 'No history data' and renders the
    indicator label."""
    page = _page()
    view = _view([_token_obs(NOW, tokens=1000)])
    page._render_result(view)
    assert page._status_label.get_text() != tr("No history data for this range")
    assert page._status_label.get_text() == tr("No quota observations for this range")
    texts = _stats_box_texts(page)
    # The compact indicator line (range label '30d' is locale-independent).
    indicator = next(t for t in texts if "30d" in t)
    assert "Total: 1,000" in indicator
    assert "2026-08-02" in indicator  # peak day rendered
    assert "100.0%" in indicator  # peak share rendered
    page.shutdown()


def test_gtk_render_truly_empty_shows_no_history() -> None:
    """A fully empty view keeps the no-history status."""
    page = _page()
    view = prepare_history_view([], range_label="30d", filter_label="All")
    page._render_result(view)
    assert page._status_label.get_text() == tr("No history data for this range")
    assert _stats_box_texts(page) == []
    page.shutdown()


def test_gtk_render_quota_plus_tokens_clears_status() -> None:
    """Quota series present: the status label is cleared and both series and
    indicator labels render."""
    page = _page()
    quota = [
        QuotaObservation(
            service=Service.CODEX,
            quota_label="Weekly",
            percentage=50.0,
            reset_at=NOW + timedelta(days=5),
            observed_at=NOW,
            source=TOKEN_SOURCE,
        )
    ]
    view = prepare_history_view(
        quota,
        range_label="30d",
        filter_label="All",
        token_observations=[_token_obs(NOW, tokens=1000)],
    )
    page._render_result(view)
    assert page._status_label.get_text() == ""
    texts = _stats_box_texts(page)
    assert any("Codex Weekly" in t for t in texts)  # series stats label
    assert any("30d" in t for t in texts)  # indicator label
    page.shutdown()


def test_gtk_render_availability_only_keeps_note() -> None:
    """Availability-only results stay visible with their sanitized note."""
    page = _page()
    avail = TokenAvailabilityRecord(
        service=Service.CLAUDE,
        observed_at=NOW,
        source=CLAUDE_SOURCE,
        status=HistoryStatus.UNSUPPORTED,
    )
    view = prepare_history_view(
        [], range_label="30d", filter_label="All", token_availability_records=[avail]
    )
    page._render_result(view)
    assert page._status_label.get_text() == tr("No quota observations for this range")
    note = page._token_note.get_text()
    assert "Claude" in note
    assert tr("Exact token usage is not available") in note
    page.shutdown()


# ── Translations ────────────────────────────────────────────────────────────


def test_french_catalog_has_quota_absent_note() -> None:
    """The new quota-absent status string is translated."""
    assert _FRENCH.get("No quota observations for this range") is not None
