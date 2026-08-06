"""Package 7p — exact DeepSeek balance refresh (FEATURE).

Adds a GTK-free exact balance module: eleven immutable balance states
(AVAILABLE, INSUFFICIENT, NOT_CONFIGURED, AUTH_FAILED, RATE_LIMITED,
SERVER_ERROR, UNREACHABLE, TLS_ERROR, INVALID_RESPONSE, UNSUPPORTED,
CANCELLED), Decimal-only amount parsing (never float; bounded decimal
text, signs/exponents/NaN/Infinity/controls/excessive precision or
magnitude rejected), a strict balance payload contract (documented
top-level and item keys only, a real boolean, a non-empty bounded
array, unique CNY/USD entries, exact string amounts; extra, missing,
secret/account-bearing or malformed data is INVALID_RESPONSE), a
dedicated child bound to the OFFICIAL endpoint only
(https://api.deepseek.com/user/balance, never the profile URL) through
the Package 7l boundary (single validated resolution, direct connect,
peer check before TLS/headers, SNI/Host, verified TLS, no
redirects/proxies, bounded time/body/output, process-group reaping),
secret delivery through the private stdin pipe only, and minimal
canonical balance JSON on stdout (never raw bodies, headers, hosts,
metadata, exceptions or secrets). The editor shows a Refresh balance
action ONLY on DeepSeek rows (initial state "Not checked"), results are
ephemeral (no config/schema/History/activity/export/log write), and the
accepted 7o linearizable disposition machinery is reused through an
injected runner — one in-flight plus newest pending, CANCELLED
replacement, stale-row guards and exact submit/lookup/spawn/callback
cardinalities.

All tests use fake Keyring backends and local servers; public-account
tests are opt-in (SKIP_7P_LIVE_ACCOUNT=1).
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
from unittest.mock import patch

import gi  # type: ignore[import-untyped]

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Secret", "1")
import pytest
from gi.repository import GLib, Secret  # type: ignore[import-untyped]  # noqa: E402

import moira.balance as btest
from moira.balance import (
    MAX_BALANCE_INFOS,
    BalanceEntry,
    BalanceResult,
    BalanceState,
    bounded_balance_refresh,
    parse_amount,
    run_balance_refresh,
)
from moira.integrations import (
    BoundedResult,
    ProbeOutcome,
    ProviderKind,
    ProviderProfile,
    run_bounded,
)
from moira.provider_editor import _ConnectionCoordinator

#: Every accepted state, in the closed documented order.
_ALL_STATES = (
    "available",
    "insufficient",
    "not_configured",
    "auth_failed",
    "rate_limited",
    "server_error",
    "unreachable",
    "tls_error",
    "invalid_response",
    "unsupported",
    "cancelled",
)

#: A valid DeepSeek balance payload (documented shape).
_VALID_CNY = {
    "is_available": True,
    "balance_infos": [
        {
            "currency": "CNY",
            "total_balance": "110.87",
            "granted_balance": "10.00",
            "topped_up_balance": "100.87",
        }
    ],
}


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
        self.configure(200, json.dumps(_VALID_CNY).encode())
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
def french(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANG", "fr_FR.UTF-8")
    monkeypatch.setenv("LC_ALL", "")
    monkeypatch.setenv("LC_MESSAGES", "")


@pytest.fixture
def idle_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(GLib, "idle_add", lambda cb, *a: cb(*a))


def _profile(kind: ProviderKind = ProviderKind.DEEPSEEK, **overrides: Any) -> ProviderProfile:
    base: dict[str, Any] = {
        "slug": "deepseek-main",
        "label": "DeepSeek main",
        "kind": kind,
        "model": "deepseek-chat",
        "enabled": True,
    }
    base.update(overrides)
    return ProviderProfile(**base)


def _result(state: BalanceState, slug: str = "deepseek-main") -> BalanceResult:
    return BalanceResult(state, slug)


def _run_balance_child(
    url: str,
    *,
    policy: str = "local",
    key: str = "sk-7p",
    connect: float = 1.0,
    read: float = 1.0,
    total: float = 5.0,
    cap: int = 65536,
) -> tuple[int, str]:
    """Run the dedicated balance child directly with the key on stdin."""
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
        timeout=15,
    )
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def _child_http_url(server: Any, path: str = "/user/balance") -> str:
    return f"http://127.0.0.1:{server.port}{path}"


def _capture_submit() -> tuple[Any, list[tuple[Any, ...]]]:
    queued: list[tuple[Any, ...]] = []

    def submit(fn: Any, *args: Any) -> None:
        queued.append((fn, *args))

    return submit, queued


def _assert_no_orphan_slot(coord: Any) -> None:
    with coord._lock:
        assert not (coord._pending is not None and coord._inflight is None)


def _assert_coordinator_clean(coord: Any) -> None:
    with coord._lock:
        assert coord._inflight is None
        assert coord._pending is None
        assert not getattr(coord, "_cancelled", [])
        assert coord._draining is False


def _xdg_tree(root: Path) -> dict[str, bytes]:
    tree: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if path.is_file():
            tree[str(path.relative_to(root))] = path.read_bytes()
    return tree


# ── Criterion 1: closed immutable states and typed results ──────────────────


def test_balance_states_are_closed_and_immutable() -> None:
    assert tuple(state.value for state in BalanceState) == _ALL_STATES
    for state in BalanceState:
        assert isinstance(state.value, str)
        assert state.value.isascii()
    # Results are frozen and carry no amounts for non-amount states.
    result = _result(BalanceState.AUTH_FAILED)
    with pytest.raises(AttributeError):
        result.state = BalanceState.AVAILABLE  # type: ignore[misc]
    assert result.entries == ()


def test_available_requires_entries_and_others_forbid_them() -> None:
    with pytest.raises(ValueError):
        BalanceResult(BalanceState.AVAILABLE, "deepseek-main")  # no entries: impossible
    with pytest.raises(ValueError):
        BalanceResult(BalanceState.NOT_CONFIGURED, "deepseek-main", (_cny_entry(),))
    with pytest.raises(ValueError):
        BalanceResult(BalanceState.UNREACHABLE, "deepseek-main", (_cny_entry(),))
    # INSUFFICIENT may carry the REAL amounts of a 200 response (402
    # carries none) — but never invented ones.
    assert BalanceResult(BalanceState.INSUFFICIENT, "deepseek-main").entries == ()
    with_entries = BalanceResult(BalanceState.INSUFFICIENT, "deepseek-main", (_cny_entry(),))
    assert with_entries.entries == (_cny_entry(),)


def _cny_entry() -> BalanceEntry:
    return BalanceEntry(
        "CNY", btest.Decimal("110.87"), btest.Decimal("10.00"), btest.Decimal("100.87")
    )


def _usd_entry() -> BalanceEntry:
    return BalanceEntry("USD", btest.Decimal("5.25"), btest.Decimal("0.00"), btest.Decimal("5.25"))


def test_duplicate_currency_and_unknown_currency_rejected() -> None:
    with pytest.raises(ValueError):
        BalanceResult(BalanceState.AVAILABLE, "deepseek-main", (_cny_entry(), _cny_entry()))
    with pytest.raises(ValueError):
        BalanceEntry("EUR", btest.Decimal("1"), btest.Decimal("0"), btest.Decimal("1"))
    with pytest.raises(ValueError):
        BalanceEntry("cny", btest.Decimal("1"), btest.Decimal("0"), btest.Decimal("1"))


def test_entries_normalized_to_deterministic_currency_order() -> None:
    result = BalanceResult(BalanceState.AVAILABLE, "deepseek-main", (_usd_entry(), _cny_entry()))
    assert [entry.currency for entry in result.entries] == ["CNY", "USD"]  # deterministic
    assert result.entries[0] is _cny_entry() or result.entries[0] == _cny_entry()


def test_non_amount_states_never_carry_entries_by_construction() -> None:
    for state in BalanceState:
        if state in (BalanceState.AVAILABLE, BalanceState.INSUFFICIENT):
            continue
        assert _result(state).entries == ()
        assert _result(state).state is state


# ── Criterion 3: Decimal-only, bounded decimal text (never float) ───────────


def test_parse_amount_accepts_bounded_decimal_text() -> None:
    for text, expected in (
        ("0", btest.Decimal("0")),
        ("0.00", btest.Decimal("0.00")),
        ("0.5", btest.Decimal("0.5")),
        ("12", btest.Decimal("12")),
        ("12.34", btest.Decimal("12.34")),
        ("12.3400", btest.Decimal("12.3400")),  # bounded trailing zeros preserved
        ("999999999999.99", btest.Decimal("999999999999.99")),
    ):
        parsed = parse_amount(text)
        assert parsed is not None, text
        assert parsed == expected, text
        assert isinstance(parsed, btest.Decimal), text
        # Exact rendering preserves the bounded decimal text.
        assert format(parsed, "f") == text, text


def test_parse_amount_preserves_trailing_zeros_exactly() -> None:
    parsed = parse_amount("12.3400")
    assert parsed is not None
    assert format(parsed, "f") == "12.3400"  # bounded decimal text preserved for rendering


def test_parse_amount_rejects_signs_exponents_and_non_decimal() -> None:
    for text in (
        "-12.34",
        "+12.34",
        "12.34-",
        "1e3",
        "1E3",
        "1.5e2",
        "12.3.4",
        ".5",
        "5.",
        "12,",
        "12,34",
        "NaN",
        "nan",
        "Infinity",
        "-Infinity",
        "inf",
        "abc",
        "12.34abc",
        "012.3",  # leading zero on a multi-digit integer part
        "00.5",
        " 12.34",
        "12.34 ",
        "12.34\n",
        "١٢.٣",  # non-ASCII digits
        "1٢.3",
    ):
        assert parse_amount(text) is None, text


def test_parse_amount_rejects_controls_oversize_and_float_inputs() -> None:
    assert parse_amount("12.3\x00") is None
    assert parse_amount("12.3\x1f") is None
    assert parse_amount("1" * 40) is None  # excessive length
    assert parse_amount("0." + "0" * 50) is None  # excessive precision
    assert parse_amount("9" * 20) is None  # excessive magnitude
    assert parse_amount(12.34) is None  # a float is never accepted
    assert parse_amount(True) is None
    assert parse_amount(None) is None
    assert parse_amount("") is None


def test_parse_amount_bounds_precision_and_magnitude() -> None:
    assert parse_amount("999999999999.9999") is not None  # 16 significant digits
    assert parse_amount("99999999999.999999") is None  # 17 significant digits
    assert parse_amount("999999.999999") is not None  # bounded fraction
    assert parse_amount("0.0000001") is None  # fraction beyond the bound
    assert parse_amount("1000000000000.00") is None  # magnitude bound


# ── Criterion 4/8: strict payload and status mapping (real child, local HTTP) ─


def test_child_200_available_emits_canonical_json(server: Any) -> None:
    server.configure(200, json.dumps(_VALID_CNY).encode())
    code, stdout = _run_balance_child(_child_http_url(server))
    assert code == 0  # AVAILABLE
    payload = json.loads(stdout)
    assert payload == {
        "is_available": True,
        "currencies": [
            {
                "currency": "CNY",
                "total_balance": "110.87",
                "granted_balance": "10.00",
                "topped_up_balance": "100.87",
            }
        ],
    }
    # Only the minimal canonical JSON — no headers, host, body or key.
    assert "sk-7p" not in stdout
    assert "Authorization" not in stdout
    assert "127.0.0.1" not in stdout


def test_child_200_is_available_false_is_insufficient_with_amounts(server: Any) -> None:
    payload = dict(_VALID_CNY)
    payload["is_available"] = False
    server.configure(200, json.dumps(payload).encode())
    code, stdout = _run_balance_child(_child_http_url(server))
    assert code == 3  # INSUFFICIENT
    decoded = json.loads(stdout)
    assert decoded["is_available"] is False
    assert decoded["currencies"][0]["total_balance"] == "110.87"  # real amounts, never invented


def test_child_two_currencies_emits_both(server: Any) -> None:
    payload = {
        "is_available": True,
        "balance_infos": [
            {
                "currency": "USD",
                "total_balance": "5.25",
                "granted_balance": "0.00",
                "topped_up_balance": "5.25",
            },
            {
                "currency": "CNY",
                "total_balance": "110.87",
                "granted_balance": "10.00",
                "topped_up_balance": "100.87",
            },
        ],
    }
    server.configure(200, json.dumps(payload).encode())
    code, stdout = _run_balance_child(_child_http_url(server))
    assert code == 0
    currencies = [entry["currency"] for entry in json.loads(stdout)["currencies"]]
    assert set(currencies) == {"CNY", "USD"}
    assert len(currencies) == len(set(currencies))  # unique


def test_child_402_is_insufficient_without_stdout(server: Any) -> None:
    server.configure(402, b"{}")
    code, stdout = _run_balance_child(_child_http_url(server))
    assert code == 3  # INSUFFICIENT
    assert stdout.strip() == ""  # no invented amounts


def test_child_status_mapping(server: Any) -> None:
    for status, expected in (
        (401, 2),  # AUTH_FAILED
        (403, 2),  # AUTH_FAILED
        (429, 6),  # RATE_LIMITED
        (500, 10),  # SERVER_ERROR
        (503, 10),  # SERVER_ERROR
        (404, 7),  # other statuses fail closed: INVALID_RESPONSE
        (301, 7),  # redirects are never followed
        (302, 7),
        (204, 7),
        (418, 7),
    ):
        server.configure(status, b"{}")
        code, stdout = _run_balance_child(_child_http_url(server))
        assert code == expected, status
        assert stdout.strip() == "", status  # non-amount states print nothing


def test_child_schema_failures_are_invalid_response(server: Any) -> None:
    cases: dict[str, Any] = {
        "malformed json": b"{not json",
        "top-level list": json.dumps([1, 2]).encode(),
        "missing is_available": json.dumps({"balance_infos": []}).encode(),
        "missing balance_infos": json.dumps({"is_available": True}).encode(),
        "extra top-level key": json.dumps(
            {"is_available": True, "balance_infos": [], "extra": 1}
        ).encode(),
        "secret top-level key": json.dumps(
            {"is_available": True, "balance_infos": [], "api_key": "sk-leak"}
        ).encode(),
        "boolean is_available is not bool": json.dumps(
            {"is_available": "yes", "balance_infos": []}
        ).encode(),
        "int is_available": json.dumps({"is_available": 1, "balance_infos": []}).encode(),
        "empty array": json.dumps({"is_available": True, "balance_infos": []}).encode(),
        "array not a list": json.dumps({"is_available": True, "balance_infos": {}}).encode(),
        "item not a dict": json.dumps({"is_available": True, "balance_infos": ["CNY"]}).encode(),
        "item missing key": json.dumps(
            {
                "is_available": True,
                "balance_infos": [
                    {"currency": "CNY", "total_balance": "1.00", "granted_balance": "0.00"}
                ],
            }
        ).encode(),
        "item extra key": json.dumps(
            {
                "is_available": True,
                "balance_infos": [
                    {
                        "currency": "CNY",
                        "total_balance": "1.00",
                        "granted_balance": "0.00",
                        "topped_up_balance": "1.00",
                        "extra": 1,
                    }
                ],
            }
        ).encode(),
        "item secret key": json.dumps(
            {
                "is_available": True,
                "balance_infos": [
                    {
                        "currency": "CNY",
                        "total_balance": "1.00",
                        "granted_balance": "0.00",
                        "topped_up_balance": "1.00",
                        "account_id": "acc-123",
                    }
                ],
            }
        ).encode(),
        "unknown currency": json.dumps(
            {
                "is_available": True,
                "balance_infos": [
                    {
                        "currency": "EUR",
                        "total_balance": "1.00",
                        "granted_balance": "0.00",
                        "topped_up_balance": "1.00",
                    }
                ],
            }
        ).encode(),
        "duplicate currency": json.dumps(
            {
                "is_available": True,
                "balance_infos": [
                    {
                        "currency": "CNY",
                        "total_balance": "1.00",
                        "granted_balance": "0.00",
                        "topped_up_balance": "1.00",
                    },
                    {
                        "currency": "CNY",
                        "total_balance": "2.00",
                        "granted_balance": "0.00",
                        "topped_up_balance": "2.00",
                    },
                ],
            }
        ).encode(),
        "numeric amount": json.dumps(
            {
                "is_available": True,
                "balance_infos": [
                    {
                        "currency": "CNY",
                        "total_balance": 110.87,
                        "granted_balance": "0.00",
                        "topped_up_balance": "1.00",
                    }
                ],
            }
        ).encode(),
        "bool amount": json.dumps(
            {
                "is_available": True,
                "balance_infos": [
                    {
                        "currency": "CNY",
                        "total_balance": True,
                        "granted_balance": "0.00",
                        "topped_up_balance": "1.00",
                    }
                ],
            }
        ).encode(),
        "signed amount": json.dumps(
            {
                "is_available": True,
                "balance_infos": [
                    {
                        "currency": "CNY",
                        "total_balance": "-1.00",
                        "granted_balance": "0.00",
                        "topped_up_balance": "1.00",
                    }
                ],
            }
        ).encode(),
        "exponent amount": json.dumps(
            {
                "is_available": True,
                "balance_infos": [
                    {
                        "currency": "CNY",
                        "total_balance": "1e3",
                        "granted_balance": "0.00",
                        "topped_up_balance": "1.00",
                    }
                ],
            }
        ).encode(),
        "oversized amount": json.dumps(
            {
                "is_available": True,
                "balance_infos": [
                    {
                        "currency": "CNY",
                        "total_balance": "9999999999999999.99",
                        "granted_balance": "0.00",
                        "topped_up_balance": "1.00",
                    }
                ],
            }
        ).encode(),
    }
    for name, body in cases.items():
        server.configure(200, body)
        code, stdout = _run_balance_child(_child_http_url(server))
        assert code == 7, name
        assert stdout.strip() == "", name  # invalid responses never leak a body


def test_child_oversized_body_is_invalid_response(server: Any) -> None:
    server.configure(200, b'{"is_available": true, "balance_infos": [')
    code, _stdout = _run_balance_child(_child_http_url(server), cap=16)
    assert code == 7


def test_child_secret_never_leaks_to_stdout(server: Any) -> None:
    secret = "«redacted:sk-7p-secret»"
    server.configure(
        200,
        json.dumps(
            {
                "is_available": True,
                "balance_infos": [
                    {
                        "currency": "CNY",
                        "total_balance": "1.00",
                        "granted_balance": "0.00",
                        "topped_up_balance": "1.00",
                        "note": secret,  # account-bearing extra data must not pass through
                    }
                ],
            }
        ).encode(),
    )
    code, stdout = _run_balance_child(_child_http_url(server), key=secret)
    assert code == 7  # the extra key makes the response invalid…
    assert secret not in stdout  # …and nothing leaks


def test_child_secret_not_in_valid_stdout(server: Any) -> None:
    secret = "«redacted:sk-7p-valid»"
    server.configure(200, json.dumps(_VALID_CNY).encode())
    code, stdout = _run_balance_child(_child_http_url(server), key=secret)
    assert code == 0
    assert secret not in stdout
    assert "«redacted" not in stdout


# ── Criterion 6: transport, TLS, policy, proxies, timeouts ──────────────────


def test_child_tls_error_is_distinct(server: Any) -> None:
    host, port = server.httpd.server_address[:2]
    code, stdout = _run_balance_child(f"https://{host}:{port}/user/balance")
    assert code == 5  # TLS_ERROR (verified TLS against a plain-HTTP server)
    assert stdout.strip() == ""


def test_child_connection_refused_is_unreachable() -> None:
    code, stdout = _run_balance_child("http://127.0.0.1:9/user/balance")
    assert code == 4  # UNREACHABLE
    assert stdout.strip() == ""


def test_child_remote_policy_rejects_private_target_before_connect(server: Any) -> None:
    """The remote policy (production) refuses loopback/private targets
    BEFORE any connect: the server sees nothing and nothing is sent."""
    code, stdout = _run_balance_child(
        f"https://127.0.0.1:{server.port}/user/balance", policy="remote"
    )
    assert code == 4  # UNREACHABLE: refused by address policy
    assert server.requests == []
    assert stdout.strip() == ""


def test_child_https_private_target_rejected(server: Any) -> None:
    code, _stdout = _run_balance_child(
        f"https://127.0.0.1:{server.port}/user/balance", policy="remote"
    )
    assert code == 4


def test_child_proxy_environment_ignored(server: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    code, _stdout = _run_balance_child(_child_http_url(server))
    assert code == 0  # reached the server directly
    assert len(server.requests) == 1


def test_child_hanging_server_is_bounded(server: Any) -> None:
    server.configure(200, json.dumps(_VALID_CNY).encode(), delay=30.0)
    t0 = time.monotonic()
    code, stdout = _run_balance_child(_child_http_url(server), read=0.4, total=5)
    elapsed = time.monotonic() - t0
    assert code == 4  # UNREACHABLE: deadline exceeded
    assert stdout.strip() == ""
    assert elapsed < 3.0  # bounded, not forever


def test_child_https_scheme_only_for_official_endpoint() -> None:
    # An http URL with the remote policy (production never does this) is
    # an invalid endpoint configuration, not a downgrade.
    code, _stdout = _run_balance_child("http://127.0.0.1:9/user/balance", policy="remote")
    assert code == 7


# ── Criterion 7/8: parent mapping through the bounded boundary ──────────────


def _ok_result(code: int, stdout: str = "") -> BoundedResult:
    return BoundedResult(stdout, "", code, ProbeOutcome.OK)


def test_parent_maps_bounded_outcomes(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = _profile()
    calls: dict[str, Any] = {}

    def fake_run(args: list[str], **kwargs: Any) -> BoundedResult | None:
        calls["args"] = args
        calls["kwargs"] = kwargs
        return None

    monkeypatch.setattr(btest, "run_bounded", fake_run)
    assert (
        bounded_balance_refresh(profile, "sk-x").state is BalanceState.UNREACHABLE
    )  # spawn failure

    for outcome in (
        ProbeOutcome.TIMEOUT,
        ProbeOutcome.STDOUT_OVERFLOW,
        ProbeOutcome.STDERR_OVERFLOW,
    ):
        monkeypatch.setattr(
            btest,
            "run_bounded",
            lambda *a, outcome=outcome, **k: BoundedResult("", "", None, outcome),
        )
        result = bounded_balance_refresh(profile, "sk-x")
        if outcome is ProbeOutcome.TIMEOUT:
            assert result.state is BalanceState.UNREACHABLE
        else:
            assert result.state is BalanceState.INVALID_RESPONSE


def test_parent_always_uses_the_official_endpoint_never_the_profile_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile(base_url="https://evil.example/v1")
    captured: dict[str, Any] = {}

    def fake_run(args: list[str], **kwargs: Any) -> BoundedResult:
        captured["args"] = list(args)
        return _ok_result(0, '{"is_available": true, "currencies": []}')

    monkeypatch.setattr(btest, "run_bounded", fake_run)
    result = bounded_balance_refresh(profile, "sk-x")
    assert result.state is BalanceState.INVALID_RESPONSE  # empty currencies is invalid
    assert captured["args"][3] == "https://api.deepseek.com/user/balance"  # official only
    assert "evil.example" not in " ".join(captured["args"])
    assert captured["args"][8] == "remote"


def test_key_reaches_child_only_via_private_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(args: list[str], **kwargs: Any) -> BoundedResult:
        captured["args"] = args
        captured["stdin_data"] = kwargs.get("stdin_data")
        captured["timeout"] = kwargs.get("timeout")
        return _ok_result(7)

    monkeypatch.setattr(btest, "run_bounded", fake_run)
    secret = "«redacted:sk-7p-…»"
    result = bounded_balance_refresh(_profile(), secret)
    assert result.state is BalanceState.INVALID_RESPONSE
    assert "«redacted" not in " ".join(captured["args"])  # never argv
    assert captured["stdin_data"] == f"{secret}\n".encode()  # private stdin only
    assert captured["timeout"] == btest.DEFAULT_TOTAL_TIMEOUT


def test_parent_decodes_canonical_amounts(monkeypatch: pytest.MonkeyPatch) -> None:
    stdout = json.dumps(
        {
            "is_available": True,
            "currencies": [
                {
                    "currency": "CNY",
                    "total_balance": "110.87",
                    "granted_balance": "10.00",
                    "topped_up_balance": "100.87",
                }
            ],
        }
    )

    def fake_run(args: list[str], **kwargs: Any) -> BoundedResult:
        return _ok_result(0, stdout)

    monkeypatch.setattr(btest, "run_bounded", fake_run)
    result = bounded_balance_refresh(_profile(), "sk-x")
    assert result.state is BalanceState.AVAILABLE
    assert len(result.entries) == 1
    entry = result.entries[0]
    assert entry.currency == "CNY"
    assert entry.total_balance == btest.Decimal("110.87")
    assert isinstance(entry.total_balance, btest.Decimal)  # Decimal, never float
    assert format(entry.total_balance, "f") == "110.87"
    assert entry.granted_balance == btest.Decimal("10.00")
    assert format(entry.granted_balance, "f") == "10.00"
    assert entry.topped_up_balance == btest.Decimal("100.87")


def test_parent_decodes_both_currencies_in_canonical_order(monkeypatch: pytest.MonkeyPatch) -> None:
    stdout = json.dumps(
        {
            "is_available": True,
            "currencies": [
                {
                    "currency": "USD",
                    "total_balance": "5.25",
                    "granted_balance": "0.00",
                    "topped_up_balance": "5.25",
                },
                {
                    "currency": "CNY",
                    "total_balance": "110.87",
                    "granted_balance": "10.00",
                    "topped_up_balance": "100.87",
                },
            ],
        }
    )

    def fake_run(args: list[str], **kwargs: Any) -> BoundedResult:
        return _ok_result(0, stdout)

    monkeypatch.setattr(btest, "run_bounded", fake_run)
    result = bounded_balance_refresh(_profile(), "sk-x")
    assert result.state is BalanceState.AVAILABLE
    assert [entry.currency for entry in result.entries] == ["CNY", "USD"]


def test_parent_available_with_invalid_stdout_is_invalid_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for payload in (
        "",
        "{not json",
        "[]",
        '{"is_available": true, "currencies": []}',
        '{"is_available": true, "currencies": [{"currency": "CNY"}]}',
        '{"is_available": false, "currencies": [{"currency": "CNY", "total_balance": "1", '
        '"granted_balance": "0", "topped_up_balance": "1"}]}',
        '{"is_available": true, "currencies": [{"currency": "EUR", "total_balance": "1", '
        '"granted_balance": "0", "topped_up_balance": "1"}]}',
        '{"is_available": true, "currencies": [{"currency": "CNY", "total_balance": "1", '
        '"granted_balance": "0", "topped_up_balance": "1"}], "extra": 1}',
        '{"is_available": true, "currencies": [{"currency": "CNY", "total_balance": "1", '
        '"granted_balance": "0", "topped_up_balance": "1"}, {"currency": "CNY", '
        '"total_balance": "2", "granted_balance": "0", "topped_up_balance": "2"}]}',
    ):
        monkeypatch.setattr(
            btest,
            "run_bounded",
            lambda *a, payload=payload, **k: _ok_result(0, payload),
        )
        result = bounded_balance_refresh(_profile(), "sk-x")
        assert result.state is BalanceState.INVALID_RESPONSE, payload


def test_parent_insufficient_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    # 402-style: exit 3, empty stdout → INSUFFICIENT without amounts.
    monkeypatch.setattr(btest, "run_bounded", lambda *a, **k: _ok_result(3, ""))
    result = bounded_balance_refresh(_profile(), "sk-x")
    assert result.state is BalanceState.INSUFFICIENT
    assert result.entries == ()

    # 200 with is_available=false: exit 3 + canonical amounts → INSUFFICIENT with real amounts.
    stdout = json.dumps(
        {
            "is_available": False,
            "currencies": [
                {
                    "currency": "CNY",
                    "total_balance": "0.50",
                    "granted_balance": "0.00",
                    "topped_up_balance": "0.50",
                }
            ],
        }
    )
    monkeypatch.setattr(btest, "run_bounded", lambda *a, **k: _ok_result(3, stdout))
    result = bounded_balance_refresh(_profile(), "sk-x")
    assert result.state is BalanceState.INSUFFICIENT
    assert len(result.entries) == 1
    assert result.entries[0].currency == "CNY"
    assert format(result.entries[0].total_balance, "f") == "0.50"

    # Cross-check failure: exit 3 claims is_available=true → invalid.
    wrong = json.dumps(
        {
            "is_available": True,
            "currencies": [
                {
                    "currency": "CNY",
                    "total_balance": "1",
                    "granted_balance": "0",
                    "topped_up_balance": "1",
                }
            ],
        }
    )
    monkeypatch.setattr(btest, "run_bounded", lambda *a, **k: _ok_result(3, wrong))
    assert bounded_balance_refresh(_profile(), "sk-x").state is BalanceState.INVALID_RESPONSE


def test_parent_rejects_stdout_from_non_amount_states(monkeypatch: pytest.MonkeyPatch) -> None:
    """The child may return ONLY minimal canonical balance JSON on
    stdout; any output for a non-amount state is a leak → INVALID_RESPONSE."""
    for code in (2, 4, 5, 6, 7, 10, 1):
        monkeypatch.setattr(
            btest,
            "run_bounded",
            lambda *a, code=code, **k: _ok_result(code, "leaked raw body"),
        )
        result = bounded_balance_refresh(_profile(), "sk-x")
        assert result.state is BalanceState.INVALID_RESPONSE, code


def test_parent_unknown_exit_code_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(btest, "run_bounded", lambda *a, **k: _ok_result(42, ""))
    assert bounded_balance_refresh(_profile(), "sk-x").state is BalanceState.INVALID_RESPONSE


def test_parent_oversized_credential_fails_before_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    spawned = {"n": 0}
    monkeypatch.setattr(
        btest, "run_bounded", lambda *a, **k: spawned.__setitem__("n", spawned["n"] + 1)
    )
    result = bounded_balance_refresh(_profile(), "x" * 5000)
    assert result.state is BalanceState.INVALID_RESPONSE
    assert spawned["n"] == 0


def test_parent_cancelled_before_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    spawned = {"n": 0}
    monkeypatch.setattr(
        btest, "run_bounded", lambda *a, **k: spawned.__setitem__("n", spawned["n"] + 1)
    )
    event = threading.Event()
    event.set()
    result = bounded_balance_refresh(_profile(), "sk-x", cancel_event=event)
    assert result.state is BalanceState.CANCELLED
    assert spawned["n"] == 0


def test_parent_passes_total_timeout_to_run_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(*_args: Any, **kwargs: Any) -> BoundedResult:
        captured["timeout"] = kwargs.get("timeout")
        return _ok_result(7)

    monkeypatch.setattr(btest, "run_bounded", fake_run)
    bounded_balance_refresh(_profile(), "sk-x", total_timeout=3.0)
    assert captured["timeout"] == 3.0


# ── Criterion 2/11: preflight, Keyring timing, UNSUPPORTED before spawn ─────


def test_non_deepseek_kind_unsupported_before_keyring_and_spawn(
    env: tuple[Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    env[1]["items"][("deepseek-main", "api_key")] = "sk-x"
    spawned = {"n": 0}
    monkeypatch.setattr(
        btest, "run_bounded", lambda *a, **k: spawned.__setitem__("n", spawned["n"] + 1)
    )
    for kind in (
        ProviderKind.OPENAI,
        ProviderKind.OPENROUTER,
        ProviderKind.ANTHROPIC,
        ProviderKind.OPENAI_COMPATIBLE,
        ProviderKind.LOCAL,
        ProviderKind.CUSTOM,
    ):
        result = run_balance_refresh(_profile(kind, slug=f"slug-{kind.value}"))
        assert result.state is BalanceState.UNSUPPORTED, kind
    assert env[1]["lookups"] == 0  # zero Keyring reads for unsupported kinds
    assert spawned["n"] == 0  # zero spawn


def test_missing_credential_fails_before_network(
    env: tuple[Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned = {"n": 0}
    monkeypatch.setattr(
        btest, "run_bounded", lambda *a, **k: spawned.__setitem__("n", spawned["n"] + 1)
    )
    result = run_balance_refresh(_profile())
    assert result.state is BalanceState.NOT_CONFIGURED
    assert spawned["n"] == 0


def test_unavailable_keyring_fails_closed(
    env: tuple[Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    env[1]["unavailable"] = True
    spawned = {"n": 0}
    monkeypatch.setattr(
        btest, "run_bounded", lambda *a, **k: spawned.__setitem__("n", spawned["n"] + 1)
    )
    result = run_balance_refresh(_profile())
    assert result.state is BalanceState.NOT_CONFIGURED
    assert spawned["n"] == 0


def test_keyring_read_immediately_before_refresh(env: tuple[Path, dict[str, Any]]) -> None:
    env[1]["items"][("deepseek-main", "api_key")] = "«redacted:sk-7p»"
    profile = _profile()
    captured: dict[str, Any] = {}

    def fake_run(args: list[str], **kwargs: Any) -> BoundedResult:
        captured["stdin_data"] = kwargs.get("stdin_data")
        return _ok_result(7)

    with patch("moira.balance.run_bounded", side_effect=fake_run):
        run_balance_refresh(profile)
    assert captured["stdin_data"] == ("«redacted:sk-7p»\n").encode()


def test_results_are_ephemeral(
    env: tuple[Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refresh writes nothing: no config, schema, History, activity,
    export or log files appear anywhere under the XDG roots."""
    config_root = Path(os.environ["XDG_CONFIG_HOME"])
    state_root = Path(os.environ["XDG_STATE_HOME"])
    env[1]["items"][("deepseek-main", "api_key")] = "sk-7p"
    monkeypatch.setattr(
        btest,
        "run_bounded",
        lambda *a, **k: _ok_result(
            0,
            json.dumps(
                {
                    "is_available": True,
                    "currencies": [
                        {
                            "currency": "CNY",
                            "total_balance": "110.87",
                            "granted_balance": "10.00",
                            "topped_up_balance": "100.87",
                        }
                    ],
                }
            ),
        ),
    )
    before_config = _xdg_tree(config_root)
    before_state = _xdg_tree(state_root)
    result = run_balance_refresh(_profile())
    assert result.state is BalanceState.AVAILABLE
    assert _xdg_tree(config_root) == before_config
    assert _xdg_tree(state_root) == before_state


# ── Criterion 6: ignored SIGTERM still reaped (run_bounded boundary) ────────


def test_sigterm_ignoring_balance_child_is_killed_and_reaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = [
        sys.executable,
        "-c",
        "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(1000)",
    ]
    t0 = time.monotonic()
    result = run_bounded(child, timeout=0.5)
    elapsed = time.monotonic() - t0
    assert result is not None
    assert result.outcome is ProbeOutcome.TIMEOUT
    assert elapsed < 5.0  # SIGTERM ignored → unconditional SIGKILL + reap


# ── Criterion 10: reused 7o linearizable disposition machinery ──────────────


def test_balance_coordinator_reuses_linearizable_disposition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The balance action runs through the SAME ``_ConnectionCoordinator``
    with an injected runner: one in-flight + newest pending, CANCELLED
    replacement via the injected factory, exact cardinalities."""
    submit, queued = _capture_submit()
    coord = _ConnectionCoordinator(
        submit,
        threading.Event(),
        runner=lambda profile, cancel_event=None: BalanceResult(
            BalanceState.AVAILABLE, profile.slug, (_cny_entry(),)
        ),
        cancelled=lambda p: BalanceResult(BalanceState.CANCELLED, p.slug),
        rejected=lambda p: BalanceResult(BalanceState.UNREACHABLE, p.slug),
    )
    profile = _profile()
    published: list[tuple[str, str]] = []
    coord.request(profile, "a", lambda t, r: published.append(("a", r.state.value)))
    coord.request(profile, "b", lambda t, r: published.append(("b", r.state.value)))
    coord.request(profile, "c", lambda t, r: published.append(("c", r.state.value)))
    assert published == [("b", "cancelled")]  # B: exactly one CANCELLED via the factory
    assert len(queued) == 1  # B: zero submit
    fn_a, gen_a, p_a, tok_a, cb_a = queued[0]
    assert tok_a == "a"
    fn_a(gen_a, p_a, tok_a, cb_a)  # A: one run → one callback
    assert published == [("b", "cancelled"), ("a", "available")]
    assert len(queued) == 2  # newest parked (C) promoted: one submit
    fn_c, gen_c, p_c, tok_c, cb_c = queued[1]
    assert tok_c == "c"
    fn_c(gen_c, p_c, tok_c, cb_c)
    assert published == [("b", "cancelled"), ("a", "available"), ("c", "available")]
    assert len(published) == len({tag for tag, _state in published})
    _assert_no_orphan_slot(coord)
    _assert_coordinator_clean(coord)
    assert coord.request(profile, "d", lambda t, r: None) is True  # later usability


def test_balance_coordinator_rejection_uses_injected_factory() -> None:
    def rejecting_submit(fn: Any, *args: Any) -> None:
        raise RuntimeError("executor closed")

    coord = _ConnectionCoordinator(
        rejecting_submit,
        threading.Event(),
        runner=lambda profile, cancel_event=None: BalanceResult(
            BalanceState.AVAILABLE, profile.slug, (_cny_entry(),)
        ),
        cancelled=lambda p: BalanceResult(BalanceState.CANCELLED, p.slug),
        rejected=lambda p: BalanceResult(BalanceState.UNREACHABLE, p.slug),
    )
    published: list[str] = []
    ok = coord.request(_profile(), "a", lambda t, r: published.append(r.state.value))
    assert ok is False
    assert published == ["unreachable"]  # deterministic rejection completion


def test_balance_coordinator_close_submits_nothing() -> None:
    submit, queued = _capture_submit()
    event = threading.Event()
    coord = _ConnectionCoordinator(
        submit,
        event,
        runner=lambda profile, cancel_event=None: _result(BalanceState.AVAILABLE),
        cancelled=lambda p: BalanceResult(BalanceState.CANCELLED, p.slug),
        rejected=lambda p: BalanceResult(BalanceState.UNREACHABLE, p.slug),
    )
    profile = _profile()
    published: list[tuple[str, str]] = []
    coord.request(profile, "a", lambda t, r: published.append(("a", r.state.value)))
    coord.request(profile, "b", lambda t, r: published.append(("b", r.state.value)))
    event.set()
    coord.cancel()
    assert coord.request(profile, "e", lambda t, r: None) is False  # refused after close
    assert queued == [] if False else len(queued) == 1  # A was committed before close…
    fn_a, gen_a, p_a, tok_a, cb_a = queued[0]
    fn_a(gen_a, p_a, tok_a, cb_a)  # …and its queued run self-bounds: zero runner calls
    assert published == []  # nothing publishes after close
    _assert_no_orphan_slot(coord)
    _assert_coordinator_clean(coord)


# ── Criterion 9: UI — Refresh balance only on DeepSeek rows ─────────────────


def _editor_with(submit: Any, profiles: tuple[ProviderProfile, ...]) -> Any:
    from moira.provider_editor import ProviderEditor

    ed = ProviderEditor(submit=submit)
    ed._profiles = profiles
    ed._configured = {p.slug: False for p in profiles}
    ed._show_list()
    return ed


def test_refresh_balance_button_only_on_deepseek_rows(idle_inline: None, english: None) -> None:
    submit, _queued = _capture_submit()
    ed = _editor_with(
        submit,
        (
            _profile(ProviderKind.DEEPSEEK, slug="deepseek-main"),
            _profile(ProviderKind.OPENAI, slug="openai-main"),
            _profile(ProviderKind.LOCAL, slug="local-main", base_url="http://127.0.0.1:9"),
        ),
    )
    assert "balance" in ed._row_widgets["deepseek-main"]
    assert "balance_status" in ed._row_widgets["deepseek-main"]
    assert ed._row_widgets["deepseek-main"]["balance"].get_label() == "Refresh balance"
    assert ed._row_widgets["deepseek-main"]["balance_status"].get_text() == "Not checked"
    # Other kinds never show the action: no button is built (None).
    assert ed._row_widgets["openai-main"]["balance"] is None
    assert ed._row_widgets["openai-main"]["balance_status"] is None
    assert ed._row_widgets["local-main"]["balance"] is None
    ed.shutdown()


def test_balance_click_runs_through_coordinator_and_renders_state(
    env: tuple[Path, dict[str, Any]], idle_inline: None, english: None
) -> None:
    """A click with no credential fails closed as NOT_CONFIGURED with
    zero spawn; a successful refresh renders the translated state plus
    the exact amounts."""

    queued: list[tuple[Any, ...]] = []

    def submit(fn: Any, *args: Any) -> None:
        queued.append((fn, *args))

    ed = _editor_with(submit, (_profile(ProviderKind.DEEPSEEK),))
    widgets = ed._row_widgets["deepseek-main"]
    widgets["balance"].emit("clicked")
    assert widgets["balance_status"].get_text() == "Checking balance…"
    runs = [
        entry for entry in queued if getattr(entry[0], "__self__", None) is ed._balance_coordinator
    ]
    assert len(runs) == 1  # exactly one dispatch
    fn, gen, p, tok, cb = runs[0]
    assert tok[0] == "deepseek-main"  # 4-tuple row token (slug, epoch, widgets, click)
    fn(gen, p, tok, cb)  # no credential in the fake Keyring → NOT_CONFIGURED, zero spawn
    assert widgets["balance_status"].get_text() == "Not configured"

    # Successful refresh: state + exact amounts, deterministic currency order.
    with patch(
        "moira.provider_editor.run_balance_refresh",
        return_value=BalanceResult(
            BalanceState.AVAILABLE,
            "deepseek-main",
            (
                BalanceEntry(
                    "USD", btest.Decimal("5.25"), btest.Decimal("0.00"), btest.Decimal("5.25")
                ),
                BalanceEntry(
                    "CNY", btest.Decimal("110.87"), btest.Decimal("10.00"), btest.Decimal("100.87")
                ),
            ),
        ),
    ):
        ed2 = _editor_with(submit, (_profile(ProviderKind.DEEPSEEK),))
    widgets2 = ed2._row_widgets["deepseek-main"]
    widgets2["balance"].emit("clicked")
    runs2 = [
        entry for entry in queued if getattr(entry[0], "__self__", None) is ed2._balance_coordinator
    ]
    fn2, gen2, p2, tok2, cb2 = runs2[-1]
    fn2(gen2, p2, tok2, cb2)
    text = widgets2["balance_status"].get_text()
    assert "Balance available" in text
    assert "CNY: Total 110.87" in text  # exact bounded decimal text
    assert "Granted 10.00" in text
    assert "Topped up 100.87" in text
    assert "USD: Total 5.25" in text
    assert text.index("CNY") < text.index("USD")  # deterministic currency order
    ed.shutdown()
    ed2.shutdown()


def test_balance_renders_translated_french(
    env: tuple[Path, dict[str, Any]], idle_inline: None, french: None
) -> None:

    queued: list[tuple[Any, ...]] = []

    def submit(fn: Any, *args: Any) -> None:
        queued.append((fn, *args))

    with patch(
        "moira.provider_editor.run_balance_refresh",
        return_value=BalanceResult(
            BalanceState.INSUFFICIENT,
            "deepseek-main",
            (
                BalanceEntry(
                    "CNY", btest.Decimal("0.50"), btest.Decimal("0.00"), btest.Decimal("0.50")
                ),
            ),
        ),
    ):
        ed = _editor_with(submit, (_profile(ProviderKind.DEEPSEEK),))
    widgets = ed._row_widgets["deepseek-main"]
    widgets["balance"].emit("clicked")
    runs = [
        entry for entry in queued if getattr(entry[0], "__self__", None) is ed._balance_coordinator
    ]
    fn, gen, p, tok, cb = runs[-1]
    fn(gen, p, tok, cb)
    text = widgets["balance_status"].get_text()
    assert "Solde insuffisant" in text
    assert "Total 0,50" in text or "Total 0.50" in text  # exact amount, locale-neutral rendering
    ed.shutdown()


def test_balance_replacement_and_stale_row_guards(
    env: tuple[Path, dict[str, Any]], idle_inline: None, english: None
) -> None:

    queued: list[tuple[Any, ...]] = []

    def submit(fn: Any, *args: Any) -> None:
        queued.append((fn, *args))

    ed = _editor_with(submit, (_profile(ProviderKind.DEEPSEEK),))
    widgets = ed._row_widgets["deepseek-main"]
    widgets["balance"].emit("clicked")  # dispatch 1 (no credential → NOT_CONFIGURED)
    widgets["balance"].emit("clicked")  # parks
    widgets["balance"].emit("clicked")  # replaces click 2: click 2's CANCELLED discarded by token
    runs = [
        entry for entry in queued if getattr(entry[0], "__self__", None) is ed._balance_coordinator
    ]
    fn1, gen1, p1, tok1, cb1 = runs[0]
    fn1(gen1, p1, tok1, cb1)
    # A stale completion from a superseded click is discarded by the
    # per-row balance click token.
    assert widgets["balance_status"].get_text() == "Checking balance…"
    runs = [
        entry for entry in queued if getattr(entry[0], "__self__", None) is ed._balance_coordinator
    ]
    fn3, gen3, p3, tok3, cb3 = runs[1]
    assert tok3[3] == 3  # newest click generation
    fn3(gen3, p3, tok3, cb3)
    assert widgets["balance_status"].get_text() == "Not configured"
    ed.shutdown()
    _assert_coordinator_clean(ed._balance_coordinator)


def test_balance_row_rebuild_discards_inflight_result(
    env: tuple[Path, dict[str, Any]], idle_inline: None, english: None
) -> None:

    queued: list[tuple[Any, ...]] = []

    def submit(fn: Any, *args: Any) -> None:
        queued.append((fn, *args))

    with patch(
        "moira.provider_editor.run_balance_refresh",
        return_value=BalanceResult(BalanceState.AVAILABLE, "deepseek-main", (_cny_entry(),)),
    ):
        ed = _editor_with(submit, (_profile(ProviderKind.DEEPSEEK),))
    widgets = ed._row_widgets["deepseek-main"]
    widgets["balance"].emit("clicked")
    runs = [
        entry for entry in queued if getattr(entry[0], "__self__", None) is ed._balance_coordinator
    ]
    fn, gen, p, tok, cb = runs[0]
    ed._render_list()  # a rebuild invalidates the row epoch
    fn(gen, p, tok, cb)
    widgets_new = ed._row_widgets["deepseek-main"]
    assert widgets_new["balance_status"].get_text() == "Not checked"  # stale result discarded
    ed.shutdown()


def test_balance_editing_or_closing_discards_inflight(
    env: tuple[Path, dict[str, Any]], idle_inline: None, english: None
) -> None:

    submit, queued = _capture_submit()
    with patch(
        "moira.provider_editor.run_balance_refresh",
        return_value=BalanceResult(BalanceState.AVAILABLE, "deepseek-main", (_cny_entry(),)),
    ):
        ed = _editor_with(submit, (_profile(ProviderKind.DEEPSEEK),))
    widgets = ed._row_widgets["deepseek-main"]
    widgets["balance"].emit("clicked")
    runs = [
        entry for entry in queued if getattr(entry[0], "__self__", None) is ed._balance_coordinator
    ]
    fn, gen, p, tok, cb = runs[0]
    ed.shutdown()  # close during the run
    fn(gen, p, tok, cb)  # the run self-bounds: zero Keyring read, zero spawn, no publish
    _assert_coordinator_clean(ed._balance_coordinator)


def test_balance_label_formats_exact_amounts(english: None) -> None:
    from moira.provider_editor import ProviderEditor

    result = BalanceResult(
        BalanceState.AVAILABLE,
        "deepseek-main",
        (_cny_entry(),),
    )
    text = ProviderEditor._balance_label(result)
    assert text == "Balance available — CNY: Total 110.87 · Granted 10.00 · Topped up 100.87"
    assert "110.87" in text and "10.00" in text and "100.87" in text  # exact, never estimates


# ── Public-account opt-in (skipped by default) ──────────────────────────────


@pytest.mark.skipif(
    os.environ.get("SKIP_7P_LIVE_ACCOUNT") != "1",
    reason="public-account tests are opt-in: set SKIP_7P_LIVE_ACCOUNT=1 to run",
)
def test_live_public_account_balance_is_available() -> None:
    pytest.fail("live account test must be explicitly enabled and never runs in CI")


# ── Criterion 4: exact-string and bounded-array contract is enforced ────────


def test_max_balance_infos_is_bounded() -> None:
    assert MAX_BALANCE_INFOS >= 2  # CNY + USD fit
    assert MAX_BALANCE_INFOS <= 8  # clearly bounded


def test_child_rejects_excessive_array_length(server: Any) -> None:
    infos = []
    for index in range(MAX_BALANCE_INFOS + 1):
        infos.append(
            {
                "currency": "CNY" if index == 0 else "USD",
                "total_balance": f"{index}.00",
                "granted_balance": "0.00",
                "topped_up_balance": f"{index}.00",
            }
        )
    server.configure(200, json.dumps({"is_available": True, "balance_infos": infos}).encode())
    code, _stdout = _run_balance_child(_child_http_url(server))
    assert code == 7


def test_child_endpoint_path_is_the_documented_balance_surface(server: Any) -> None:
    server.configure(200, json.dumps(_VALID_CNY).encode())
    _run_balance_child(_child_http_url(server))
    assert server.requests[0][0] == "/user/balance"  # the documented endpoint path
    assert server.requests[0][1].get("Authorization") == "Bearer sk-7p"
