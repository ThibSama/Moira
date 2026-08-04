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
    SemVer,
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


# ── SemVer 2.0 ──


def test_parse_version_basic() -> None:
    assert parse_version("0.2.2") == SemVer(0, 2, 2)
    assert parse_version("v0.2.2") == SemVer(0, 2, 2)
    assert parse_version("V1.2.3") == SemVer(1, 2, 3)
    assert parse_version("0.10.0") == SemVer(0, 10, 0)
    assert parse_version("1.2.3-rc1") == SemVer(1, 2, 3, ("rc1",))
    assert parse_version("1.2.3-alpha.beta") == SemVer(1, 2, 3, ("alpha", "beta"))
    # Build metadata is parsed but discarded (never affects precedence).
    assert parse_version("1.2.3+build.5") == SemVer(1, 2, 3)
    assert parse_version("1.2.3-rc1+build.5") == SemVer(1, 2, 3, ("rc1",))
    assert parse_version("1.0.0-0") == SemVer(1, 0, 0, ("0",))


def test_parse_version_rejects_leading_zeros() -> None:
    for bad in ("01.2.3", "1.02.3", "1.2.03", "v01.2.3", "0.0.01", "1.2.3-01"):
        assert parse_version(bad) is None, bad


def test_parse_version_rejects_malformed() -> None:
    for bad in (
        "",
        "latest",
        "1.2",
        "1.2.x",
        "abc.def.ghi",
        "1.2.3.4",
        "v1.2.3.4",
        "1..3",
        "1.2.3-",  # empty prerelease suffix
        "1.2.3+",  # empty build suffix
        "1.2.3-alpha..1",  # empty identifier
        "1.2.3-alpha.",  # trailing empty identifier
        "1.2.3+..build",  # empty build identifier
        "1.2.3-αβγ",  # non-ASCII identifiers
        "1.2.3-rc_1",  # underscore is not a SemVer identifier char
    ):
        assert parse_version(bad) is None, bad


def test_compare_versions_numeric() -> None:
    assert compare_versions(SemVer(0, 2, 2), SemVer(0, 10, 0)) == -1
    assert compare_versions(SemVer(0, 10, 0), SemVer(0, 2, 2)) == 1
    assert compare_versions(SemVer(0, 2, 2), SemVer(0, 2, 2)) == 0
    assert compare_versions(SemVer(1, 0, 0), SemVer(0, 99, 99)) == 1


def test_prerelease_precedence() -> None:
    """The canonical SemVer 2.0 example ordering."""
    ordered = (
        "1.0.0-alpha",
        "1.0.0-alpha.1",
        "1.0.0-alpha.beta",
        "1.0.0-beta",
        "1.0.0-beta.2",
        "1.0.0-beta.11",
        "1.0.0-rc.1",
        "1.0.0",
    )
    parsed: list[SemVer] = []
    for tag in ordered:
        version = parse_version(tag)
        assert version is not None, tag
        parsed.append(version)
    for left, right in zip(parsed, parsed[1:], strict=False):
        assert compare_versions(left, right) == -1, (left, right)
        assert compare_versions(right, left) == 1, (right, left)


def test_build_metadata_ignored_for_precedence() -> None:
    release = parse_version("1.0.0")
    build_a = parse_version("1.0.0+build.1")
    build_b = parse_version("1.0.0+build.99")
    prerelease = parse_version("1.0.0-rc1+build.1")
    bare_rc = parse_version("1.0.0-rc1")
    assert release is not None
    assert build_a is not None
    assert build_b is not None
    assert prerelease is not None
    assert bare_rc is not None
    assert compare_versions(build_a, release) == 0
    assert compare_versions(build_a, build_b) == 0
    assert compare_versions(prerelease, bare_rc) == 0
    # Release beats prerelease even when the prerelease carries build metadata.
    assert compare_versions(release, prerelease) == 1


def test_numeric_identifiers_compare_numerically() -> None:
    v2 = parse_version("1.0.0-2")
    v10 = parse_version("1.0.0-10")
    rc2 = parse_version("1.0.0-rc.2")
    rc10 = parse_version("1.0.0-rc.10")
    v1 = parse_version("1.0.0-1")
    va = parse_version("1.0.0-a")
    assert v2 is not None and v10 is not None
    assert rc2 is not None and rc10 is not None
    assert v1 is not None and va is not None
    assert compare_versions(v2, v10) == -1
    assert compare_versions(rc2, rc10) == -1
    # Numeric identifiers always sort below alphanumeric ones.
    assert compare_versions(v1, va) == -1


def test_longer_prerelease_wins_when_prefix_equal() -> None:
    alpha = parse_version("1.0.0-alpha")
    alpha1 = parse_version("1.0.0-alpha.1")
    assert alpha is not None and alpha1 is not None
    assert compare_versions(alpha, alpha1) == -1
    assert compare_versions(alpha1, alpha) == 1


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


def test_latest_prerelease_still_update_when_above_current() -> None:
    """A prerelease tag above the current version is an update candidate
    (0.3.0-rc1 > 0.2.2), while a prerelease below the same release is not
    (1.0.0-rc1 < 1.0.0)."""
    rc = json.dumps({"tag_name": "0.3.0-rc1"}).encode()
    result = check_latest_release("ThibSama/moira", current="0.2.2", opener=_opener(rc))
    assert result.status == STATUS_UPDATE_AVAILABLE
    assert result.latest == "0.3.0-rc1"

    same_release_rc = json.dumps({"tag_name": "1.0.0-rc1"}).encode()
    result = check_latest_release(
        "ThibSama/moira", current="1.0.0", opener=_opener(same_release_rc)
    )
    assert result.status == STATUS_UP_TO_DATE


def test_invalid_current_version_fails_closed() -> None:
    """An invalid ``current`` version is never reported as up to date — the
    check fails closed with a sanitized status."""
    body = json.dumps({"tag_name": "0.3.0"}).encode()
    for bad_current in ("", "latest", "01.2.3", "1.2", "1.2.3-", "v1.2.3.4"):
        result = check_latest_release("ThibSama/moira", current=bad_current, opener=_opener(body))
        assert result.ok is False, bad_current
        assert result.status == STATUS_CHECK_FAILED, bad_current


def test_non_semver_latest_tag_is_invalid_response() -> None:
    """A release tag that is not strict SemVer (e.g. leading zero or an
    empty suffix) yields a sanitized invalid-response outcome."""
    for tag in ("v0.3.0-", "1.2.03", "01.2.3"):
        body = json.dumps({"tag_name": tag}).encode()
        result = check_latest_release("ThibSama/moira", current="0.2.2", opener=_opener(body))
        assert result.ok is False, tag
        assert result.status == STATUS_INVALID_RESPONSE, tag


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
