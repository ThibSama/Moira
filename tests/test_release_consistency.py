"""Package 6a — release-candidate version consistency.

Every release surface must agree with the single authoritative runtime
version ``moira.__version__`` (defined in ``src/moira/__init__.py``).
Stale supported-version literals anywhere in a release surface fail this
module deterministically, so a partial version bump can never pass.

The supported release (0.3.0) and its AppStream date are pinned here as
the acceptance contract; changing them is a deliberate release action
visible in the diff, not a silent edit.
"""

from __future__ import annotations

import json
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

import moira
from moira.ntfy import Notification, build_request
from moira.updates import STATUS_UP_TO_DATE, check_latest_release

REPO_ROOT = Path(__file__).resolve().parents[1]
SUPPORTED_VERSION = "0.3.0"
STALE_VERSION = "0.2.2"
RELEASE_DATE = "2026-08-04"

SRC = REPO_ROOT / "src" / "moira"
SURFACE_FILES = [
    REPO_ROOT / "pyproject.toml",
    REPO_ROOT / "packaging" / "control",
    REPO_ROOT / "scripts" / "build-deb.sh",
    REPO_ROOT / "README.md",
    REPO_ROOT / "data" / "io.github.moira.QuotaMonitor.metainfo.xml",
    REPO_ROOT / "data" / "io.github.moira.QuotaMonitor.desktop",
]


def _surface_python_files() -> list[Path]:
    return sorted(SRC.glob("*.py"))


def _metainfo() -> ET.Element:
    path = REPO_ROOT / "data" / "io.github.moira.QuotaMonitor.metainfo.xml"
    return ET.parse(path).getroot()


def _request_header(request: object, name: str) -> str | None:
    """Case-insensitive header lookup across Python versions (urllib
    normalizes stored header names, e.g. ``User-Agent`` → ``User-agent``)."""
    for key, value in request.header_items():  # type: ignore[attr-defined]
        if key.lower() == name.lower() and isinstance(value, str):
            return value
    return None


# ── Single authoritative source ──


def test_runtime_version_is_supported_release() -> None:
    assert moira.__version__ == SUPPORTED_VERSION


def test_supported_literal_appears_only_in_version_source() -> None:
    """Within the Python package, the supported literal exists ONLY in
    ``__init__.py`` — every other module must derive it."""
    for path in _surface_python_files():
        text = path.read_text(encoding="utf-8")
        if path.name == "__init__.py":
            assert SUPPORTED_VERSION in text
        else:
            assert SUPPORTED_VERSION not in text, path


def test_no_stale_literal_in_any_release_surface() -> None:
    # README and AppStream legitimately document the previous release
    # (upgrade instructions / preserved release entries); they are checked
    # by their own targeted assertions below.
    for path in _surface_python_files():
        assert STALE_VERSION not in path.read_text(encoding="utf-8"), path
    for path in SURFACE_FILES:
        if path in (
            REPO_ROOT / "README.md",
            REPO_ROOT / "data" / "io.github.moira.QuotaMonitor.metainfo.xml",
        ):
            continue
        assert STALE_VERSION not in path.read_text(encoding="utf-8"), path


# ── Static metadata agrees with the runtime version ──


def test_pyproject_version_agrees() -> None:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        pyproject = tomllib.load(handle)
    assert pyproject["project"]["version"] == moira.__version__


def test_debian_control_version_agrees() -> None:
    control = (REPO_ROOT / "packaging" / "control").read_text(encoding="utf-8")
    assert f"Version: {moira.__version__}" in control


def test_build_script_derives_version_authoritatively() -> None:
    script = (REPO_ROOT / "scripts" / "build-deb.sh").read_text(encoding="utf-8")
    # The artifact version is derived from the authoritative source…
    assert "src/moira/__init__.py" in script
    assert "__version__" in script
    assert "moira_${version}_all.deb" in script
    # …and static metadata is verified with fail-closed mismatches.
    assert "pyproject.toml" in script
    assert "packaging/control" in script
    assert "exit 1" in script


def test_readme_install_and_upgrade_commands_use_supported_version() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert f"dist/moira_{SUPPORTED_VERSION}_all.deb" in readme
    # The only 0.2.2 mention is the documented upgrade path; no stale build
    # path or old install command may survive.
    assert "dist/moira_0.2.2_all.deb" not in readme
    assert "Upgrading from 0.2.2" in readme
    assert readme.count(STALE_VERSION) == 1


# ── AppStream ──


def test_appstream_top_release_is_supported_version() -> None:
    releases = _metainfo().findall("./releases/release")
    assert releases, "AppStream has no release entries"
    top = releases[0]
    assert top.attrib["version"] == SUPPORTED_VERSION
    assert top.attrib["date"] == RELEASE_DATE


def test_appstream_preserves_older_entries() -> None:
    versions = [r.attrib["version"] for r in _metainfo().findall("./releases/release")]
    assert versions[0] == SUPPORTED_VERSION
    for older in ("0.2.2", "0.2.1", "0.2.0", "0.1.1"):
        assert older in versions


def test_appstream_release_notes_cover_0_3_0_features() -> None:
    top = _metainfo().findall("./releases/release")[0]
    description = " ".join(p.text or "" for p in top.findall("description/p")).lower()
    for keyword in (
        "history",
        "24h",
        "90d",
        "account usage",
        "per-provider",
        "native",
        "ntfy",
        "diagnostics",
        "csv/json",
        "deletion",
        "update checks",
        "activity",
        "spinner",
        "hooks",
    ):
        assert keyword in description, keyword
    # Exact token statistics are Codex-only; Claude token support is never claimed.
    assert "claude remains percentage-only" in description
    # Activity claims are privacy-minimal and never overclaim Codex capability.
    assert "hashed session identities" in description
    assert "no polling or terminal scraping" in description
    # Package 7a: the Integrations page and inventory are documented
    # truthfully — read-only now, editing and balance deferred.
    for keyword in ("read-only", "integrations", "providers and models", "inventory"):
        assert keyword in description, keyword
    assert "deferred" in description
    assert "deepseek balance is not configured" in description


def test_appstream_about_text_mentions_integrations() -> None:
    description = " ".join(p.text or "" for p in _metainfo().findall("./description/p")).lower()
    assert "integrations" in description


def test_readme_documents_integrations_view() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "## Integrations" in readme
    assert "**Integrations** view" in readme
    assert "Agents" in readme and "Providers and models" in readme
    assert "hermes config get model --json" in readme
    assert "never parses the whole Hermes YAML" in readme
    assert "auth.json" in readme
    assert "no `/user/balance` call" in readme
    assert "newest-wins coordinator" in readme
    # The activity setup reference now points at the Integrations view.
    assert "configured in the Integrations view" in readme
    # Deferred scope is stated honestly: provider editing, Keyring
    # credentials and bounded read-only connection tests are implemented
    # (local only, fakes-only in the suite); the rest stays deferred.
    for deferred in (
        "Hermes writes",
        "DeepSeek balance",
        "financial units",
    ):
        assert deferred in readme, deferred
    assert "Test connection" in readme
    assert "Edit providers" in readme
    assert "GNOME Keyring" in readme


def test_man_page_documents_integrations_view() -> None:
    man = (REPO_ROOT / "packaging" / "man" / "moira.1").read_text(encoding="utf-8")
    assert "Integrations view" in man
    assert "Providers and models" in man
    assert "hermes config get model \\-\\-json" in man
    assert "never zero" in man
    assert "deferred" in man


# ── Runtime derivation (no circular imports, correct values) ──


def test_ntfy_user_agent_derives_from_runtime_version() -> None:
    request = build_request("https://ntfy.example", "topic", Notification("t", "m"))
    assert _request_header(request, "User-Agent") == f"Moira/{moira.__version__}"


def test_update_check_user_agent_and_default_current_derive() -> None:
    seen_agents: list[str | None] = []

    class _FakeResponse:
        def __enter__(self) -> _FakeResponse:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def read(self, limit: int) -> bytes:
            return json.dumps({"tag_name": "0.2.2"}).encode()

    def fake(request: object, timeout: float) -> _FakeResponse:
        seen_agents.append(_request_header(request, "User-Agent"))
        return _FakeResponse()

    result = check_latest_release("ThibSama/moira", opener=fake)
    assert seen_agents == [f"Moira/{moira.__version__}"]
    assert result.current == moira.__version__
    assert result.status == STATUS_UP_TO_DATE  # 0.2.2 < 0.3.0


def test_ui_and_collector_derive_version_without_literals() -> None:
    ui_source = (SRC / "ui.py").read_text(encoding="utf-8")
    assert "from . import __version__ as APP_VERSION" in ui_source
    collector_source = (SRC / "collectors.py").read_text(encoding="utf-8")
    assert "from . import __version__" in collector_source
    assert '"version": __version__' in collector_source  # Codex clientInfo
