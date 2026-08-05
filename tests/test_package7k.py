"""Package 7k — harden provider connection tests (ACCEPTANCE_CORRECTION).

RED tests on 6904582 for the five blocking findings:

1. DNS TOCTOU: the policy check resolves once, then HTTP(S)Connection
   resolves AGAIN — rebinding can pass the public check and connect to a
   private address.
2. Keyring is read before classification: credential-less ``custom``
   becomes NOT_CONFIGURED instead of UNSUPPORTED; missing model or
   missing compatible/local base URL map incorrectly.
3. The JSON parser skips malformed ``data`` elements: a mixed response
   containing the model plus invalid entries can become CONNECTED.
4. ``run_bounded`` writes ``stdin_data`` synchronously before the
   deadline — a large credential or a child that never reads stdin can
   block outside the total bound.
5. ``_ConnectionCoordinator`` does not catch submit failures (``_inflight``
   latches) and queued work may still touch Keyring or spawn after close.
"""

from __future__ import annotations

import json
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
from moira.integrations import ProviderKind, ProviderProfile, run_bounded
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
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def port(self) -> int:
        return int(self.httpd.server_address[1])

    def configure(
        self, *, status: int = 200, body: bytes | None = None, delay: float = 0.0
    ) -> None:
        self.httpd.status = status
        self.httpd.body = body if body is not None else self.httpd.body
        self.httpd.delay = delay

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
    return _profile(ProviderKind.LOCAL, slug="local-main", base_url=server.url, model=model)


# ── Finding 1: DNS pinning (single validated resolution) ────────────────────


def test_resolve_target_rejects_mixed_public_private(monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve_target performs EXACTLY ONE resolution and returns the
    validated target — a rebinding second resolution never happens, so
    the first (public) result can never be followed by a private one."""
    public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
    private = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443))]
    calls = {"n": 0}

    def rebinding_getaddrinfo(*args: Any, **kwargs: Any) -> list[Any]:
        calls["n"] += 1
        return public if calls["n"] == 1 else private

    monkeypatch.setattr(socket, "getaddrinfo", rebinding_getaddrinfo)
    target = ctest.resolve_target("api.example.com", 443, "remote")
    assert target is not None and target.sockaddr[0] == "8.8.8.8"
    assert calls["n"] == 1  # exactly one resolution — no TOCTOU window


def test_resolve_target_rejects_mixed_ipv4_ipv6(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mixed families: a public IPv4 + private IPv6 resolution is refused
    (the ANY-non-public rule), and a loopback-only resolution passes the
    local policy with a single chosen target."""
    mixed = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fd00::1", 443)),
    ]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: mixed)
    assert ctest.resolve_target("api.example.com", 443, "remote") is None

    loopback = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80)),
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 80)),
    ]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: loopback)
    target = ctest.resolve_target("localhost", 80, "local")
    assert target is not None and target.sockaddr[0] == "127.0.0.1"


def test_resolve_target_public_only_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: public)
    target = ctest.resolve_target("api.example.com", 443, "remote")
    assert target is not None and target.sockaddr[0] == "8.8.8.8"


def test_resolve_target_unresolvable_or_private_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(OSError("nxdomain"))
    )
    assert ctest.resolve_target("nope.invalid", 443, "remote") is None
    private = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.5", 443))]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: private)
    assert ctest.resolve_target("host.lan", 443, "remote") is None


def test_private_server_receives_nothing_after_rebinding(server: Any) -> None:
    """End to end: the child resolves ONCE and connects ONLY to the
    validated address; a private target is refused before any request."""
    host = "127.0.0.1"
    port = server.port
    argv = [
        sys.executable,
        "-c",
        ctest._CHILD_CODE,
        "openai_compatible",
        f"https://{host}:{port}/models",
        "deepseek-chat",
        "1.0",
        "1.0",
        "5.0",
        "1000",
        "bearer",
        "remote",
    ]
    result = subprocess.run(argv, input=b"sk-7k\n", capture_output=True, check=False)
    assert result.returncode == 4  # UNREACHABLE: refused by address policy
    assert server.requests == []


def test_peer_mismatch_is_tls_error(tmp_path: Path, server: Any) -> None:
    """A trusted-but-wrong-host certificate is refused by hostname
    verification: the peer is verified BEFORE any credential leaves the
    child (TLS_ERROR, distinct from transport failures)."""
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    made = subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=wrong-host.example",
            "-addext",
            "subjectAltName=DNS:wrong-host.example",
        ],
        capture_output=True,
        check=False,
    )
    if made.returncode != 0:
        pytest.skip("openssl unavailable")
    context = __import__("ssl").SSLContext(__import__("ssl").PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert), str(key))
    server.httpd.socket = context.wrap_socket(server.httpd.socket, server_side=True)
    profile = _profile(
        ProviderKind.LOCAL,
        slug="peer",
        base_url=f"https://127.0.0.1:{server.port}",
        model="deepseek-chat",
    )
    os.environ["SSL_CERT_FILE"] = str(cert)
    try:
        result = ctest.bounded_connection_test(profile, "sk-7k")
    finally:
        os.environ.pop("SSL_CERT_FILE", None)
    assert result.state is ctest.ConnectionState.TLS_ERROR
    assert server.requests == []  # the credential never left the child


# ── Finding 2: preflight before Keyring or spawn ────────────────────────────


def _spy_run_bounded(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    calls = {"n": 0}
    monkeypatch.setattr(
        ctest, "run_bounded", lambda *a, **k: calls.__setitem__("n", calls["n"] + 1)
    )
    return calls


def test_custom_unsupported_regardless_of_credential(
    env: tuple[Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    env[1]["items"][("custom-main", "api_key")] = "sk-7k"
    calls = _spy_run_bounded(monkeypatch)
    result = ctest.run_connection_test(_profile(ProviderKind.CUSTOM, slug="custom-main"))
    assert result.state is ctest.ConnectionState.UNSUPPORTED
    assert calls["n"] == 0 and env[1]["lookups"] == 0  # no Keyring read, no spawn


def test_missing_model_is_not_configured(
    env: tuple[Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _spy_run_bounded(monkeypatch)
    result = ctest.run_connection_test(
        _profile(ProviderKind.LOCAL, base_url="http://127.0.0.1:9", model="")
    )
    assert result.state is ctest.ConnectionState.NOT_CONFIGURED
    assert calls["n"] == 0 and env[1]["lookups"] == 0


def test_missing_base_url_is_not_configured(
    env: tuple[Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    for kind in (ProviderKind.LOCAL, ProviderKind.OPENAI_COMPATIBLE):
        calls = _spy_run_bounded(monkeypatch)
        result = ctest.run_connection_test(_profile(kind))  # no base_url
        assert result.state is ctest.ConnectionState.NOT_CONFIGURED
        assert calls["n"] == 0 and env[1]["lookups"] == 0


def test_preset_cancellation_is_cancelled_before_anything(
    env: tuple[Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    event = threading.Event()
    event.set()
    calls = _spy_run_bounded(monkeypatch)
    result = ctest.run_connection_test(
        _profile(ProviderKind.LOCAL, base_url="http://127.0.0.1:9"), cancel_event=event
    )
    assert result.state is ctest.ConnectionState.CANCELLED
    assert calls["n"] == 0 and env[1]["lookups"] == 0


def test_oversized_key_fails_closed_before_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _spy_run_bounded(monkeypatch)
    result = ctest.bounded_connection_test(
        _profile(ProviderKind.LOCAL, base_url="http://127.0.0.1:9"), "x" * (ctest.MAX_KEY_BYTES + 1)
    )
    assert result.state is ctest.ConnectionState.INVALID_RESPONSE
    assert calls["n"] == 0


# ── Finding 3: strict Models JSON ───────────────────────────────────────────


def _json_body(items: list[Any], extra_top: dict[str, Any] | None = None) -> bytes:
    payload: dict[str, Any] = {"data": items}
    if extra_top:
        payload.update(extra_top)
    return json.dumps(payload).encode()


def test_mixed_valid_and_malformed_items_is_invalid(server: Any) -> None:
    server.configure(body=_json_body([{"id": "deepseek-chat"}, {"id": 123}]))
    result = ctest.bounded_connection_test(_local_profile(server), "sk-7k")
    assert result.state is ctest.ConnectionState.INVALID_RESPONSE


def test_non_dict_item_is_invalid(server: Any) -> None:
    server.configure(body=_json_body(["deepseek-chat"]))
    result = ctest.bounded_connection_test(_local_profile(server), "sk-7k")
    assert result.state is ctest.ConnectionState.INVALID_RESPONSE


def test_duplicate_model_id_is_invalid(server: Any) -> None:
    server.configure(body=_json_body([{"id": "deepseek-chat"}, {"id": "deepseek-chat"}]))
    result = ctest.bounded_connection_test(_local_profile(server), "sk-7k")
    assert result.state is ctest.ConnectionState.INVALID_RESPONSE


def test_excessive_model_count_is_invalid(server: Any) -> None:
    items = [{"id": f"m-{i}"} for i in range(ctest.MAX_MODELS + 1)]
    server.configure(body=_json_body(items))
    result = ctest.bounded_connection_test(_local_profile(server), "sk-7k")
    assert result.state is ctest.ConnectionState.INVALID_RESPONSE


def test_secret_bearing_item_is_invalid(server: Any) -> None:
    server.configure(body=_json_body([{"id": "deepseek-chat", "api_key": "sk-leak"}]))
    result = ctest.bounded_connection_test(_local_profile(server), "sk-7k")
    assert result.state is ctest.ConnectionState.INVALID_RESPONSE


def test_secret_bearing_top_level_is_invalid(server: Any) -> None:
    server.configure(body=_json_body([{"id": "deepseek-chat"}], extra_top={"account_id": "a-1"}))
    result = ctest.bounded_connection_test(_local_profile(server), "sk-7k")
    assert result.state is ctest.ConnectionState.INVALID_RESPONSE


def test_control_characters_in_id_are_invalid(server: Any) -> None:
    server.configure(body=_json_body([{"id": "deepseek-chat\n"}]))
    result = ctest.bounded_connection_test(_local_profile(server), "sk-7k")
    assert result.state is ctest.ConnectionState.INVALID_RESPONSE


def test_fully_valid_response_still_connected(server: Any) -> None:
    server.configure(
        body=_json_body(
            [
                {"id": "other-model"},
                {"id": "deepseek-chat", "object": "model", "created": 1, "owned_by": "ds"},
            ]
        )
    )
    result = ctest.bounded_connection_test(_local_profile(server), "sk-7k")
    assert result.state is ctest.ConnectionState.CONNECTED


# ── Finding 4: bounded stdin delivery ───────────────────────────────────────


def test_stdin_write_is_inside_the_total_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    """A child that never reads stdin with a credential larger than the
    pipe buffer must be terminated and reaped at the total bound — the
    write happens inside the deadline, never before it."""
    from moira.integrations import ProbeOutcome

    child = [sys.executable, "-c", "import time; time.sleep(1000)"]  # never reads stdin
    t0 = time.monotonic()
    result = run_bounded(child, timeout=0.5, stdin_data=b"x" * 1_000_000)
    elapsed = time.monotonic() - t0
    assert result is not None
    assert result.outcome is ProbeOutcome.TIMEOUT
    assert elapsed < 3.0


def test_child_closing_stdin_without_reading_does_not_hang() -> None:
    child = [sys.executable, "-c", "import sys; sys.exit(7)"]
    result = run_bounded(child, timeout=2.0, stdin_data=b"sk-7k" * 100)
    assert result is not None and result.returncode == 7


# ── Finding 5: coordinator races ────────────────────────────────────────────


def _capture_submit() -> tuple[Any, list[tuple[Any, ...]]]:
    queued: list[tuple[Any, ...]] = []

    def submit(fn: Any, *args: Any) -> None:
        queued.append((fn, *args))

    return submit, queued


def _result(state: ctest.ConnectionState) -> ctest.ConnectionResult:
    return ctest.ConnectionResult(state, "local-main")


def test_submit_rejection_clears_slot_and_accepts_later() -> None:
    queued: list[tuple[Any, ...]] = []
    rejected = {"first": True}

    def flaky_submit(fn: Any, *args: Any) -> None:
        if rejected["first"]:
            rejected["first"] = False
            raise RuntimeError("executor closed")
        queued.append((fn, *args))

    coord = _ConnectionCoordinator(flaky_submit, threading.Event())
    profile = _profile(ProviderKind.LOCAL, base_url="http://127.0.0.1:9")
    assert coord.request(profile, "t1", lambda t, r: None) is False  # rejected
    assert coord.request(profile, "t2", lambda t, r: None) is True  # later accepted
    assert len(queued) == 1


def test_promotion_submit_rejection_clears_slot() -> None:
    queued: list[tuple[Any, ...]] = []
    reject_promotion = {"n": 0}

    def flaky_submit(fn: Any, *args: Any) -> None:
        if reject_promotion["n"] > 0:
            reject_promotion["n"] = 0  # one-shot rejection
            raise RuntimeError("executor closed")  # rejected: never dispatched
        queued.append((fn, *args))

    coord = _ConnectionCoordinator(flaky_submit, threading.Event())
    profile = _profile(ProviderKind.LOCAL, base_url="http://127.0.0.1:9")
    coord.request(profile, "t1", lambda t, r: None)
    coord.request(profile, "t2", lambda t, r: None)  # parked
    fn, gen, p, token, cb = queued[0]
    reject_promotion["n"] = 1  # the promotion submit fails
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=_result(ctest.ConnectionState.CONNECTED),
    ):
        fn(gen, p, token, cb)
    assert len(queued) == 1  # the parked t2 was NOT dispatched
    assert coord.request(profile, "t3", lambda t, r: None) is True  # slot cleared


def test_callback_failure_does_not_wedge_coordinator() -> None:
    submit, queued = _capture_submit()
    coord = _ConnectionCoordinator(submit, threading.Event())
    profile = _profile(ProviderKind.LOCAL, base_url="http://127.0.0.1:9")

    def exploding(_t: Any, _r: Any) -> None:
        raise RuntimeError("publisher exploded")

    coord.request(profile, "t1", exploding)
    fn, gen, p, token, cb = queued[0]
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=_result(ctest.ConnectionState.CONNECTED),
    ):
        fn(gen, p, token, cb)  # must not raise
    assert coord.request(profile, "t2", lambda t, r: None) is True  # still functional


def test_close_before_worker_start_performs_zero_keyring_spawn(
    env: tuple[Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    submit, queued = _capture_submit()
    shutdown_event = threading.Event()
    coord = _ConnectionCoordinator(submit, shutdown_event)
    profile = _profile(ProviderKind.LOCAL, base_url="http://127.0.0.1:9")
    calls = {"n": 0}
    monkeypatch.setattr(
        "moira.provider_editor.run_connection_test",
        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1),
    )
    coord.request(profile, "t1", lambda t, r: None)
    shutdown_event.set()  # close before the worker starts
    coord.cancel()
    fn, gen, p, token, cb = queued[0]
    fn(gen, p, token, cb)
    assert calls["n"] == 0  # zero Keyring reads, zero spawn


def test_close_during_run_discards_result() -> None:
    submit, queued = _capture_submit()
    coord = _ConnectionCoordinator(submit, threading.Event())
    profile = _profile(ProviderKind.LOCAL, base_url="http://127.0.0.1:9")
    published: list[str] = []
    coord.request(profile, "t1", lambda t, r: published.append(r.state.value))
    fn, gen, p, token, cb = queued[0]
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=_result(ctest.ConnectionState.CONNECTED),
    ):
        fn(gen, p, token, cb)  # completes BEFORE close
    assert published == ["connected"]
    coord.request(profile, "t2", lambda t, r: published.append(r.state.value))
    fn2, gen2, p2, token2, cb2 = queued[1]
    coord.cancel()  # close during the second run
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=_result(ctest.ConnectionState.AUTH_FAILED),
    ):
        fn2(gen2, p2, token2, cb2)
    assert published == ["connected"]  # the second result never published


def test_rapid_retest_preserves_one_inflight_plus_newest_pending() -> None:
    submit, queued = _capture_submit()
    coord = _ConnectionCoordinator(submit, threading.Event())
    profile = _profile(ProviderKind.LOCAL, base_url="http://127.0.0.1:9")
    published: list[str] = []
    for tag in ("a", "b", "c", "d"):
        coord.request(profile, tag, lambda t, r: published.append(f"{t}:{r.state.value}"))
    assert len(queued) == 1  # only the first dispatched; the rest park (newest wins)
    fn, gen, p, token, cb = queued[0]
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=_result(ctest.ConnectionState.CONNECTED),
    ):
        fn(gen, p, token, cb)
    assert len(queued) == 2  # only the newest parked (d) is promoted
    assert published == ["a:connected"]
    fn2, gen2, p2, token2, cb2 = queued[1]
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=_result(ctest.ConnectionState.RATE_LIMITED),
    ):
        fn2(gen2, p2, token2, cb2)
    assert published == ["a:connected", "d:rate_limited"]


def test_click_rejection_resets_row_status(
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
    # The deterministic rejection completion resets the row to the
    # translated sanitized failure — never stuck on "Testing…".
    assert widgets["test_status"].get_text() == "Unreachable"
    ed.shutdown()
