from __future__ import annotations

import urllib.parse
import urllib.request
from dataclasses import dataclass


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
) -> None:
    request = build_request(server, topic, notification, token)
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        if not 200 <= response.status < 300:
            raise OSError(f"NTFY returned HTTP {response.status}")
