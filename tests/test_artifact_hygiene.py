"""Package 6b — deterministic artifact-content hygiene tests.

Builds fresh wheel, sdist and .deb from the current tree into an isolated
output directory (once per module) and enumerates every archive member.
Fails on any forbidden entry — tests, caches, bytecode, VCS/venv trees,
local build trees, user configuration/state/history, credentials, fixture
secrets, absolute private paths — and validates the positive contract:

- wheel: package module set, dist-info metadata, console entry points;
- sdist: offline build inputs (pyproject.toml, README, LICENSE, package
  sources, packaging metadata) and no tests;
- .deb: Debian control metadata, launcher modes, Python package modes,
  desktop file, icon and AppStream metainfo.

The sdist is additionally proven to rebuild a working 0.3.0 wheel in a
fresh isolated extraction, without reading the original repository or
using the network; that wheel imports ``moira.__version__ == "0.3.0"``.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

import moira

REPO_ROOT = Path(__file__).resolve().parents[1]
VERSION = moira.__version__
SDIST_ROOT = f"moira_quota_monitor-{VERSION}"
WHEEL_NAME = f"moira_quota_monitor-{VERSION}-py3-none-any.whl"
SDIST_NAME = f"moira_quota_monitor-{VERSION}.tar.gz"
DEB_NAME = f"moira_{VERSION}_all.deb"
DIST_INFO = f"moira_quota_monitor-{VERSION}.dist-info"

#: Member names that must never appear in any artifact.
_FORBIDDEN_PATTERNS = (
    re.compile(r"(^|/)(tests?|__pycache__)(/|$)"),
    re.compile(r"\.pyc$"),
    re.compile(r"(^|/)(\.git|\.svn|\.hg|\.venv)(/|$)"),
    re.compile(r"(^|/)(\.mypy_cache|\.pytest_cache|\.ruff_cache)(/|$)"),
    re.compile(r"(^|/)(build|dist)(/|$)"),
    re.compile(r"(^|/)(\.env|.*\.pem|.*\.key|.*\.p12)$"),
    re.compile(
        r"(^|/)(config\.json|state\.json|history\.sqlite3|claude-rate-limits\.json|activity\.json)$"
    ),
    re.compile(
        r"(^|/)(config\.yaml|shell-hooks-allowlist\.json|settings\.json|claude-integration\.json)$"
    ),
    re.compile(r"/home/|/Users/"),
)


def _forbidden_hits(members: list[str]) -> list[str]:
    """Return every member that violates the artifact hygiene contract."""
    hits: list[str] = []
    for name in members:
        if name.startswith("/"):
            hits.append(f"absolute path: {name}")
        for pattern in _FORBIDDEN_PATTERNS:
            if pattern.search(name):
                hits.append(f"forbidden entry: {name}")
                break
    return hits


def _package_modules() -> list[str]:
    """Every module shipped by the moira package (from the current tree)."""
    return sorted(path.name for path in (REPO_ROOT / "src" / "moira").glob("*.py"))


def _build_artifacts(output: Path) -> dict[str, Path]:
    """Build fresh wheel+sdist (``python -m build``, offline) and .deb into
    an isolated output directory."""
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    subprocess.run(
        [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(output)],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [str(REPO_ROOT / "scripts" / "build-deb.sh")],
        cwd=REPO_ROOT,
        env={**env, "MOIRA_OUTPUT_DIR": str(output)},
        check=True,
        capture_output=True,
        text=True,
    )
    artifacts = {
        "wheel": output / WHEEL_NAME,
        "sdist": output / SDIST_NAME,
        "deb": output / DEB_NAME,
    }
    for name, path in artifacts.items():
        assert path.is_file(), f"{name} not built: {path}"
    return artifacts


@pytest.fixture(scope="module")
def artifacts(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    output = tmp_path_factory.mktemp("artifacts")
    return _build_artifacts(output)


def test_contract_version_is_supported_release() -> None:
    assert VERSION == "0.3.0"


# ── Wheel ──


def test_wheel_hygiene_and_metadata(artifacts: dict[str, Path]) -> None:
    with zipfile.ZipFile(artifacts["wheel"]) as zf:
        members = zf.namelist()
        assert _forbidden_hits(members) == []

        # The wheel ships exactly the package modules from the tree.
        modules = {m for m in members if m.startswith("moira/") and m.endswith(".py")}
        assert modules == {f"moira/{name}" for name in _package_modules()}

        # dist-info metadata and console entry points.
        assert f"{DIST_INFO}/METADATA" in members
        assert f"{DIST_INFO}/WHEEL" in members
        assert f"{DIST_INFO}/RECORD" in members
        assert f"{DIST_INFO}/entry_points.txt" in members
        metadata = zf.read(f"{DIST_INFO}/METADATA").decode("utf-8")
        assert "Name: moira-quota-monitor" in metadata
        assert f"Version: {VERSION}" in metadata
        assert "Requires-Python: >=3.11" in metadata
        entry_points = zf.read(f"{DIST_INFO}/entry_points.txt").decode("utf-8")
        assert "moira = moira.app:main" in entry_points
        assert "moira-claude-statusline = moira.claude_integration:statusline_main" in entry_points
        assert "moira-agent-hook = moira.agent_hooks:agent_hook_main" in entry_points
        # Package 6c modules ship in the wheel.
        for module in (
            "activity.py",
            "agent_hooks.py",
            "agent_integration.py",
            "hermes_hooks.py",
            "codex_activity.py",
            "activity_view.py",
        ):
            assert f"moira/{module}" in modules, module
        wheel = zf.read(f"{DIST_INFO}/WHEEL").decode("utf-8")
        assert "Wheel-Version: 1.0" in wheel
        assert "Tag: py3-none-any" in wheel


# ── Sdist ──


def test_sdist_hygiene_and_build_inputs(artifacts: dict[str, Path]) -> None:
    with tarfile.open(artifacts["sdist"], "r:gz") as tf:
        names = tf.getnames()
        rel = [name.removeprefix(f"{SDIST_ROOT}/") for name in names if name != SDIST_ROOT]
        assert _forbidden_hits(rel) == []
        assert not any(name.startswith("tests/") for name in rel)

        # Offline build inputs: package sources, project metadata, docs.
        for required in ("pyproject.toml", "README.md", "LICENSE", "MANIFEST.in", "setup.cfg"):
            assert required in rel, required
        expected = {f"src/moira/{name}" for name in _package_modules()}
        assert expected <= set(rel)

        pkg_info = tf.extractfile(f"{SDIST_ROOT}/PKG-INFO")
        assert pkg_info is not None
        assert f"Version: {VERSION}" in pkg_info.read().decode("utf-8")

        # Shipped SOURCES.txt must itself be consistent (no tests, no caches).
        sources = tf.extractfile(f"{SDIST_ROOT}/src/moira_quota_monitor.egg-info/SOURCES.txt")
        assert sources is not None
        sources_text = sources.read().decode("utf-8")
        assert "tests/" not in sources_text
        assert _forbidden_hits(sources_text.splitlines()) == []


# ── Debian package ──


def test_deb_hygiene_metadata_and_modes(artifacts: dict[str, Path]) -> None:
    listing = subprocess.run(
        ["dpkg-deb", "--contents", str(artifacts["deb"])],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    entries: dict[str, tuple[str, int]] = {}
    for line in listing.splitlines():
        match = re.match(r"^(\S+)\s+\S+\s+(\d+)\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s+(.+)$", line)
        assert match is not None, f"unparseable dpkg-deb line: {line}"
        mode, size, path = match.group(1), int(match.group(2)), match.group(3)
        entries[path] = (mode, size)
    paths = list(entries)
    assert _forbidden_hits([p.removeprefix("./") for p in paths]) == []

    control = subprocess.run(
        ["dpkg-deb", "--field", str(artifacts["deb"])],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "Package: moira" in control
    assert f"Version: {VERSION}" in control
    assert "Architecture: all" in control
    assert "python3-gi" in control
    assert "gir1.2-gtk-4.0" in control
    assert "gir1.2-adw-1" in control
    assert "gir1.2-secret-1" in control

    # Launchers are executable; everything else is 0644.
    assert entries["./usr/bin/moira"][0] == "-rwxr-xr-x"
    assert entries["./usr/bin/moira-claude-statusline"][0] == "-rwxr-xr-x"
    assert entries["./usr/bin/moira-agent-hook"][0] == "-rwxr-xr-x"
    deb_modules = sorted(
        p for p in paths if p.startswith("./usr/lib/moira/moira/") and p.endswith(".py")
    )
    assert len(deb_modules) == len(_package_modules())
    for path in deb_modules:
        assert entries[path][0] == "-rw-r--r--", path
    assert (
        entries["./usr/share/applications/io.github.moira.QuotaMonitor.desktop"][0] == "-rw-r--r--"
    )
    assert (
        entries["./usr/share/icons/hicolor/scalable/apps/io.github.moira.QuotaMonitor.svg"][0]
        == "-rw-r--r--"
    )
    assert (
        entries["./usr/share/metainfo/io.github.moira.QuotaMonitor.metainfo.xml"][0] == "-rw-r--r--"
    )


# ── Offline rebuild from the sdist ──


def test_sdist_rebuilds_wheel_offline(artifacts: dict[str, Path], tmp_path: Path) -> None:
    """Extract the sdist and build a wheel from it in isolation, without
    the original repository or network; the wheel must be 0.3.0 and its
    package must import with ``moira.__version__ == "0.3.0"``."""
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    with tarfile.open(artifacts["sdist"], "r:gz") as tf:
        tf.extractall(extracted)
    source = extracted / SDIST_ROOT
    assert source.is_dir()

    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    offline_out = tmp_path / "offline-out"
    subprocess.run(
        [sys.executable, "-m", "build", "--no-isolation", "--wheel", "--outdir", str(offline_out)],
        cwd=source,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    rebuilt = offline_out / WHEEL_NAME
    assert rebuilt.is_file()
    with zipfile.ZipFile(rebuilt) as zf:
        metadata = zf.read(f"{DIST_INFO}/METADATA").decode("utf-8")
        assert f"Version: {VERSION}" in metadata

    unzipped = tmp_path / "unzipped"
    with zipfile.ZipFile(rebuilt) as zf:
        zf.extractall(unzipped)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import moira; print(moira.__version__); print(moira.__file__)",
        ],
        cwd=tmp_path,
        env={**env, "PYTHONPATH": str(unzipped)},
        check=True,
        capture_output=True,
        text=True,
    )
    lines = result.stdout.strip().splitlines()
    assert lines[0] == "0.3.0"
    assert lines[1].startswith(str(unzipped))
