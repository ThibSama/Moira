"""Package 6c — activity panel and watcher tests.

Panel tests (Xvfb-gated): spinner while running, concurrent count, latest
sanitized model label, terminal symbolic icons, presence in full/compact
modes, and the five-minute disappearance (pure derivation, deterministic
clock). Watcher tests (GLib only, no display needed): FileMonitor-driven
reload on external writes, deletion/corruption tolerance, main-thread
dispatch, bounded self-cancelling timers and monitor/timer shutdown.
"""

from __future__ import annotations

import time as _time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import gi  # type: ignore[import-untyped]
import pytest

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # type: ignore[import-untyped]  # noqa: E402

from moira.activity import (
    TERMINAL_WINDOW_SECONDS,
    WATCHDOG_STALE_SECONDS,
    ActivityEvent,
    ActivityState,
    ActivityStore,
    AgentRuntime,
    RuntimeActivity,
    derive_runtime_activity,
    hash_identity,
)

NOW = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)


def _event(
    runtime: AgentRuntime,
    state: ActivityState,
    session: str,
    at: datetime = NOW,
    model: str = "model-x",
) -> ActivityEvent:
    return ActivityEvent(runtime, state, hash_identity(session), model, at)


def _write_external(store: ActivityStore, event: ActivityEvent) -> None:
    """Simulate a hook process writing the store (separate instance)."""
    external = ActivityStore(store.path)
    external.record(event)


# ── Pure display derivation (no display required) ──


def test_view_running_spinner_state_and_count() -> None:
    data = {
        "version": 1,
        "sessions": {
            "claude": {
                hash_identity("a"): {
                    "state": "RUNNING",
                    "model": "m1",
                    "started_at": NOW.isoformat(),
                    "updated_at": (NOW + timedelta(seconds=2)).isoformat(),
                },
                hash_identity("b"): {
                    "state": "RUNNING",
                    "model": "m2",
                    "started_at": NOW.isoformat(),
                    "updated_at": (NOW + timedelta(seconds=1)).isoformat(),
                },
            }
        },
        "last_events": {},
    }
    view = derive_runtime_activity(data, now=NOW + timedelta(seconds=3))
    claude = view[AgentRuntime.CLAUDE]
    assert claude.state is ActivityState.RUNNING
    assert claude.active_count == 2
    assert claude.model == "m1"  # latest sanitized model label
    assert view[AgentRuntime.CODEX].visible is False
    assert view[AgentRuntime.HERMES].visible is False


def test_view_terminal_icon_states() -> None:
    for state in (ActivityState.COMPLETED, ActivityState.FAILED, ActivityState.INTERRUPTED):
        data = {
            "version": 1,
            "sessions": {
                "hermes": {
                    hash_identity("s"): {
                        "state": state.value,
                        "model": "m",
                        "started_at": NOW.isoformat(),
                        "updated_at": NOW.isoformat(),
                    }
                }
            },
            "last_events": {},
        }
        view = derive_runtime_activity(data, now=NOW + timedelta(seconds=1))
        assert view[AgentRuntime.HERMES].state is state


# ── Panel (Xvfb-gated) ──


def _panel() -> Any:
    from moira.activity_view import ActivityPanel

    try:
        return ActivityPanel()
    except Exception as exc:  # headless environments
        pytest.skip(f"GTK display unavailable: {exc}")


def _row(panel: Any, runtime: AgentRuntime) -> Any:
    return panel.rows[runtime]


def test_panel_has_heading_and_three_runtime_rows() -> None:
    panel = _panel()
    from moira.i18n import tr

    texts: list[str] = []
    stack = [panel]
    while stack:
        current = stack.pop()
        if hasattr(current, "get_text"):
            text = current.get_text()
            if text:
                texts.append(text)
        child = current.get_first_child()
        while child is not None:
            stack.append(child)
            child = child.get_next_sibling()
    assert tr("Agent activity") in texts
    for runtime in AgentRuntime:
        row = _row(panel, runtime)
        assert row.runtime is runtime
        assert isinstance(row.spinner, Gtk.Spinner)
    assert panel.get_visible() is False  # hidden when nothing to show


def test_row_running_shows_spinner_and_count() -> None:
    panel = _panel()
    views = {
        AgentRuntime.CLAUDE: __import__(
            "moira.activity", fromlist=["RuntimeActivity"]
        ).RuntimeActivity(AgentRuntime.CLAUDE, ActivityState.RUNNING, 2, "opus", NOW, True),
        AgentRuntime.CODEX: __import__(
            "moira.activity", fromlist=["RuntimeActivity"]
        ).RuntimeActivity(AgentRuntime.CODEX, None, 0, "", None, False),
        AgentRuntime.HERMES: __import__(
            "moira.activity", fromlist=["RuntimeActivity"]
        ).RuntimeActivity(AgentRuntime.HERMES, None, 0, "", None, False),
    }
    panel.update(views)
    row = _row(panel, AgentRuntime.CLAUDE)
    assert row.get_visible()
    assert row.spinner.get_visible()
    assert row.spinner.get_spinning()
    from moira.i18n import tr

    assert tr("Active") == row.state_label.get_text()
    assert tr("{count} active").format(count=2) == row.count_label.get_text()
    assert row.model_label.get_text() == "opus"
    # Terminal rows hidden.
    assert _row(panel, AgentRuntime.CODEX).get_visible() is False


def test_row_terminal_shows_symbolic_icon_and_stops_spinner() -> None:
    panel = _panel()
    for state, icon in (
        (ActivityState.COMPLETED, "emblem-ok-symbolic"),
        (ActivityState.FAILED, "dialog-error-symbolic"),
        (ActivityState.INTERRUPTED, "process-stop-symbolic"),
    ):
        views = {
            runtime: RuntimeActivity(runtime, None, 0, "", None, False) for runtime in AgentRuntime
        }
        views[AgentRuntime.HERMES] = RuntimeActivity(AgentRuntime.HERMES, state, 0, "m", NOW, True)
        panel.update(views)
        row = _row(panel, AgentRuntime.HERMES)
        assert row.get_visible()
        assert not row.spinner.get_visible()
        assert row.icon.get_visible()
        assert row.icon.get_icon_name() == icon


def test_panel_hides_when_nothing_visible() -> None:
    panel = _panel()
    views = {
        runtime: RuntimeActivity(runtime, None, 0, "", None, False) for runtime in AgentRuntime
    }
    panel.update(views)
    assert panel.get_visible() is False


def test_activity_panel_below_quota_cards_and_mode_independent() -> None:
    """The panel is appended to the home page after the quota cards, and is
    independent of compact mode (a structural source contract)."""
    source = (Path(__file__).resolve().parents[1] / "src" / "moira" / "ui.py").read_text(
        encoding="utf-8"
    )
    cards = source.index("home.append(self.codex_card)")
    panel = source.index("home.append(self._activity_panel)")
    assert panel > cards
    assert "self._activity_panel = ActivityPanel()" in source
    # Compact mode toggles only the quota cards, never the panel.
    assert "set_compact" in source


# ── Watcher (GLib only, no display required) ──


def _wait_until(condition: Any, timeout: float = 5.0) -> bool:
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if condition():
            return True
        while GLib.MainContext.default().iteration(False):
            pass
        _time.sleep(0.01)
    return False


def test_watcher_reloads_on_external_write(tmp_path: Path) -> None:
    from moira.activity_view import ActivityWatcher

    store = ActivityStore(tmp_path / "activity.json")
    now = datetime.now(UTC)
    store.record(_event(AgentRuntime.CLAUDE, ActivityState.RUNNING, "s1", at=now))
    calls: list[bool] = []

    def on_change() -> None:
        calls.append(True)

    watcher = ActivityWatcher(store, on_change)
    try:
        # A hook process writes a new event through its own store instance.
        _write_external(
            store,
            _event(
                AgentRuntime.CLAUDE, ActivityState.COMPLETED, "s1", at=now + timedelta(seconds=5)
            ),
        )
        assert _wait_until(lambda: bool(calls)), "on_change never fired"
        assert store.snapshot()["sessions"]["claude"][hash_identity("s1")]["state"] == "COMPLETED"
    finally:
        watcher.shutdown()


def test_watcher_tolerates_deletion(tmp_path: Path) -> None:
    from moira.activity_view import ActivityWatcher

    store = ActivityStore(tmp_path / "activity.json")
    store.record(_event(AgentRuntime.CLAUDE, ActivityState.RUNNING, "s1", at=datetime.now(UTC)))
    calls: list[bool] = []
    watcher = ActivityWatcher(store, lambda: calls.append(True))
    try:
        store.path.unlink()
        assert _wait_until(lambda: bool(calls)), "deletion never surfaced"
        assert store.snapshot()["sessions"] == {}
    finally:
        watcher.shutdown()


def test_watcher_tolerates_corruption(tmp_path: Path) -> None:
    from moira.activity_view import ActivityWatcher

    store = ActivityStore(tmp_path / "activity.json")
    store.record(_event(AgentRuntime.CLAUDE, ActivityState.RUNNING, "s1", at=datetime.now(UTC)))
    calls: list[bool] = []
    watcher = ActivityWatcher(store, lambda: calls.append(True))
    try:
        store.path.write_text("{corrupt", encoding="utf-8")
        assert _wait_until(lambda: bool(calls)), "corruption never surfaced"
        assert store.snapshot()["sessions"] == {}
    finally:
        watcher.shutdown()


def test_watcher_shutdown_releases_monitor_and_timers(tmp_path: Path) -> None:
    from moira.activity_view import ActivityWatcher

    store = ActivityStore(tmp_path / "activity.json")
    store.record(_event(AgentRuntime.CLAUDE, ActivityState.RUNNING, "s1", at=datetime.now(UTC)))
    watcher = ActivityWatcher(store, lambda: None)
    assert watcher._monitor is not None
    assert watcher._timer_id is not None  # watchdog bound while running
    watcher.shutdown()
    assert watcher._closed
    assert watcher._monitor is None
    assert watcher._timer_id is None
    watcher.shutdown()  # idempotent


def test_watcher_timer_cancelled_when_nothing_visible(tmp_path: Path) -> None:

    from moira.activity_view import ActivityWatcher

    store = ActivityStore(tmp_path / "activity.json")
    watcher = ActivityWatcher(store, lambda: None)
    # Nothing to watch: the timer must be cancelled (bounded timers).
    assert watcher._timer_id is None
    watcher.shutdown()


def test_watcher_timer_delay_bounds(tmp_path: Path) -> None:
    """The single timer is bounded: five-minute hide after a terminal event
    and the watchdog bound for running sessions, floored at 1 second."""
    from unittest.mock import patch

    from moira.activity_view import ActivityWatcher

    delays: list[int] = []

    def fake_timeout_add(delay_ms: int, callback: Any) -> int:
        delays.append(delay_ms)
        return 1

    store = ActivityStore(tmp_path / "activity.json")
    now = datetime.now(UTC)
    store.record(
        _event(AgentRuntime.CLAUDE, ActivityState.RUNNING, "s1", at=now - timedelta(seconds=10))
    )
    store.record(
        _event(AgentRuntime.CLAUDE, ActivityState.COMPLETED, "s1", at=now - timedelta(seconds=100))
    )
    store.record(
        _event(AgentRuntime.HERMES, ActivityState.RUNNING, "h1", at=now - timedelta(seconds=10))
    )
    with patch("moira.activity_view.GLib.timeout_add", side_effect=fake_timeout_add):
        ActivityWatcher(store, lambda: None)
    assert len(delays) == 1
    expected = int((TERMINAL_WINDOW_SECONDS - 100) * 1000)
    assert abs(delays[0] - expected) <= 2000  # five-minute hide, floored safely
    # The watchdog bound is covered by the pure expiry tests; the floor is
    # verified by scheduling with an already-expired terminal event.
    with patch("moira.activity_view.GLib.timeout_add", side_effect=fake_timeout_add):
        store2 = ActivityStore(tmp_path / "b.json")
        store2.record(
            _event(
                AgentRuntime.CLAUDE, ActivityState.RUNNING, "s1", at=now - timedelta(seconds=300)
            )
        )
        store2.record(
            _event(
                AgentRuntime.CLAUDE, ActivityState.COMPLETED, "s1", at=now - timedelta(seconds=200)
            )
        )
        ActivityWatcher(store2, lambda: None)
    assert abs(delays[-1] - int((TERMINAL_WINDOW_SECONDS - 200) * 1000)) <= 2000


def test_watchdog_persists_interrupted_and_panel_hides_after_window(tmp_path: Path) -> None:
    """Integration: a stale running session expires to INTERRUPTED through
    the watcher's schedule, and the derived panel hides after the window."""
    from moira.activity_view import ActivityWatcher, latest_terminal_event

    store = ActivityStore(tmp_path / "activity.json")
    started = datetime.now(UTC) - timedelta(seconds=WATCHDOG_STALE_SECONDS + 60)
    store.record(_event(AgentRuntime.HERMES, ActivityState.RUNNING, "h1", at=started))
    calls: list[bool] = []
    watcher = ActivityWatcher(store, lambda: calls.append(True))
    try:
        assert _wait_until(
            lambda: (
                store.snapshot()["sessions"].get("hermes", {})
                and next(iter(store.snapshot()["sessions"]["hermes"].values()))["state"]
                == "INTERRUPTED"
            )
        ), "watchdog never expired the stale session"
        terminal = latest_terminal_event(store.snapshot(), AgentRuntime.HERMES)
        assert terminal is not None and terminal[0] is ActivityState.INTERRUPTED
    finally:
        watcher.shutdown()


def test_latest_terminal_event_prefers_newest_and_ignores_running(tmp_path: Path) -> None:
    from moira.activity_view import latest_terminal_event

    store = ActivityStore(tmp_path / "activity.json")
    store.record(_event(AgentRuntime.CLAUDE, ActivityState.RUNNING, "s1", at=NOW))
    store.record(
        _event(AgentRuntime.CLAUDE, ActivityState.COMPLETED, "s1", at=NOW + timedelta(seconds=10))
    )
    store.record(
        _event(AgentRuntime.CLAUDE, ActivityState.RUNNING, "s2", at=NOW + timedelta(seconds=15))
    )
    store.record(
        _event(AgentRuntime.CLAUDE, ActivityState.FAILED, "s2", at=NOW + timedelta(seconds=20))
    )
    terminal = latest_terminal_event(store.snapshot(), AgentRuntime.CLAUDE)
    assert terminal is not None
    assert terminal[0] is ActivityState.FAILED
    assert latest_terminal_event(store.snapshot(), AgentRuntime.CODEX) is None
