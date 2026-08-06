"""Package 7r — close balance protocol and Debian gate gaps (ACCEPTANCE_CORRECTION).

Corrections on top of Package 7q:

1. No valid-state alias for abnormal exits: the child runs ``main()``
   under a ``BaseException`` boundary and terminates through
   ``os._exit(validated_code)`` OUTSIDE it. An imported module calling
   ``sys.exit(1)``, a ``KeyboardInterrupt``, any runtime/import failure,
   a signal exit and an unknown code all become INVALID_RESPONSE — exit
   1 is REMOVED from the accepted protocol (missing credentials already
   fail before spawn in the parent, so NOT_CONFIGURED never needs a
   child code).

2. Raw stdin validation: the child reads stdin without stripping, removes
   exactly the ONE transport newline the parent appends, then rejects
   every remaining control character and empty/oversized keys. Leading,
   trailing and embedded CR/LF/TAB/NUL/DEL are rejected with zero
   requests and zero secret output.

3. Byte-for-byte output protocol: accepted outcomes require byte-exact
   empty stderr; non-amount outcomes require byte-exact empty stdout;
   amount outcomes accept only bounded canonical JSON without any
   prefix or suffix. Whitespace-only diagnostics fail closed.

4. The Lintian gate is pinned immutably (digest-pinned image + exact
   lintian version) and invokes every display/failure level
   (error, warning, info, pedantic, experimental) with a deterministic
   self-test proving a hidden tag fails the gate.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import gi  # type: ignore[import-untyped]

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Secret", "1")
import pytest
from gi.repository import Secret  # type: ignore[import-untyped]  # noqa: E402

import moira.balance as btest  # noqa: E402
from moira.balance import (  # noqa: E402
    BalanceState,
    bounded_balance_refresh,
    run_balance_refresh,
)
from moira.connection_test import MAX_KEY_BYTES  # noqa: E402
from moira.integrations import (  # noqa: E402
    BoundedResult,
    ProbeOutcome,
    ProviderKind,
    ProviderProfile,
)

#: A valid canonical amount-bearing child output (exact, no whitespace).
_VALID_CANONICAL = json.dumps(
    {
        "is_available": True,
        "currencies": [
            {"currency": "CNY", "total_balance": "110.87", "granted_balance": "10.00",
             "topped_up_balance": "100.87"}
        ],
    },
    separators=(",", ":"),
)


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
        self.configure(200, json.dumps({"is_available": True, "balance_infos": [
            {"currency": "CNY", "total_balance": "110.87", "granted_balance": "10.00",
             "topped_up_balance": "100.87"}
        ]}).encode())
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def configure(self, status: int = 200, body: bytes | None = None, delay: float = 0.0) -> None:
        self.httpd.status = status
        self.httpd.body = body if body is not None else b""
        self.httpd.delay = delay
        self.httpd.requests = []

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


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, Any]]:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    fake: dict[str, Any] = {"items": {}, "unavailable": False, "lookups": 0}

    def lookup(_schema: Any, attributes: dict[str, str], _cancellable: Any) -> str | None:
        fake["lookups"] += 1
        if fake["unavailable"]:
            raise RuntimeError("secret vault locked")
        return fake["items"].get((attributes["slug"], attributes["purpose"]))  # type: ignore[no-any-return]

    monkeypatch.setattr(Secret, "password_lookup_sync", lookup)
    return tmp_path, fake


def _profile(**overrides: Any) -> ProviderProfile:
    base: dict[str, Any] = {
        "slug": "deepseek-main",
        "label": "DeepSeek main",
        "kind": ProviderKind.DEEPSEEK,
        "model": "deepseek-chat",
        "enabled": True,
    }
    base.update(overrides)
    return ProviderProfile(**base)


def _ok_result(code: int, stdout: str = "", stderr: str = "") -> BoundedResult:
    return BoundedResult(stdout, stderr, code, ProbeOutcome.OK)


def _child_http_url(server: Any) -> str:
    return f"http://127.0.0.1:{server.port}/user/balance"


def _run_balance_child(
    url: str,
    *,
    policy: str = "local",
    key: str = "sk-7r",
    connect: float = 1.0,
    read: float = 1.0,
    total: float = 5.0,
    cap: int = 65536,
    env: dict[str, str] | None = None,
) -> tuple[int, str, bytes]:
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            btest._BALANCE_CHILD_CODE,
            url,
            str(connect),
            str(read),
            str(total),
            str(cap),
            policy,
        ],
        input=(key + "\n").encode("utf-8"),
        capture_output=True,
        timeout=20,
        env=env,
    )
    return proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr


# ── Finding 1: BaseException boundary, no valid-state alias ────────────────


def test_parent_exit_one_is_invalid_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """RED on f8a36e9: exit 1 still mapped to NOT_CONFIGURED — a generic
    interpreter exit (e.g. an imported module calling sys.exit(1)) aliased
    a valid state. Code 1 is removed from the protocol: INVALID_RESPONSE."""
    monkeypatch.setattr(btest, "run_bounded", lambda *a, **k: _ok_result(1, ""))
    result = bounded_balance_refresh(_profile(), "sk-x")
    assert result.state is BalanceState.INVALID_RESPONSE


def test_real_child_imported_systemexit_one_is_sanitized(tmp_path: Path) -> None:
    """RED on f8a36e9: an imported module calling ``sys.exit(1)`` escaped
    the ``except Exception`` boundary with code 1 and empty stderr →
    NOT_CONFIGURED. The BaseException boundary exits the distinct 11."""
    pkg = tmp_path / "fakepkg" / "moira"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("import sys\nsys.exit(1)\n")
    child_env = dict(os.environ)
    child_env["PYTHONPATH"] = str(tmp_path / "fakepkg")
    code, stdout, stderr = _run_balance_child(
        "https://api.deepseek.com/user/balance", policy="remote", env=child_env
    )
    assert code == 11  # the sanitized crash code, never 1
    assert stdout == ""
    assert stderr == b""  # no traceback, nothing leaked


def test_real_child_imported_keyboardinterrupt_is_sanitized(tmp_path: Path) -> None:
    """RED on f8a36e9: KeyboardInterrupt (a BaseException) escaped with a
    traceback and the interpreter's 130 exit. The boundary exits 11 with
    nothing on stderr."""
    pkg = tmp_path / "fakepkg" / "moira"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("raise KeyboardInterrupt\n")
    child_env = dict(os.environ)
    child_env["PYTHONPATH"] = str(tmp_path / "fakepkg")
    code, stdout, stderr = _run_balance_child(
        "https://api.deepseek.com/user/balance", policy="remote", env=child_env
    )
    assert code == 11
    assert stdout == ""
    assert stderr == b""
    assert b"KeyboardInterrupt" not in stderr


def test_signal_exit_and_unknown_code_stay_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    for rc in (-9, -15, 42, 130, 1):
        monkeypatch.setattr(
            btest, "run_bounded", lambda *a, rc=rc, **k: _ok_result(rc, "")
        )
        result = bounded_balance_refresh(_profile(), "sk-x")
        assert result.state is BalanceState.INVALID_RESPONSE, rc


# ── Finding 2: raw stdin, exact transport newline, strict key ───────────────


@pytest.mark.parametrize(
    "key",
    [
        "\tsk-7r",
        "sk-7r\t",
        "\rsk-7r",
        "sk-7r\r",
        "\nsk-7r",
        "sk-7r\n\n",  # one is the transport newline, the other is embedded
        "sk-7r\nX-Injected: 1",
        "\x00sk-7r",
        "sk-7r\x00",
        "\x7fsk-7r",
        "sk-7r\x7f",
    ],
)
def test_child_rejects_leading_trailing_and_embedded_controls(
    server: Any, key: str
) -> None:
    """RED on f8a36e9: ``strip()`` silently removed leading/trailing
    controls. The raw stdin keeps them and every remaining control
    character is rejected: zero requests, zero secret output."""
    code, stdout, stderr = _run_balance_child(_child_http_url(server), key=key)
    assert code == 7, repr(key)
    assert server.requests == [], repr(key)  # nothing ever reached the server
    assert stdout.strip() == ""
    assert "sk-7r" not in stdout
    assert b"sk-7r" not in stderr


def test_child_accepts_clean_key_exactly(server: Any) -> None:
    code, stdout, _stderr = _run_balance_child(_child_http_url(server), key="sk-7r")
    assert code == 0
    assert len(server.requests) == 1
    assert server.requests[0][1].get("Authorization") == "Bearer sk-7r"


def test_child_empty_key_is_invalid_response(server: Any) -> None:
    """RED on f8a36e9: an empty key exited 1 (NOT_CONFIGURED). Code 1 is
    removed: an empty credential is an invalid response."""
    code, stdout, _stderr = _run_balance_child(_child_http_url(server), key="")
    assert code == 7
    assert server.requests == []
    assert stdout.strip() == ""


def test_child_oversized_key_is_rejected(server: Any) -> None:
    """The child bounds its stdin read: an oversized key is rejected (7)
    without unbounded buffering and without any request."""
    code, stdout, _stderr = _run_balance_child(
        _child_http_url(server), key="x" * (MAX_KEY_BYTES + 64)
    )
    assert code == 7
    assert server.requests == []
    assert stdout.strip() == ""


# ── Finding 3: byte-for-byte empty stderr/stdout ────────────────────────────


def test_parent_whitespace_only_stderr_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """RED on f8a36e9: ``stderr.strip()`` let whitespace-only diagnostics
    pass. Accepted outcomes require byte-exact empty stderr."""
    for stderr in (" ", "\n", "\t\n", " \n\t"):
        monkeypatch.setattr(
            btest,
            "run_bounded",
            lambda *a, stderr=stderr, **k: _ok_result(0, _VALID_CANONICAL, stderr),
        )
        result = bounded_balance_refresh(_profile(), "sk-x")
        assert result.state is BalanceState.INVALID_RESPONSE, repr(stderr)


def test_parent_newline_only_stdout_non_amount_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED on f8a36e9: a non-amount outcome with whitespace-only stdout
    passed. Non-amount outcomes require byte-exact empty stdout."""
    for code in (2, 4, 5, 6, 7, 10, 1):
        monkeypatch.setattr(
            btest,
            "run_bounded",
            lambda *a, code=code, **k: _ok_result(code, "\n", ""),
        )
        result = bounded_balance_refresh(_profile(), "sk-x")
        assert result.state is BalanceState.INVALID_RESPONSE, code


def test_parent_amount_stdout_prefix_suffix_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """RED on f8a36e9: ``_decode_child_output`` stripped the canonical
    JSON, accepting whitespace prefixes/suffixes. Amount outcomes accept
    ONLY the bounded canonical JSON with no prefix or suffix."""
    for stdout in (" " + _VALID_CANONICAL, _VALID_CANONICAL + "\n", "\n" + _VALID_CANONICAL):
        monkeypatch.setattr(
            btest,
            "run_bounded",
            lambda *a, stdout=stdout, **k: _ok_result(0, stdout),
        )
        result = bounded_balance_refresh(_profile(), "sk-x")
        assert result.state is BalanceState.INVALID_RESPONSE, repr(stdout[:20])


def test_parent_exact_canonical_stdout_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        btest, "run_bounded", lambda *a, **k: _ok_result(0, _VALID_CANONICAL)
    )
    result = bounded_balance_refresh(_profile(), "sk-x")
    assert result.state is BalanceState.AVAILABLE
    assert format(result.entries[0].total_balance, "f") == "110.87"


# ── Finding: empty stored credential is NOT_CONFIGURED before spawn ─────────


def test_empty_stored_credential_is_not_configured(
    env: tuple[Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing credentials fail before spawn: an EMPTY stored value is a
    missing credential (NOT_CONFIGURED, zero spawn) — the child never
    needs an exit-1 protocol code for it."""
    env[1]["items"][("deepseek-main", "api_key")] = ""
    spawned = {"n": 0}
    monkeypatch.setattr(
        btest, "run_bounded", lambda *a, **k: spawned.__setitem__("n", spawned["n"] + 1)
    )
    result = run_balance_refresh(_profile())
    assert result.state is BalanceState.NOT_CONFIGURED
    assert spawned["n"] == 0
    assert env[1]["lookups"] == 1


# ── Criterion 6: preserved transport/TLS/timeout distinctions ───────────────


def test_child_alarm_is_one_timeout_state(server: Any) -> None:
    server.configure(200, b"{}", delay=30.0)
    code, stdout, _stderr = _run_balance_child(_child_http_url(server), read=10.0, total=1)
    assert code == 12  # the child alarm: the ONE timeout state (UNREACHABLE)
    assert stdout.strip() == ""


def test_parent_deadline_is_the_same_timeout_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        btest, "run_bounded", lambda *a, **k: BoundedResult("", "", None, ProbeOutcome.TIMEOUT)
    )
    result = bounded_balance_refresh(_profile(), "sk-x")
    assert result.state is BalanceState.UNREACHABLE


def test_transport_and_tls_stay_distinct(server: Any) -> None:
    code, stdout, _stderr = _run_balance_child("http://127.0.0.1:9/user/balance")
    assert code == 4  # UNREACHABLE
    assert stdout.strip() == ""
    host, port = server.httpd.server_address[:2]
    code, stdout, _stderr = _run_balance_child(f"https://{host}:{port}/user/balance")
    assert code == 5  # TLS_ERROR
    assert stdout.strip() == ""


def test_parent_deep_json_stdout_still_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    deep = "[" * 150000 + "]" * 150000
    monkeypatch.setattr(btest, "run_bounded", lambda *a, **k: _ok_result(0, deep))
    result = bounded_balance_refresh(_profile(), "sk-x")
    assert result.state is BalanceState.INVALID_RESPONSE
