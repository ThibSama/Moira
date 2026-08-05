"""Package 7j — bounded, read-only provider connection tests (GTK-free).

A connection test verifies a profile's Keyring credential and its
configured model against the provider's documented read-only Models
endpoint, WITHOUT calling chat, responses, completions, embeddings,
inference, usage, credit or balance endpoints and without generating
tokens or mutations.

Pipeline: the Keyring is read IMMEDIATELY before testing and the
credential is handed to a dedicated child process through a PRIVATE
stdin pipe only (never argv, environment, disk, config, journal,
diagnostics or logs). The child performs one strict HTTP GET with
verified TLS, no redirects, no proxy environment, resolved-address
policy checks (remote kinds reject non-public addresses; ``local`` is
loopback-only) and bounded connect/read deadlines. The target is
resolved EXACTLY ONCE: ``resolve_target`` returns an immutable
``ValidatedTarget`` (family, socktype, proto, sockaddr from the single
accepted ``getaddrinfo`` call) and the child creates the socket
directly, ``connect``s to that sockaddr (no second lookup, no
resolving helper), then normalizes and compares ``getpeername()``
with the validated sockaddr BEFORE any TLS handshake or HTTP header —
a mismatch is UNREACHABLE with zero credential transmission. The
parent enforces the total wall-time bound, output caps and
process-group reaping (SIGTERM then unconditional SIGKILL escalation).
The child prints NOTHING — the outcome travels as a sanitized exit
code — and no response body, header, account data, host, IP or
exception text is ever retained or rendered.
"""

from __future__ import annotations

import ipaddress
import socket
import sys
import threading
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .integrations import ProbeOutcome, ProviderKind, ProviderProfile, run_bounded
from .secrets import KeyringLookup, inspect_provider_secret

#: Maximum accepted credential length (bytes). A larger credential is an
#: invalid input — fail closed BEFORE any spawn, so stdin delivery stays
#: inside the wall-time bound.
MAX_KEY_BYTES = 4096
#: Maximum accepted number of model entries and per-id length in the
#: strict Models payload; beyond these the response is INVALID_RESPONSE.
MAX_MODELS = 10_000
MAX_ID_LEN = 200
#: Substrings that make a JSON key secret- or account-bearing. Any such
#: key in the Models payload (top level or inside a model item) makes
#: the response INVALID_RESPONSE — the response must never carry
#: credentials, account data or endpoints.
_SECRET_KEY_SUBSTRINGS = (
    "key",
    "token",
    "secret",
    "credential",
    "password",
    "auth",
    "cookie",
    "session",
    "account",
)


def contains_secret_keys(payload: dict[str, Any]) -> bool:
    """True when any payload key is secret- or account-bearing (case-
    insensitive substring match)."""
    for key in payload:
        lowered = key.lower()
        for fragment in _SECRET_KEY_SUBSTRINGS:
            if fragment in lowered:
                return True
    return False


def _endpoint_key(sockaddr: tuple[Any, ...]) -> tuple[str, int]:
    """Normalized endpoint identity: address (zone stripped) + port.

    ``getaddrinfo`` may return a scope-qualified IPv6 address (``%zone``)
    and flowinfo/scope_id that differ from the socket's ``getpeername``
    view — only the normalized address and port are compared, so a
    legitimate connect is never misjudged while any real mismatch still
    fails closed.
    """
    return (str(sockaddr[0]).split("%", 1)[0], int(sockaddr[1]))


def same_endpoint(peer: tuple[Any, ...], validated: tuple[Any, ...]) -> bool:
    """True when the connected peer matches the validated sockaddr.

    Used by the child IMMEDIATELY after ``connect`` and BEFORE any TLS
    handshake or HTTP header: a mismatch means the socket is not on the
    validated endpoint and the run fails closed (UNREACHABLE) with zero
    credential transmission.
    """
    return _endpoint_key(peer) == _endpoint_key(validated)


@dataclass(frozen=True, slots=True)
class ValidatedTarget:
    """Immutable validated connect target from ONE ``getaddrinfo`` call.

    Carries the exact family, socktype, proto and sockaddr of the single
    accepted resolution so the child can create the socket directly and
    connect to the validated sockaddr — there is never a second lookup,
    so DNS rebinding cannot pass the policy check and win.
    """

    family: int
    socktype: int
    proto: int
    sockaddr: tuple[Any, ...]


def resolve_target(host: str, port: int, policy: str) -> ValidatedTarget | None:
    """Resolve ONCE and return the validated target to connect to.

    The caller connects ONLY to this target's sockaddr — there is never
    a second, unchecked resolution, so DNS rebinding cannot pass the
    policy check and reach a private address. ``remote`` refuses the
    whole resolution if ANY address is loopback/private/link-local/
    multicast/reserved/unspecified; ``local`` refuses unless every
    address is loopback. None means unresolvable or refused by policy.
    """
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except OSError:
        return None
    addresses: list[str] = []
    first: tuple[Any, ...] | None = None
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        addresses.append(str(address))
        if first is None:
            first = info
    if not addresses:
        return None
    if policy == "local":
        if any(not ipaddress.ip_address(item).is_loopback for item in addresses):
            return None
    elif any(
        ipaddress.ip_address(item).is_loopback
        or ipaddress.ip_address(item).is_private
        or ipaddress.ip_address(item).is_link_local
        or ipaddress.ip_address(item).is_multicast
        or ipaddress.ip_address(item).is_reserved
        or ipaddress.ip_address(item).is_unspecified
        for item in addresses
    ):
        return None
    assert first is not None
    family, socktype, proto, _canonname, sockaddr = first
    return ValidatedTarget(family, socktype, proto, sockaddr)


def _preflight(
    profile: ProviderProfile, cancel_event: threading.Event | None
) -> ConnectionResult | None:
    """Classification BEFORE any Keyring read or spawn. None means the
    test may proceed; otherwise the run fails closed with the given
    result. Cancellation wins over everything (a closed editor performs
    zero Keyring calls and zero spawn); ``custom`` is UNSUPPORTED
    regardless of any credential."""
    if cancel_event is not None and cancel_event.is_set():
        return ConnectionResult(ConnectionState.CANCELLED, profile.slug)
    adapter = adapter_for(profile.kind)
    if adapter is None or not (adapter.url or adapter.base_url_models):
        return ConnectionResult(ConnectionState.UNSUPPORTED, profile.slug)
    if not profile.model:
        return ConnectionResult(ConnectionState.NOT_CONFIGURED, profile.slug)
    if adapter.base_url_models and not profile.base_url:
        return ConnectionResult(ConnectionState.NOT_CONFIGURED, profile.slug)
    return None


#: The fixed result states. A test outcome is ALWAYS one of these; an
#: unknown outcome can never become CONNECTED.
class ConnectionState(StrEnum):
    CONNECTED = "connected"
    NOT_CONFIGURED = "not_configured"
    AUTH_FAILED = "auth_failed"
    MODEL_NOT_FOUND = "model_not_found"
    UNREACHABLE = "unreachable"
    TLS_ERROR = "tls_error"
    RATE_LIMITED = "rate_limited"
    INVALID_RESPONSE = "invalid_response"
    UNSUPPORTED = "unsupported"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ConnectionResult:
    """GTK-free immutable result of one connection test."""

    state: ConnectionState
    profile_slug: str = ""

    @property
    def connected(self) -> bool:
        return self.state is ConnectionState.CONNECTED


#: Exit-code contract of the dedicated child. 1 is reserved for
#: NOT_CONFIGURED (the parent normally fails closed before spawning);
#: every other outcome is an explicit state — unknown codes map to
#: INVALID_RESPONSE (unknown outcomes never become CONNECTED).
_CHILD_CODES: dict[int, ConnectionState] = {
    0: ConnectionState.CONNECTED,
    1: ConnectionState.NOT_CONFIGURED,
    2: ConnectionState.AUTH_FAILED,
    3: ConnectionState.MODEL_NOT_FOUND,
    4: ConnectionState.UNREACHABLE,
    5: ConnectionState.TLS_ERROR,
    6: ConnectionState.RATE_LIMITED,
    7: ConnectionState.INVALID_RESPONSE,
    8: ConnectionState.UNSUPPORTED,
    9: ConnectionState.CANCELLED,
}


@dataclass(frozen=True, slots=True)
class Adapter:
    """One documented read-only Models endpoint contract per kind.

    ``url`` is the fixed endpoint for the hosted kinds; ``base_url_models``
    builds ``<validated base_url>/models`` for compatible/local servers.
    """

    kind: ProviderKind
    url: str = ""
    base_url_models: bool = False
    bearer: bool = True
    extra_headers: tuple[tuple[str, str], ...] = ()
    loopback_http: bool = False


#: Closed adapter registry by ProviderKind. ``custom`` has NO documented
#: compatible contract → UNSUPPORTED. Endpoints are read-only Models
#: list APIs, all currently documented officially (Package 7j evidence):
#: DeepSeek — api-docs.deepseek.com/api/list-models (GET /models);
#: OpenAI — openapi.yaml models/list (GET /v1/models);
#: OpenRouter — docs/api-reference/models/list-all-models-and-their-
#: properties (GET /api/v1/models);
#: Anthropic — docs.anthropic.com/en/api/models-list (GET /v1/models,
#: ``x-api-key`` auth + ``anthropic-version: 2023-06-01`` header).
_ADAPTERS: dict[ProviderKind, Adapter] = {
    ProviderKind.DEEPSEEK: Adapter(ProviderKind.DEEPSEEK, url="https://api.deepseek.com/models"),
    ProviderKind.OPENAI: Adapter(ProviderKind.OPENAI, url="https://api.openai.com/v1/models"),
    ProviderKind.OPENROUTER: Adapter(
        ProviderKind.OPENROUTER, url="https://openrouter.ai/api/v1/models"
    ),
    ProviderKind.ANTHROPIC: Adapter(
        ProviderKind.ANTHROPIC,
        url="https://api.anthropic.com/v1/models",
        bearer=False,
        extra_headers=(("anthropic-version", "2023-06-01"),),
    ),
    ProviderKind.OPENAI_COMPATIBLE: Adapter(ProviderKind.OPENAI_COMPATIBLE, base_url_models=True),
    ProviderKind.LOCAL: Adapter(ProviderKind.LOCAL, base_url_models=True, loopback_http=True),
}


def adapter_for(kind: ProviderKind) -> Adapter | None:
    """Closed registry lookup; None means UNSUPPORTED (no documented
    compatible contract — e.g. ``custom``)."""
    return _ADAPTERS.get(kind)


def endpoint_url(kind: ProviderKind, base_url: str) -> str | None:
    """Resolve the read-only Models endpoint for a kind. None when the
    kind is unsupported or the base URL cannot be turned into a strict
    models endpoint. ``base_url`` is already profile-validated; the
    child re-validates the resolved endpoint anyway."""
    adapter = adapter_for(kind)
    if adapter is None:
        return None
    if adapter.url:
        return adapter.url
    if not base_url:
        return None
    return base_url.rstrip("/") + "/models"


#: The dedicated child: prints NOTHING, receives the credential on stdin
#: (private pipe), enforces connect/read deadlines plus a self-armed
#: wall-clock alarm, verified TLS with the peer verified BEFORE any
#: credential leaves (SNI/hostname verification against the ORIGINAL
#: hostname), no redirects, no proxy environment, and connects ONLY to
#: the single validated target from ``resolve_target`` (the socket is
#: created from its family/socktype/proto and ``connect``ed to its
#: sockaddr — no second resolution, no resolving helper; DNS rebinding
#: cannot win) with ``getpeername`` normalized and compared to the
#: validated sockaddr before any TLS or HTTP header. Outcome = exit
#: code only.
_CHILD_CODE = r"""
import http.client
import json
import signal
import socket
import ssl
import sys
from urllib.parse import urlsplit

from moira.connection_test import contains_secret_keys, resolve_target, same_endpoint


def main() -> int:
    try:
        kind, url, model, connect_s, read_s, total_s, cap_s, auth, policy = sys.argv[1:10]
        connect = float(connect_s)
        read = float(read_s)
        total = float(total_s)
        cap = int(cap_s)
    except (IndexError, ValueError):
        return 7  # invalid response: malformed invocation
    signal.alarm(int(total))  # self-bound, mirroring the parent's bound
    key = sys.stdin.read().strip()
    if not key:
        return 1  # not configured (the parent normally fails before spawn)
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
        if auth == "x-api-key":
            headers = {"x-api-key": key}
        else:
            headers = {"Authorization": "Bearer " + key}
        if kind == "anthropic":
            headers["anthropic-version"] = "2023-06-01"
        conn.request("GET", parts.path or "/", headers=headers)
        response = conn.getresponse()
        status = response.status
        if status in (401, 403):
            return 2  # authentication failed
        if status == 404:
            return 3  # model not found
        if status == 429:
            return 6  # rate limited
        if not 200 <= status < 300:
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
    # Strict Models payload: every item must be a dict with exactly one
    # string id, no secret/account-bearing keys anywhere, no duplicate
    # or excessive entries. CONNECTED requires a fully valid response
    # and one exact model match.
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return 7  # invalid response: malformed JSON
    if not isinstance(payload, dict) or contains_secret_keys(payload):
        return 7
    items = payload.get("data")
    if not isinstance(items, list):
        return 7
    if len(items) > %(max_models)d:
        return 7  # excessive model count
    ids = []
    for item in items:
        if not isinstance(item, dict) or contains_secret_keys(item):
            return 7
        value = item.get("id")
        if (
            not isinstance(value, str)
            or not value
            or len(value) > %(max_id_len)d
            or any(ord(char) < 32 for char in value)
        ):
            return 7  # malformed model id
        if value in ids:
            return 7  # duplicate ambiguity
        ids.append(value)
    if model and model in ids:
        return 0  # connected: authentication/reachability + model present
    return 3  # model not found in the strict models list


if __name__ == "__main__":
    sys.exit(main())
""".replace("%(max_models)d", str(MAX_MODELS)).replace("%(max_id_len)d", str(MAX_ID_LEN))

#: Default wall-clock bounds (seconds) and response-body cap (bytes).
DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_READ_TIMEOUT = 5.0
DEFAULT_TOTAL_TIMEOUT = 15.0
DEFAULT_BODY_CAP = 65536


def _child_command(
    profile: ProviderProfile,
    adapter: Adapter,
    *,
    connect_timeout: float,
    read_timeout: float,
    total_timeout: float,
    body_cap: int,
) -> list[str]:
    url = endpoint_url(profile.kind, profile.base_url) or ""
    policy = "local" if profile.kind is ProviderKind.LOCAL else "remote"
    auth = "x-api-key" if not adapter.bearer else "bearer"
    return [
        sys.executable,
        "-c",
        _CHILD_CODE,
        profile.kind.value,
        url,
        profile.model or "",
        str(connect_timeout),
        str(read_timeout),
        str(total_timeout),
        str(body_cap),
        auth,
        policy,
    ]


def bounded_connection_test(
    profile: ProviderProfile,
    key: str,
    *,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    read_timeout: float = DEFAULT_READ_TIMEOUT,
    total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
    body_cap: int = DEFAULT_BODY_CAP,
    cancel_event: threading.Event | None = None,
) -> ConnectionResult:
    """Run one bounded, reaped connection test with the given credential.

    The key travels to the child ONLY through the private stdin pipe.
    The child prints nothing; the outcome is its sanitized exit code.
    Timeout, overflow, spawn failure and unknown codes are mapped to
    non-CONNECTED states; a cancelled run (shutdown) is CANCELLED.
    Classification happens BEFORE anything: unsupported kinds, missing
    model or base URL and oversized credentials fail closed with no
    spawn.
    """
    preflight = _preflight(profile, cancel_event)
    if preflight is not None:
        return preflight
    if len(key.encode("utf-8")) > MAX_KEY_BYTES:
        return ConnectionResult(ConnectionState.INVALID_RESPONSE, profile.slug)
    adapter = adapter_for(profile.kind)
    assert adapter is not None
    result = run_bounded(
        _child_command(
            profile,
            adapter,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            total_timeout=total_timeout,
            body_cap=body_cap,
        ),
        timeout=total_timeout,
        stdin_data=(key + "\n").encode("utf-8"),
    )
    if result is None:
        return ConnectionResult(ConnectionState.UNREACHABLE, profile.slug)
    if result.outcome is ProbeOutcome.TIMEOUT:
        return ConnectionResult(ConnectionState.UNREACHABLE, profile.slug)
    if result.outcome is not ProbeOutcome.OK:
        return ConnectionResult(ConnectionState.INVALID_RESPONSE, profile.slug)
    state = _CHILD_CODES.get(result.returncode) if result.returncode is not None else None
    if state is None:
        return ConnectionResult(ConnectionState.INVALID_RESPONSE, profile.slug)
    return ConnectionResult(state, profile.slug)


def run_connection_test(
    profile: ProviderProfile,
    *,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    read_timeout: float = DEFAULT_READ_TIMEOUT,
    total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
    body_cap: int = DEFAULT_BODY_CAP,
    cancel_event: threading.Event | None = None,
) -> ConnectionResult:
    """Classify FIRST (unsupported/missing model/base URL/cancellation),
    then read the credential from the Keyring IMMEDIATELY before testing
    and run the bounded child. A missing credential or an unavailable
    Keyring fails closed as NOT_CONFIGURED BEFORE any network or spawn;
    the credential never reaches argv, environment, disk or logs."""
    preflight = _preflight(profile, cancel_event)
    if preflight is not None:
        return preflight
    inspection = inspect_provider_secret(profile.slug)
    if inspection is None or inspection.state is KeyringLookup.UNAVAILABLE:
        # With the vault unavailable the credential state is unknown:
        # fail closed as not configured, before any spawn.
        return ConnectionResult(ConnectionState.NOT_CONFIGURED, profile.slug)
    if inspection.state is KeyringLookup.ABSENT:
        return ConnectionResult(ConnectionState.NOT_CONFIGURED, profile.slug)
    assert inspection.value is not None
    return bounded_connection_test(
        profile,
        inspection.value,
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        total_timeout=total_timeout,
        body_cap=body_cap,
        cancel_event=cancel_event,
    )
