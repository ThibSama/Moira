"""Package 7n — terminate replaced connection requests (ACCEPTANCE_CORRECTION).

RED tests on 317600c for the blocking finding:

A newly parked request silently replaces the previous ``_pending``
tuple. The replaced request receives NO callback even though its editor
row was already set to "Testing…": with A in flight, B pending on
another row and C replacing B, row B remains stuck indefinitely. The
current newest-wins tests explicitly expect B never to publish, while
the UI claims every request terminates.

The fix gives every accepted request exactly one terminal disposition:
a normal provider result, a sanitized submit/runner failure, CANCELLED
when replaced by a newer pending request (published ITERATIVELY outside
the lock — a replacement callback that re-enters ``request()`` appends
to the drain instead of recursing), or a silent cancellation only after
editor shutdown or stale-row invalidation. The editor row token gains a
per-row click generation, so an older generation (in-flight result or
superseded CANCELLED) can never overwrite a newer "Testing…" or the
final result.
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
    """No latched generation, no orphaned pending slot and no pending
    replacement cancellations."""
    with coord._lock:
        assert coord._inflight is None
        assert coord._pending is None
        assert not getattr(coord, "_cancelled", [])


# ── Finding: a replaced pending must terminate as CANCELLED ─────────────────


def test_replaced_pending_publishes_cancelled_exactly_once() -> None:
    """B parked behind A is replaced by C: B must terminate as CANCELLED
    through its own callback — exactly once — while A and C keep their
    independent results (the current code publishes nothing for B)."""
    submit, queued = _capture_submit()
    coord = _ConnectionCoordinator(submit, threading.Event())
    profile = _profile(ProviderKind.LOCAL, base_url="http://127.0.0.1:9")
    published: list[tuple[str, str]] = []
    coord.request(profile, "a", lambda t, r: published.append(("a", r.state.value)))
    coord.request(profile, "b", lambda t, r: published.append(("b", r.state.value)))
    coord.request(profile, "c", lambda t, r: published.append(("c", r.state.value)))
    assert published == [("b", "cancelled")]  # RED: B publishes nothing today
    assert len(queued) == 1  # only A dispatched; C parked
    fn, gen, p, token, cb = queued[0]
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=_result(ctest.ConnectionState.CONNECTED),
    ):
        fn(gen, p, token, cb)
    assert published == [("b", "cancelled"), ("a", "connected")]
    assert len(queued) == 2  # the newest parked request (C) is promoted
    fn2, gen2, p2, token2, cb2 = queued[1]
    assert token2 == "c"
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=_result(ctest.ConnectionState.AUTH_FAILED),
    ):
        fn2(gen2, p2, token2, cb2)
    assert published == [("b", "cancelled"), ("a", "connected"), ("c", "auth_failed")]
    assert len(published) == len({tag for tag, _state in published})  # no duplicate callback
    _assert_no_orphan_slot(coord)
    _assert_coordinator_clean(coord)
    assert coord.request(profile, "d", lambda t, r: None) is True  # later usability


# ── Criterion 4/8: a replacement callback re-entering request() must not ────
# ── recurse without bound — the drain is ITERATIVE ──────────────────────────


def test_replacement_callback_reentry_drains_iteratively_not_recursively() -> None:
    """Every CANCELLED completion parks the next request (the callback
    re-enters ``request()``): the replacement chain must drain
    ITERATIVELY with a constant stack — the naive synchronous
    cancellation has no fixed stack bound and overflows a small-stack
    thread (and RecursionError is silently swallowed by the callback
    try/except, dropping requests instead of crashing)."""
    submit, queued = _capture_submit()
    coord = _ConnectionCoordinator(submit, threading.Event())
    profile = _profile(ProviderKind.LOCAL, base_url="http://127.0.0.1:9")
    published: list[tuple[str, str]] = []
    pending_tags: list[str] = [f"t{i}" for i in range(2, 302)]
    state: dict[str, Any] = {"coord": None}

    def record(tag: str) -> Any:
        def cb(_t: Any, r: ctest.ConnectionResult) -> None:
            published.append((tag, r.state.value))
            if pending_tags:  # every completion parks the next request
                nxt = pending_tags.pop(0)
                state["coord"].request(profile, nxt, record(nxt))

        return cb

    coord.request(profile, "t0", record("t0"))  # in flight
    coord.request(profile, "t1", record("t1"))  # parked
    state["coord"] = coord
    errors: list[BaseException] = []

    def scenario() -> None:
        import sys

        try:
            old_limit = sys.getrecursionlimit()
            sys.setrecursionlimit(100)  # deterministic RED: recursion overflows, iteration does not
            try:
                # The replacement itself triggers the drain chain: t1's
                # CANCELLED parks t2, which replaces the replacement,
                # whose CANCELLED parks t3, and so on.
                coord.request(profile, "boom", record("boom"))
                fn0, gen0, p0, token0, cb0 = queued[0]
                with patch(
                    "moira.provider_editor.run_connection_test",
                    return_value=_result(ctest.ConnectionState.CONNECTED),
                ):
                    fn0(gen0, p0, token0, cb0)
                fn2, gen2, p2, token2, cb2 = queued[1]  # the last parked request
                with patch(
                    "moira.provider_editor.run_connection_test",
                    return_value=_result(ctest.ConnectionState.CONNECTED),
                ):
                    fn2(gen2, p2, token2, cb2)
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
    assert errors == []  # RED: RecursionError on the recursive form
    cancelled = {tag for tag, _state in published if _state == "cancelled"}
    assert cancelled == {"t1", "boom", *{f"t{i}" for i in range(2, 301)}}
    connected = {tag for tag, _state in published if _state == "connected"}
    assert connected == {"t0", "t301"}  # only the in-flight and the last parked run
    assert len(published) == len({tag for tag, _state in published})  # no duplicate callback
    _assert_no_orphan_slot(coord)
    _assert_coordinator_clean(coord)
    assert coord.request(profile, "zz", record("zz")) is True  # later usability


# ── Criterion 7: replacement during a blocked first submit ──────────────────


def test_replacement_during_blocked_first_submit() -> None:
    """A newer click replaces the parked one while the FIRST submit is
    still blocked: the superseded request publishes CANCELLED (its row
    never stays stuck), the committed first submit completes normally,
    and the newest parked request is promoted."""
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
    assert entered.wait(5)  # A's first submit is blocked
    coord.request(profile, "b", lambda t, r: published.append(("b", r.state.value)))
    coord.request(profile, "c", lambda t, r: published.append(("c", r.state.value)))
    assert published == [("b", "cancelled")]  # RED: B publishes nothing today
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert outcome["ok"] is True  # A was committed before close
    assert len(queued) == 1  # B was never submitted
    fn, gen, p, token, cb = queued[0]
    assert token == "a"
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=_result(ctest.ConnectionState.CONNECTED),
    ):
        fn(gen, p, token, cb)
    assert published == [("b", "cancelled"), ("a", "connected")]
    assert len(queued) == 2  # C promoted
    fn2, gen2, p2, token2, cb2 = queued[1]
    assert token2 == "c"
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=_result(ctest.ConnectionState.RATE_LIMITED),
    ):
        fn2(gen2, p2, token2, cb2)
    assert published == [("b", "cancelled"), ("a", "connected"), ("c", "rate_limited")]
    assert len(published) == len({tag for tag, _state in published})
    _assert_no_orphan_slot(coord)
    _assert_coordinator_clean(coord)
    assert coord.request(profile, "d", lambda t, r: None) is True  # later usability


# ── Criterion 7: replacement during a rejection callback ────────────────────


def test_replacement_during_rejection_callback() -> None:
    """A replacement (C then D, D newer) landing inside a rejection
    callback: the superseded C terminates as CANCELLED through its own
    callback — exactly once — while the rejected request gets its
    sanitized failure and the detached parked request is dispatched."""
    queued: list[tuple[Any, ...]] = []
    calls = {"n": 0}

    def reentrant_submit(fn: Any, *args: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 1:  # the first submit rejects; B parks mid-submit
            coord.request(profile, "b", record("b"))
            raise RuntimeError("executor closed")
        queued.append((fn, *args))

    profile = _profile(ProviderKind.LOCAL, base_url="http://127.0.0.1:9")
    coord = _ConnectionCoordinator(reentrant_submit, threading.Event())
    published: list[tuple[str, str]] = []

    def record(tag: str) -> Any:
        def cb(_t: Any, r: ctest.ConnectionResult) -> None:
            published.append((tag, r.state.value))
            if tag == "a":
                # A's rejection callback parks C then D (D replaces C).
                coord.request(profile, "c", record("c"))
                coord.request(profile, "d", record("d"))

        return cb

    assert coord.request(profile, "a", record("a")) is False  # A rejected
    assert published == [("a", "unreachable"), ("c", "cancelled")]  # RED: C publishes nothing today
    assert len(queued) == 1  # B dispatched — never orphaned
    fn, gen, p, token, cb = queued[0]
    assert token == "b"
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=_result(ctest.ConnectionState.CONNECTED),
    ):
        fn(gen, p, token, cb)
    assert published == [("a", "unreachable"), ("c", "cancelled"), ("b", "connected")]
    assert len(queued) == 2  # the newest parked request (D) is promoted
    fn2, gen2, p2, token2, cb2 = queued[1]
    assert token2 == "d"
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=_result(ctest.ConnectionState.CONNECTED),
    ):
        fn2(gen2, p2, token2, cb2)
    assert published == [
        ("a", "unreachable"),
        ("c", "cancelled"),
        ("b", "connected"),
        ("d", "connected"),
    ]
    assert len(published) == len({tag for tag, _state in published})
    _assert_no_orphan_slot(coord)
    _assert_coordinator_clean(coord)
    assert coord.request(profile, "e", lambda t, r: None) is True  # later usability


# ── Criterion 7: replacement during a runner callback ───────────────────────


def test_replacement_during_runner_callback() -> None:
    """A replacement landing while the in-flight runner is blocked: the
    superseded parked request publishes CANCELLED, the in-flight result
    still publishes, and the newest parked request is promoted."""
    submit, queued = _capture_submit()
    coord = _ConnectionCoordinator(submit, threading.Event())
    profile = _profile(ProviderKind.LOCAL, base_url="http://127.0.0.1:9")
    published: list[tuple[str, str]] = []
    coord.request(profile, "a", lambda t, r: published.append(("a", r.state.value)))
    coord.request(profile, "b", lambda t, r: published.append(("b", r.state.value)))
    fn, gen, p, token, cb = queued[0]
    entered = threading.Event()
    release = threading.Event()

    def blocked_runner(*_args: Any, **_kwargs: Any) -> ctest.ConnectionResult:
        entered.set()
        release.wait(5)
        return _result(ctest.ConnectionState.CONNECTED)

    with patch("moira.provider_editor.run_connection_test", side_effect=blocked_runner):
        worker = threading.Thread(target=fn, args=(gen, p, token, cb), daemon=True)
        worker.start()
        assert entered.wait(5)  # A's runner is blocked mid-run
        coord.request(profile, "c", lambda t, r: published.append(("c", r.state.value)))
        assert published == [("b", "cancelled")]  # RED: B publishes nothing today
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert published == [("b", "cancelled"), ("a", "connected")]
    assert len(queued) == 2  # C promoted
    fn2, gen2, p2, token2, cb2 = queued[1]
    assert token2 == "c"
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=_result(ctest.ConnectionState.AUTH_FAILED),
    ):
        fn2(gen2, p2, token2, cb2)
    assert published == [("b", "cancelled"), ("a", "connected"), ("c", "auth_failed")]
    assert len(published) == len({tag for tag, _state in published})
    _assert_no_orphan_slot(coord)
    _assert_coordinator_clean(coord)
    assert coord.request(profile, "d", lambda t, r: None) is True  # later usability


# ── Criterion 7: replacement immediately before shutdown ────────────────────


def test_replacement_immediately_before_shutdown() -> None:
    """Replacement then close: the superseded request publishes CANCELLED
    (it was replaced, not discarded), nothing else publishes after
    close, and no parked work is ever submitted after close."""
    submit, queued = _capture_submit()
    event = threading.Event()
    coord = _ConnectionCoordinator(submit, event)
    profile = _profile(ProviderKind.LOCAL, base_url="http://127.0.0.1:9")
    published: list[tuple[str, str]] = []
    coord.request(profile, "a", lambda t, r: published.append(("a", r.state.value)))
    coord.request(profile, "b", lambda t, r: published.append(("b", r.state.value)))
    coord.request(profile, "c", lambda t, r: published.append(("c", r.state.value)))
    assert published == [("b", "cancelled")]  # replaced BEFORE close: CANCELLED
    event.set()  # close wins immediately after the replacement
    coord.cancel()
    fn, gen, p, token, cb = queued[0]
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=_result(ctest.ConnectionState.CONNECTED),
    ):
        fn(gen, p, token, cb)  # committed work self-bounds: nothing publishes
    assert published == [("b", "cancelled")]  # nothing publishes after close
    assert len(queued) == 1  # C never submitted after close
    _assert_no_orphan_slot(coord)
    _assert_coordinator_clean(coord)


# ── Criterion 5: editor-level multi-row replacement (real GTK) ──────────────


def test_editor_replaced_row_shows_cancelled(
    env: tuple[Path, dict[str, Any]], idle_inline: None, english: None
) -> None:
    """A in flight on its row, B parked on another row and C replacing B
    on a third row: B leaves "Testing…" and shows the translated
    "Cancelled"; A and C retain their correct independent results."""
    from moira.provider_editor import ProviderEditor

    submit, queued = _capture_submit()
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
    widgets_a["test"].emit("clicked")  # A in flight
    widgets_b["test"].emit("clicked")  # B parked on its own row
    widgets_c["test"].emit("clicked")  # C replaces B
    assert widgets_b["test_status"].get_text() == "Cancelled"  # RED: B stays "Testing…" today
    assert widgets_a["test_status"].get_text() == "Testing…"
    assert widgets_c["test_status"].get_text() == "Testing…"
    fn, gen, p, token, cb = _find_connection_run(queued, ed._connection_coordinator)
    assert token[0] == "local-main"  # A's run
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=ctest.ConnectionResult(ctest.ConnectionState.CONNECTED, "local-main"),
    ):
        fn(gen, p, token, cb)
    assert widgets_a["test_status"].get_text() == "Connected"  # A keeps its result
    assert len(queued) == 3  # reload + A + the promoted newest parked (C)
    fn2, gen2, p2, token2, cb2 = queued[2]
    assert token2[0] == "local-third"
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=ctest.ConnectionResult(ctest.ConnectionState.AUTH_FAILED, "local-third"),
    ):
        fn2(gen2, p2, token2, cb2)
    assert widgets_c["test_status"].get_text() == "Authentication failed"
    assert widgets_a["test_status"].get_text() == "Connected"
    assert widgets_b["test_status"].get_text() == "Cancelled"  # B's row keeps its terminal state
    ed.shutdown()
    _assert_coordinator_clean(ed._connection_coordinator)


# ── Criterion 6: rapid retest of the SAME row ───────────────────────────────


def test_rapid_retest_same_row_older_generation_never_overwrites(
    env: tuple[Path, dict[str, Any]], idle_inline: None, english: None
) -> None:
    """Rapid retest of the same row: an older in-flight generation that
    completes after a newer click must NOT overwrite the newer
    "Testing…" or the final result, and the older pending's CANCELLED
    completion is discarded too — the row token carries a per-row click
    generation (RED: today the token is only (slug, epoch, widgets) and
    the older in-flight result overwrites the row)."""
    from moira.provider_editor import ProviderEditor

    submit, queued = _capture_submit()
    ed = ProviderEditor(submit=submit)
    ed._profiles = (_profile(ProviderKind.LOCAL, base_url="http://127.0.0.1:9"),)
    ed._show_list()
    widgets = ed._row_widgets["local-main"]
    widgets["test"].emit("clicked")  # click 1: in flight
    widgets["test"].emit("clicked")  # click 2: parks
    widgets["test"].emit("clicked")  # click 3: replaces click 2 (CANCELLED, then discarded)
    assert widgets["test_status"].get_text() == "Testing…"  # click 2's CANCELLED was discarded
    fn, gen, p, token, cb = _find_connection_run(queued, ed._connection_coordinator)
    assert len(token) == 4  # RED: the token must carry the per-row click generation
    assert token[3] == 1  # the in-flight run is click 1
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=ctest.ConnectionResult(ctest.ConnectionState.CONNECTED, "local-main"),
    ):
        fn(gen, p, token, cb)
    # RED: the OLD in-flight generation completed after a newer click —
    # it must not overwrite the newer "Testing…".
    assert widgets["test_status"].get_text() == "Testing…"
    assert len(queued) == 3  # reload + click 1 + the promoted newest click
    fn2, gen2, p2, token2, cb2 = queued[2]
    assert token2[3] == 3  # only the newest click is promoted
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=ctest.ConnectionResult(ctest.ConnectionState.AUTH_FAILED, "local-main"),
    ):
        fn2(gen2, p2, token2, cb2)
    assert widgets["test_status"].get_text() == "Authentication failed"  # the newest click's result
    ed.shutdown()
    _assert_coordinator_clean(ed._connection_coordinator)


# ── Criterion 5: no status written to a rebuilt row ─────────────────────────


def test_replaced_result_never_written_to_rebuilt_row(
    env: tuple[Path, dict[str, Any]], idle_inline: None, english: None
) -> None:
    """A replacement cancellation and an in-flight result are discarded
    when the row was rebuilt (edited, renamed, toggled): no status is
    written to the NEW row widgets."""
    from moira.provider_editor import ProviderEditor

    submit, queued = _capture_submit()
    ed = ProviderEditor(submit=submit)
    ed._profiles = (
        _profile(ProviderKind.LOCAL, slug="local-main", base_url="http://127.0.0.1:9"),
        _profile(ProviderKind.LOCAL, slug="local-second", base_url="http://127.0.0.1:9"),
    )
    ed._show_list()
    ed._row_widgets["local-main"]["test"].emit("clicked")  # A in flight
    ed._row_widgets["local-second"]["test"].emit("clicked")  # B parked
    ed._show_list()  # rebuild (e.g. a toggle or edit): new widgets, new epoch
    widgets_b = ed._row_widgets["local-second"]
    widgets_b["test"].emit("clicked")  # C replaces B on the NEW row
    assert widgets_b["test_status"].get_text() == "Testing…"  # B's CANCELLED was discarded
    fn, gen, p, token, cb = _find_connection_run(queued, ed._connection_coordinator)
    assert token[0] == "local-main"
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=ctest.ConnectionResult(ctest.ConnectionState.CONNECTED, "local-main"),
    ):
        fn(gen, p, token, cb)
    # A's result was discarded too: the rebuild invalidated every row.
    assert widgets_b["test_status"].get_text() == "Testing…"
    ed.shutdown()
    _assert_coordinator_clean(ed._connection_coordinator)
