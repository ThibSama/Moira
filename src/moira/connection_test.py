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
loopback-only) and bounded connect/read deadlines; the parent enforces
the total wall-time bound, output caps and process-group reaping
(SIGTERM then unconditional SIGKILL escalation). The child prints
NOTHING — the outcome travels as a sanitized exit code — and no
response body, header, account data, host, IP or exception text is
ever retained or rendered.
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass
from enum import StrEnum

from .integrations import ProbeOutcome, ProviderKind, ProviderProfile, run_bounded
from .secrets import KeyringLookup, inspect_provider_secret


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


#: The dedicated child: self-contained, prints NOTHING, receives the
#: credential on stdin (private pipe), enforces connect/read deadlines
#: plus a self-armed wall-clock alarm, verified TLS, no redirects, no
#: proxy environment and the resolved-address policy. Outcome = exit
#: code only.
_CHILD_CODE = r"""
import http.client
import ipaddress
import json
import signal
import socket
import ssl
import sys
from urllib.parse import urlsplit


def _policy_rejected(host: str, port: int, policy: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except OSError:
        return True  # unresolvable target
    addresses = []
    for info in infos:
        try:
            addresses.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    if not addresses:
        return True
    if policy == "local":
        return any(not ip.is_loopback for ip in addresses)
    return any(
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        for ip in addresses
    )


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
    if _policy_rejected(host, port, policy):
        return 4  # unreachable: refused by address policy (SSRF guard)
    try:
        if use_https:
            context = ssl.create_default_context()  # verified TLS
            conn = http.client.HTTPSConnection(host, port, timeout=connect, context=context)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=connect)
    except (OSError, ValueError):
        return 7
    if auth == "x-api-key":
        headers = {"x-api-key": key}
    else:
        headers = {"Authorization": "Bearer " + key}
    if kind == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
    try:
        conn.request("GET", parts.path or "/", headers=headers)
        conn.sock.settimeout(read)  # per-read deadline from here on
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
        return 5  # TLS error
    except socket.timeout:
        return 4  # unreachable: deadline exceeded
    except (OSError, http.client.HTTPException):
        return 4  # unreachable: transport failure
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return 7  # invalid response: malformed JSON
    if not isinstance(payload, dict):
        return 7
    items = payload.get("data")
    if not isinstance(items, list):
        return 7
    ids = [
        item.get("id")
        for item in items
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    if model and model in ids:
        return 0  # connected: authentication/reachability + model present
    return 3  # model not found in the strict models list


if __name__ == "__main__":
    sys.exit(main())
"""

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
    """
    if cancel_event is not None and cancel_event.is_set():
        return ConnectionResult(ConnectionState.CANCELLED, profile.slug)
    adapter = adapter_for(profile.kind)
    if adapter is None or not (adapter.url or adapter.base_url_models):
        return ConnectionResult(ConnectionState.UNSUPPORTED, profile.slug)
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
    """Read the credential from the Keyring IMMEDIATELY before testing
    and run the bounded child. A missing credential or an unavailable
    Keyring fails closed as NOT_CONFIGURED BEFORE any network or spawn;
    the credential never reaches argv, environment, disk or logs."""
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
