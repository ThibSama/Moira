"""Package 5: manual GitHub release checking — SemVer parsing/comparison,
bounded request, sanitized outcomes, no telemetry/token/auto-install."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from moira.updates import (
    STATUS_CHECK_FAILED,
    STATUS_INVALID_RESPONSE,
    STATUS_UP_TO_DATE,
    STATUS_UPDATE_AVAILABLE,
    UpdateCheckResult,
    check_latest_release,
    compare_versions,
    parse_version,
)


class _FakeResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, n: int = -1) -> bytes:
        return self._body[:n] if n >= 0 else self._body


def _opener(
    body: bytes, status: int = 200, exc: Exception | None = None
) -> Callable[..., _FakeResponse]:
    def fake(request: Any, timeout: float) -> _FakeResponse:
        assert timeout > 0
        if exc is not None:
            raise exc
        return _FakeResponse(body, status)

    return fake


# ── SemVer ──


def test_parse_version_basic() -> None:
    assert parse_version("0.2.2") == (0, 2, 2)
    assert parse_version("v0.2.2") == (0, 2, 2)
    assert parse_version("V1.2.3") == (1, 2, 3)
    assert parse_version("0.10.0") == (0, 10, 0)
    assert parse_version("1.2.3-rc1") == (1, 2, 3)
    assert parse_version("1.2.3+build.5") == (1, 2, 3)


def test_parse_version_rejects_malformed() -> None:
    for bad in ("", "latest", "1.2", "1.2.x", "abc.def.ghi", "1.2.3.4", "v1.2.3.4", "1..3"):
        assert parse_version(bad) is None, bad


def test_compare_versions_numeric() -> None:
    assert compare_versions((0, 2, 2), (0, 10, 0)) == -1
    assert compare_versions((0, 10, 0), (0, 2, 2)) == 1
    assert compare_versions((0, 2, 2), (0, 2, 2)) == 0
    assert compare_versions((1, 0, 0), (0, 99, 99)) == 1


# ── Release check outcomes ──


def test_update_available() -> None:
    body = json.dumps({"tag_name": "v0.3.0", "name": "Release 0.3.0"}).encode()
    result = check_latest_release("ThibSama/moira", current="0.2.2", opener=_opener(body))
    assert result == UpdateCheckResult(True, STATUS_UPDATE_AVAILABLE, "0.2.2", "v0.3.0")


def test_up_to_date() -> None:
    body = json.dumps({"tag_name": "0.2.2"}).encode()
    result = check_latest_release("ThibSama/moira", current="0.2.2", opener=_opener(body))
    assert result.status == STATUS_UP_TO_DATE
    assert result.latest == "0.2.2"


def test_up_to_date_when_latest_is_older() -> None:
    body = json.dumps({"tag_name": "0.1.0"}).encode()
    result = check_latest_release("ThibSama/moira", current="0.2.2", opener=_opener(body))
    assert result.status == STATUS_UP_TO_DATE


def test_invalid_response_non_json() -> None:
    result = check_latest_release(
        "ThibSama/moira", current="0.2.2", opener=_opener(b"<html>rate limited</html>")
    )
    assert result.ok is False
    assert result.status == STATUS_INVALID_RESPONSE
    assert "rate limited" not in result.status


def test_invalid_response_missing_tag() -> None:
    result = check_latest_release(
        "ThibSama/moira", current="0.2.2", opener=_opener(b'{"name": "x"}')
    )
    assert result.status == STATUS_INVALID_RESPONSE


def test_invalid_response_non_semver_tag() -> None:
    result = check_latest_release(
        "ThibSama/moira", current="0.2.2", opener=_opener(b'{"tag_name": "latest"}')
    )
    assert result.status == STATUS_INVALID_RESPONSE


def test_invalid_response_non_200() -> None:
    result = check_latest_release(
        "ThibSama/moira", current="0.2.2", opener=_opener(b"{}", status=404)
    )
    assert result.status == STATUS_INVALID_RESPONSE


def test_oversized_response_is_invalid() -> None:
    body = json.dumps({"tag_name": "v0.3.0"}).encode() + b"x" * 100_000
    result = check_latest_release(
        "ThibSama/moira", current="0.2.2", max_bytes=4096, opener=_opener(body)
    )
    assert result.status == STATUS_INVALID_RESPONSE


def test_timeout_is_check_failed() -> None:
    result = check_latest_release(
        "ThibSama/moira", current="0.2.2", opener=_opener(b"", exc=TimeoutError())
    )
    assert result.status == STATUS_CHECK_FAILED


def test_network_error_is_check_failed() -> None:
    result = check_latest_release(
        "ThibSama/moira", current="0.2.2", opener=_opener(b"", exc=OSError("boom"))
    )
    assert result.status == STATUS_CHECK_FAILED
    assert "boom" not in result.status


def test_invalid_repo_fails_closed() -> None:
    for bad in ("", "no-slash", "owner/name/extra", "owner@x/name"):
        result = check_latest_release(bad, current="0.2.2")
        assert result.status == STATUS_CHECK_FAILED


def test_timeout_parameter_forwarded() -> None:
    captured: list[float] = []

    def fake(request: Any, timeout: float) -> _FakeResponse:
        captured.append(timeout)
        return _FakeResponse(json.dumps({"tag_name": "0.2.2"}).encode())

    check_latest_release("ThibSama/moira", current="0.2.2", timeout=7.5, opener=fake)
    assert captured == [7.5]


def test_no_token_and_single_request() -> None:
    """The check never sends credentials and makes exactly one request."""
    seen: list[tuple[str, str]] = []

    def fake(request: Any, timeout: float) -> _FakeResponse:
        seen.append((request.full_url, request.get_header("Authorization") or ""))
        return _FakeResponse(json.dumps({"tag_name": "0.2.2"}).encode())

    check_latest_release("ThibSama/moira", current="0.2.2", opener=fake)
    assert len(seen) == 1
    url, auth = seen[0]
    assert url == "https://api.github.com/repos/ThibSama/moira/releases/latest"
    assert auth == ""
