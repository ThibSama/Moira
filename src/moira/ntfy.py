"""NTFY delivery with bounded timeout and typed sanitized outcomes.

The network layer never raises raw exceptions to callers: ``send`` returns
a frozen ``NtfyResult`` carrying one of a fixed set of sanitized status
strings. Server URL, topic, token, response body, raw exception text and
paths never appear in outcomes — they are absent by construction.
"""

from __future__ import annotations

import urllib.parse
import urllib.request
from dataclasses import dataclass

#: Fixed sanitized outcome statuses. No free-form strings may flow here.
STATUS_SENT = "sent"
STATUS_INVALID = "invalid configuration"
STATUS_NETWORK = "network failure"
STATUS_TIMEOUT = "timed out"
STATUS_SERVER = "server error"

#: Bounded response read: the body is consumed (and discarded) up to this
#: many bytes so a hostile or oversized server cannot exhaust memory.
DEFAULT_MAX_RESPONSE_BYTES = 4096


@dataclass(frozen=True, slots=True)
class NtfyResult:
    """Typed outcome of one NTFY delivery attempt."""

    ok: bool
    status: str


@dataclass(frozen=True, slots=True)
class Notification:
    title: str
    message: str
    tags: str = "chart_with_upwards_trend"
    priority: int = 3


def build_request(
    server: str, topic: str, notification: Notification, token: str | None = None
) -> urllib.request.Request:
    parsed = urllib.parse.urlparse(server)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("NTFY server must be an HTTP(S) URL")
    clean_topic = topic.strip().strip("/")
    if not clean_topic or "/" in clean_topic:
        raise ValueError("NTFY topic must be one non-empty path segment")
    url = server.rstrip("/") + "/" + urllib.parse.quote(clean_topic, safe="")
    headers = {
        "Content-Type": "text/plain; charset=utf-8",
        "Title": notification.title,
        "Tags": notification.tags,
        "Priority": str(notification.priority),
        "User-Agent": "Moira/0.2.2",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, notification.message.encode("utf-8"), headers, method="POST")


def send(
    server: str,
    topic: str,
    notification: Notification,
    token: str | None = None,
    timeout: float = 10.0,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> NtfyResult:
    """Deliver one notification and return a typed sanitized outcome.

    Never raises for network/configuration failures and never exposes the
    server, topic, token, response body, raw exception or any path in the
    returned status. The response body is read up to ``max_response_bytes``
    (then discarded) so memory stays bounded.
    """
    try:
        request = build_request(server, topic, notification, token)
    except ValueError:
        return NtfyResult(False, STATUS_INVALID)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            if not 200 <= response.status < 300:
                return NtfyResult(False, STATUS_SERVER)
            response.read(max_response_bytes)
        return NtfyResult(True, STATUS_SENT)
    except TimeoutError:
        return NtfyResult(False, STATUS_TIMEOUT)
    except OSError:  # covers urllib.error.URLError/HTTPError
        return NtfyResult(False, STATUS_NETWORK)
    except Exception:
        return NtfyResult(False, STATUS_NETWORK)
