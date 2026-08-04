"""Manual GitHub release checking with strict SemVer comparison.

The check is triggered ONLY by the user (no startup check, no telemetry,
no token, no auto-download, no install). The request is bounded in time
and response size, and every outcome is a fixed sanitized status string —
the response body, raw exceptions, URLs, and repository details are never
exposed to the UI.

Version comparison is numeric and pre-release suffixes are ignored for
ordering (``0.10.0`` > ``0.2.2``; ``v1.2.3-rc1`` parses as ``1.2.3``).
"""

from __future__ import annotations

import json
import re
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

#: Fixed sanitized outcome statuses.
STATUS_UP_TO_DATE = "up to date"
STATUS_UPDATE_AVAILABLE = "update available"
STATUS_CHECK_FAILED = "check failed"
STATUS_INVALID_RESPONSE = "invalid response"

#: Bounded read: at most this many bytes of the response body are consumed.
DEFAULT_MAX_BYTES = 65536

_VERSION_RE = re.compile(r"^[vV]?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")


@dataclass(frozen=True, slots=True)
class UpdateCheckResult:
    """Bounded outcome of one manual release check."""

    ok: bool
    status: str
    current: str
    latest: str | None = None


def parse_version(tag: str) -> tuple[int, int, int] | None:
    """Parse a SemVer tag like ``0.2.2`` or ``v1.2.3-rc1`` into ints."""
    match = _VERSION_RE.match(tag.strip())
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def compare_versions(a: tuple[int, int, int], b: tuple[int, int, int]) -> int:
    """Numeric three-part comparison: -1, 0, or 1 (a vs b)."""
    if a < b:
        return -1
    if a > b:
        return 1
    return 0


def _valid_repo(repo: str) -> bool:
    """Validate an ``owner/name`` reference (fail closed, no separators)."""
    if not isinstance(repo, str) or "/" not in repo:
        return False
    owner, _, name = repo.partition("/")
    if not owner or not name or "/" in name:
        return False
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-")
    return all(ch in allowed for ch in owner) and all(ch in allowed for ch in name)


def _fetch_latest_tag(
    repo: str,
    *,
    timeout: float,
    max_bytes: int,
    opener: Callable[..., Any],
) -> str | None:
    """Fetch the latest release tag, bounded; returns None on any failure.

    Raises only for protocol-level problems (timeout, transport), which the
    caller maps to ``check failed``; invalid bodies map to ``invalid
    response`` via ``None``/JSON errors raised here being reclassified.
    """
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    request = urllib.request.Request(url, headers={"User-Agent": "Moira/0.2.2"})
    with opener(request, timeout=timeout) as response:
        status = getattr(response, "status", 200)
        if not 200 <= status < 300:
            return None
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            return None
        data = json.loads(body.decode("utf-8"))
    if not isinstance(data, dict):
        return None
    tag = data.get("tag_name")
    if not isinstance(tag, str) or not tag.strip():
        return None
    return tag.strip()


def check_latest_release(
    repo: str,
    *,
    current: str = "0.2.2",
    timeout: float = 5.0,
    max_bytes: int = DEFAULT_MAX_BYTES,
    opener: Callable[..., Any] | None = None,
) -> UpdateCheckResult:
    """Check the repository's latest release against ``current``.

    ``opener`` is injectable for tests (defaults to ``urllib.request.urlopen``).
    Returns a sanitized result; never raises for network or parse failures.
    """
    if not _valid_repo(repo):
        return UpdateCheckResult(False, STATUS_CHECK_FAILED, current)
    urlopen = opener if opener is not None else urllib.request.urlopen
    try:
        tag = _fetch_latest_tag(repo, timeout=timeout, max_bytes=max_bytes, opener=urlopen)
    except TimeoutError:
        return UpdateCheckResult(False, STATUS_CHECK_FAILED, current)
    except OSError:
        return UpdateCheckResult(False, STATUS_CHECK_FAILED, current)
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return UpdateCheckResult(False, STATUS_INVALID_RESPONSE, current)
    if tag is None:
        return UpdateCheckResult(False, STATUS_INVALID_RESPONSE, current)
    latest = parse_version(tag)
    if latest is None:
        return UpdateCheckResult(False, STATUS_INVALID_RESPONSE, current)
    parsed_current = parse_version(current)
    if parsed_current is None:
        # Current version is not SemVer — treat as up to date with the tag.
        return UpdateCheckResult(True, STATUS_UP_TO_DATE, current, tag)
    if compare_versions(latest, parsed_current) > 0:
        return UpdateCheckResult(True, STATUS_UPDATE_AVAILABLE, current, tag)
    return UpdateCheckResult(True, STATUS_UP_TO_DATE, current, tag)
