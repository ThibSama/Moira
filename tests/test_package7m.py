"""Package 7m — close connection coordinator race windows (ACCEPTANCE_CORRECTION).

RED tests on 2326407 for the three blocking findings:

1. ``request()`` reserves ``_inflight`` and calls the submitter OUTSIDE
   the lock; a request that parks while that first submit is in
   progress is ORPHANED when the submit is rejected — the catch clears
   only ``_inflight``, leaving ``_pending != None`` with
   ``_inflight == None`` (no worker, no dispatch, no rejection).
2. Shutdown can win after the under-lock check but before the external
   submit: ``request()``'s first submit and ``_dispatch_promotion()``
   both submit without a commit recheck, so work is submitted after
   close (the current close test even allows a promoted task after
   close).
3. ``_dispatch_promotion()`` retries recursively; repeated promotion
   rejections plus concurrent parking have no fixed stack bound.

The fix centralizes reservation, commit, failure recovery, promotion
and cancellation in one invariant-preserving state machine with an
ITERATIVE dispatcher: every external submit rechecks shutdown
immediately before committing (close wins → zero submit, zero Keyring
read, zero spawn), a rejected submit atomically clears the failed
generation and detaches the newest parked request, rejects the failed
request outside the lock, and iteratively attempts the detached request
— a request parking during the rejection stays reachable.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any
from unittest.mock import patch

import gi  # type: ignore[import-untyped]

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Secret", "1")
import pytest
from gi.repository import GLib, Secret  # type: ignore[import-untyped]  # noqa: E402

import moira.connection_test as ctest
from moira.integrations import ProviderKind, ProviderProfile
from moira.provider_editor import _ConnectionCoordinator


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, Any]]:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    fake: dict[str, Any] = {"items": {}, "unavailable": False, "lookups": 0}

    def lookup(_schema: Any, attributes: dict[str, str], _cancellable: Any) -> str | None:
        fake["lookups"] += 1
        if fake["unavailable"]:
            raise RuntimeError("secret vault locked")
        if attributes.get("account") == "ntfy-token":
            return fake["items"].get(("ntfy", ""))  # type: ignore[no-any-return]
        return fake["items"].get((attributes["slug"], attributes["purpose"]))  # type: ignore[no-any-return]

    def store(
        _schema: Any,
        attributes: dict[str, str],
        _collection: Any,
        _label: Any,
        value: str,
        _cancellable: Any,
    ) -> None:
        fake["items"][(attributes["slug"], attributes["purpose"])] = value

    monkeypatch.setattr(Secret, "password_lookup_sync", lookup)
    monkeypatch.setattr(Secret, "password_store_sync", store)
    return tmp_path, fake


@pytest.fixture
def english(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("LC_ALL", "")
    monkeypatch.setenv("LC_MESSAGES", "")


@pytest.fixture
def idle_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(GLib, "idle_add", lambda cb, *a: cb(*a))


def _profile(kind: ProviderKind = ProviderKind.LOCAL, **overrides: Any) -> ProviderProfile:
    base: dict[str, Any] = {
        "slug": "local-main",
        "label": "Local main",
        "kind": kind,
        "model": "deepseek-chat",
        "enabled": True,
    }
    base.update(overrides)
    return ProviderProfile(**base)


def _result(state: ctest.ConnectionState) -> ctest.ConnectionResult:
    return ctest.ConnectionResult(state, "local-main")


def _capture_submit() -> tuple[Any, list[tuple[Any, ...]]]:
    queued: list[tuple[Any, ...]] = []

    def submit(fn: Any, *args: Any) -> None:
        queued.append((fn, *args))

    return submit, queued


def _find_connection_run(queued: list[tuple[Any, ...]], coord: Any) -> tuple[Any, ...]:
    for entry in queued:
        fn = entry[0]
        if getattr(fn, "__self__", None) is coord and fn.__name__ == "_run":
            return entry
    raise AssertionError("connection test never dispatched")


def _assert_no_orphan_slot(coord: Any) -> None:
    """The core invariant: ``_pending`` is never set while ``_inflight``
    is clear — every accepted request is running, parked behind a
    running request, or already terminated."""
    with coord._lock:
        assert not (coord._pending is not None and coord._inflight is None)


def _assert_coordinator_clean(coord: Any) -> None:
    """No latched generation and no orphaned pending slot."""
    with coord._lock:
        assert coord._inflight is None
        assert coord._pending is None


# ── Finding 1: first-submit rejection must not orphan a parked request ──────


def test_first_rejection_does_not_orphan_concurrently_parked_request() -> None:
    """A request parking while the FIRST submit is in progress (the
    submit is called outside the lock) must not be orphaned when that
    submit is rejected: the failed generation is cleared and the parked
    request is detached and attempted."""
    queued: list[tuple[Any, ...]] = []
    state: dict[str, Any] = {"first": True, "coord": None}
    published: list[tuple[str, str]] = []

    def record(tag: str) -> Any:
        return lambda t, r: published.append((tag, r.state.value))

    def reentrant_submit(fn: Any, *args: Any) -> None:
        if state["first"]:
            state["first"] = False
            # B parks while A's submit is still in progress (the race
            # window: the submitter runs outside the coordinator lock).
            state["coord"].request(profile, "b", record("b"))
            raise RuntimeError("executor closed")
        queued.append((fn, *args))

    profile = _profile(ProviderKind.LOCAL, base_url="http://127.0.0.1:9")
    coord = _ConnectionCoordinator(reentrant_submit, threading.Event())
    state["coord"] = coord
    assert coord.request(profile, "a", record("a")) is False  # A rejected
    assert ("a", "unreachable") in published  # A completed deterministically
    assert len(queued) == 1  # B was dispatched — never orphaned
    fn, gen, p, token, cb = queued[0]
    assert token == "b"
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=_result(ctest.ConnectionState.CONNECTED),
    ):
        fn(gen, p, token, cb)
    assert ("b", "connected") in published
    _assert_no_orphan_slot(coord)
    _assert_coordinator_clean(coord)
    assert coord.request(profile, "c", record("c")) is True  # later requests usable


# ── Finding 2: close racing reservation/commit must submit nothing ──────────


class _ArmLock:
    """A lock-alike that sets the shutdown event on its FIRST release —
    a deterministic stand-in for close winning in the window between the
    under-lock reservation and the external submit."""

    def __init__(self, event: threading.Event) -> None:
        self._lock = threading.Lock()
        self._event = event
        self._fired = False

    def __enter__(self) -> _ArmLock:
        self._lock.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._lock.release()
        if not self._fired:
            self._fired = True
            self._event.set()


def test_close_between_reservation_and_first_submit_submits_nothing() -> None:
    """Finding 2 (first-submit side): if close wins AFTER the under-lock
    reservation but BEFORE the external submit, the request is NOT
    submitted — the commit protocol rechecks shutdown and releases the
    reservation (zero submit, nothing published)."""
    queued: list[tuple[Any, ...]] = []

    def submit(fn: Any, *args: Any) -> None:
        queued.append((fn, *args))

    event = threading.Event()
    coord = _ConnectionCoordinator(submit, event)
    coord._lock = _ArmLock(event)  # type: ignore[assignment]  # fires close on first release
    published: list[str] = []
    profile = _profile(ProviderKind.LOCAL, base_url="http://127.0.0.1:9")
    assert coord.request(profile, "t1", lambda t, r: published.append(r.state.value)) is False
    assert queued == []  # zero submit: close won before the commit
    assert published == []  # shutdown discards everything
    _assert_coordinator_clean(coord)


def test_close_during_publish_never_submits_promotion() -> None:
    """Finding 2 (promotion side): close winning while the in-flight
    completion publishes must cancel the pending promotion — the
    promotion commit rechecks shutdown and performs ZERO submits after
    close."""
    submit, queued = _capture_submit()
    event = threading.Event()
    coord = _ConnectionCoordinator(submit, event)
    profile = _profile(ProviderKind.LOCAL, base_url="http://127.0.0.1:9")
    published: list[tuple[str, str]] = []
    coord.request(profile, "t1", lambda t, r: published.append(("t1", r.state.value)))
    coord.request(profile, "t2", lambda t, r: published.append(("t2", r.state.value)))
    fn, gen, p, token, cb = queued[0]
    entered = threading.Event()
    release = threading.Event()

    def close_during_publish(_t: Any, r: ctest.ConnectionResult) -> None:
        entered.set()
        release.wait(5)
        published.append(("t1", r.state.value))

    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=_result(ctest.ConnectionState.CONNECTED),
    ):
        worker = threading.Thread(
            target=fn, args=(gen, p, token, close_during_publish), daemon=True
        )
        worker.start()
        assert entered.wait(5)  # t1's completion callback is blocked
        event.set()  # close wins while the callback is blocked
        coord.cancel()
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert published == [("t1", "connected")]  # only t1; t2 never publishes
    assert len(queued) == 1  # t2 was NEVER submitted after close
    _assert_coordinator_clean(coord)


def test_first_submit_blocked_close_release_committed_work_self_bounds() -> None:
    """Close during a blocked FIRST submit: the committed executor work
    self-bounds (zero Keyring reads, zero spawns, nothing publishes) and
    the parked request is never submitted after close."""
    entered = threading.Event()
    release = threading.Event()
    queued: list[tuple[Any, ...]] = []

    def blocking_submit(fn: Any, *args: Any) -> None:
        entered.set()
        release.wait(5)
        queued.append((fn, *args))

    event = threading.Event()
    coord = _ConnectionCoordinator(blocking_submit, event)
    profile = _profile(ProviderKind.LOCAL, base_url="http://127.0.0.1:9")
    published: list[tuple[str, str]] = []
    outcome: dict[str, Any] = {}

    def do_a() -> None:
        outcome["ok"] = coord.request(
            profile, "a", lambda t, r: published.append(("a", r.state.value))
        )

    thread = threading.Thread(target=do_a, daemon=True)
    thread.start()
    assert entered.wait(5)
    assert (
        coord.request(profile, "b", lambda t, r: published.append(("b", r.state.value))) is True
    )  # parks while A's submit is blocked
    event.set()  # close wins while the first submit is still in progress
    coord.cancel()
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert outcome["ok"] is True  # A was committed before close
    assert len(queued) == 1  # only A; B never submitted after close
    runs = {"n": 0}
    with patch(
        "moira.provider_editor.run_connection_test",
        side_effect=lambda *a, **k: runs.__setitem__("n", runs["n"] + 1),
    ):
        fn, gen, p, token, cb = queued[0]
        fn(gen, p, token, cb)
    assert runs["n"] == 0  # committed work self-bounds: zero Keyring reads, zero spawns
    assert published == []  # nothing publishes after close
    _assert_coordinator_clean(coord)


def test_first_submit_blocked_close_release_to_rejection_publishes_nothing() -> None:
    """Close winning during a blocked FIRST submit whose release
    REJECTS: the reservation is abandoned — zero submits, and because
    shutdown discards everything, not even the rejection completion
    publishes."""
    entered = threading.Event()
    release = threading.Event()

    def blocking_rejecting_submit(fn: Any, *args: Any) -> None:
        entered.set()
        release.wait(5)
        raise RuntimeError("executor closed")

    event = threading.Event()
    coord = _ConnectionCoordinator(blocking_rejecting_submit, event)
    profile = _profile(ProviderKind.LOCAL, base_url="http://127.0.0.1:9")
    published: list[str] = []
    outcome: dict[str, Any] = {}

    def do_a() -> None:
        outcome["ok"] = coord.request(profile, "a", lambda t, r: published.append(r.state.value))

    thread = threading.Thread(target=do_a, daemon=True)
    thread.start()
    assert entered.wait(5)
    assert (
        coord.request(profile, "b", lambda t, r: published.append(r.state.value)) is True
    )  # parks while A's submit is blocked
    event.set()
    coord.cancel()
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert outcome["ok"] is False
    assert published == []  # shutdown discards everything: no rejection publish
    _assert_coordinator_clean(coord)


# ── Finding 3: promotion retry is iterative, never recursive ────────────────


def test_promotion_retry_is_iterative_not_recursive() -> None:
    """Repeated promotion rejections with a new request parking on every
    rejection must drain ITERATIVELY — the recursive retry has no fixed
    stack bound and overflows a small-stack thread."""
    queued: list[tuple[Any, ...]] = []
    calls = {"n": 0}

    def flaky_submit(fn: Any, *args: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 1:  # only the first (t0) submit is accepted
            queued.append((fn, *args))
            return
        raise RuntimeError("executor closed")

    profile = _profile(ProviderKind.LOCAL, base_url="http://127.0.0.1:9")
    coord = _ConnectionCoordinator(flaky_submit, threading.Event())
    published: list[tuple[str, str]] = []
    pending_tags: list[str] = [f"t{i}" for i in range(2, 302)]
    state: dict[str, Any] = {"coord": None}

    def record(tag: str) -> Any:
        def cb(_t: Any, r: ctest.ConnectionResult) -> None:
            published.append((tag, r.state.value))
            # every completion/rejection parks the next request
            if pending_tags:
                nxt = pending_tags.pop(0)
                state["coord"].request(profile, nxt, record(nxt))

        return cb

    coord.request(profile, "t0", record("t0"))
    coord.request(profile, "t1", record("t1"))  # parked
    state["coord"] = coord
    errors: list[BaseException] = []

    def scenario() -> None:
        import sys

        try:
            old_limit = sys.getrecursionlimit()
            sys.setrecursionlimit(100)  # deterministic RED: recursion overflows, iteration does not
            try:
                fn0, gen0, p0, token0, cb0 = queued[0]
                with patch(
                    "moira.provider_editor.run_connection_test",
                    return_value=_result(ctest.ConnectionState.CONNECTED),
                ):
                    fn0(gen0, p0, token0, cb0)
            finally:
                sys.setrecursionlimit(old_limit)
        except BaseException as exc:  # pragma: no cover - the recursive RED path
            errors.append(exc)

    old_stack = threading.stack_size()
    threading.stack_size(53 * 1024)  # small stack: recursion overflows, iteration does not
    try:
        thread = threading.Thread(target=scenario, daemon=True)
        thread.start()
        thread.join(60)
    finally:
        threading.stack_size(old_stack)
    assert not thread.is_alive()
    assert errors == []  # RED: RecursionError on the recursive retry
    assert published[0] == ("t0", "connected")
    rejected = {tag for tag, _state in published[1:]}
    assert rejected == {f"t{i}" for i in range(1, 302)}  # every request rejected once
    assert len(published) == len({tag for tag, _state in published})  # no duplicate callback
    _assert_no_orphan_slot(coord)
    _assert_coordinator_clean(coord)
    # later requests remain usable (deterministically rejected, never wedged)
    assert coord.request(profile, "zz", record("zz")) is False
    assert ("zz", "unreachable") in published
    _assert_no_orphan_slot(coord)
    _assert_coordinator_clean(coord)


# ── Barrier: newest-wins under promotion failure, park during rejection ─────


def test_newest_wins_under_promotion_failure_parked_during_rejection_dispatched() -> None:
    """A newer parked request replaces the parked one; when the promoted
    submit fails, the request parking DURING the rejection is detached
    and attempted — never orphaned (rapid retest / newest-wins)."""
    queued: list[tuple[Any, ...]] = []
    fail_promotion = {"n": 0}

    def flaky_submit(fn: Any, *args: Any) -> None:
        if fail_promotion["n"] > 0:
            fail_promotion["n"] = 0
            raise RuntimeError("executor closed")
        queued.append((fn, *args))

    coord = _ConnectionCoordinator(flaky_submit, threading.Event())
    profile = _profile(ProviderKind.LOCAL, base_url="http://127.0.0.1:9")
    published: list[tuple[str, str]] = []

    def record(tag: str) -> Any:
        def cb(_t: Any, r: ctest.ConnectionResult) -> None:
            published.append((tag, r.state.value))
            if tag == "c":
                coord.request(profile, "d", record("d"))  # parks during c's rejection

        return cb

    coord.request(profile, "a", record("a"))
    coord.request(profile, "b", record("b"))
    coord.request(profile, "c", record("c"))  # replaces b (newest wins)
    fn, gen, p, token, cb = queued[0]
    fail_promotion["n"] = 1  # c's promotion submit fails
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=_result(ctest.ConnectionState.CONNECTED),
    ):
        fn(gen, p, token, cb)
    assert published[0] == ("a", "connected")
    assert ("c", "unreachable") in published  # c rejected deterministically
    assert ("b", "unreachable") not in published  # b was replaced, never fires
    assert len(queued) == 2  # a + d; b replaced, c rejected
    fn2, gen2, p2, token2, cb2 = queued[1]
    assert token2 == "d"  # parked-during-rejection is dispatched
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=_result(ctest.ConnectionState.CONNECTED),
    ):
        fn2(gen2, p2, token2, cb2)
    assert ("d", "connected") in published
    _assert_no_orphan_slot(coord)
    _assert_coordinator_clean(coord)
    assert coord.request(profile, "e", record("e")) is True  # later requests usable


# ── Barrier: runner and callback exceptions ─────────────────────────────────


def test_runner_and_callback_exceptions_leave_state_clean() -> None:
    """A raising runner completes deterministically (sanitized
    UNREACHABLE) through the same publish/promote path, and a raising
    publish callback must not wedge the coordinator."""
    submit, queued = _capture_submit()
    coord = _ConnectionCoordinator(submit, threading.Event())
    profile = _profile(ProviderKind.LOCAL, base_url="http://127.0.0.1:9")
    published: list[tuple[str, str]] = []
    coord.request(profile, "t1", lambda t, r: published.append(("t1", r.state.value)))
    coord.request(profile, "t2", lambda t, r: published.append(("t2", r.state.value)))
    fn, gen, p, token, cb = queued[0]
    with patch(
        "moira.provider_editor.run_connection_test",
        side_effect=RuntimeError("runner boom"),
    ):
        fn(gen, p, token, cb)  # must not raise
    assert published == [("t1", "unreachable")]  # sanitized completion
    assert len(queued) == 2  # t2 is still promoted
    fn2, gen2, p2, token2, cb2 = queued[1]

    def exploding(_t: Any, _r: Any) -> None:
        raise RuntimeError("publisher exploded")

    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=_result(ctest.ConnectionState.CONNECTED),
    ):
        fn2(gen2, p2, token2, exploding)  # the failing publisher must not wedge
    _assert_no_orphan_slot(coord)
    _assert_coordinator_clean(coord)
    assert (
        coord.request(profile, "t3", lambda t, r: published.append(("t3", r.state.value))) is True
    )


# ── Barrier: rejections reset the exact editor rows from "Testing…" ─────────


def test_first_rejection_with_parked_request_resets_exact_rows(
    env: tuple[Path, dict[str, Any]], idle_inline: None, english: None
) -> None:
    from moira.provider_editor import ProviderEditor

    queued: list[tuple[Any, ...]] = []
    calls = {"n": 0}
    state: dict[str, Any] = {"ed": None}

    def reentrant_submit(fn: Any, *args: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 2:  # reload=1, first test submit=2 → park B + reject
            ed = state["ed"]
            ed._row_widgets["local-second"]["test"].emit("clicked")
            raise RuntimeError("executor closed")
        queued.append((fn, *args))

    ed = ProviderEditor(submit=reentrant_submit)
    state["ed"] = ed
    ed._profiles = (
        _profile(ProviderKind.LOCAL, slug="local-main", base_url="http://127.0.0.1:9"),
        _profile(ProviderKind.LOCAL, slug="local-second", base_url="http://127.0.0.1:9"),
    )
    ed._show_list()
    widgets_a = ed._row_widgets["local-main"]
    widgets_b = ed._row_widgets["local-second"]
    widgets_a["test"].emit("clicked")
    assert widgets_a["test_status"].get_text() == "Unreachable"  # A's row reset from "Testing…"
    assert widgets_b["test_status"].get_text() == "Testing…"  # B parked on its own row
    fn, gen, p, token, cb = _find_connection_run(queued, ed._connection_coordinator)
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=ctest.ConnectionResult(ctest.ConnectionState.CONNECTED, "local-second"),
    ):
        fn(gen, p, token, cb)
    assert widgets_b["test_status"].get_text() == "Connected"  # B dispatched, never orphaned
    ed.shutdown()
    _assert_coordinator_clean(ed._connection_coordinator)
