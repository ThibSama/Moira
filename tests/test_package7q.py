"""Package 7q — harden exact balance reporting (ACCEPTANCE_CORRECTION).

Corrections on top of Package 7p:

1. Crash-safe child protocol: exit ``1`` may no longer alias
   NOT_CONFIGURED. The dedicated child runs under a top-level sanitized
   exception boundary (distinct exit ``11`` for any uncaught exception
   or import failure), the child alarm exits with a distinct timeout
   code (``12``) so the child alarm and the parent deadline share ONE
   deterministic timeout state (UNREACHABLE), deep JSON raises are
   caught (RecursionError → INVALID_RESPONSE), and the parent fails
   closed on non-empty stderr, abnormal/signal exits and unknown or
   malformed outcomes — raw stderr is never rendered or retained.
   Credentials that could inject headers (control characters) are
   rejected before any spawn and by the child itself.

2. ``BalanceEntry`` enforces the parser's exact contract: every Decimal
   is finite, non-negative and within the bounded precision/magnitude
   contract of ``parse_amount``.

3. Immediate invalidation: edit, toggle, remove confirmation, credential
   removal and save/rename invalidate test and balance row generations
   at the START of the action, so a late completion performs zero GTK
   writes even when persistence blocks, rejects or fails. Barrier GTK
   tests prove later usability and no stale status.

4. Capability matrix: ``build_snapshot`` accepts local typed profiles as
   a bounded immutable input, deduplicates them against the Hermes
   inventory without credentials, URLs or raw configuration, and derives
   balance support ONLY from ``ProviderKind`` — DeepSeek may report
   ``balance=available``, every other kind and every profile-less
   discovered provider stays UNSUPPORTED. Balance support changes no
   token, cost, usage, quota or activity badge.
"""

from __future__ import annotations

import json
import os
import signal
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

import moira.balance as btest  # noqa: E402
from moira.activity import AgentRuntime  # noqa: E402
from moira.agent_integration import CapabilityReport  # noqa: E402
from moira.balance import (  # noqa: E402
    BalanceEntry,
    BalanceResult,
    BalanceState,
    bounded_balance_refresh,
)
from moira.integrations import (  # noqa: E402
    MAX_PROFILES,
    BoundedResult,
    HermesInventory,
    IntegrationState,
    ProbeOutcome,
    ProviderKind,
    ProviderProfile,
    build_snapshot,
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

#: Depth guaranteed to raise RecursionError in ``json.loads`` on the
#: supported interpreters (the C scanner overflows around 1e5 levels).
_DEEP_DEPTH = 150000


# ── Fixtures (fake Keyring, fake local server, inline idle dispatch) ────────


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

    def clear(_schema: Any, attributes: dict[str, str], _cancellable: Any) -> None:
        if fake["unavailable"]:
            raise RuntimeError("secret vault locked")
        fake["items"].pop((attributes["slug"], attributes["purpose"]), None)

    monkeypatch.setattr(Secret, "password_lookup_sync", lookup)
    monkeypatch.setattr(Secret, "password_store_sync", store)
    monkeypatch.setattr(Secret, "password_clear_sync", clear)
    return tmp_path, fake


@pytest.fixture
def english(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANG", "en_US.UTF-8")
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


def _cny_entry() -> BalanceEntry:
    return BalanceEntry(
        "CNY", btest.Decimal("110.87"), btest.Decimal("10.00"), btest.Decimal("100.87")
    )


def _ok_result(code: int, stdout: str = "", stderr: str = "") -> BoundedResult:
    return BoundedResult(stdout, stderr, code, ProbeOutcome.OK)


def _child_http_url(server: Any, path: str = "/user/balance") -> str:
    return f"http://127.0.0.1:{server.port}{path}"


def _run_balance_child(
    url: str,
    *,
    policy: str = "local",
    key: str = "sk-7q",
    connect: float = 1.0,
    read: float = 1.0,
    total: float = 5.0,
    cap: int = 65536,
) -> tuple[int, str, bytes]:
    """Run the dedicated balance child directly (stdout decoded, raw stderr)."""
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
        timeout=30,
    )
    return proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr


def _assert_no_orphan_slot(coord: Any) -> None:
    with coord._lock:
        assert not (coord._pending is not None and coord._inflight is None)


def _assert_coordinator_clean(coord: Any) -> None:
    with coord._lock:
        assert coord._inflight is None
        assert coord._pending is None
        assert not getattr(coord, "_cancelled", [])
        assert coord._draining is False


def _capture_submit() -> tuple[Any, list[tuple[Any, ...]]]:
    queued: list[tuple[Any, ...]] = []

    def submit(fn: Any, *args: Any) -> None:
        queued.append((fn, *args))

    return submit, queued


def _editor_with(submit: Any, profiles: tuple[ProviderProfile, ...]) -> Any:
    from moira.provider_editor import ProviderEditor

    ed = ProviderEditor(submit=submit)
    ed._profiles = profiles
    ed._configured = {p.slug: False for p in profiles}
    ed._show_list()
    return ed


def _editor_persisted(
    submit: Any,
    queued: list[tuple[Any, ...]],
    profiles: tuple[ProviderProfile, ...],
) -> Any:
    """Build an editor whose persisted collection matches ``profiles`` and
    whose constructor reload op has COMPLETED (so later mutation ops start
    immediately instead of parking behind the reload)."""
    from dataclasses import replace

    from moira.persistence import update_settings
    from moira.provider_editor import ProviderEditor

    update_settings(lambda s: replace(s, provider_profiles=tuple(profiles)))
    ed = ProviderEditor(submit=submit)
    ed._configured = {p.slug: False for p in profiles}
    ed._show_list()
    for entry in _op_runs(queued, ed):
        fn, op, gen, ev = entry
        fn(op, gen, ev)  # the constructor's reload completes → rows from disk
    return ed


def _balance_runs(queued: list[tuple[Any, ...]], ed: Any) -> list[tuple[Any, ...]]:
    return [
        entry
        for entry in queued
        if getattr(entry[0], "__self__", None) is ed._balance_coordinator
    ]


def _op_runs(queued: list[tuple[Any, ...]], ed: Any) -> list[tuple[Any, ...]]:
    return [
        entry
        for entry in queued
        if getattr(entry[0], "__self__", None) is ed and entry[0].__name__ == "_run_op"
    ]


def _capabilities() -> dict[AgentRuntime, CapabilityReport]:
    return {
        AgentRuntime.CLAUDE: CapabilityReport("full", ""),
        AgentRuntime.CODEX: CapabilityReport("session_owned", ""),
        AgentRuntime.HERMES: CapabilityReport("full", ""),
    }


def _full_inventory() -> HermesInventory:
    return HermesInventory(
        IntegrationState.AVAILABLE,
        version="0.20.0",
        main_provider="deepseek",
        main_model="deepseek-v4-flash",
        named=(("openrouter", "o3-mini"),),
    )


# ── Finding 1: crash-safe child protocol (criteria 2–4) ─────────────────────


def test_child_crash_never_aliases_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """RED on 1ff28a1: an unhandled crash exited 1 → NOT_CONFIGURED while
    the parent ignored stderr. Post-fix the distinct crash code AND the
    non-empty stderr each fail closed to INVALID_RESPONSE."""
    cases = (
        (1, "Traceback (most recent call last):\nboom"),  # crashed with the old exit code
        (11, ""),  # the new sanitized crash code
        (1, "RuntimeError: boom"),  # crash text on stderr with the old code
    )
    for code, stderr in cases:
        monkeypatch.setattr(
            btest,
            "run_bounded",
            lambda *a, code=code, stderr=stderr, **k: _ok_result(code, "", stderr),
        )
        result = bounded_balance_refresh(_profile(), "sk-x")
        assert result.state is BalanceState.INVALID_RESPONSE, (code, stderr)


def test_exit_stderr_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    """The full exit/stderr matrix: abnormal exits and ANY stderr fail
    closed to INVALID_RESPONSE; clean codes keep their mapping."""
    valid = json.dumps(
        {
            "is_available": True,
            "currencies": [
                {"currency": "CNY", "total_balance": "1", "granted_balance": "0",
                 "topped_up_balance": "1"}
            ],
        }
    )
    insufficient = json.dumps(
        {
            "is_available": False,
            "currencies": [
                {"currency": "CNY", "total_balance": "1", "granted_balance": "0",
                 "topped_up_balance": "1"}
            ],
        }
    )
    cases: list[tuple[int, str, str, BalanceState]] = [
        (0, valid, "", BalanceState.AVAILABLE),
        (3, insufficient, "", BalanceState.INSUFFICIENT),
        (1, "", "", BalanceState.INVALID_RESPONSE),  # code 1 removed (7r): no valid-state alias
        (7, "", "", BalanceState.INVALID_RESPONSE),
        (12, "", "", BalanceState.UNREACHABLE),  # child alarm = parent deadline state
        (0, valid, "warning", BalanceState.INVALID_RESPONSE),  # RED pre-fix
        (1, "", "Traceback", BalanceState.INVALID_RESPONSE),  # RED pre-fix
        (4, "", "log noise", BalanceState.INVALID_RESPONSE),  # RED pre-fix
        (0, valid, " ", BalanceState.INVALID_RESPONSE),  # 7r: whitespace-only stderr
        (2, "\n", "", BalanceState.INVALID_RESPONSE),  # 7r: whitespace-only stdout
        (-9, "", "", BalanceState.INVALID_RESPONSE),  # signal exit
        (-15, "", "", BalanceState.INVALID_RESPONSE),  # signal exit
        (42, "", "", BalanceState.INVALID_RESPONSE),  # unknown code
        (0, "{broken", "", BalanceState.INVALID_RESPONSE),  # malformed stdout
    ]
    for code, stdout, stderr, expected in cases:
        monkeypatch.setattr(
            btest,
            "run_bounded",
            lambda *a, code=code, stdout=stdout, stderr=stderr, **k: _ok_result(
                code, stdout, stderr
            ),
        )
        result = bounded_balance_refresh(_profile(), "sk-x")
        assert result.state is expected, (code, stderr, result.state)


def test_real_child_deep_json_fails_closed_no_traceback(server: Any) -> None:
    """RED on 1ff28a1: a deep JSON body raised an uncaught RecursionError
    → exit 1 (aliased NOT_CONFIGURED) with a traceback on stderr. Post-fix:
    clean INVALID_RESPONSE exit, no output, no traceback, bounded/reaped."""
    body = ("[" * _DEEP_DEPTH + "]" * _DEEP_DEPTH).encode()
    server.configure(200, body)
    t0 = time.monotonic()
    code, stdout, stderr = _run_balance_child(_child_http_url(server), cap=400000)
    elapsed = time.monotonic() - t0
    assert code == 7  # INVALID_RESPONSE — never 1 (NOT_CONFIGURED) nor -14
    assert stdout.strip() == ""
    assert b"RecursionError" not in stderr
    assert b"Traceback" not in stderr
    assert elapsed < 15.0  # bounded and reaped


def test_parent_deep_json_stdout_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """RED on 1ff28a1: RecursionError escaped the parent decode. Post-fix:
    deeply nested stdout from a misbehaving child is INVALID_RESPONSE."""
    deep = "[" * _DEEP_DEPTH + "]" * _DEEP_DEPTH
    monkeypatch.setattr(btest, "run_bounded", lambda *a, **k: _ok_result(0, deep))
    result = bounded_balance_refresh(_profile(), "sk-x")
    assert result.state is BalanceState.INVALID_RESPONSE


def test_real_child_import_failure_is_sanitized(tmp_path: Path) -> None:
    """RED on 1ff28a1: an import failure inside the child crashed to exit 1
    with a traceback. Post-fix the sanitized boundary exits 11 with NO
    stderr, no secret and no failure text."""
    pkg = tmp_path / "fakepkg" / "moira"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("raise RuntimeError('injected-import-failure')\n")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(tmp_path / "fakepkg")
    t0 = time.monotonic()
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            btest._BALANCE_CHILD_CODE,
            "https://api.deepseek.com/user/balance",
            "1",
            "1",
            "5",
            "65536",
            "remote",
        ],
        input=b"sk-7q-secret\n",
        capture_output=True,
        timeout=20,
        env=env,
    )
    elapsed = time.monotonic() - t0
    assert proc.returncode == 11  # distinct crash code, never 1
    assert proc.stderr == b""  # sanitized boundary: no traceback
    assert proc.stdout == b""
    assert b"injected-import-failure" not in proc.stderr
    assert b"sk-7q-secret" not in proc.stderr
    assert elapsed < 15.0


def test_real_child_signal_exit_is_bounded_and_silent(server: Any) -> None:
    """A real SIGTERM'd child dies by the signal (abnormal exit): the parent
    maps it to INVALID_RESPONSE (covered by the matrix above); the child
    itself stays silent (no traceback) and is reaped."""
    server.configure(200, json.dumps(_VALID_CNY).encode(), delay=30.0)
    secret = "sk-7q-signal"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            btest._BALANCE_CHILD_CODE,
            _child_http_url(server),
            "1",
            "10",
            "30",
            "65536",
            "local",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin is not None
    proc.stdin.write(f"{secret}\n".encode())
    proc.stdin.close()
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and not server.requests:
        time.sleep(0.05)
    assert server.requests  # the child reached the in-flight request phase
    proc.send_signal(signal.SIGTERM)
    out, err = proc.communicate(timeout=10)
    assert proc.returncode == -15  # died by SIGTERM: abnormal exit
    assert err == b""  # no traceback on signal death
    assert secret.encode() not in out and secret.encode() not in err  # nothing leaks
    with pytest.raises(ProcessLookupError):
        os.kill(proc.pid, 0)  # reaped, not a zombie


def test_invalid_header_credential_fails_before_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    """RED on 1ff28a1: a credential carrying control characters reached the
    child and could inject headers. Post-fix: rejected before any spawn."""
    spawned = {"n": 0}
    monkeypatch.setattr(
        btest, "run_bounded", lambda *a, **k: spawned.__setitem__("n", spawned["n"] + 1)
    )
    for key in ("sk-x\r\nX-Injected: 1", "sk-x\nX-Injected: 1", "sk-x\x00", "sk-x\x1f", "sk-x\x7f"):
        result = bounded_balance_refresh(_profile(), key)
        assert result.state is BalanceState.INVALID_RESPONSE, repr(key)
    assert spawned["n"] == 0  # zero spawn for header-injecting credentials


def test_child_rejects_invalid_header_credential(server: Any) -> None:
    """The child independently rejects control-character credentials: the
    request never reaches the server, nothing is sent, nothing leaks."""
    code, stdout, _stderr = _run_balance_child(
        _child_http_url(server), key="sk-7q\r\nX-Injected: 1"
    )
    assert code == 7
    assert server.requests == []  # RED pre-fix: the injected request went through
    assert stdout.strip() == ""


def test_child_alarm_is_one_deterministic_timeout_state(server: Any) -> None:
    """RED on 1ff28a1: the child alarm killed the child by SIGALRM (-14 →
    INVALID_RESPONSE) while the parent deadline mapped to UNREACHABLE.
    Post-fix the child alarm exits 12 → UNREACHABLE: ONE timeout state."""
    server.configure(200, json.dumps(_VALID_CNY).encode(), delay=30.0)
    t0 = time.monotonic()
    code, stdout, stderr = _run_balance_child(_child_http_url(server), read=10.0, total=1)
    elapsed = time.monotonic() - t0
    assert code == 12  # the child's own alarm → UNREACHABLE
    assert stdout.strip() == ""
    assert stderr == b""
    assert elapsed < 10.0


def test_parent_deadline_is_the_same_timeout_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        btest, "run_bounded", lambda *a, **k: BoundedResult("", "", None, ProbeOutcome.TIMEOUT)
    )
    result = bounded_balance_refresh(_profile(), "sk-x")
    assert result.state is BalanceState.UNREACHABLE  # same state as the child alarm


def test_parent_rejects_stdout_from_crash_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A crashed child (11) that somehow emitted bytes still fails closed;
    stderr text never becomes a detail string anywhere."""
    monkeypatch.setattr(
        btest,
        "run_bounded",
        lambda *a, **k: _ok_result(11, '{"is_available": true, "currencies": []}', "boom"),
    )
    result = bounded_balance_refresh(_profile(), "sk-x")
    assert result.state is BalanceState.INVALID_RESPONSE
    assert "boom" not in repr(result)  # raw stderr is never retained


# ── Criterion 5: BalanceEntry invariants ────────────────────────────────────


def test_balance_entry_rejects_non_finite_amounts() -> None:
    for value in (
        btest.Decimal("NaN"),
        btest.Decimal("sNaN"),
        btest.Decimal("Infinity"),
        btest.Decimal("-Infinity"),
    ):
        with pytest.raises(ValueError):
            BalanceEntry("CNY", value, btest.Decimal("1"), btest.Decimal("1"))
        with pytest.raises(ValueError):
            BalanceEntry("CNY", btest.Decimal("1"), value, btest.Decimal("1"))
        with pytest.raises(ValueError):
            BalanceEntry("CNY", btest.Decimal("1"), btest.Decimal("1"), value)


def test_balance_entry_rejects_negative_amounts() -> None:
    with pytest.raises(ValueError):
        BalanceEntry("CNY", btest.Decimal("-0.01"), btest.Decimal("1"), btest.Decimal("1"))
    with pytest.raises(ValueError):
        BalanceEntry("CNY", btest.Decimal("1"), btest.Decimal("-0.00"), btest.Decimal("1"))


def test_balance_entry_rejects_magnitude_and_precision_violations() -> None:
    # Magnitude bound (>= 1e12 rejected).
    with pytest.raises(ValueError):
        BalanceEntry(
            "CNY", btest.Decimal("1000000000000.00"), btest.Decimal("1"), btest.Decimal("1")
        )
    # Significant-digit bound (17 digits rejected).
    with pytest.raises(ValueError):
        BalanceEntry(
            "CNY", btest.Decimal("99999999999.999999"), btest.Decimal("1"), btest.Decimal("1")
        )
    # Fraction-digit bound (7 fraction digits rejected).
    with pytest.raises(ValueError):
        BalanceEntry("CNY", btest.Decimal("0.0000001"), btest.Decimal("1"), btest.Decimal("1"))


def test_balance_entry_accepts_parser_contract_values() -> None:
    entry = BalanceEntry(
        "CNY",
        btest.Decimal("999999999999.9999"),
        btest.Decimal("0.00"),
        btest.Decimal("0"),
    )
    assert entry.total_balance == btest.Decimal("999999999999.9999")
    assert format(entry.granted_balance, "f") == "0.00"  # exact text preserved
    # Every amount accepted by the parser is accepted by the entry.
    for text in ("0", "0.5", "12.3400", "999999999999.99"):
        parsed = btest.parse_amount(text)
        assert parsed is not None
        BalanceEntry("CNY", parsed, btest.Decimal("0"), btest.Decimal("0"))


def test_balance_result_rejects_invalid_entries() -> None:
    with pytest.raises(ValueError):
        BalanceResult(
            BalanceState.AVAILABLE,
            "deepseek-main",
            (BalanceEntry("CNY", btest.Decimal("NaN"), btest.Decimal("1"), btest.Decimal("1")),),
        )
    with pytest.raises(ValueError):
        BalanceResult(
            BalanceState.AVAILABLE,
            "deepseek-main",
            (BalanceEntry("CNY", btest.Decimal("-5"), btest.Decimal("1"), btest.Decimal("1")),),
        )


# ── Finding 2: immediate invalidation barriers (criteria 6–7) ───────────────


def _available_result() -> BalanceResult:
    return BalanceResult(BalanceState.AVAILABLE, "deepseek-main", (_cny_entry(),))


def test_balance_edit_begins_invalidates_generation(
    env: tuple[Path, dict[str, Any]], idle_inline: None, english: None
) -> None:
    """RED on 1ff28a1: opening the edit form did NOT invalidate the
    in-flight balance generation — the late AVAILABLE result overwrote
    the row. Post-fix the invalidation happens when edit BEGINS."""
    submit, queued = _capture_submit()
    with patch("moira.provider_editor.run_balance_refresh", return_value=_available_result()):
        ed = _editor_persisted(submit, queued, (_profile(ProviderKind.DEEPSEEK),))
    widgets = ed._row_widgets["deepseek-main"]
    widgets["balance"].emit("clicked")
    runs = _balance_runs(queued, ed)
    assert len(runs) == 1
    fn, gen, p, tok, cb = runs[0]
    ed._on_edit_clicked(None, "deepseek-main")  # edit begins → invalidation NOW
    assert ed._form_area.get_visible()
    fn(gen, p, tok, cb)  # the late completion performs ZERO GTK writes
    assert widgets["balance_status"].get_text() == "Not checked"
    assert "Balance available" not in widgets["balance_status"].get_text()
    # Later usability: cancel the form, the rebuilt row accepts a new run.
    ed.form_cancel_button.emit("clicked")
    fresh = ed._row_widgets["deepseek-main"]
    assert fresh["balance_status"].get_text() == "Not checked"
    fresh["balance"].emit("clicked")
    runs2 = _balance_runs(queued, ed)
    fn2, gen2, p2, tok2, cb2 = runs2[-1]
    fn2(gen2, p2, tok2, cb2)
    assert "Balance available" in fresh["balance_status"].get_text()
    ed.shutdown()
    _assert_coordinator_clean(ed._balance_coordinator)


def test_balance_toggle_begins_invalidates_generation(
    env: tuple[Path, dict[str, Any]], idle_inline: None, english: None
) -> None:
    """RED on 1ff28a1: a toggle parked the persistence op but the in-flight
    balance run still passed the epoch/click guards and wrote its result.
    Post-fix the toggle invalidates immediately — even while the save op
    is still parked (persistence blocks)."""
    submit, queued = _capture_submit()
    with patch("moira.provider_editor.run_balance_refresh", return_value=_available_result()):
        ed = _editor_persisted(submit, queued, (_profile(ProviderKind.DEEPSEEK),))
    widgets = ed._row_widgets["deepseek-main"]
    widgets["balance"].emit("clicked")
    runs = _balance_runs(queued, ed)
    fn, gen, p, tok, cb = runs[0]
    ed._on_enabled_toggled(widgets["switch"], False, "deepseek-main")  # toggle begins
    assert _op_runs(queued, ed)  # the save op is queued (persistence pending)
    fn(gen, p, tok, cb)  # late completion: zero GTK writes
    assert widgets["balance_status"].get_text() == "Not checked"
    assert widgets["test_status"].get_text() == ""
    # The parked save op never completes in this branch: the row must not
    # be stuck on a stale status while persistence blocks.
    assert "Checking balance" not in widgets["balance_status"].get_text()
    # Persistence completes: rebuild; the row is fresh and usable.
    op_fn, op, op_gen, op_ev = _op_runs(queued, ed)[-1]
    op_fn(op, op_gen, op_ev)
    fresh = ed._row_widgets["deepseek-main"]
    assert fresh["balance_status"].get_text() == "Not checked"
    fresh["balance"].emit("clicked")
    runs2 = _balance_runs(queued, ed)
    fn2, gen2, p2, tok2, cb2 = runs2[-1]
    fn2(gen2, p2, tok2, cb2)
    assert "Balance available" in fresh["balance_status"].get_text()
    ed.shutdown()
    _assert_coordinator_clean(ed._balance_coordinator)


def test_balance_remove_confirmation_invalidates_generation(
    env: tuple[Path, dict[str, Any]], idle_inline: None, english: None
) -> None:
    """RED on 1ff28a1: the remove confirmation hid the row's balance button
    but did not invalidate the in-flight run; the late result still wrote.
    Post-fix the confirmation itself invalidates; cancelling leaves a
    truthful, usable row."""
    submit, queued = _capture_submit()
    with patch("moira.provider_editor.run_balance_refresh", return_value=_available_result()):
        ed = _editor_persisted(submit, queued, (_profile(ProviderKind.DEEPSEEK),))
    widgets = ed._row_widgets["deepseek-main"]
    widgets["balance"].emit("clicked")
    runs = _balance_runs(queued, ed)
    fn, gen, p, tok, cb = runs[0]
    ed._on_remove_clicked(None, "deepseek-main")  # remove confirmation begins
    assert widgets["confirm"].get_visible()
    fn(gen, p, tok, cb)  # late completion: zero GTK writes
    assert widgets["balance_status"].get_text() == "Not checked"
    ed._on_cancel_remove(None, "deepseek-main")  # cancellation: row usable again
    fresh = ed._row_widgets["deepseek-main"]
    fresh["balance"].emit("clicked")
    runs2 = _balance_runs(queued, ed)
    fn2, gen2, p2, tok2, cb2 = runs2[-1]
    fn2(gen2, p2, tok2, cb2)
    assert "Balance available" in fresh["balance_status"].get_text()
    ed.shutdown()
    _assert_coordinator_clean(ed._balance_coordinator)


def test_balance_confirm_remove_invalidates_generation(
    env: tuple[Path, dict[str, Any]], idle_inline: None, english: None
) -> None:
    """The confirmed removal invalidates immediately; the late completion
    writes nothing and the removal op removes the profile."""
    submit, queued = _capture_submit()
    with patch("moira.provider_editor.run_balance_refresh", return_value=_available_result()):
        ed = _editor_persisted(submit, queued, (_profile(ProviderKind.DEEPSEEK),))
    widgets = ed._row_widgets["deepseek-main"]
    widgets["balance"].emit("clicked")
    runs = _balance_runs(queued, ed)
    fn, gen, p, tok, cb = runs[0]
    ed._on_confirm_remove(None, "deepseek-main")  # removal begins → invalidation NOW
    assert _op_runs(queued, ed)
    fn(gen, p, tok, cb)
    assert widgets["balance_status"].get_text() == "Not checked"
    op_fn, op, op_gen, op_ev = _op_runs(queued, ed)[-1]
    op_fn(op, op_gen, op_ev)  # removal completes: the row is gone
    assert "deepseek-main" not in ed._row_widgets
    assert ed._profiles == ()
    ed.shutdown()
    _assert_coordinator_clean(ed._balance_coordinator)


def test_balance_credential_removal_invalidates_generation(
    env: tuple[Path, dict[str, Any]], idle_inline: None, english: None
) -> None:
    """RED on 1ff28a1: removing the credential parked a persistence op but
    the in-flight balance run still wrote. Post-fix the removal
    invalidates immediately and the completed removal rebuilds a fresh
    row."""
    submit, queued = _capture_submit()
    with patch("moira.provider_editor.run_balance_refresh", return_value=_available_result()):
        ed = _editor_persisted(submit, queued, (_profile(ProviderKind.DEEPSEEK),))
    widgets = ed._row_widgets["deepseek-main"]
    widgets["balance"].emit("clicked")
    runs = _balance_runs(queued, ed)
    fn, gen, p, tok, cb = runs[0]
    ed._on_remove_credential(None, "deepseek-main")  # removal begins → invalidation NOW
    assert _op_runs(queued, ed)
    fn(gen, p, tok, cb)  # late completion: zero GTK writes
    assert widgets["balance_status"].get_text() == "Not checked"
    op_fn, op, op_gen, op_ev = _op_runs(queued, ed)[-1]
    op_fn(op, op_gen, op_ev)  # completes → rebuild → fresh usable row
    fresh = ed._row_widgets["deepseek-main"]
    assert fresh["balance_status"].get_text() == "Not checked"
    fresh["balance"].emit("clicked")
    runs2 = _balance_runs(queued, ed)
    fn2, gen2, p2, tok2, cb2 = runs2[-1]
    fn2(gen2, p2, tok2, cb2)
    assert "Balance available" in fresh["balance_status"].get_text()
    ed.shutdown()
    _assert_coordinator_clean(ed._balance_coordinator)


def test_balance_save_rename_invalidates_generation(
    env: tuple[Path, dict[str, Any]], idle_inline: None, english: None
) -> None:
    """RED on 1ff28a1: saving/renaming from the form did not invalidate the
    in-flight run — the late result still passed the old row's guards.
    Post-fix the save invalidates immediately, the renamed row is fresh
    and a new run renders on the new slug."""
    submit, queued = _capture_submit()
    with patch("moira.provider_editor.run_balance_refresh", return_value=_available_result()):
        ed = _editor_persisted(submit, queued, (_profile(ProviderKind.DEEPSEEK),))
    widgets = ed._row_widgets["deepseek-main"]
    widgets["balance"].emit("clicked")
    runs = _balance_runs(queued, ed)
    fn, gen, p, tok, cb = runs[0]
    ed._show_form(_profile(ProviderKind.DEEPSEEK))  # edit the profile
    ed.slug_entry.set_text("deepseek-renamed")  # rename
    ed._on_form_save()  # save begins → invalidation NOW
    assert _op_runs(queued, ed)
    fn(gen, p, tok, cb)  # late completion (old slug token): zero GTK writes
    assert widgets["balance_status"].get_text() == "Not checked"
    op_fn, op, op_gen, op_ev = _op_runs(queued, ed)[-1]
    op_fn(op, op_gen, op_ev)  # rename completes → rebuilt under the new slug
    assert "deepseek-main" not in ed._row_widgets
    fresh = ed._row_widgets["deepseek-renamed"]
    assert fresh["balance_status"].get_text() == "Not checked"
    fresh["balance"].emit("clicked")
    runs2 = _balance_runs(queued, ed)
    fn2, gen2, p2, tok2, cb2 = runs2[-1]
    fn2(gen2, p2, tok2, cb2)
    assert "Balance available" in fresh["balance_status"].get_text()
    ed.shutdown()
    _assert_coordinator_clean(ed._balance_coordinator)


def test_balance_persistence_rejection_leaves_no_stale_status(
    env: tuple[Path, dict[str, Any]], idle_inline: None, english: None
) -> None:
    """When the persistence submit is REJECTED, the invalidation already
    reset the row: no stale balance and no stuck working state."""
    state: dict[str, Any] = {"fail_next": False, "queued": []}

    def submit(fn: Any, *args: Any) -> None:
        if state["fail_next"]:
            state["fail_next"] = False
            raise RuntimeError("executor closed")
        state["queued"].append((fn, *args))

    with patch("moira.provider_editor.run_balance_refresh", return_value=_available_result()):
        ed = _editor_persisted(submit, state["queued"], (_profile(ProviderKind.DEEPSEEK),))
    widgets = ed._row_widgets["deepseek-main"]
    widgets["balance"].emit("clicked")
    runs = _balance_runs(state["queued"], ed)
    fn, gen, p, tok, cb = runs[0]
    state["fail_next"] = True
    ed._on_enabled_toggled(widgets["switch"], False, "deepseek-main")  # rejection path
    fn(gen, p, tok, cb)  # late completion: zero GTK writes
    assert widgets["balance_status"].get_text() == "Not checked"
    assert ed.status_label.get_text() == "Operation failed."
    # Later usability: the editor accepts a new balance run after the
    # rejection (the failed generation never latches).
    widgets["balance"].emit("clicked")
    runs2 = _balance_runs(state["queued"], ed)
    assert len(runs2) == 2
    fn2, gen2, p2, tok2, cb2 = runs2[-1]
    fn2(gen2, p2, tok2, cb2)
    assert "Balance available" in widgets["balance_status"].get_text()
    ed.shutdown()
    _assert_coordinator_clean(ed._balance_coordinator)


def test_balance_persistence_failure_leaves_no_stale_status(
    env: tuple[Path, dict[str, Any]], idle_inline: None, english: None
) -> None:
    """When the persistence op FAILS (Keyring unavailable), there is no
    rebuild — but the invalidation already happened, so the row stays
    truthful ("Not checked") and the late completion wrote nothing."""
    submit, queued = _capture_submit()
    with patch("moira.provider_editor.run_balance_refresh", return_value=_available_result()):
        ed = _editor_persisted(submit, queued, (_profile(ProviderKind.DEEPSEEK),))
    env[1]["unavailable"] = True  # from here on the Keyring fails closed
    widgets = ed._row_widgets["deepseek-main"]
    widgets["balance"].emit("clicked")
    runs = _balance_runs(queued, ed)
    fn, gen, p, tok, cb = runs[0]
    ed._on_remove_credential(None, "deepseek-main")  # begins → invalidation NOW
    fn(gen, p, tok, cb)  # late completion: zero GTK writes
    assert widgets["balance_status"].get_text() == "Not checked"
    op_fn, op, op_gen, op_ev = _op_runs(queued, ed)[-1]
    op_fn(op, op_gen, op_ev)  # the op fails: no rebuild, no stale balance
    assert widgets["balance_status"].get_text() == "Not checked"
    assert "Keyring unavailable." in ed.status_label.get_text()
    ed.shutdown()
    _assert_coordinator_clean(ed._balance_coordinator)


def test_connection_test_generations_invalidated_too(
    env: tuple[Path, dict[str, Any]], idle_inline: None, english: None
) -> None:
    """Criterion 6 covers the connection-test generations as well: a toggle
    discards the in-flight test result (zero GTK writes)."""
    from moira.connection_test import ConnectionResult, ConnectionState

    submit, queued = _capture_submit()
    with patch(
        "moira.provider_editor.run_connection_test",
        return_value=ConnectionResult(ConnectionState.CONNECTED, "deepseek-main"),
    ):
        ed = _editor_persisted(submit, queued, (_profile(ProviderKind.DEEPSEEK),))
    widgets = ed._row_widgets["deepseek-main"]
    widgets["test"].emit("clicked")
    runs = [
        entry
        for entry in queued
        if getattr(entry[0], "__self__", None) is ed._connection_coordinator
    ]
    assert len(runs) == 1
    fn, gen, p, tok, cb = runs[0]
    ed._on_enabled_toggled(widgets["switch"], False, "deepseek-main")  # toggle begins
    fn(gen, p, tok, cb)  # would publish CONNECTED — discarded by the token
    assert widgets["test_status"].get_text() == ""
    op_fn, op, op_gen, op_ev = _op_runs(queued, ed)[-1]
    op_fn(op, op_gen, op_ev)
    assert ed._row_widgets["deepseek-main"]["test_status"].get_text() == ""
    ed.shutdown()
    _assert_coordinator_clean(ed._connection_coordinator)


def test_balance_close_barrier_is_unchanged(
    env: tuple[Path, dict[str, Any]], idle_inline: None, english: None
) -> None:
    """Close remains the terminal barrier: queued work self-bounds and
    nothing publishes (the pre-existing editing/closing contract)."""
    submit, queued = _capture_submit()
    with patch("moira.provider_editor.run_balance_refresh", return_value=_available_result()):
        ed = _editor_with(submit, (_profile(ProviderKind.DEEPSEEK),))
    widgets = ed._row_widgets["deepseek-main"]
    widgets["balance"].emit("clicked")
    runs = _balance_runs(queued, ed)
    fn, gen, p, tok, cb = runs[0]
    ed.shutdown()
    fn(gen, p, tok, cb)  # self-bounds: zero runner calls, zero publishes
    assert widgets["balance_status"].get_text() == "Checking balance…"
    _assert_coordinator_clean(ed._balance_coordinator)


# ── Finding 3: capability matrix from typed kinds (criteria 8–9) ────────────


def _matrix(snapshot: Any) -> dict[tuple[str, str], Any]:
    return {(c.provider, c.capability): c for c in snapshot.capabilities}


def test_snapshot_discovered_balance_unsupported_without_typed_profile() -> None:
    """RED on 1ff28a1: discovered providers were marked
    balance=NOT_CONFIGURED/deferred. Without a typed local profile there
    is no adapter knowledge: balance is UNSUPPORTED (fail closed), while
    cost stays deferred and untouched."""
    snapshot = build_snapshot(
        hermes=_full_inventory(), capabilities=_capabilities(), quota_readings=()
    )
    caps = _matrix(snapshot)
    for slug in ("deepseek", "openrouter"):
        assert caps[(slug, "activity")].state is IntegrationState.UNSUPPORTED
        assert caps[(slug, "balance")].state is IntegrationState.UNSUPPORTED
        assert caps[(slug, "balance")].detail == ""
        assert caps[(slug, "cost")].state is IntegrationState.NOT_CONFIGURED
        assert caps[(slug, "cost")].detail == "deferred"


def test_snapshot_typed_deepseek_profile_balance_available() -> None:
    """RED on 1ff28a1: build_snapshot accepted no typed Moira profiles and
    never reported the implemented DeepSeek adapter as available."""
    profile = _profile(ProviderKind.DEEPSEEK, slug="deepseek-main", label="DeepSeek main")
    snapshot = build_snapshot(
        hermes=_full_inventory(),
        capabilities=_capabilities(),
        quota_readings=(),
        profiles=(profile,),
    )
    caps = _matrix(snapshot)
    assert caps[("deepseek-main", "balance")].state is IntegrationState.AVAILABLE
    assert caps[("deepseek-main", "balance")].detail == ""
    assert caps[("deepseek-main", "cost")].state is IntegrationState.NOT_CONFIGURED
    assert caps[("deepseek-main", "cost")].detail == "deferred"
    # The typed profile adds an identity with its user-visible label.
    provider = next(p for p in snapshot.providers if p.slug == "deepseek-main")
    assert provider.label == "DeepSeek main"
    # The runtime rows and the inventory assignments are untouched.
    assert [p.slug for p in snapshot.providers[:3]] == ["claude", "codex", "hermes"]
    main_assignments = [a for a in snapshot.assignments if a.provider.slug == "deepseek"]
    assert main_assignments and main_assignments[0].role == "main"


def test_snapshot_non_deepseek_kinds_remain_unsupported() -> None:
    for kind in (
        ProviderKind.OPENAI,
        ProviderKind.OPENROUTER,
        ProviderKind.ANTHROPIC,
        ProviderKind.OPENAI_COMPATIBLE,
        ProviderKind.LOCAL,
        ProviderKind.CUSTOM,
    ):
        slug = f"p-{kind.value}"
        base_url = "http://127.0.0.1:9/v1" if kind is ProviderKind.LOCAL else ""
        profile = _profile(kind, slug=slug, base_url=base_url)
        snapshot = build_snapshot(
            hermes=_full_inventory(),
            capabilities=_capabilities(),
            quota_readings=(),
            profiles=(profile,),
        )
        caps = _matrix(snapshot)
        assert caps[(slug, "balance")].state is IntegrationState.UNSUPPORTED, kind
        assert caps[(slug, "balance")].detail == ""
        assert caps[(slug, "cost")].state is IntegrationState.NOT_CONFIGURED
        assert caps[(slug, "cost")].detail == "deferred"


def test_snapshot_balance_support_changes_no_other_badges() -> None:
    """Criterion 8: reporting DeepSeek balance=available changes no token,
    cost, usage, quota or activity badge — only the new balance badge."""
    kwargs: dict[str, Any] = {
        "hermes": _full_inventory(),
        "capabilities": _capabilities(),
        "quota_readings": (),
    }
    base = build_snapshot(**kwargs)
    base_caps = {(c.provider, c.capability): (c.state, c.detail) for c in base.capabilities}
    with_profile = build_snapshot(**kwargs, profiles=(_profile(ProviderKind.DEEPSEEK),))
    with_caps = {
        (c.provider, c.capability): (c.state, c.detail) for c in with_profile.capabilities
    }
    for key, value in base_caps.items():
        assert with_caps[key] == value, key
    assert with_caps[("deepseek-main", "balance")] == (IntegrationState.AVAILABLE, "")
    assert with_caps[("deepseek-main", "cost")] == (IntegrationState.NOT_CONFIGURED, "deferred")
    # The profile adds no assignment and no runtime (inventory-only).
    assert "deepseek-main" not in {a.provider.slug for a in with_profile.assignments}
    assert [r.slug for r in with_profile.runtimes] == ["claude", "codex", "hermes"]


def test_snapshot_local_profile_dedupes_with_inventory() -> None:
    """A local profile whose slug matches a discovered provider governs the
    capability (typed kind wins over the bare inventory name) and appears
    once in the provider list."""
    profile = _profile(ProviderKind.DEEPSEEK, slug="deepseek", label="DeepSeek main")
    snapshot = build_snapshot(
        hermes=_full_inventory(),
        capabilities=_capabilities(),
        quota_readings=(),
        profiles=(profile,),
    )
    slugs = [p.slug for p in snapshot.providers]
    assert slugs.count("deepseek") == 1
    caps = _matrix(snapshot)
    assert caps[("deepseek", "balance")].state is IntegrationState.AVAILABLE
    # The inventory assignment (main) is preserved alongside.
    main_assignments = [a for a in snapshot.assignments if a.provider.slug == "deepseek"]
    assert main_assignments and main_assignments[0].model == "deepseek-v4-flash"


def test_snapshot_profiles_never_leak_credentials_urls_or_raw_config() -> None:
    """Criterion 9: local profiles feed the snapshot WITHOUT credentials,
    URLs or raw configuration — only slugs, labels and kind-derived
    capability states survive."""
    profile = _profile(
        ProviderKind.DEEPSEEK,
        slug="deepseek-main",
        label="DeepSeek main",
        base_url="https://secret-balance-host.invalid/v1",
        hermes_label="secret-hermes-label",
    )
    snapshot = build_snapshot(
        hermes=_full_inventory(),
        capabilities=_capabilities(),
        quota_readings=(),
        profiles=(profile,),
    )
    blob = repr(snapshot)
    for forbidden in (
        "secret-balance-host.invalid",
        "secret-hermes-label",
        "https://",
        "sk-",
        "api_key",
        "base_url",
        "hermes_label",
    ):
        assert forbidden not in blob, forbidden
    assert "DeepSeek main" in blob  # the user-visible label survives


def test_snapshot_profiles_input_is_bounded_and_typed() -> None:
    kwargs: dict[str, Any] = {
        "hermes": _full_inventory(),
        "capabilities": _capabilities(),
        "quota_readings": (),
    }
    with pytest.raises(ValueError):
        build_snapshot(**kwargs, profiles="not-a-sequence")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        build_snapshot(**kwargs, profiles=iter(()))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        build_snapshot(**kwargs, profiles=(1, 2))  # type: ignore[arg-type]
    too_many = tuple(
        _profile(ProviderKind.DEEPSEEK, slug=f"p-{index}") for index in range(MAX_PROFILES + 1)
    )
    with pytest.raises(ValueError):
        build_snapshot(**kwargs, profiles=too_many)


def test_snapshot_runtime_balance_badges_stay_unsupported() -> None:
    """The three monitored runtimes never report balance support."""
    snapshot = build_snapshot(
        hermes=_full_inventory(), capabilities=_capabilities(), quota_readings=()
    )
    caps = _matrix(snapshot)
    for slug in ("claude", "codex", "hermes"):
        assert caps[(slug, "balance")].state is IntegrationState.UNSUPPORTED


# ── Preserved 7p contracts (guards against regressions) ─────────────────────


def test_non_deepseek_kind_unsupported_before_keyring_and_spawn(
    env: tuple[Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from moira.balance import run_balance_refresh

    spawned = {"n": 0}
    monkeypatch.setattr(
        btest, "run_bounded", lambda *a, **k: spawned.__setitem__("n", spawned["n"] + 1)
    )
    result = run_balance_refresh(_profile(ProviderKind.OPENROUTER, slug="or-main"))
    assert result.state is BalanceState.UNSUPPORTED
    assert spawned["n"] == 0
    assert env[1]["lookups"] == 0


def test_refresh_uses_official_endpoint_only(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(args: list[str], **kwargs: Any) -> BoundedResult:
        captured["args"] = list(args)
        return _ok_result(7)

    monkeypatch.setattr(btest, "run_bounded", fake_run)
    result = bounded_balance_refresh(_profile(base_url="https://evil.example/v1"), "sk-x")
    assert result.state is BalanceState.INVALID_RESPONSE
    assert captured["args"][3] == "https://api.deepseek.com/user/balance"
    assert "evil.example" not in " ".join(captured["args"])
