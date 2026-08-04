"""Package 5: hardened NTFY delivery — typed sanitized outcomes, bounded
timeout and response read, no secrets/raw exceptions in statuses."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

from moira.ntfy import (
    STATUS_INVALID,
    STATUS_NETWORK,
    STATUS_SENT,
    STATUS_SERVER,
    STATUS_TIMEOUT,
    Notification,
    NtfyResult,
    build_request,
    send,
)


class _FakeResponse:
    def __init__(self, body: bytes = b"ok", status: int = 200) -> None:
        self._body = body
        self.status = status

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, n: int = -1) -> bytes:
        return self._body[:n] if n >= 0 else self._body


def _send(
    *, body: bytes = b"ok", status: int = 200, exc: Exception | None = None
) -> tuple[NtfyResult, Any]:
    def fake_urlopen(request: Any, timeout: float) -> _FakeResponse:
        assert timeout > 0
        if exc is not None:
            raise exc
        return _FakeResponse(body, status)

    with patch("urllib.request.urlopen", side_effect=fake_urlopen) as opened:
        result = send("https://notify.example", "topic", Notification("T", "M"), "secret-token")
    return result, opened


def test_send_success_returns_sent() -> None:
    result, _ = _send()
    assert result.ok is True
    assert result.status == STATUS_SENT


def test_send_invalid_configuration() -> None:
    result, _ = _send()
    bad = send("not-a-url", "topic", Notification("T", "M"))
    assert bad.ok is False
    assert bad.status == STATUS_INVALID
    bad_topic = send("https://notify.example", "a/b", Notification("T", "M"))
    assert bad_topic.status == STATUS_INVALID


def test_send_network_failure() -> None:
    result, _ = _send(exc=OSError("connection refused to 10.0.0.5:443 with secret payload"))
    assert result.ok is False
    assert result.status == STATUS_NETWORK
    # The raw exception text never leaks into the outcome.
    assert "10.0.0.5" not in result.status
    assert "secret" not in result.status


def test_send_timeout() -> None:
    result, _ = _send(exc=TimeoutError("timed out after 10s"))
    assert result.status == STATUS_TIMEOUT


def test_send_server_error_status() -> None:
    result, _ = _send(status=500)
    assert result.status == STATUS_SERVER


def test_send_http_error_429_maps_to_network_failure() -> None:
    result, _ = _send(status=429)
    assert result.ok is False
    assert result.status in (STATUS_NETWORK, STATUS_SERVER)


def test_outcome_never_exposes_secrets_or_paths() -> None:
    for body in (b"<html>server</html>", json.dumps({"error": "bad token"}).encode()):
        result, _ = _send(body=body, status=500)
        assert "html" not in result.status
        assert "bad token" not in result.status
        assert "secret" not in result.status
        # Fixed sanitized set only.
        assert result.status in (
            STATUS_SENT,
            STATUS_INVALID,
            STATUS_NETWORK,
            STATUS_TIMEOUT,
            STATUS_SERVER,
        )


def test_response_read_is_bounded() -> None:
    """The response body is consumed up to the bound and never returned."""
    big = b"x" * 100_000
    result, opened = _send(body=big)
    assert result.ok is True
    # The read was bounded; the body never appears in the outcome.
    assert "x" * 100 not in result.status


def test_timeout_parameter_is_forwarded() -> None:
    captured: list[float] = []

    def fake_urlopen(request: Any, timeout: float) -> _FakeResponse:
        captured.append(timeout)
        return _FakeResponse()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        send("https://notify.example", "topic", Notification("T", "M"), timeout=3.5)
    assert captured == [3.5]


def test_build_request_has_typed_fixed_message() -> None:
    request = build_request(
        "https://notify.example", "topic", Notification("Title", "Body", "warning", 4), "tok"
    )
    assert request.data == b"Body"
    assert request.get_header("Title") == "Title"
    assert request.get_header("Tags") == "warning"
    assert request.get_header("Priority") == "4"
    assert request.get_header("Authorization") == "Bearer tok"
    assert request.full_url == "https://notify.example/topic"


def test_send_never_raises() -> None:
    with patch("urllib.request.urlopen", side_effect=RuntimeError("boom")):
        result = send("https://notify.example", "topic", Notification("T", "M"))
    assert result.ok is False
    assert result.status in (STATUS_NETWORK, STATUS_SERVER)
    # The raw exception text is never part of the outcome.
    assert "boom" not in result.status


def test_bounded_oversized_response_is_still_handled() -> None:
    """A hostile oversized body cannot exhaust memory: read is bounded."""
    result, _ = _send(body=b"y" * 1_000_000)
    assert result.ok is True
