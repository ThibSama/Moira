"""Package 7p — exact DeepSeek balance refresh (GTK-free).

Shows EXACT balances from the official read-only DeepSeek endpoint
``GET https://api.deepseek.com/user/balance`` (Bearer auth) — never
estimates, conversions, aggregation or persistent history. Only
``ProviderKind.DEEPSEEK`` is supported: every other kind is UNSUPPORTED
BEFORE any Keyring read or spawn, and a missing credential is
NOT_CONFIGURED before any network.

Amounts are parsed with ``Decimal`` (exact fixed point, never float):
bounded decimal text is preserved for rendering, and signs, exponents,
NaN/Infinity, control characters and excessive precision or magnitude
are all rejected. The payload contract is strict — documented top-level
keys (``is_available`` + ``balance_infos``) and item keys (``currency``,
``total_balance``, ``granted_balance``, ``topped_up_balance``) only, a
REAL boolean, a non-empty bounded array, unique CNY/USD entries and
exact string amounts; extra, missing, secret/account-bearing or
malformed data is INVALID_RESPONSE. Nothing is inferred, no
undocumented sum is enforced, nothing is rounded, converted or
combined: Total, Granted and Topped up are rendered separately in
deterministic currency order (CNY then USD).

The run reuses the Package 7l boundary: the Keyring is read IMMEDIATELY
before execution and the credential is handed to a dedicated child
through a PRIVATE stdin pipe (never argv, environment, disk or logs);
the child resolves the official endpoint EXACTLY ONCE into an immutable
``ValidatedTarget``, connects DIRECTLY to the validated sockaddr,
verifies the peer (``getpeername`` against the validated sockaddr, then
verified TLS with SNI/hostname on the ORIGINAL hostname) BEFORE any
credential leaves, and uses no redirects and no proxy environment, with
bounded connect/read/total deadlines, bounded body/output and
process-group reaping. The child may return ONLY minimal canonical
balance JSON through bounded stdout (never raw bodies, headers, hosts,
IPs, metadata, exceptions or secrets); for every non-amount outcome it
prints NOTHING, and any stdout there is INVALID_RESPONSE.

Mapping (strict, fail closed): 200 → AVAILABLE/INSUFFICIENT from
``is_available``; 401/403 → AUTH_FAILED; 402 → INSUFFICIENT without
invented amounts; 429 → RATE_LIMITED; 5xx → SERVER_ERROR; transport and
TLS failures stay distinct (UNREACHABLE / TLS_ERROR); every other
status and every unknown outcome is INVALID_RESPONSE. The child is
crash-safe (Package 7q): a top-level sanitized exception boundary turns
any uncaught exception or import failure into the distinct exit 11 with
nothing on stderr (never a traceback, never an alias of exit 1 /
NOT_CONFIGURED); the child alarm and the parent deadline share ONE
deterministic timeout state (exit 12 / TIMEOUT → UNREACHABLE); deep
JSON (RecursionError) is failed closed; non-empty stderr, abnormal or
signal exits, unknown codes and malformed stdout are all
INVALID_RESPONSE, and raw stderr is never rendered or retained. A
successful refresh implies NOTHING about token, cost or usage support
(``balance=available`` is reported only for the supported DeepSeek
adapter). Results are ephemeral: nothing is written to config, schema,
History, activity, exports or logs.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from decimal import Decimal as Decimal
from decimal import InvalidOperation
from enum import StrEnum
from typing import Any

from .connection_test import (
    DEFAULT_BODY_CAP as DEFAULT_BODY_CAP,
)
from .connection_test import (
    DEFAULT_CONNECT_TIMEOUT as DEFAULT_CONNECT_TIMEOUT,
)
from .connection_test import (
    DEFAULT_READ_TIMEOUT as DEFAULT_READ_TIMEOUT,
)
from .connection_test import (
    DEFAULT_TOTAL_TIMEOUT as DEFAULT_TOTAL_TIMEOUT,
)
from .connection_test import (
    MAX_KEY_BYTES as MAX_KEY_BYTES,
)
from .integrations import ProbeOutcome, ProviderKind, ProviderProfile, run_bounded
from .secrets import KeyringLookup, inspect_provider_secret

#: The ONLY balance endpoint: the official read-only DeepSeek surface.
#: The profile URL is never used for balance — the endpoint is fixed.
BALANCE_ENDPOINT = "https://api.deepseek.com/user/balance"

#: Maximum accepted number of balance entries; the array must be
#: non-empty and bounded (CNY/USD uniqueness caps it at two in practice).
MAX_BALANCE_INFOS = 4
#: Canonical deterministic currency order for rendering.
_CURRENCY_ORDER = ("CNY", "USD")

#: Bounded decimal-text contract. ASCII digits only (never ``\\d``):
#: an optional fraction, no sign, no exponent, no leading zero on a
#: multi-digit integer part ("0" and "0.5" stay valid).
_AMOUNT_RE = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
#: Bounded amount text length, significant digits, fraction digits and
#: magnitude. Beyond these the amount (and so the response) is invalid.
MAX_AMOUNT_TEXT_LEN = 32
MAX_SIGNIFICANT_DIGITS = 16
MAX_FRACTION_DIGITS = 6
MAX_AMOUNT_MAGNITUDE = Decimal("1e12")


class BalanceState(StrEnum):
    """Exact closed availability set of one balance refresh.

    The enum values are the only strings that may flow through the
    system: AVAILABLE/INSUFFICIENT are the only amount-bearing states,
    every other outcome fails closed with no amounts.
    """

    AVAILABLE = "available"
    INSUFFICIENT = "insufficient"
    NOT_CONFIGURED = "not_configured"
    AUTH_FAILED = "auth_failed"
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    UNREACHABLE = "unreachable"
    TLS_ERROR = "tls_error"
    INVALID_RESPONSE = "invalid_response"
    UNSUPPORTED = "unsupported"
    CANCELLED = "cancelled"


def parse_amount(text: object) -> Decimal | None:
    """Strictly parse one exact amount; None on ANY deviation.

    The amount must be a bounded string of ASCII digits with an optional
    fraction — no sign, no exponent, no NaN/Infinity, no controls, no
    leading zero on a multi-digit integer part, bounded length, bounded
    significant digits, bounded fraction digits and bounded magnitude.
    Returns an exact ``Decimal`` (never float) whose ``"f"`` rendering
    preserves the bounded decimal text.
    """
    if not isinstance(text, str) or not text:
        return None
    if len(text) > MAX_AMOUNT_TEXT_LEN:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        return None
    if not _AMOUNT_RE.fullmatch(text):
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    if not _amount_decimal_ok(value):
        return None
    return value


def _amount_decimal_ok(value: Decimal) -> bool:
    """True when the Decimal respects the parser's exact bounded contract.

    The SINGLE shared check behind ``parse_amount`` and ``BalanceEntry``:
    finite, non-negative, below the magnitude bound, within the
    significant-digit bound and within the fraction-digit bound. A
    ``BalanceEntry`` can therefore never hold an amount the parser would
    reject.
    """
    if not value.is_finite() or value < 0 or value.is_signed() or value >= MAX_AMOUNT_MAGNITUDE:
        return False
    if len(value.as_tuple().digits) > MAX_SIGNIFICANT_DIGITS:
        return False
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int) or exponent < -MAX_FRACTION_DIGITS:
        return False
    return True


@dataclass(frozen=True, slots=True)
class BalanceEntry:
    """One validated currency's exact balances (Decimal only).

    Every amount respects the parser's exact bounded contract: finite,
    non-negative, below the magnitude bound, within the significant- and
    fraction-digit bounds — an entry can never hold a value ``parse_amount``
    would reject (Package 7q criterion 5).
    """

    currency: str
    total_balance: Decimal
    granted_balance: Decimal
    topped_up_balance: Decimal

    def __post_init__(self) -> None:
        if self.currency not in _CURRENCY_ORDER:
            raise ValueError(f"currency must be one of {_CURRENCY_ORDER}")
        for name in ("total_balance", "granted_balance", "topped_up_balance"):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise ValueError(f"{name} must be a Decimal")
            if not _amount_decimal_ok(value):
                raise ValueError(f"{name} violates the exact bounded amount contract")


@dataclass(frozen=True, slots=True)
class BalanceResult:
    """GTK-free immutable result of one balance refresh.

    AVAILABLE always carries the exact per-currency entries; INSUFFICIENT
    carries the REAL amounts of a 200 response (a 402 carries none —
    amounts are never invented); every other state carries none. Entries
    are normalized to the deterministic currency order (CNY then USD)
    with unique currencies.
    """

    state: BalanceState
    profile_slug: str = ""
    entries: tuple[BalanceEntry, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.state, BalanceState):
            raise ValueError("state must be a BalanceState value")
        if not isinstance(self.entries, tuple):
            raise ValueError("entries must be a tuple")
        for entry in self.entries:
            if not isinstance(entry, BalanceEntry):
                raise ValueError("entries must contain BalanceEntry values")
        if self.state is BalanceState.AVAILABLE:
            if not self.entries:
                raise ValueError("AVAILABLE results require entries")
        elif self.state is not BalanceState.INSUFFICIENT:
            if self.entries:
                raise ValueError(f"{self.state.value} results must not carry entries")
        seen: set[str] = set()
        for entry in self.entries:
            if entry.currency in seen:
                raise ValueError("duplicate currency entries are ambiguous")
            seen.add(entry.currency)
        ordered = tuple(sorted(self.entries, key=lambda item: _CURRENCY_ORDER.index(item.currency)))
        object.__setattr__(self, "entries", ordered)


#: Exit-code contract of the dedicated balance child (Package 7q: no
#: abnormal exit may alias a valid state). 1 is reserved for
#: NOT_CONFIGURED (the parent normally fails closed before spawning);
#: 0/3 are the only amount-bearing states and print the canonical JSON;
#: 11 is the top-level sanitized crash boundary (any uncaught exception
#: or import failure — never a traceback on stderr, so a crashed child
#: can never become NOT_CONFIGURED); 12 is the child's own alarm, which
#: shares ONE deterministic timeout state with the parent deadline
#: (UNREACHABLE). Every other outcome is explicit — unknown, signal or
#: negative codes map to INVALID_RESPONSE (unknown outcomes never
#: become AVAILABLE). Non-empty stderr fails closed in the parent.
_CHILD_CODES: dict[int, BalanceState] = {
    0: BalanceState.AVAILABLE,
    1: BalanceState.NOT_CONFIGURED,
    2: BalanceState.AUTH_FAILED,
    3: BalanceState.INSUFFICIENT,
    4: BalanceState.UNREACHABLE,
    5: BalanceState.TLS_ERROR,
    6: BalanceState.RATE_LIMITED,
    7: BalanceState.INVALID_RESPONSE,
    8: BalanceState.UNSUPPORTED,
    9: BalanceState.CANCELLED,
    10: BalanceState.SERVER_ERROR,
    11: BalanceState.INVALID_RESPONSE,  # sanitized crash boundary
    12: BalanceState.UNREACHABLE,  # the child's own alarm (one timeout state)
}


def _reject_non_finite(_value: str) -> Any:
    """``json.loads`` hook: NaN/Infinity literals are INVALID_RESPONSE."""
    raise ValueError("non-finite JSON constant")


#: The dedicated child: prints NOTHING except the minimal canonical
#: balance JSON of amount-bearing outcomes; receives the credential on
#: stdin (private pipe); enforces connect/read deadlines plus a
#: self-armed wall-clock alarm; verified TLS with the peer verified
#: BEFORE any credential leaves (single validated resolution, direct
#: connect to the validated sockaddr, ``getpeername`` normalized and
#: compared before any TLS or HTTP header, SNI/hostname verification on
#: the ORIGINAL hostname); no redirects; no proxy environment. Outcome
#: = exit code (0/3 for amount states, plus canonical JSON on stdout).
#:
#: Package 7q crash safety: ALL imports live inside ``main()`` and the
#: whole script runs under a top-level sanitized exception boundary —
#: an uncaught exception or import failure exits the DISTINCT code 11
#: with NOTHING on stderr (no traceback), so a crashed child can never
#: alias exit 1 (NOT_CONFIGURED). The child alarm exits 12 (the same
#: timeout state as the parent deadline, UNREACHABLE); deep JSON raises
#: ``RecursionError`` and control-character credentials are each failed
#: closed explicitly.
_BALANCE_CHILD_CODE = r"""
import os
import signal
import sys


def _timeout_exit(_signum, _frame):
    # The child alarm and the parent deadline share ONE deterministic
    # timeout state (12 → UNREACHABLE). os._exit: no cleanup, no
    # exception propagation, no partial output.
    os._exit(12)


def main() -> int:
    import http.client
    import json
    import socket
    import ssl
    from urllib.parse import urlsplit

    from moira.balance import parse_amount
    from moira.connection_test import contains_secret_keys, resolve_target, same_endpoint

    def _reject_constant(_value):
        raise ValueError("non-finite constant")

    try:
        url, connect_s, read_s, total_s, cap_s, policy = sys.argv[1:7]
        connect = float(connect_s)
        read = float(read_s)
        total = float(total_s)
        cap = int(cap_s)
    except (IndexError, ValueError):
        return 7  # invalid response: malformed invocation
    signal.signal(signal.SIGALRM, _timeout_exit)
    signal.alarm(max(1, int(total)))  # self-bound, mirroring the parent's bound
    key = sys.stdin.read().strip()
    if not key:
        return 1  # not configured (the parent normally fails before spawn)
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in key):
        return 7  # control characters could inject headers: invalid response
    try:
        parts = urlsplit(url)
        if parts.scheme == "https":
            use_https = True
        elif parts.scheme == "http" and policy == "local":
            use_https = False
        else:
            return 7  # invalid endpoint configuration
        host = parts.hostname or ""
        if not host:
            return 7
        port = parts.port or (443 if use_https else 80)
    except ValueError:
        return 7
    # Resolve ONCE; connect ONLY to the validated sockaddr. The peer is
    # verified (getpeername against the validated sockaddr, then TLS)
    # before any credential is sent.
    target = resolve_target(host, port, policy)
    if target is None:
        return 4  # unreachable: refused by address policy or unresolved
    try:
        sock = socket.socket(target.family, target.socktype, target.proto)
        sock.settimeout(connect)
        sock.connect(target.sockaddr)
    except socket.timeout:
        return 4
    except OSError:
        return 4
    try:
        peer = sock.getpeername()
    except OSError:
        return 4
    if not same_endpoint(peer, target.sockaddr):
        return 4  # peer mismatch: UNREACHABLE, zero credential transmission
    try:
        if use_https:
            context = ssl.create_default_context()  # verified TLS
            sock = context.wrap_socket(sock, server_hostname=host)  # SNI + hostname verification
        conn = http.client.HTTPConnection(host, port, timeout=connect)
        conn.sock = sock  # pre-connected to the validated sockaddr
        conn.sock.settimeout(read)  # per-read deadline from here on
        conn.request("GET", parts.path or "/", headers={"Authorization": "Bearer " + key})
        response = conn.getresponse()
        status = response.status
        if status in (401, 403):
            return 2  # authentication failed
        if status == 402:
            return 3  # insufficient — no invented amounts
        if status == 429:
            return 6  # rate limited
        if 500 <= status < 600:
            return 10  # server error
        if status != 200:
            return 7  # redirects and other statuses are invalid responses
        body = response.read(cap + 1)  # one byte past the cap = oversized
        if len(body) > cap:
            return 7  # invalid response: oversized body
    except ssl.SSLError:
        return 5  # TLS error (including peer/hostname mismatch)
    except socket.timeout:
        return 4  # unreachable: deadline exceeded
    except (OSError, http.client.HTTPException):
        return 4  # unreachable: transport failure
    # Strict balance payload: documented top-level and item keys only,
    # a REAL boolean, a non-empty bounded array, unique CNY/USD entries
    # and exact bounded string amounts; extra, missing,
    # secret/account-bearing or malformed data is INVALID_RESPONSE.
    # Deep nesting raises RecursionError — failed closed like any
    # malformed body (never an uncaught crash).
    try:
        payload = json.loads(body, parse_constant=_reject_constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return 7  # invalid response: malformed or too-deep JSON
    if not isinstance(payload, dict) or contains_secret_keys(payload):
        return 7
    if set(payload) != {"is_available", "balance_infos"}:
        return 7  # extra or missing top-level keys
    is_available = payload["is_available"]
    if not isinstance(is_available, bool):
        return 7  # a real boolean is required
    infos = payload["balance_infos"]
    if not isinstance(infos, list) or not infos or len(infos) > %(max_infos)d:
        return 7  # non-empty bounded array
    seen = set()
    currencies = []
    for item in infos:
        if not isinstance(item, dict) or contains_secret_keys(item):
            return 7
        if set(item) != {"currency", "total_balance", "granted_balance", "topped_up_balance"}:
            return 7  # exact documented item keys
        currency = item["currency"]
        if not isinstance(currency, str) or currency not in ("CNY", "USD"):
            return 7
        if currency in seen:
            return 7  # unique currency entries
        seen.add(currency)
        amounts = []
        for name in ("total_balance", "granted_balance", "topped_up_balance"):
            value = parse_amount(item[name])
            if value is None:
                return 7  # exact bounded decimal string amount required
            amounts.append(format(value, "f"))
        currencies.append(
            {
                "currency": currency,
                "total_balance": amounts[0],
                "granted_balance": amounts[1],
                "topped_up_balance": amounts[2],
            }
        )
    # Minimal canonical balance JSON only — never the raw body, headers,
    # host, IP, metadata, exception text or the credential.
    sys.stdout.write(
        json.dumps({"is_available": is_available, "currencies": currencies}, separators=(",", ":"))
    )
    return 0 if is_available else 3


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(11)  # sanitized crash boundary: no traceback, never alias exit 1
""".replace("%(max_infos)d", str(MAX_BALANCE_INFOS))


def _decode_child_output(raw: str) -> tuple[bool, tuple[BalanceEntry, ...]] | None:
    """Strict decode of the child's canonical minimal JSON.

    Returns ``(is_available, entries)``; None on ANY deviation (empty
    output, malformed JSON, NaN/Infinity, wrong shape, unknown or
    duplicate currencies, non-string or invalid amounts, secret keys).
    """
    text = raw.strip()
    if not text:
        return None
    try:
        payload = json.loads(text, parse_constant=_reject_non_finite)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None  # malformed, NaN/Infinity or too-deep JSON
    if not isinstance(payload, dict) or set(payload) != {"is_available", "currencies"}:
        return None
    is_available = payload["is_available"]
    if not isinstance(is_available, bool):
        return None
    currencies = payload["currencies"]
    if not isinstance(currencies, list) or not currencies or len(currencies) > MAX_BALANCE_INFOS:
        return None
    seen: set[str] = set()
    entries: list[BalanceEntry] = []
    for item in currencies:
        if not isinstance(item, dict) or set(item) != {
            "currency",
            "total_balance",
            "granted_balance",
            "topped_up_balance",
        }:
            return None
        currency = item["currency"]
        if not isinstance(currency, str) or currency not in _CURRENCY_ORDER:
            return None
        if currency in seen:
            return None
        seen.add(currency)
        amounts: list[Decimal] = []
        for name in ("total_balance", "granted_balance", "topped_up_balance"):
            value = parse_amount(item[name])
            if value is None:
                return None
            amounts.append(value)
        entries.append(BalanceEntry(currency, amounts[0], amounts[1], amounts[2]))
    return is_available, tuple(entries)


def bounded_balance_refresh(
    profile: ProviderProfile,
    key: str,
    *,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    read_timeout: float = DEFAULT_READ_TIMEOUT,
    total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
    body_cap: int = DEFAULT_BODY_CAP,
    cancel_event: Any = None,
) -> BalanceResult:
    """Run one bounded, reaped balance refresh with the given credential.

    The key travels to the child ONLY through the private stdin pipe.
    The child connects ONLY to the official endpoint (the profile URL is
    never used) with the remote resolved-address policy, verified TLS,
    no redirects and no proxy environment; its stdout is bounded and may
    carry ONLY the minimal canonical balance JSON. Timeout, overflow,
    spawn failure and unknown codes fail closed; a cancelled run is
    CANCELLED. Classification happens BEFORE anything: unsupported kinds
    fail closed with no spawn, and oversized credentials are invalid.
    """
    preflight = _preflight(profile, cancel_event)
    if preflight is not None:
        return preflight
    if len(key.encode("utf-8")) > MAX_KEY_BYTES:
        return BalanceResult(BalanceState.INVALID_RESPONSE, profile.slug)
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in key):
        # Control characters in a credential could inject HTTP headers:
        # rejected BEFORE any spawn (the child re-checks independently).
        return BalanceResult(BalanceState.INVALID_RESPONSE, profile.slug)
    result = run_bounded(
        [
            sys.executable,
            "-c",
            _BALANCE_CHILD_CODE,
            BALANCE_ENDPOINT,
            str(connect_timeout),
            str(read_timeout),
            str(total_timeout),
            str(body_cap),
            "remote",
        ],
        timeout=total_timeout,
        stdin_data=(key + "\n").encode("utf-8"),
    )
    if result is None:
        return BalanceResult(BalanceState.UNREACHABLE, profile.slug)
    if result.outcome is ProbeOutcome.TIMEOUT:
        return BalanceResult(BalanceState.UNREACHABLE, profile.slug)
    if result.outcome is not ProbeOutcome.OK:
        return BalanceResult(BalanceState.INVALID_RESPONSE, profile.slug)
    if result.returncode is None or result.returncode < 0:
        # Abnormal/signal exits never alias a valid state: a child killed
        # by a signal is INVALID_RESPONSE, never NOT_CONFIGURED.
        return BalanceResult(BalanceState.INVALID_RESPONSE, profile.slug)
    if result.stderr.strip():
        # Non-empty stderr (warnings, tracebacks, crash text) fails
        # closed. The raw text is never rendered or retained anywhere.
        return BalanceResult(BalanceState.INVALID_RESPONSE, profile.slug)
    state = _CHILD_CODES.get(result.returncode) if result.returncode is not None else None
    if state is None:
        return BalanceResult(BalanceState.INVALID_RESPONSE, profile.slug)
    if state is BalanceState.AVAILABLE:
        decoded = _decode_child_output(result.stdout)
        if decoded is None or not decoded[0]:
            return BalanceResult(BalanceState.INVALID_RESPONSE, profile.slug)
        return BalanceResult(state, profile.slug, decoded[1])
    if state is BalanceState.INSUFFICIENT:
        decoded = _decode_child_output(result.stdout)
        if decoded is None:
            # 402-style: INSUFFICIENT without invented amounts.
            return BalanceResult(state, profile.slug)
        if decoded[0]:
            return BalanceResult(BalanceState.INVALID_RESPONSE, profile.slug)
        return BalanceResult(state, profile.slug, decoded[1])
    if result.stdout.strip():
        # The child may return ONLY minimal canonical balance JSON on
        # stdout: any output for a non-amount state is a leak.
        return BalanceResult(BalanceState.INVALID_RESPONSE, profile.slug)
    return BalanceResult(state, profile.slug)


def _preflight(profile: ProviderProfile, cancel_event: Any) -> BalanceResult | None:
    """Classification BEFORE any Keyring read or spawn. None means the
    refresh may proceed; otherwise the run fails closed. Cancellation
    wins over everything; only DEEPSEEK is supported — every other kind
    is UNSUPPORTED regardless of any credential."""
    if cancel_event is not None and cancel_event.is_set():
        return BalanceResult(BalanceState.CANCELLED, profile.slug)
    if profile.kind is not ProviderKind.DEEPSEEK:
        return BalanceResult(BalanceState.UNSUPPORTED, profile.slug)
    return None


def run_balance_refresh(
    profile: ProviderProfile,
    *,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    read_timeout: float = DEFAULT_READ_TIMEOUT,
    total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
    body_cap: int = DEFAULT_BODY_CAP,
    cancel_event: Any = None,
) -> BalanceResult:
    """Classify FIRST (unsupported kind / cancellation), then read the
    credential from the Keyring IMMEDIATELY before refreshing and run the
    bounded child. A missing credential or an unavailable Keyring fails
    closed as NOT_CONFIGURED BEFORE any network or spawn; the credential
    never reaches argv, environment, disk or logs."""
    preflight = _preflight(profile, cancel_event)
    if preflight is not None:
        return preflight
    inspection = inspect_provider_secret(profile.slug)
    if inspection is None or inspection.state is not KeyringLookup.FOUND:
        # Missing or unknown credential state: fail closed as not
        # configured, before any spawn.
        return BalanceResult(BalanceState.NOT_CONFIGURED, profile.slug)
    assert inspection.value is not None
    return bounded_balance_refresh(
        profile,
        inspection.value,
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        total_timeout=total_timeout,
        body_cap=body_cap,
        cancel_event=cancel_event,
    )
