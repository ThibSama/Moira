"""Package 7l — finish connection-test isolation (ACCEPTANCE_CORRECTION).

RED tests on 1ecbf9b for the three blocking findings:

1. ``resolve_target()`` returns only an address string, then the child
   calls ``socket.create_connection((target, port))``: the validated
   family/protocol/sockaddr are discarded and the convenience function
   performs another lookup — the claimed single-resolution /
   direct-connect contract is not met, and nothing verifies that the
   connected peer matches the validated sockaddr before TLS or headers.
2. If submission of the newest parked test fails during promotion,
   ``_ConnectionCoordinator`` clears its slot but never rejects that
   request: its row can remain permanently on "Testing…".
3. ``run_bounded()`` selects for writable stdin but leaves the pipe
   blocking and calls ``os.write()`` with 8192 bytes: readiness does not
   guarantee that the whole blocking write fits, so the total deadline
   is not formally enforced for partial readers.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
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
from moira.integrations import ProbeOutcome, ProviderKind, ProviderProfile, run_bounded
from moira.provider_editor import _ConnectionCoordinator

# ── Fake local HTTP server (loopback only, no accounts) ─────────────────────


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        server: Any = self.server
        server.requests.append((self.path, dict(self.headers)))
        if server.delay:
            time.sleep(server.delay)
        body: bytes = server.body
        self.send_response(server.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


class _FakeServer:
    def __init__(self) -> None:
        self.httpd: Any = HTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.status = 200  # type: ignore[attr-defined]
        self.httpd.body = b'{"data": [{"id": "deepseek-chat"}]}'  # type: ignore[attr-defined]
        self.httpd.delay = 0.0  # type: ignore[attr-defined]
        self.httpd.requests = []  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        return int(self.httpd.server_address[1])

    @property
    def requests(self) -> list[tuple[str, dict[str, str]]]:
        return self.httpd.requests  # type: ignore[no-any-return]

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server() -> Any:
    fake = _FakeServer()
    yield fake
    fake.close()


class _V6HTTPServer(HTTPServer):
    address_family = socket.AF_INET6


@pytest.fixture
def v6server() -> Any:
    try:
        httpd = _V6HTTPServer(("::1", 0), _Handler)
    except OSError as exc:
        pytest.skip(f"IPv6 loopback unavailable: {exc}")
    httpd.status = 200  # type: ignore[attr-defined]
    httpd.body = b'{"data": [{"id": "deepseek-chat"}]}'  # type: ignore[attr-defined]
    httpd.delay = 0.0  # type: ignore[attr-defined]
    httpd.requests = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


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


def _local_profile(server: Any, model: str = "deepseek-chat") -> ProviderProfile:
    url = f"http://127.0.0.1:{server.port}"
    return _profile(ProviderKind.LOCAL, slug="local-main", base_url=url, model=model)


# ── Finding 1: validated immutable target, direct connect, peer check ──────


def test_resolve_target_returns_validated_immutable_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve_target returns an immutable target carrying the EXACT
    family/socktype/proto/sockaddr of the ONE accepted getaddrinfo call;
    a second resolution is never attempted (it would raise here)."""
    first = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
    calls = {"n": 0}

    def single_getaddrinfo(*args: Any, **kwargs: Any) -> list[Any]:
        calls["n"] += 1
        if calls["n"] > 1:
            raise AssertionError("a second resolution was attempted")
        return first

    monkeypatch.setattr(socket, "getaddrinfo", single_getaddrinfo)
    target = ctest.resolve_target("api.example.com", 443, "remote")
    assert target is not None
    assert target.family == socket.AF_INET
    assert target.socktype == socket.SOCK_STREAM
    assert target.proto == 6
    assert target.sockaddr == ("8.8.8.8", 443)
    assert calls["n"] == 1  # exactly one resolution — no TOCTOU window


def test_resolve_target_ipv6_validated_target(monkeypatch: pytest.MonkeyPatch) -> None:
    first = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700::1", 443, 0, 0))]
    calls = {"n": 0}

    def single_getaddrinfo(*args: Any, **kwargs: Any) -> list[Any]:
        calls["n"] += 1
        if calls["n"] > 1:
            raise AssertionError("a second resolution was attempted")
        return first

    monkeypatch.setattr(socket, "getaddrinfo", single_getaddrinfo)
    target = ctest.resolve_target("api.example.com", 443, "remote")
    assert target is not None
    assert target.family == socket.AF_INET6
    assert target.socktype == socket.SOCK_STREAM
    assert target.proto == 6
    assert target.sockaddr == ("2606:4700::1", 443, 0, 0)
    assert calls["n"] == 1


def test_resolve_target_mixed_public_private_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    mixed = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fd00::1", 443, 0, 0)),
    ]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: mixed)
    assert ctest.resolve_target("api.example.com", 443, "remote") is None


def test_same_endpoint_normalizes_and_compares() -> None:
    """getpeername is normalized (zone stripped, address+port only) and
    compared with the validated sockaddr; flowinfo/scope_id differences
    never matter."""
    assert ctest.same_endpoint(("8.8.8.8", 443), ("8.8.8.8", 443)) is True
    assert ctest.same_endpoint(("::1", 443, 0, 0), ("::1", 443, 12, 7)) is True
    assert ctest.same_endpoint(("fe80::1", 443, 0, 3), ("fe80::1%eth0", 443, 0, 0)) is True
    assert ctest.same_endpoint(("8.8.8.8", 443), ("8.8.8.9", 443)) is False
    assert ctest.same_endpoint(("8.8.8.8", 443), ("8.8.8.8", 80)) is False
    assert ctest.same_endpoint(("::1", 443), ("127.0.0.1", 443)) is False


def test_child_code_contract_single_resolution_direct_connect() -> None:
    """Source contract of the dedicated child: it never resolves again,
    never uses a resolving helper (create_connection / conn.connect) and
    verifies the peer against the validated sockaddr BEFORE any TLS wrap
    or HTTP header."""
    code = ctest._CHILD_CODE
    assert "getaddrinfo" not in code
    assert "create_connection" not in code
    assert "conn.connect" not in code
    assert "socket.socket(" in code  # the socket is created directly
    assert ".connect(" in code  # and connected to the validated sockaddr
    assert "getpeername" in code
    assert code.index("getpeername") < code.index("wrap_socket")
    assert code.index("same_endpoint") < code.index("wrap_socket")


def test_child_connects_ipv6_loopback_end_to_end(v6server: Any) -> None:
    """The validated IPv6 sockaddr (family AF_INET6, 4-tuple) is used
    directly: connect + getpeername normalization + HTTP all work."""
    port = int(v6server.server_address[1])
    profile = _profile(ProviderKind.LOCAL, base_url=f"http://[::1]:{port}")
    result = ctest.bounded_connection_test(profile, "sk-7l")
    assert result.state is ctest.ConnectionState.CONNECTED
    assert v6server.requests[0][1].get("Authorization") == "Bearer sk-7l"


def test_refused_private_target_receives_no_request_or_auth_header(server: Any) -> None:
    """A refused private target receives NO request and NO auth header —
    and the control run proves the listener would have recorded them."""
    argv = [
        sys.executable,
        "-c",
        ctest._CHILD_CODE,
        "openai_compatible",
        f"https://127.0.0.1:{server.port}/models",
        "deepseek-chat",
        "1.0",
        "1.0",
        "5.0",
        "1000",
        "bearer",
        "remote",
    ]
    result = subprocess.run(argv, input=b"sk-7l\n", capture_output=True, check=False)
    assert result.returncode == 4  # UNREACHABLE: refused by address policy
    assert server.requests == []  # no request, no auth header reached the target
    ok = ctest.bounded_connection_test(_local_profile(server), "sk-7l")
    assert ok.state is ctest.ConnectionState.CONNECTED
    assert server.requests[-1][1].get("Authorization") == "Bearer sk-7l"


# ── Finding 2: deterministic coordinator rejection completion ───────────────


def _capture_submit() -> tuple[Any, list[tuple[Any, ...]]]:
    queued: list[tuple[Any, ...]] = []

    def submit(fn: Any, *args: Any) -> None:
        queued.append((fn, *args))

    return submit, queued


def _result(state: ctest.ConnectionState) -> ctest.ConnectionResult:
    return ctest.ConnectionResult(state, "local-main")


def test_first_rejection_completes_request_with_sanitized_failure() -> None:
    def rejecting_submit(fn: Any, *args: Any) -> None:
        raise RuntimeError("executor closed")

    coord = _ConnectionCoordinator(rejecting_submit, threading.Event())
    profile = _profile(ProviderKind.LOCAL, base_url="http://127.0.0.1:9")
    completed: list[tuple[str, str]] = []
    assert coord.request(profile, "t1", lambda t, r: completed.append((t, r.state.value))) is False
    assert completed == [("t1", "unreachable")]  # deterministic rejection completion


def test_rejection_completion_runs_outside_the_lock() -> None:
    def rejecting_submit(fn: Any, *args: Any) -> None:
        raise RuntimeError("executor closed")

    coord = _ConnectionCoordinator(rejecting_submit, threading.Event())
    profile = _profile(ProviderKind.LOCAL, base_url="http://127.0.0.1:9")
    observed: list[tuple[str, str, bool]] = []

    def record(token: str, result: ctest.ConnectionResult) -> None:
        observed.append((token, result.state.value, coord._lock.locked()))

    assert coord.request(profile, "t1", record) is False
    assert observed == [("t1", "unreachable", False)]  # completed outside the lock


def test_promoted_rejection_completes_pending_outside_the_lock() -> None:
    queued: list[tuple[Any, ...]] = []
    reject_promotion = {"n": 0}

    def flaky_submit(fn: Any, *args: Any) -> None:
        if reject_promotion["n"] > 0:
            reject_promotion["n"] = 0
            raise RuntimeError("executor closed")
        queued.append((fn, *args))

    coord = _ConnectionCoordinator(flaky_submit, threading.Event())
    profile = _profile(ProviderKind.LOCAL, base_url="http://127.0.0.1:9")
    observed: list[tuple[str, str, bool]] = []
    coord.request(
        profile, "t1", lambda t, r: observed.append(("t1", r.state.value, coord._lock.locked()))
    )
    coord.request(
        profile, "t2", lambda t, r: observed.append(("t2", r.state.value, coord._lock.locked()))
    )
    fn, gen, p, token, cb = queued[0]
    reject_promotion["n"] = 1  # the promotion submit fails
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=_result(ctest.ConnectionState.CONNECTED),
    ):
        fn(gen, p, token, cb)
    assert ("t1", "connected", False) in observed
    assert ("t2", "unreachable", False) in observed  # the parked row is completed
    assert coord.request(profile, "t3", lambda t, r: None) is True  # slot cleared


def test_request_parked_during_failed_promotion_is_dispatched() -> None:
    """A request that parks while the promotion submit fails is still
    dispatched (recursion bounded to one level) — never orphaned."""
    queued: list[tuple[Any, ...]] = []
    reject_promotion = {"n": 0}

    def flaky_submit(fn: Any, *args: Any) -> None:
        if reject_promotion["n"] > 0:
            reject_promotion["n"] = 0
            raise RuntimeError("executor closed")
        queued.append((fn, *args))

    coord = _ConnectionCoordinator(flaky_submit, threading.Event())
    profile = _profile(ProviderKind.LOCAL, base_url="http://127.0.0.1:9")
    published: list[tuple[str, str]] = []

    def record(tag: str) -> Any:
        return lambda t, r: published.append((tag, r.state.value))

    coord.request(profile, "a", record("a"))
    coord.request(profile, "b", record("b"))  # parked
    reject_promotion["n"] = 1  # the promotion submit for b will fail
    fn, gen, p, token, cb = queued[0]

    def park_during_promotion(_t: Any, r: ctest.ConnectionResult) -> None:
        published.append(("a", r.state.value))
        coord.request(profile, "c", record("c"))  # parks while b's promotion fails

    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=_result(ctest.ConnectionState.CONNECTED),
    ):
        fn(gen, p, token, park_during_promotion)
    assert published == [("a", "connected"), ("b", "unreachable")]
    assert len(queued) == 2  # a + c; b was rejected, never dispatched
    fn2, gen2, p2, token2, cb2 = queued[1]
    assert token2 == "c"  # the request parked during the failure is dispatched


def test_runner_exception_completes_deterministically_and_promotes() -> None:
    submit, queued = _capture_submit()
    coord = _ConnectionCoordinator(submit, threading.Event())
    profile = _profile(ProviderKind.LOCAL, base_url="http://127.0.0.1:9")
    published: list[tuple[str, str]] = []
    coord.request(profile, "t1", lambda t, r: published.append(("t1", r.state.value)))
    coord.request(profile, "t2", lambda t, r: published.append(("t2", r.state.value)))
    fn, gen, p, token, cb = queued[0]
    with patch(
        "moira.provider_editor.run_connection_test",
        side_effect=RuntimeError("boom"),
    ):
        fn(gen, p, token, cb)  # must not raise
    assert published == [("t1", "unreachable")]  # sanitized deterministic completion
    assert len(queued) == 2  # t2 is still promoted


def test_callback_failure_after_rejection_does_not_wedge() -> None:
    calls = {"n": 0}

    def flaky_submit(fn: Any, *args: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("executor closed")
        args[-1]("t2", _result(ctest.ConnectionState.CANCELLED))  # run inline

    coord = _ConnectionCoordinator(flaky_submit, threading.Event())
    profile = _profile(ProviderKind.LOCAL, base_url="http://127.0.0.1:9")

    def exploding(_t: Any, _r: Any) -> None:
        raise RuntimeError("publisher exploded")

    assert coord.request(profile, "t1", exploding) is False  # rejection completes, no raise
    assert coord.request(profile, "t2", lambda t, r: None) is True  # later accepted


def test_close_racing_request_submits_no_new_work() -> None:
    submit, queued = _capture_submit()
    event = threading.Event()
    event.set()
    coord = _ConnectionCoordinator(submit, event)
    profile = _profile(ProviderKind.LOCAL, base_url="http://127.0.0.1:9")
    assert coord.request(profile, "t1", lambda t, r: None) is False
    assert queued == []


def test_close_during_inflight_run_zero_keyring_zero_spawn_for_queued() -> None:
    submit, queued = _capture_submit()
    event = threading.Event()
    coord = _ConnectionCoordinator(submit, event)
    profile = _profile(ProviderKind.LOCAL, base_url="http://127.0.0.1:9")
    published: list[str] = []
    coord.request(profile, "t1", lambda t, r: published.append(r.state.value))
    coord.request(profile, "t2", lambda t, r: published.append(r.state.value))
    fn, gen, p, token, cb = queued[0]

    def close_during_publish(_t: Any, r: ctest.ConnectionResult) -> None:
        event.set()  # close races with the in-flight completion
        published.append(r.state.value)

    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=_result(ctest.ConnectionState.CONNECTED),
    ):
        fn(gen, p, token, close_during_publish)
    assert published == ["connected"]  # only t1; t2 never publishes
    fn2, gen2, p2, token2, cb2 = queued[1]  # t2 was promoted and dispatched
    runs = {"n": 0}
    with patch(
        "moira.provider_editor.run_connection_test",
        side_effect=lambda *a, **k: runs.__setitem__("n", runs["n"] + 1),
    ):
        fn2(gen2, p2, token2, cb2)
    assert runs["n"] == 0  # queued work after close: zero Keyring reads, zero spawn


def test_first_rejection_resets_row_to_translated_failure(
    env: tuple[Path, dict[str, Any]], idle_inline: None, english: None
) -> None:
    from moira.provider_editor import ProviderEditor

    def rejecting_submit(fn: Any, *args: Any) -> None:
        raise RuntimeError("executor closed")

    ed = ProviderEditor(submit=rejecting_submit)
    ed._profiles = (_profile(ProviderKind.LOCAL, base_url="http://127.0.0.1:9"),)
    ed._show_list()
    widgets = ed._row_widgets["local-main"]
    widgets["test"].emit("clicked")
    assert widgets["test_status"].get_text() == "Unreachable"  # translated, never stuck
    ed.shutdown()


def _find_connection_run(queued: list[tuple[Any, ...]], coord: Any) -> tuple[Any, ...]:
    for entry in queued:
        fn = entry[0]
        if getattr(fn, "__self__", None) is coord and fn.__name__ == "_run":
            return entry
    raise AssertionError("connection test never dispatched")


def test_promoted_rejection_resets_other_row(
    env: tuple[Path, dict[str, Any]], idle_inline: None, english: None
) -> None:
    from moira.provider_editor import ProviderEditor

    queued: list[tuple[Any, ...]] = []
    calls = {"n": 0}

    def flaky_submit(fn: Any, *args: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 3:  # reload=1, first test=2, promoted test=3 → rejected
            raise RuntimeError("executor closed")
        queued.append((fn, *args))

    ed = ProviderEditor(submit=flaky_submit)
    ed._profiles = (
        _profile(ProviderKind.LOCAL, slug="local-main", base_url="http://127.0.0.1:9"),
        _profile(ProviderKind.LOCAL, slug="local-second", base_url="http://127.0.0.1:9"),
    )
    ed._show_list()
    widgets_a = ed._row_widgets["local-main"]
    widgets_b = ed._row_widgets["local-second"]
    widgets_a["test"].emit("clicked")
    widgets_b["test"].emit("clicked")  # parked on its own row
    assert widgets_a["test_status"].get_text() == "Testing…"
    assert widgets_b["test_status"].get_text() == "Testing…"
    fn, gen, p, token, cb = _find_connection_run(queued, ed._connection_coordinator)
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=ctest.ConnectionResult(ctest.ConnectionState.CONNECTED, "local-main"),
    ):
        fn(gen, p, token, cb)
    assert widgets_a["test_status"].get_text() == "Connected"
    assert widgets_b["test_status"].get_text() == "Unreachable"  # never stuck on "Testing…"
    ed.shutdown()


# ── Finding 3: nonblocking bounded stdin delivery ───────────────────────────


def test_partial_reader_stdin_delivery_is_bounded_and_secret_free() -> None:
    """A child that reads one small chunk then stops: the remaining
    credential must never block the parent past the total bound, and the
    failure result retains no secret bytes."""
    secret = "sk-7l-secret-"
    child = [sys.executable, "-c", "import os, time; os.read(0, 8); time.sleep(1000)"]
    t0 = time.monotonic()
    result = run_bounded(child, timeout=0.5, stdin_data=(secret * 100_000).encode())
    elapsed = time.monotonic() - t0
    assert result is not None
    assert result.outcome is ProbeOutcome.TIMEOUT
    assert elapsed < 3.0  # the write stayed inside the deadline
    assert result.stdout == "" and result.stderr == ""
    assert secret not in result.stdout and secret not in result.stderr


def test_reduced_pipe_size_never_blocks_parent() -> None:
    """With a pipe smaller than the write chunk (when the platform
    supports F_SETPIPE_SZ), a non-reading child still cannot block the
    parent: partial writes and BlockingIOError keep the deadline."""
    fcntl = pytest.importorskip("fcntl")
    if not hasattr(fcntl, "F_SETPIPE_SZ"):
        pytest.skip("F_SETPIPE_SZ unsupported")
    child = [
        sys.executable,
        "-c",
        "import fcntl, time\n"
        "try:\n"
        "    fcntl.fcntl(0, fcntl.F_SETPIPE_SZ, 4096)\n"
        "except OSError:\n"
        "    pass\n"
        "time.sleep(1000)\n",
    ]
    t0 = time.monotonic()
    result = run_bounded(child, timeout=0.5, stdin_data=b"x" * 1_000_000)
    elapsed = time.monotonic() - t0
    assert result is not None and result.outcome is ProbeOutcome.TIMEOUT
    assert elapsed < 3.0


def test_child_closing_stdin_immediately_is_bounded() -> None:
    child = [sys.executable, "-c", "import sys, time; sys.stdin.close(); time.sleep(1000)"]
    t0 = time.monotonic()
    result = run_bounded(child, timeout=0.5, stdin_data=b"sk-7l" * 1000)
    elapsed = time.monotonic() - t0
    assert result is not None and result.outcome is ProbeOutcome.TIMEOUT
    assert elapsed < 3.0


def test_ignored_sigterm_with_pending_stdin_group_is_reaped() -> None:
    """The child ignores SIGTERM, spawns a group member and never reads
    stdin: the whole process group is SIGKILLed and reaped at the bound
    (no marker process survives)."""
    marker = "moira-7l-group-marker"
    child = [
        sys.executable,
        "-c",
        "import signal, subprocess, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(1000)', '{marker}'])\n"
        "time.sleep(1000)\n",
    ]
    t0 = time.monotonic()
    result = run_bounded(child, timeout=0.5, stdin_data=b"x" * 1_000_000)
    elapsed = time.monotonic() - t0
    assert result is not None and result.outcome is ProbeOutcome.TIMEOUT
    assert elapsed < 3.0  # SIGTERM-ignoring child still SIGKILLed + reaped
    deadline = time.monotonic() + 2.0
    probe = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True, check=False)
    while time.monotonic() < deadline:
        if probe.returncode != 0:
            break
        time.sleep(0.1)
        probe = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True, check=False)
    assert probe.returncode != 0  # group reaped: no member survives


def test_stdin_pipe_is_closed_on_every_terminal_path() -> None:
    """Repeated timed-out runs with pending stdin leak no pipe file
    descriptor: the write end is closed on every terminal path."""
    fd_dir = Path("/proc/self/fd")

    def count_fds() -> int:
        return len(list(fd_dir.iterdir()))

    child = [sys.executable, "-c", "import time; time.sleep(1000)"]
    baseline = count_fds()
    for _ in range(5):
        result = run_bounded(child, timeout=0.4, stdin_data=b"sk-7l-" * 100_000)
        assert result is not None and result.outcome is ProbeOutcome.TIMEOUT
    assert count_fds() <= baseline  # no pipe descriptor leaked


def test_stdin_pipe_is_nonblocking_during_the_run() -> None:
    """Criterion 6: stdin is put in NONBLOCKING mode before the selector
    loop — observable via /proc/self/fdinfo: exactly one pipe write end
    carries O_NONBLOCK while the child runs (the stdin delivery pipe)."""
    fd_dir = Path("/proc/self/fd")
    info_dir = Path("/proc/self/fdinfo")
    child = [sys.executable, "-c", "import time; time.sleep(1.5)"]
    observed: dict[str, list[int]] = {"fds": []}

    def scan() -> None:
        time.sleep(0.4)
        for entry in fd_dir.iterdir():
            try:
                fd = int(entry.name)
            except ValueError:
                continue
            try:
                lines = (info_dir / str(fd)).read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            flags = [line for line in lines if line.startswith("flags:")]
            if not flags:
                continue
            value = int(flags[0].split()[1], 8)
            if value & 0o4000 and value & 0o1:  # O_NONBLOCK on a write end
                observed["fds"].append(fd)

    thread = threading.Thread(target=scan)
    thread.start()
    try:
        # A payload larger than the pipe keeps the stdin pipe open (full,
        # nonblocking) until the deadline, so the scan observes it.
        result = run_bounded(child, timeout=1.0, stdin_data=b"sk-7l" * 100_000)
    finally:
        thread.join()
    assert result is not None and result.outcome is ProbeOutcome.TIMEOUT
    assert len(observed["fds"]) == 1  # the stdin pipe, nonblocking


def test_full_small_pipe_never_blocks_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stdin pipe smaller than the write chunk (reduced pipe size, when
    supported) with a never-reading child: the first 8192-byte write
    cannot fit in a 4096-byte blocking pipe — the parent must never block
    past the total bound (partial writes + BlockingIOError)."""
    fcntl = pytest.importorskip("fcntl")
    if not hasattr(fcntl, "F_SETPIPE_SZ"):
        pytest.skip("F_SETPIPE_SZ unsupported")
    real_pipe = os.pipe

    def small_pipe() -> tuple[int, int]:
        read_fd, write_fd = real_pipe()
        try:
            fcntl.fcntl(write_fd, fcntl.F_SETPIPE_SZ, 4096)
        except OSError:
            os.close(read_fd)
            os.close(write_fd)
            raise
        return read_fd, write_fd

    monkeypatch.setattr(os, "pipe", small_pipe)
    child = [sys.executable, "-c", "import time; time.sleep(1000)"]  # never reads stdin
    t0 = time.monotonic()
    result = run_bounded(child, timeout=0.5, stdin_data=b"x" * 1_000_000)
    elapsed = time.monotonic() - t0
    assert result is not None and result.outcome is ProbeOutcome.TIMEOUT
    assert elapsed < 3.0  # the blocking write never escapes the deadline
