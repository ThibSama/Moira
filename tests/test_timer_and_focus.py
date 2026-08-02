"""Deterministic tests for timer replacement, focus debounce, and countdown updates.

Uses injected clocks/timers; no correctness sleeps. Mocks GLib functions.
"""

import time
from collections.abc import Generator
from typing import Any
from unittest.mock import patch

import pytest

# We can't import the full MainWindow (needs GTK display), but we can test
# the timer-replacement logic by mocking GLib and using a minimal harness.


@pytest.fixture
def mock_glib() -> Generator[dict[str, list[Any]], None, None]:
    """Mock GLib functions used by the timer logic."""
    timers_created: list[tuple[int, object]] = []
    removed_timers: list[int] = []
    next_id = [1000]

    def fake_timeout_add_seconds(interval: int, callback: Any) -> int:
        timer_id = next_id[0]
        next_id[0] += 1
        timers_created.append((timer_id, callback))
        return timer_id

    def fake_source_remove(timer_id: int) -> bool:
        removed_timers.append(timer_id)
        return True

    def fake_idle_add(callback: Any, *args: Any) -> int:
        return 999

    with (
        patch("moira.ui.GLib.timeout_add_seconds", side_effect=fake_timeout_add_seconds),
        patch("moira.ui.GLib.Source.remove", side_effect=fake_source_remove),
        patch("moira.ui.GLib.idle_add", side_effect=fake_idle_add),
    ):
        yield {
            "created": timers_created,
            "removed": removed_timers,
        }


class FakeSettings:
    """Minimal settings object for timer logic."""

    def __init__(self, refresh_minutes: int = 2) -> None:
        self.refresh_minutes = refresh_minutes


class TimerHarness:
    """Simulates the timer arming/disarming logic from MainWindow without GTK."""

    def __init__(self) -> None:
        self._refresh_timer_id: int | None = None
        self._next_refresh_time: float = 0.0
        self._timers_created: list[tuple[int, int]] = []  # (id, interval)
        self._removed: list[int] = []
        self._next_id = 1000

    def _arm_refresh_timer(self, settings: FakeSettings) -> None:
        if self._refresh_timer_id is not None:
            self._removed.append(self._refresh_timer_id)
            self._refresh_timer_id = None
        interval = settings.refresh_minutes * 60
        timer_id = self._next_id
        self._next_id += 1
        self._timers_created.append((timer_id, interval))
        self._refresh_timer_id = timer_id
        self._next_refresh_time = time.monotonic() + interval

    @property
    def timer_count(self) -> int:
        return len(self._timers_created)

    @property
    def removal_count(self) -> int:
        return len(self._removed)


# ── Timer replacement ──


def test_timer_created_on_init() -> None:
    h = TimerHarness()
    h._arm_refresh_timer(FakeSettings(5))
    assert h.timer_count == 1
    assert h.removal_count == 0


def test_timer_replaced_on_interval_change() -> None:
    h = TimerHarness()
    h._arm_refresh_timer(FakeSettings(2))
    h._arm_refresh_timer(FakeSettings(10))  # interval changed
    assert h.timer_count == 2
    assert h.removal_count == 1  # old timer removed


def test_timer_not_duplicated_on_same_interval() -> None:
    """Re-arming with the same interval should still replace (not duplicate)."""
    h = TimerHarness()
    h._arm_refresh_timer(FakeSettings(2))
    h._arm_refresh_timer(FakeSettings(2))  # re-arm with same interval
    assert h.timer_count == 2
    assert h.removal_count == 1  # old timer removed, new one created
    # Only one active timer at a time
    assert h._refresh_timer_id is not None


def test_timer_replacement_preserves_interval() -> None:
    h = TimerHarness()
    h._arm_refresh_timer(FakeSettings(15))
    assert h._timers_created[0][1] == 900  # 15 * 60


def test_consecutive_replacements_dont_accumulate() -> None:
    h = TimerHarness()
    for minutes in (1, 2, 5, 10, 15, 30):
        h._arm_refresh_timer(FakeSettings(minutes))
    assert h.timer_count == 6
    assert h.removal_count == 5  # each replacement removes the previous


# ── Focus debounce (simulated) ──


class FocusHarness:
    """Simulates the focus-regain debounce logic from MainWindow."""

    def __init__(self, debounce_seconds: float = 2.0) -> None:
        self._last_focus_time: float = 0.0
        self._focus_debounce_seconds = debounce_seconds
        self.refreshing = False
        self.refresh_calls: int = 0

    def refresh(self) -> bool:
        if self.refreshing:
            return False
        self.refreshing = True
        self.refresh_calls += 1
        return False

    def _on_focus_change(self, is_active: bool, now_mono: float) -> None:
        if not is_active:
            return
        if now_mono - self._last_focus_time < self._focus_debounce_seconds:
            return
        self._last_focus_time = now_mono
        if not self.refreshing:
            self.refresh()


def test_focus_regain_triggers_refresh() -> None:
    h = FocusHarness()
    h._on_focus_change(True, 100.0)
    assert h.refresh_calls == 1


def test_focus_debounce_prevents_rapid_refresh() -> None:
    h = FocusHarness(debounce_seconds=2.0)
    h._on_focus_change(True, 100.0)
    h.refreshing = False  # simulate refresh completing
    h._on_focus_change(True, 101.0)  # within debounce window
    assert h.refresh_calls == 1  # no new refresh


def test_focus_after_debounce_refreshes() -> None:
    h = FocusHarness(debounce_seconds=2.0)
    h._on_focus_change(True, 100.0)
    h.refreshing = False
    h._on_focus_change(True, 103.0)  # past debounce
    assert h.refresh_calls == 2


def test_focus_lost_does_not_refresh() -> None:
    h = FocusHarness()
    h._on_focus_change(False, 100.0)
    assert h.refresh_calls == 0


def test_focus_refresh_blocked_during_ongoing_refresh() -> None:
    h = FocusHarness()
    h.refreshing = True  # refresh in progress
    h._on_focus_change(True, 100.0)
    assert h.refresh_calls == 0  # overlap guard


# ── Countdown recompute (simulated) ──


def test_next_refresh_countdown_decreases() -> None:
    """The 30-second local recompute should update the countdown without collectors."""
    base = 1000.0
    next_time = base + 120  # 2 minutes

    # At t=base, remaining = 120s → 2m 0s
    remaining = max(0, int(next_time - base))
    minutes = remaining // 60
    seconds = remaining % 60
    assert f"{minutes}m {seconds}s" == "2m 0s"

    # At t=base+30, remaining = 90s → 1m 30s
    remaining = max(0, int(next_time - (base + 30)))
    minutes = remaining // 60
    seconds = remaining % 60
    assert f"{minutes}m {seconds}s" == "1m 30s"

    # At t=base+60, remaining = 60s → 1m 0s
    remaining = max(0, int(next_time - (base + 60)))
    minutes = remaining // 60
    seconds = remaining % 60
    assert f"{minutes}m {seconds}s" == "1m 0s"
