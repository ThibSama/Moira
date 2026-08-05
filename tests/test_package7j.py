"""Package 7j — bounded provider connection tests (FEATURE_IMPLEMENTATION).

RED tests on 42cae53: the connection-test module, the per-row button and
the coordinator do not exist yet. Mandatory tests use a local fake HTTP
server on loopback (no accounts, no public network): every state, model
match/mismatch, malformed/oversized JSON, timeout, ignored SIGTERM,
redirect, TLS failure, private-address rejection, proxy suppression,
Keyring failure, secret non-leakage, races and close/removal during test.
"""

from __future__ import annotations

import os
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
from moira.integrations import ProviderKind, ProviderProfile
from moira.persistence import load_settings, save_settings
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
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Any]:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    fake: dict[str, Any] = {"items": {}, "unavailable": False}

    def lookup(_schema: Any, attributes: dict[str, str], _cancellable: Any) -> str | None:
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


def _run_child(url: str, *, policy: str = "local", kind: str = "local") -> int:
    """Run the dedicated child directly (bypassing profile validation)
    for policy-level tests."""
    argv = [
        sys.executable,
        "-c",
        ctest._CHILD_CODE,
        kind,
        url,
        "deepseek-chat",
        "1.0",
        "1.0",
        "5.0",
        "1000",
        "bearer",
        policy,
    ]
    result = subprocess.run(argv, input=b"sk-7j-policy-test\n", capture_output=True, check=False)
    return result.returncode


# ── Criterion 1: closed state set ───────────────────────────────────────────


def test_state_set_is_closed() -> None:
    states = {s.value for s in ctest.ConnectionState}
    assert states == {
        "connected",
        "not_configured",
        "auth_failed",
        "model_not_found",
        "unreachable",
        "tls_error",
        "rate_limited",
        "invalid_response",
        "unsupported",
        "cancelled",
    }


# ── Criterion 2: closed adapter registry by kind ────────────────────────────


def test_endpoint_registry() -> None:
    assert ctest.endpoint_url(ProviderKind.DEEPSEEK, "") == "https://api.deepseek.com/models"
    assert ctest.endpoint_url(ProviderKind.OPENAI, "") == "https://api.openai.com/v1/models"
    assert ctest.endpoint_url(ProviderKind.OPENROUTER, "") == "https://openrouter.ai/api/v1/models"
    assert ctest.endpoint_url(ProviderKind.ANTHROPIC, "") == "https://api.anthropic.com/v1/models"
    assert (
        ctest.endpoint_url(ProviderKind.OPENAI_COMPATIBLE, "https://host/v1")
        == "https://host/v1/models"
    )
    assert (
        ctest.endpoint_url(ProviderKind.LOCAL, "http://127.0.0.1:8080")
        == "http://127.0.0.1:8080/models"
    )
    assert ctest.endpoint_url(ProviderKind.CUSTOM, "") is None
    assert ctest.endpoint_url(ProviderKind.OPENAI_COMPATIBLE, "") is None


def test_anthropic_adapter_uses_x_api_key_header() -> None:
    adapter = ctest.adapter_for(ProviderKind.ANTHROPIC)
    assert adapter is not None and adapter.bearer is False
    assert adapter.extra_headers == (("anthropic-version", "2023-06-01"),)


def test_custom_kind_unsupported_without_spawn(
    env: tuple[Path, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned = {"n": 0}
    monkeypatch.setattr(
        ctest, "run_bounded", lambda *a, **k: spawned.__setitem__("n", spawned["n"] + 1)
    )
    result = ctest.bounded_connection_test(_profile(ProviderKind.CUSTOM), "sk-x")
    assert result.state is ctest.ConnectionState.UNSUPPORTED
    assert spawned["n"] == 0


# ── Criterion 8: deterministic HTTP mapping (fakes only) ────────────────────


def _local_profile(server: Any, model: str = "deepseek-chat") -> ProviderProfile:
    return _profile(ProviderKind.LOCAL, slug="local-main", base_url=server.url, model=model)


def test_connected_on_2xx_with_model(server: Any) -> None:
    profile = _local_profile(server)
    result = ctest.bounded_connection_test(profile, "sk-7j")
    assert result.state is ctest.ConnectionState.CONNECTED
    assert result.connected
    assert result.profile_slug == "local-main"


def test_model_mismatch_is_model_not_found(server: Any) -> None:
    profile = _local_profile(server, model="ghost-model")
    result = ctest.bounded_connection_test(profile, "sk-7j")
    assert result.state is ctest.ConnectionState.MODEL_NOT_FOUND


def test_401_and_403_are_auth_failed(server: Any) -> None:
    for status in (401, 403):
        server.configure(status=status, body=b'{"error": "denied"}')
        result = ctest.bounded_connection_test(_local_profile(server), "sk-7j")
        assert result.state is ctest.ConnectionState.AUTH_FAILED


def test_404_is_model_not_found(server: Any) -> None:
    server.configure(status=404, body=b'{"error": "not found"}')
    result = ctest.bounded_connection_test(_local_profile(server), "sk-7j")
    assert result.state is ctest.ConnectionState.MODEL_NOT_FOUND


def test_429_is_rate_limited(server: Any) -> None:
    server.configure(status=429, body=b'{"error": "slow down"}')
    result = ctest.bounded_connection_test(_local_profile(server), "sk-7j")
    assert result.state is ctest.ConnectionState.RATE_LIMITED


def test_5xx_is_invalid_response(server: Any) -> None:
    server.configure(status=500, body=b'{"error": "boom"}')
    result = ctest.bounded_connection_test(_local_profile(server), "sk-7j")
    assert result.state is ctest.ConnectionState.INVALID_RESPONSE


def test_redirect_rejected_without_following(server: Any) -> None:
    server.configure(status=302, body=b"")
    result = ctest.bounded_connection_test(_local_profile(server), "sk-7j")
    assert result.state is ctest.ConnectionState.INVALID_RESPONSE
    assert len(server.requests) == 1  # never followed


def test_malformed_json_is_invalid_response(server: Any) -> None:
    server.configure(body=b"<html>not json</html>")
    result = ctest.bounded_connection_test(_local_profile(server), "sk-7j")
    assert result.state is ctest.ConnectionState.INVALID_RESPONSE


def test_wrong_shape_is_invalid_response(server: Any) -> None:
    server.configure(body=b'{"data": "not-a-list"}')
    result = ctest.bounded_connection_test(_local_profile(server), "sk-7j")
    assert result.state is ctest.ConnectionState.INVALID_RESPONSE


def test_non_string_ids_never_match(server: Any) -> None:
    """A malformed item (non-string id) makes the WHOLE response
    invalid — a mixed response can never become CONNECTED."""
    server.configure(body=b'{"data": [{"id": 123}]}')
    result = ctest.bounded_connection_test(_local_profile(server, model="123"), "sk-7j")
    assert result.state is ctest.ConnectionState.INVALID_RESPONSE


def test_oversized_body_is_invalid_response(server: Any) -> None:
    server.configure(body=b'{"data": [{"id": "' + b"x" * 2000 + b'"}]}')
    result = ctest.bounded_connection_test(_local_profile(server), "sk-7j", body_cap=64)
    assert result.state is ctest.ConnectionState.INVALID_RESPONSE


def test_unknown_outcome_never_connected(
    env: tuple[Path, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from moira.integrations import BoundedResult, ProbeOutcome

    monkeypatch.setattr(
        ctest,
        "run_bounded",
        lambda *a, **k: BoundedResult("", "", 99, ProbeOutcome.OK),  # unknown exit code
    )
    result = ctest.bounded_connection_test(
        _profile(ProviderKind.LOCAL, base_url="http://127.0.0.1:9"), "sk-7j"
    )
    assert result.state is ctest.ConnectionState.INVALID_RESPONSE


# ── Criterion 5/6/12: bounds, TLS, policy, proxies ──────────────────────────


def test_hanging_server_is_bounded_unreachable(server: Any) -> None:
    server.configure(delay=30.0)
    t0 = time.monotonic()
    result = ctest.bounded_connection_test(
        _local_profile(server), "sk-7j", read_timeout=0.4, total_timeout=5.0
    )
    elapsed = time.monotonic() - t0
    assert result.state is ctest.ConnectionState.UNREACHABLE
    assert elapsed < 3.0  # bounded, not forever


def test_tls_error_is_distinct(server: Any) -> None:
    """https:// against a plain-HTTP loopback server: the TLS handshake
    fails → TLS_ERROR (transport and TLS failures stay distinct)."""
    host, port = server.httpd.server_address
    profile = _profile(
        ProviderKind.LOCAL,
        slug="tls-main",
        base_url=f"https://{host}:{port}",
        model="deepseek-chat",
    )
    result = ctest.bounded_connection_test(profile, "sk-7j")
    assert result.state is ctest.ConnectionState.TLS_ERROR


def test_connection_refused_is_unreachable(server: Any) -> None:
    profile = _profile(ProviderKind.LOCAL, slug="refused", base_url="http://127.0.0.1:9", model="m")
    result = ctest.bounded_connection_test(profile, "sk-7j")
    assert result.state is ctest.ConnectionState.UNREACHABLE


def test_remote_private_address_rejected_before_connect(server: Any) -> None:
    """The child's resolved-address policy rejects loopback/private targets
    for remote kinds BEFORE any connect: the server sees nothing."""
    host, port = server.httpd.server_address[:2]
    result = _run_child(f"https://{host}:{port}/models", policy="remote", kind="openai_compatible")
    assert result == 4  # UNREACHABLE: refused by address policy
    assert server.requests == []


def test_local_policy_rejects_non_loopback(server: Any) -> None:
    # 192.0.2.1 is TEST-NET-1 (reserved, non-loopback): the local policy
    # must refuse it before any connect.
    result = _run_child("http://192.0.2.1/models")
    assert result == 4


def test_proxy_environment_ignored(server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    result = ctest.bounded_connection_test(_local_profile(server), "sk-7j")
    assert result.state is ctest.ConnectionState.CONNECTED  # reached the server directly
    assert len(server.requests) == 1


# ── Criterion 4/11: key transport and Keyring timing ────────────────────────


def test_key_reaches_child_only_via_stdin(
    env: tuple[Path, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def capture(args: list[str], **kwargs: Any) -> Any:
        captured["args"] = args
        captured["stdin_data"] = kwargs.get("stdin_data")
        return None

    monkeypatch.setattr(ctest, "run_bounded", capture)
    result = ctest.bounded_connection_test(
        _profile(ProviderKind.LOCAL, base_url="http://127.0.0.1:9"), "sk-7j-very-secret"
    )
    assert result.state is ctest.ConnectionState.UNREACHABLE  # spawn "failed" -> mapped
    joined = "\n".join(captured["args"])
    assert "sk-7j-very-secret" not in joined  # never argv
    assert captured["stdin_data"] == b"sk-7j-very-secret\n"  # private stdin pipe only


def test_key_flows_end_to_end_but_never_to_disk(env: tuple[Path, Any], server: Any) -> None:
    secret = "sk-7j-end-to-end"
    result = ctest.bounded_connection_test(_local_profile(server), secret)
    assert result.state is ctest.ConnectionState.CONNECTED
    assert server.requests[0][1].get("Authorization") == f"Bearer {secret}"  # reached via the child
    xdg = Path(os.environ["XDG_CONFIG_HOME"]), Path(os.environ["XDG_STATE_HOME"])
    for root in xdg:
        for path in root.rglob("*"):
            if path.is_file():
                assert secret not in path.read_text(encoding="utf-8", errors="replace")


def test_missing_credential_fails_before_spawn(
    env: tuple[Path, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned = {"n": 0}
    monkeypatch.setattr(
        ctest, "run_bounded", lambda *a, **k: spawned.__setitem__("n", spawned["n"] + 1)
    )
    result = ctest.run_connection_test(_profile(ProviderKind.LOCAL))
    assert result.state is ctest.ConnectionState.NOT_CONFIGURED
    assert spawned["n"] == 0  # no network, no spawn


def test_unavailable_keyring_fails_before_spawn(
    env: tuple[Path, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    env[1]["unavailable"] = True
    spawned = {"n": 0}
    monkeypatch.setattr(
        ctest, "run_bounded", lambda *a, **k: spawned.__setitem__("n", spawned["n"] + 1)
    )
    result = ctest.run_connection_test(_profile(ProviderKind.LOCAL))
    assert result.state is ctest.ConnectionState.NOT_CONFIGURED
    assert spawned["n"] == 0


def test_keyring_supplies_credential_immediately_before_test(
    env: tuple[Path, Any], server: Any
) -> None:
    env[1]["items"][("local-main", "api_key")] = "sk-7j-from-keyring"
    profile = _local_profile(server)
    result = ctest.run_connection_test(profile)
    assert result.state is ctest.ConnectionState.CONNECTED
    assert server.requests[0][1].get("Authorization") == "Bearer sk-7j-from-keyring"


def test_cancelled_before_spawn(env: tuple[Path, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    spawned = {"n": 0}
    monkeypatch.setattr(
        ctest, "run_bounded", lambda *a, **k: spawned.__setitem__("n", spawned["n"] + 1)
    )
    event = threading.Event()
    event.set()
    result = ctest.bounded_connection_test(_profile(ProviderKind.LOCAL), "sk-x", cancel_event=event)
    assert result.state is ctest.ConnectionState.CANCELLED
    assert spawned["n"] == 0


# ── Criterion 5: ignored SIGTERM still reaped (run_bounded boundary) ────────


def test_sigterm_ignoring_child_is_killed_and_reaped(monkeypatch: pytest.MonkeyPatch) -> None:
    from moira.integrations import ProbeOutcome, run_bounded

    child = [
        sys.executable,
        "-c",
        "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(1000)",
    ]
    t0 = time.monotonic()
    result = run_bounded(child, timeout=0.5)
    elapsed = time.monotonic() - t0
    assert result is not None and result.outcome is ProbeOutcome.TIMEOUT
    assert elapsed < 3.0  # SIGKILL escalation reaped it


# ── Criterion 10: results are ephemeral ─────────────────────────────────────


def test_results_are_ephemeral(env: tuple[Path, Any], server: Any) -> None:
    save_settings(type(load_settings())(provider_profiles=(_local_profile(server),)))
    before = load_settings()
    result = ctest.bounded_connection_test(_local_profile(server), "sk-7j")
    assert result.state is ctest.ConnectionState.CONNECTED
    assert load_settings() == before  # no schema, no History, no config write
    assert not (Path(os.environ["XDG_STATE_HOME"]) / "moira" / "profile-tx.json").exists()


# ── Criterion 9: coordinator races (one in-flight + newest pending) ──────────


def _capture_submit() -> tuple[Any, list[tuple[Any, ...]]]:
    queued: list[tuple[Any, ...]] = []

    def submit(fn: Any, *args: Any) -> None:
        queued.append((fn, *args))

    return submit, queued


def test_coordinator_newest_pending_wins() -> None:
    submit, queued = _capture_submit()
    coord = _ConnectionCoordinator(submit, threading.Event())
    published: list[str] = []
    profile = _profile(ProviderKind.LOCAL)
    coord.request(profile, "t1", lambda t, r: published.append(f"{t}:{r.state.value}"))
    coord.request(profile, "t2", lambda t, r: published.append(f"{t}:{r.state.value}"))
    coord.request(
        profile, "t3", lambda t, r: published.append(f"{t}:{r.state.value}")
    )  # replaces t2
    assert published == ["t2:cancelled"]  # t2 terminates as CANCELLED — never silent
    assert len(queued) == 1  # only the first is dispatched
    fn, gen, p, token, cb = queued[0]
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=ctest.ConnectionResult(ctest.ConnectionState.CONNECTED),
    ):
        fn(gen, p, token, cb)  # completes → publishes t1 AND dispatches the parked t3
    assert len(queued) == 2
    assert published == ["t2:cancelled", "t1:connected"]
    fn2, gen2, p2, token2, cb2 = queued[1]
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=ctest.ConnectionResult(ctest.ConnectionState.AUTH_FAILED),
    ):
        fn2(gen2, p2, token2, cb2)
    assert published == ["t2:cancelled", "t1:connected", "t3:auth_failed"]  # t2 already terminated


def test_coordinator_cancel_discards_everything() -> None:
    submit, queued = _capture_submit()
    coord = _ConnectionCoordinator(submit, threading.Event())
    published: list[str] = []
    profile = _profile(ProviderKind.LOCAL)
    coord.request(profile, "t1", lambda t, r: published.append("publish"))
    coord.request(profile, "t2", lambda t, r: published.append("publish"))
    coord.cancel()
    fn, gen, p, token, cb = queued[0]
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=ctest.ConnectionResult(ctest.ConnectionState.CONNECTED),
    ):
        fn(gen, p, token, cb)
    assert published == []  # never published after cancel
    assert len(queued) == 1  # parked t2 never dispatched


def test_coordinator_shutdown_rejects_new_requests() -> None:
    event = threading.Event()
    event.set()
    coord = _ConnectionCoordinator(lambda *a: None, event)
    assert coord.request(_profile(ProviderKind.LOCAL), "t", lambda t, r: None) is False


# ── Editor-level: button, publication, discard on rebuild/remove/close ──────


def _open_editor(
    env: tuple[Path, Any],
    *,
    submit: Any = None,
    profiles: tuple[ProviderProfile, ...] = (),
) -> Any:
    from moira.provider_editor import ProviderEditor

    submit = submit if submit is not None else lambda fn, *a: fn(*a)
    ed = ProviderEditor(submit=submit)
    ed._profiles = profiles
    ed._show_list()
    return ed


@pytest.fixture
def idle_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(GLib, "idle_add", lambda cb, *a: cb(*a))


def test_editor_row_has_test_button_and_click_runs(
    env: tuple[Path, Any], idle_inline: None, english: None
) -> None:

    ed = _open_editor(env, profiles=(_profile(ProviderKind.LOCAL),))
    widgets = ed._row_widgets["local-main"]
    assert widgets["test"].get_label() == "Test connection"
    assert widgets["test_status"].get_text() == ""
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=ctest.ConnectionResult(ctest.ConnectionState.CONNECTED, "local-main"),
    ):
        widgets["test"].emit("clicked")
    assert widgets["test_status"].get_text() == "Connected"


def test_disabled_profile_still_testable(
    env: tuple[Path, Any], idle_inline: None, english: None
) -> None:
    ed = _open_editor(env, profiles=(_profile(ProviderKind.LOCAL, enabled=False),))
    assert ed._row_widgets["local-main"]["test"].get_sensitive() is True


def _find_connection_run(queued: list[tuple[Any, ...]], coord: Any) -> tuple[Any, ...]:
    for entry in queued:
        fn = entry[0]
        if getattr(fn, "__self__", None) is coord and fn.__name__ == "_run":
            return entry
    raise AssertionError("connection test never dispatched")


def test_rebuild_discards_inflight_result(
    env: tuple[Path, Any], idle_inline: None, english: None
) -> None:
    submit, queued = _capture_submit()
    ed = _open_editor(env, submit=submit, profiles=(_profile(ProviderKind.LOCAL),))
    widgets = ed._row_widgets["local-main"]
    widgets["test"].emit("clicked")
    assert widgets["test_status"].get_text() == "Testing…"
    ed._show_list()  # rebuild bumps the row epoch (e.g. an edit or toggle)
    fn, gen, p, token, cb = _find_connection_run(queued, ed._connection_coordinator)
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=ctest.ConnectionResult(ctest.ConnectionState.CONNECTED, "local-main"),
    ):
        fn(gen, p, token, cb)
    # stale result discarded: the NEW row's label is untouched
    assert ed._row_widgets["local-main"]["test_status"].get_text() == ""


def test_removal_discards_inflight_result(
    env: tuple[Path, Any], idle_inline: None, english: None
) -> None:
    submit, queued = _capture_submit()
    ed = _open_editor(env, submit=submit, profiles=(_profile(ProviderKind.LOCAL),))
    ed._row_widgets["local-main"]["test"].emit("clicked")
    ed._profiles = ()  # the profile is removed
    ed._show_list()
    fn, gen, p, token, cb = _find_connection_run(queued, ed._connection_coordinator)
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=ctest.ConnectionResult(ctest.ConnectionState.CONNECTED, "local-main"),
    ):
        fn(gen, p, token, cb)
    assert "local-main" not in ed._row_widgets  # no publication target exists


def test_close_discards_inflight_and_pending(
    env: tuple[Path, Any], idle_inline: None, english: None
) -> None:
    submit, queued = _capture_submit()
    ed = _open_editor(env, submit=submit, profiles=(_profile(ProviderKind.LOCAL),))
    ed._row_widgets["local-main"]["test"].emit("clicked")
    ed.shutdown()
    fn, gen, p, token, cb = _find_connection_run(queued, ed._connection_coordinator)
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=ctest.ConnectionResult(ctest.ConnectionState.CONNECTED, "local-main"),
    ):
        fn(gen, p, token, cb)
    assert ed._row_widgets["local-main"]["test_status"].get_text() == "Testing…"  # never updated
