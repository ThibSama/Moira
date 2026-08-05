"""Package 7o — make connection disposition linearizable (ACCEPTANCE_CORRECTION).

RED tests on 6d8f030 for the two blocking findings:

1. ``request()`` decides whether its generation is "first" or "parked"
   AFTER releasing ``_lock`` by re-reading the mutable ``_inflight``. A
   worker may complete the in-flight run in that window, promote the
   parked request (``_inflight = genB``) and dispatch it — then the
   parked request's own ``request()`` call ALSO sees ``_inflight ==
   genB`` and dispatches it a SECOND time: two submits, two
   Keyring/network operations and two publishes with the same token.

2. ``_draining`` is checked and changed OUTSIDE ``_lock``. A drainer may
   observe ``_cancelled`` empty and begin returning while another thread
   appends a superseded request; that thread sees ``_draining`` True and
   declines to drain; the first thread then clears ``_draining``,
   leaving the CANCELLED completion stranded forever.

The fix makes the reservation an explicit immutable decision computed
UNDER the lock (DISPATCH / PARKED / REPLACED / CLOSED) — ownership is
never inferred later from ``_inflight`` — and replaces the boolean drain
protocol with a lock-protected ownership/handoff whose empty-exit is
atomic, so appending while a drainer exits is either consumed by it or
atomically claims a successor (no lost wakeups).

Every scenario asserts: exactly one submit, one Keyring lookup/spawn and
one terminal callback per non-replaced accepted request; exactly one
CANCELLED and zero submit/lookup/spawn per replaced request; no orphan
``_pending``/``_cancelled``; no latched generation or drain owner; zero
post-close Keyring/spawn; later usability when open.
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


class _PauseLock:
    """A lock-alike whose ``release()`` can pause the releasing thread
    AFTER the underlying release — a deterministic barrier placed exactly
    on an unlock→next-statement window. A sleep can never park a thread
    on a precise instruction; an injected release hook can (finding 1:
    the window between the reservation unlock and the ownership check;
    finding 2: the window between the drainer's empty observation and
    its ``_draining`` clear). The pause is armed once and consumed by
    the next release."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._armed = threading.Event()
        self._resume = threading.Event()
        self._resume.set()
        self.paused = threading.Event()

    def __enter__(self) -> _PauseLock:
        self._lock.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._lock.release()
        if self._armed.is_set():
            self._armed.clear()
            self.paused.set()
            self._resume.clear()
            self._resume.wait(10)

    def arm(self) -> None:
        self._armed.set()
        self.paused.clear()
        self._resume.set()

    def unblock(self) -> None:
        self._resume.set()


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


def _connection_runs(queued: list[tuple[Any, ...]], coord: Any) -> list[tuple[Any, ...]]:
    return [
        entry
        for entry in queued
        if getattr(entry[0], "__self__", None) is coord and entry[0].__name__ == "_run"
    ]


def _assert_no_orphan_slot(coord: Any) -> None:
    """The core invariant: ``_pending`` is never set while ``_inflight``
    is clear — every accepted request is running, parked behind a
    running request, or already terminated."""
    with coord._lock:
        assert not (coord._pending is not None and coord._inflight is None)


def _assert_coordinator_clean(coord: Any) -> None:
    """No latched generation, no orphaned pending slot, no pending
    replacement cancellations and no latched drain owner."""
    with coord._lock:
        assert coord._inflight is None
        assert coord._pending is None
        assert not getattr(coord, "_cancelled", [])
        assert coord._draining is False


# ── Finding 1: a parked request must never be dispatched twice ──────────────


def test_promotion_between_unlock_and_ownership_check_dispatches_once() -> None:
    """A completes and promotes B while B's own ``request()`` call is
    between its reservation unlock and its (old) ``_inflight ==
    generation`` ownership check: B must be dispatched EXACTLY ONCE — by
    the promotion — and its ``request()`` call must never submit it
    (RED on 6d8f030: B is submitted a second time by its own call)."""
    submit, queued = _capture_submit()
    coord = _ConnectionCoordinator(submit, threading.Event())
    gate = _PauseLock()
    coord._lock = gate  # type: ignore[assignment]
    profile = _profile(ProviderKind.LOCAL, base_url="http://127.0.0.1:9")
    published: list[tuple[str, str]] = []
    coord.request(profile, "a", lambda t, r: published.append(("a", r.state.value)))
    assert len(queued) == 1  # A dispatched
    gate.arm()
    outcome: dict[str, Any] = {}

    def request_b() -> None:
        outcome["ok"] = coord.request(
            profile, "b", lambda t, r: published.append(("b", r.state.value))
        )

    thread = threading.Thread(target=request_b, daemon=True)
    thread.start()
    assert gate.paused.wait(5)  # B reserved (parked) and paused before its ownership check
    fn_a, gen_a, p_a, tok_a, cb_a = queued[0]
    assert tok_a == "a"
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=_result(ctest.ConnectionState.CONNECTED),
    ):
        fn_a(gen_a, p_a, tok_a, cb_a)  # A completes and promotes B in B's window
    gate.unblock()
    thread.join(5)
    assert not thread.is_alive()
    assert outcome["ok"] is True  # B was accepted (parked → promoted)
    assert len(queued) == 2  # RED: today the promotion AND B's own call both submit B → 3
    tokens = [entry[3] for entry in queued]
    assert tokens == ["a", "b"]  # B dispatched exactly once, by the promotion
    fn_b, gen_b, p_b, tok_b, cb_b = queued[1]
    assert tok_b == "b"
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=_result(ctest.ConnectionState.AUTH_FAILED),
    ):
        fn_b(gen_b, p_b, tok_b, cb_b)
    assert published == [("a", "connected"), ("b", "auth_failed")]
    assert len(published) == len({tag for tag, _state in published})  # no duplicate callback
    _assert_no_orphan_slot(coord)
    _assert_coordinator_clean(coord)
    assert coord.request(profile, "d", lambda t, r: None) is True  # later usability


# ── Finding 2: a drainer empty-exit must never strand a replacement ─────────


def test_replacement_during_drainer_empty_exit_is_not_stranded() -> None:
    """B→C replacement lands while the drainer is between its empty
    observation and its ``_draining`` clear: the appending thread sees
    ``_draining`` True and declines, so the drainer's atomic exit must
    have already released ownership — C is consumed by the appender's
    own claim (RED on 6d8f030: the empty-exit and the clear are separate
    critical sections and C's CANCELLED is stranded forever)."""
    submit, queued = _capture_submit()
    coord = _ConnectionCoordinator(submit, threading.Event())
    gate = _PauseLock()
    coord._lock = gate  # type: ignore[assignment]
    profile = _profile(ProviderKind.LOCAL, base_url="http://127.0.0.1:9")
    published: list[tuple[str, str]] = []
    coord.request(profile, "a", lambda t, r: published.append(("a", r.state.value)))

    def cb_b(_t: Any, r: ctest.ConnectionResult) -> None:
        published.append(("b", r.state.value))
        gate.arm()  # the next release is the drainer's empty-exit release

    coord.request(profile, "b", cb_b)
    errors: list[BaseException] = []
    done = threading.Event()

    def other_thread() -> None:
        try:
            assert gate.paused.wait(5)  # the drainer's empty-exit release is paused
            coord.request(profile, "d", lambda t, r: published.append(("d", r.state.value)))
            gate.unblock()
            done.set()
        except BaseException as exc:  # pragma: no cover - failure diagnostics
            errors.append(exc)
            gate.unblock()

    thread = threading.Thread(target=other_thread, daemon=True)
    thread.start()

    def cb_c(_t: Any, r: ctest.ConnectionResult) -> None:
        published.append(("c", r.state.value))

    # C replaces B: the drain pops B (its callback arms the gate) and is
    # then PAUSED between its empty observation and its ownership clear.
    coord.request(profile, "c", cb_c)
    thread.join(5)
    assert errors == []
    assert done.wait(5)
    assert not thread.is_alive()
    # RED on 6d8f030: C's CANCELLED is stranded — the drainer exited and
    # the appending thread declined to drain.
    assert ("c", "cancelled") in published
    assert published.count(("c", "cancelled")) == 1
    fn_a, gen_a, p_a, tok_a, cb_a = queued[0]
    assert tok_a == "a"
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=_result(ctest.ConnectionState.CONNECTED),
    ):
        fn_a(gen_a, p_a, tok_a, cb_a)  # A completes and promotes the newest parked (D)
    assert published == [
        ("b", "cancelled"),
        ("c", "cancelled"),
        ("a", "connected"),
    ]
    assert len(queued) == 2  # A + the single D dispatch
    fn_d, gen_d, p_d, tok_d, cb_d = queued[1]
    assert tok_d == "d"
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=_result(ctest.ConnectionState.AUTH_FAILED),
    ):
        fn_d(gen_d, p_d, tok_d, cb_d)
    assert published == [
        ("b", "cancelled"),
        ("c", "cancelled"),
        ("a", "connected"),
        ("d", "auth_failed"),
    ]
    assert len(published) == len({tag for tag, _state in published})
    _assert_no_orphan_slot(coord)
    _assert_coordinator_clean(coord)
    assert coord.request(profile, "e", lambda t, r: None) is True  # later usability


# ── Criterion 4: submit/lookup/spawn/callback cardinalities ─────────────────


def test_one_submit_one_run_one_callback_per_accepted_request() -> None:
    """Cardinalities: every non-replaced accepted request gets exactly
    one submit, one Keyring lookup + spawn (``run_connection_test``) and
    one terminal callback; a replaced request gets exactly one CANCELLED
    and zero submit/lookup/spawn."""
    submit, queued = _capture_submit()
    coord = _ConnectionCoordinator(submit, threading.Event())
    profile = _profile(ProviderKind.LOCAL, base_url="http://127.0.0.1:9")
    published: list[tuple[str, str]] = []
    runs: list[str] = []

    def counted_run(prof: ProviderProfile, **_: Any) -> ctest.ConnectionResult:
        runs.append(prof.slug)
        return _result(ctest.ConnectionState.CONNECTED)

    with patch("moira.provider_editor.run_connection_test", side_effect=counted_run):
        coord.request(profile, "a", lambda t, r: published.append(("a", r.state.value)))
        coord.request(profile, "b", lambda t, r: published.append(("b", r.state.value)))
        coord.request(profile, "c", lambda t, r: published.append(("c", r.state.value)))
        assert published == [("b", "cancelled")]  # B: exactly one CANCELLED
        assert runs == []  # B: zero Keyring lookup, zero spawn
        assert len(queued) == 1  # B: zero submit
        fn_a, gen_a, p_a, tok_a, cb_a = queued[0]
        assert tok_a == "a"
        fn_a(gen_a, p_a, tok_a, cb_a)  # A: one submit → one run
        assert runs == ["local-main"]
        assert len(queued) == 2  # the newest parked (C) is promoted: one submit
        fn_c, gen_c, p_c, tok_c, cb_c = queued[1]
        assert tok_c == "c"
        fn_c(gen_c, p_c, tok_c, cb_c)  # C: one run
        assert runs == ["local-main", "local-main"]
    assert published == [("b", "cancelled"), ("a", "connected"), ("c", "connected")]
    tokens = [entry[3] for entry in queued]
    assert tokens.count("a") == 1 and tokens.count("c") == 1 and "b" not in tokens
    assert len(published) == 3 and len({tag for tag, _state in published}) == 3
    _assert_no_orphan_slot(coord)
    _assert_coordinator_clean(coord)
    assert coord.request(profile, "d", lambda t, r: None) is True  # later usability


# ── Criterion 8: replacement callback re-entry from another thread ──────────


def test_reentry_and_cross_thread_replacement_drain_exactly_once() -> None:
    """B's CANCELLED callback (on the draining thread) re-enters
    ``request()`` while ANOTHER thread replaces the parked request:
    every superseded request terminates CANCELLED exactly once, the
    drain is ITERATIVE (re-entrant appends are popped by the outermost
    drain), and no completion is duplicated or stranded."""
    submit, queued = _capture_submit()
    coord = _ConnectionCoordinator(submit, threading.Event())
    profile = _profile(ProviderKind.LOCAL, base_url="http://127.0.0.1:9")
    published: list[tuple[str, str]] = []
    coord.request(profile, "a", lambda t, r: published.append(("a", r.state.value)))
    in_cb = threading.Event()
    release_cb = threading.Event()
    errors: list[BaseException] = []
    other_done = threading.Event()

    def cb_b(_t: Any, r: ctest.ConnectionResult) -> None:
        published.append(("b", r.state.value))
        in_cb.set()
        release_cb.wait(5)
        # Re-enter from the DRAINING thread while another thread's
        # replacement is pending: E parks (replacing D → D appended);
        # the outermost drain pops it after this callback.
        coord.request(profile, "e", lambda t, r: published.append(("e", r.state.value)))

    coord.request(profile, "b", cb_b)

    def other_thread() -> None:
        try:
            assert in_cb.wait(5)
            # ANOTHER thread replaces the parked request while the
            # drainer is inside B's callback.
            coord.request(profile, "c", lambda t, r: published.append(("c", r.state.value)))
            coord.request(profile, "d", lambda t, r: published.append(("d", r.state.value)))
            other_done.set()
        except BaseException as exc:  # pragma: no cover - failure diagnostics
            errors.append(exc)
            release_cb.set()

    thread = threading.Thread(target=other_thread, daemon=True)
    thread.start()
    coord.request(profile, "c0", lambda t, r: published.append(("c0", r.state.value)))
    # c0 replaces B → the drainer (this thread) pops B and blocks in cb_b.
    assert in_cb.wait(5)
    assert other_done.wait(5)
    release_cb.set()
    thread.join(5)
    assert errors == []
    assert not thread.is_alive()
    assert published == [
        ("b", "cancelled"),
        ("c0", "cancelled"),
        ("c", "cancelled"),
        ("d", "cancelled"),
    ]
    assert len(published) == len({tag for tag, _state in published})  # no duplicate callback
    # The re-entrant request E is the NEWEST parked request (it replaced
    # D, which was drained): it must still terminate — A completes and
    # promotes E, which is dispatched exactly once and runs once.
    fn_a, gen_a, p_a, tok_a, cb_a = queued[0]
    assert tok_a == "a"
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=_result(ctest.ConnectionState.CONNECTED),
    ):
        fn_a(gen_a, p_a, tok_a, cb_a)
    assert published == [
        ("b", "cancelled"),
        ("c0", "cancelled"),
        ("c", "cancelled"),
        ("d", "cancelled"),
        ("a", "connected"),
    ]
    assert len(queued) == 2  # A + the single E dispatch
    fn_e, gen_e, p_e, tok_e, cb_e = queued[1]
    assert tok_e == "e"
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=_result(ctest.ConnectionState.AUTH_FAILED),
    ):
        fn_e(gen_e, p_e, tok_e, cb_e)
    assert published == [
        ("b", "cancelled"),
        ("c0", "cancelled"),
        ("c", "cancelled"),
        ("d", "cancelled"),
        ("a", "connected"),
        ("e", "auth_failed"),
    ]
    _assert_no_orphan_slot(coord)
    _assert_coordinator_clean(coord)
    assert coord.request(profile, "z", lambda t, r: None) is True  # later usability


# ── Criterion 8: close during dispatch and drain handoffs ───────────────────


def test_close_during_dispatch_handoff_submits_nothing() -> None:
    """Close wins between the reservation and the dispatch commit: the
    DISPATCH reservation performs ZERO submits, nothing publishes and
    the reservation is released (CLOSED → False)."""
    submit, queued = _capture_submit()
    event = threading.Event()
    coord = _ConnectionCoordinator(submit, event)
    gate = _PauseLock()
    coord._lock = gate  # type: ignore[assignment]
    profile = _profile(ProviderKind.LOCAL, base_url="http://127.0.0.1:9")
    published: list[tuple[str, str]] = []
    gate.arm()
    outcome: dict[str, Any] = {}

    def request_a() -> None:
        outcome["ok"] = coord.request(
            profile, "a", lambda t, r: published.append(("a", r.state.value))
        )

    thread = threading.Thread(target=request_a, daemon=True)
    thread.start()
    assert gate.paused.wait(5)  # A reserved (DISPATCH) and paused before its commit
    event.set()  # close wins before the commit
    coord.cancel()
    gate.unblock()
    thread.join(5)
    assert not thread.is_alive()
    assert outcome["ok"] is False  # CLOSED: the reservation was released
    assert queued == []  # zero submit after close
    assert published == []  # nothing publishes after close
    _assert_no_orphan_slot(coord)
    _assert_coordinator_clean(coord)


def test_close_during_drain_handoff_never_strands() -> None:
    """Close wins while the drainer is inside a CANCELLED callback: the
    popped completion lands exactly once, the remaining queue is
    discarded atomically by ``cancel()``, queued work self-bounds with
    ZERO Keyring/spawn, and later requests are refused."""
    submit, queued = _capture_submit()
    event = threading.Event()
    coord = _ConnectionCoordinator(submit, event)
    profile = _profile(ProviderKind.LOCAL, base_url="http://127.0.0.1:9")
    published: list[tuple[str, str]] = []
    coord.request(profile, "a", lambda t, r: published.append(("a", r.state.value)))
    in_cb = threading.Event()
    release_cb = threading.Event()

    def cb_b(_t: Any, r: ctest.ConnectionResult) -> None:
        published.append(("b", r.state.value))
        in_cb.set()
        release_cb.wait(5)

    coord.request(profile, "b", cb_b)

    thread = threading.Thread(
        target=lambda: coord.request(
            profile, "c", lambda t, r: published.append(("c", r.state.value))
        ),
        daemon=True,
    )
    thread.start()
    assert in_cb.wait(5)  # C replaced B; the drainer (worker) is inside B's callback
    event.set()  # close wins during the drain handoff
    coord.cancel()  # discards the parked C and any remaining drain queue
    assert coord.request(profile, "e", lambda t, r: None) is False  # refused after close
    release_cb.set()
    thread.join(5)
    assert not thread.is_alive()
    assert published == [("b", "cancelled")]  # C was discarded by close, never published
    runs = {"n": 0}

    def counted_run(*_a: Any, **_k: Any) -> ctest.ConnectionResult:
        runs["n"] += 1
        return _result(ctest.ConnectionState.CONNECTED)

    with patch("moira.provider_editor.run_connection_test", side_effect=counted_run):
        fn_a, gen_a, p_a, tok_a, cb_a = queued[0]
        fn_a(gen_a, p_a, tok_a, cb_a)  # committed work self-bounds
    assert runs["n"] == 0  # zero Keyring lookup, zero spawn after close
    assert published == [("b", "cancelled")]  # nothing publishes after close
    assert len(queued) == 1  # C never submitted
    _assert_no_orphan_slot(coord)
    _assert_coordinator_clean(coord)


# ── Criterion 8: submit rejection after a promotion race ────────────────────


def test_submit_rejection_after_promotion_race() -> None:
    """B is promoted by A's completion while B's own ``request()`` call
    is still between its reservation and its return. On 6d8f030 B's call
    then re-dispatches B and that second submit is REJECTED — B is
    submitted twice. The fix: B is submitted exactly once (by the
    promotion), the rejection recovery completes B deterministically and
    dispatches the request parked during the rejection — no duplicate
    submit, no orphan."""
    profile = _profile(ProviderKind.LOCAL, base_url="http://127.0.0.1:9")
    queued: list[tuple[Any, ...]] = []
    state: dict[str, int] = {"b_submits": 0}
    published: list[tuple[str, str]] = []

    def flaky_submit(fn: Any, *args: Any) -> None:
        _gen, _p, token, _cb = args
        if token == "b":
            state["b_submits"] += 1
            if state["b_submits"] == 2:  # the duplicate dispatch (6d8f030) is rejected
                raise RuntimeError("executor closed")
        queued.append((fn, *args))

    coord = _ConnectionCoordinator(flaky_submit, threading.Event())
    gate = _PauseLock()
    coord._lock = gate  # type: ignore[assignment]
    coord.request(profile, "a", lambda t, r: published.append(("a", r.state.value)))
    gate.arm()
    outcome: dict[str, Any] = {}

    def request_b() -> None:
        outcome["ok"] = coord.request(
            profile, "b", lambda t, r: published.append(("b", r.state.value))
        )

    thread = threading.Thread(target=request_b, daemon=True)
    thread.start()
    assert gate.paused.wait(5)  # B reserved (parked), paused before its ownership check
    fn_a, gen_a, p_a, tok_a, cb_a = queued[0]
    assert tok_a == "a"
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=_result(ctest.ConnectionState.CONNECTED),
    ):
        fn_a(gen_a, p_a, tok_a, cb_a)  # A completes and promotes B (submit #1 accepted)
    coord.request(profile, "c", lambda t, r: published.append(("c", r.state.value)))
    gate.unblock()
    thread.join(5)
    assert not thread.is_alive()
    assert state["b_submits"] == 1  # RED: today B is submitted a second time by its own call
    assert outcome["ok"] is True  # B was accepted (parked → promoted)
    fn_b, gen_b, p_b, tok_b, cb_b = queued[1]
    assert tok_b == "b"
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=_result(ctest.ConnectionState.AUTH_FAILED),
    ):
        fn_b(gen_b, p_b, tok_b, cb_b)  # B completes: one run, one callback
    assert published == [("a", "connected"), ("b", "auth_failed")]
    assert len(queued) == 3  # A, B, and the newest parked (C) promoted by B
    fn_c, gen_c, p_c, tok_c, cb_c = queued[2]
    assert tok_c == "c"
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=_result(ctest.ConnectionState.CONNECTED),
    ):
        fn_c(gen_c, p_c, tok_c, cb_c)
    assert published == [("a", "connected"), ("b", "auth_failed"), ("c", "connected")]
    assert len(published) == len({tag for tag, _state in published})
    _assert_no_orphan_slot(coord)
    _assert_coordinator_clean(coord)
    assert coord.request(profile, "d", lambda t, r: None) is True  # later usability


# ── Criterion 8/9: rapid same-row and three-row GTK barriers ────────────────


def test_gtk_rapid_same_row_with_blocked_first_submit(
    env: tuple[Path, dict[str, Any]], idle_inline: None, english: None
) -> None:
    """Rapid same-row clicks with the first submit BLOCKED: click 2's
    CANCELLED is discarded by the per-row click token, only clicks 1 and
    3 are dispatched — exactly once each — and the row's final state is
    click 3's result."""
    from moira.provider_editor import ProviderEditor

    entered = threading.Event()
    release = threading.Event()
    queued: list[tuple[Any, ...]] = []
    connection_dispatches = {"n": 0}

    def submit(fn: Any, *args: Any) -> None:
        if getattr(fn, "__name__", "") == "_run":
            connection_dispatches["n"] += 1
            if connection_dispatches["n"] == 1:
                entered.set()
                release.wait(5)
        queued.append((fn, *args))

    ed = ProviderEditor(submit=submit)
    ed._profiles = (_profile(ProviderKind.LOCAL, base_url="http://127.0.0.1:9"),)
    ed._show_list()
    widgets = ed._row_widgets["local-main"]
    widgets["test"].emit("clicked")  # click 1: dispatched, submit blocked
    assert entered.wait(5)
    widgets["test"].emit("clicked")  # click 2: parks
    widgets["test"].emit("clicked")  # click 3: replaces click 2 (CANCELLED discarded by token)
    assert widgets["test_status"].get_text() == "Testing…"
    release.set()
    runs = _connection_runs(queued, ed._connection_coordinator)
    assert len(runs) == 1  # only click 1 dispatched so far
    fn1, gen1, p1, tok1, cb1 = runs[0]
    assert tok1[3] == 1
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=ctest.ConnectionResult(ctest.ConnectionState.CONNECTED, "local-main"),
    ):
        fn1(gen1, p1, tok1, cb1)
    assert widgets["test_status"].get_text() == "Testing…"  # click 1's stale result discarded
    runs = _connection_runs(queued, ed._connection_coordinator)
    assert len(runs) == 2  # the newest click (3) is promoted and dispatched once
    fn3, gen3, p3, tok3, cb3 = runs[1]
    assert tok3[3] == 3
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=ctest.ConnectionResult(ctest.ConnectionState.AUTH_FAILED, "local-main"),
    ):
        fn3(gen3, p3, tok3, cb3)
    assert widgets["test_status"].get_text() == "Authentication failed"
    assert connection_dispatches["n"] == 2  # exactly two dispatches, no duplicates
    ed.shutdown()
    _assert_coordinator_clean(ed._connection_coordinator)


def test_gtk_three_row_with_blocked_first_submit(
    env: tuple[Path, dict[str, Any]], idle_inline: None, english: None
) -> None:
    """A on row 1 (submit blocked), B parked on row 2, C on row 3
    replacing B: B's row shows the translated Cancelled exactly once, A
    and C run exactly once each and keep their own independent results."""
    from moira.provider_editor import ProviderEditor

    entered = threading.Event()
    release = threading.Event()
    queued: list[tuple[Any, ...]] = []
    connection_dispatches = {"n": 0}

    def submit(fn: Any, *args: Any) -> None:
        if getattr(fn, "__name__", "") == "_run":
            connection_dispatches["n"] += 1
            if connection_dispatches["n"] == 1:
                entered.set()
                release.wait(5)
        queued.append((fn, *args))

    ed = ProviderEditor(submit=submit)
    ed._profiles = (
        _profile(ProviderKind.LOCAL, slug="local-main", base_url="http://127.0.0.1:9"),
        _profile(ProviderKind.LOCAL, slug="local-second", base_url="http://127.0.0.1:9"),
        _profile(ProviderKind.LOCAL, slug="local-third", base_url="http://127.0.0.1:9"),
    )
    ed._show_list()
    widgets_a = ed._row_widgets["local-main"]
    widgets_b = ed._row_widgets["local-second"]
    widgets_c = ed._row_widgets["local-third"]
    widgets_a["test"].emit("clicked")  # A: dispatched, submit blocked
    assert entered.wait(5)
    widgets_b["test"].emit("clicked")  # B: parks on its own row
    widgets_c["test"].emit("clicked")  # C: replaces B
    assert widgets_b["test_status"].get_text() == "Cancelled"  # B terminated exactly once
    assert widgets_a["test_status"].get_text() == "Testing…"
    assert widgets_c["test_status"].get_text() == "Testing…"
    release.set()
    runs = _connection_runs(queued, ed._connection_coordinator)
    assert len(runs) == 1
    fn_a, gen_a, p_a, tok_a, cb_a = runs[0]
    assert tok_a[0] == "local-main"
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=ctest.ConnectionResult(ctest.ConnectionState.CONNECTED, "local-main"),
    ):
        fn_a(gen_a, p_a, tok_a, cb_a)
    assert widgets_a["test_status"].get_text() == "Connected"
    assert widgets_b["test_status"].get_text() == "Cancelled"  # B keeps its terminal state
    runs = _connection_runs(queued, ed._connection_coordinator)
    assert len(runs) == 2  # the newest parked (C) promoted and dispatched once
    fn_c, gen_c, p_c, tok_c, cb_c = runs[1]
    assert tok_c[0] == "local-third"
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=ctest.ConnectionResult(ctest.ConnectionState.AUTH_FAILED, "local-third"),
    ):
        fn_c(gen_c, p_c, tok_c, cb_c)
    assert widgets_c["test_status"].get_text() == "Authentication failed"
    assert connection_dispatches["n"] == 2  # exactly two dispatches, no duplicates
    ed.shutdown()
    _assert_coordinator_clean(ed._connection_coordinator)
