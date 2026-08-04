"""Manual GitHub release checking with strict SemVer 2.0 comparison.

The check is triggered ONLY by the user (no startup check, no telemetry,
no token, no auto-download, no install). The request is bounded in time
and response size, and every outcome is a fixed sanitized status string —
the response body, raw exceptions, URLs, and repository details are never
exposed to the UI.

Version comparison follows SemVer 2.0.0 precedence exactly: numeric
three-part core (``0.10.0`` > ``0.2.2``), prerelease ordering
(``1.0.0-alpha`` < ``1.0.0-alpha.1`` < ``1.0.0-beta`` < ``1.0.0``),
build metadata ignored for ordering, leading zeros rejected, empty or
invalid identifiers rejected, and an invalid ``current`` version fails
the check (never reported as "up to date"). Parsing is total and bounded:
numeric identifiers longer than ``MAX_NUMERIC_IDENTIFIER_DIGITS`` are
rejected, so no input can ever make ``int()`` raise during parsing or
comparison — every string returns ``SemVer | None``.
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

#: Full SemVer 2.0 shape (a ``v``/``V`` tag prefix is tolerated, matching
#: common GitHub release tags). Core groups are ASCII ``[0-9]`` only —
#: ``\\d`` would accept non-ASCII decimal digits (Arabic-Indic, full-width)
#: which strict SemVer forbids. Leading zeros are rejected by validation
#: afterwards. Prerelease/build groups require at least one identifier
#: character (``1.2.3-`` / ``1.2.3+`` never match). Surrounding whitespace
#: is NOT stripped: the grammar is anchored, so ``" 1.2.3 "`` is invalid.
_VERSION_RE = re.compile(
    r"^[vV]?([0-9]+)\.([0-9]+)\.([0-9]+)(?:-([0-9A-Za-z.-]+))?(?:\+([0-9A-Za-z.-]+))?$"
)
_IDENTIFIER_RE = re.compile(r"^[0-9A-Za-z-]+$")

#: Deterministic safe bound for numeric SemVer identifiers. Any core or
#: prerelease numeric identifier longer than this is rejected as invalid.
#: The bound is far above any real release tag and far below Python's
#: int-string conversion limit (4300 digits by default), so ``int()`` can
#: never raise during parsing or comparison: ``parse_version`` is total —
#: every string returns ``SemVer | None``.
MAX_NUMERIC_IDENTIFIER_DIGITS = 64


@dataclass(frozen=True, slots=True)
class SemVer:
    """A parsed SemVer 2.0 version. Build metadata is discarded by design
    (it never affects precedence); prerelease identifiers are kept."""

    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class UpdateCheckResult:
    """Bounded outcome of one manual release check."""

    ok: bool
    status: str
    current: str
    latest: str | None = None


def _valid_identifier(identifier: str) -> bool:
    """Validate one dot-separated SemVer identifier.

    Numeric identifiers (all digits) MUST NOT carry leading zeros (``01``
    is invalid, ``0`` is valid) and MUST stay within the documented digit
    bound (``MAX_NUMERIC_IDENTIFIER_DIGITS``) so conversion can never
    raise. Alphanumeric/hyphen identifiers are valid as-is. Empty
    identifiers are always invalid.
    """
    if not identifier or not _IDENTIFIER_RE.fullmatch(identifier):
        return False
    if identifier.isdigit():
        if len(identifier) > MAX_NUMERIC_IDENTIFIER_DIGITS:
            return False
        return len(identifier) == 1 or identifier[0] != "0"
    return True


def parse_version(tag: str) -> SemVer | None:
    """Parse a SemVer 2.0 tag like ``0.2.2``, ``v1.2.3-rc1`` or
    ``1.2.3+build.5``. Total and bounded: NEVER raises, every input returns
    ``SemVer | None``. ASCII-only: non-ASCII decimal digits and surrounding
    whitespace are rejected (``tag`` is parsed exactly as given — no
    stripping). Returns None for anything non-conforming (leading zeros,
    empty/invalid identifiers, oversized numeric identifiers, malformed
    shapes)."""
    if not isinstance(tag, str):
        return None
    # fullmatch (not match): Python's `$` matches before a trailing newline,
    # which would let "0.3.0\n" through despite the anchored grammar.
    match = _VERSION_RE.fullmatch(tag)
    if match is None:
        return None
    for group in (match.group(1), match.group(2), match.group(3)):
        if len(group) > 1 and group.startswith("0"):
            return None
        if len(group) > MAX_NUMERIC_IDENTIFIER_DIGITS:
            return None
    prerelease: tuple[str, ...] = ()
    if match.group(4) is not None:
        parts = match.group(4).split(".")
        if any(not _valid_identifier(part) for part in parts):
            return None
        prerelease = tuple(parts)
    if match.group(5) is not None:
        # Build metadata: identifiers may contain digits with leading
        # zeros, but must be non-empty (``1.2.3+`` is rejected by the
        # regex; ``1.2.3+..x`` and ``1.2.3+a.`` are rejected here).
        if any(not part or not _IDENTIFIER_RE.match(part) for part in match.group(5).split(".")):
            return None
    return SemVer(int(match.group(1)), int(match.group(2)), int(match.group(3)), prerelease)


def _compare_prerelease(a: tuple[str, ...], b: tuple[str, ...]) -> int:
    """SemVer 2.0 prerelease precedence: numeric identifiers compare
    numerically, alphanumeric identifiers compare ASCII, numeric <
    alphanumeric, and a longer identifier set wins when all preceding
    identifiers are equal."""
    for x, y in zip(a, b, strict=False):
        if x == y:
            continue
        x_numeric = x.isdigit()
        y_numeric = y.isdigit()
        if x_numeric and y_numeric:
            return -1 if int(x) < int(y) else 1
        if x_numeric:
            return -1
        if y_numeric:
            return 1
        return -1 if x < y else 1
    if len(a) < len(b):
        return -1
    if len(a) > len(b):
        return 1
    return 0


def compare_versions(a: SemVer, b: SemVer) -> int:
    """SemVer 2.0 precedence: -1, 0, or 1 (a vs b).

    Core is compared numerically; a version WITHOUT prerelease has higher
    precedence than one WITH it; build metadata is ignored.
    """
    if (a.major, a.minor, a.patch) != (b.major, b.minor, b.patch):
        return -1 if (a.major, a.minor, a.patch) < (b.major, b.minor, b.patch) else 1
    if not a.prerelease and not b.prerelease:
        return 0
    if not a.prerelease:
        return 1  # release > prerelease
    if not b.prerelease:
        return -1
    return _compare_prerelease(a.prerelease, b.prerelease)


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
    # Returned exactly as received: surrounding whitespace or non-ASCII
    # digits in the tag fail strict parse_version and map to a sanitized
    # invalid-response outcome (never silently normalized).
    return tag


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
    An invalid ``current`` version (not strict SemVer) fails the check
    closed — it is never reported as "up to date".
    """
    if not _valid_repo(repo):
        return UpdateCheckResult(False, STATUS_CHECK_FAILED, current)
    parsed_current = parse_version(current)
    if parsed_current is None:
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
    if compare_versions(latest, parsed_current) > 0:
        return UpdateCheckResult(True, STATUS_UPDATE_AVAILABLE, current, tag)
    return UpdateCheckResult(True, STATUS_UP_TO_DATE, current, tag)
