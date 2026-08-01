import os
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from moira.desktop import create_shortcut, desktop_directory, remove_shortcut


def test_xdg_desktop_resolution_from_command(tmp_path: Path) -> None:
    desktop = tmp_path / "Bureau"
    result = CompletedProcess(["xdg-user-dir", "DESKTOP"], 0, str(desktop) + "\n", "")
    with (
        patch("moira.desktop.shutil.which", return_value="/usr/bin/xdg-user-dir"),
        patch("moira.desktop.subprocess.run", return_value=result),
    ):
        assert desktop_directory() == desktop


def test_xdg_config_fallback(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "user-dirs.dirs").write_text('XDG_DESKTOP_DIR="$HOME/Desk-localized"\n')
    with (
        patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config), "HOME": str(tmp_path)}),
        patch("moira.desktop.shutil.which", return_value=None),
        patch("moira.desktop.Path.home", return_value=tmp_path),
    ):
        assert desktop_directory() == tmp_path / "Desk-localized"


def test_shortcut_create_idempotency_and_removal(tmp_path: Path) -> None:
    desktop = tmp_path / "Desktop-from-XDG"
    desktop.mkdir()
    source = tmp_path / "io.github.moira.QuotaMonitor.desktop"
    source.write_text("[Desktop Entry]\nExec=moira\n")
    with (
        patch("moira.desktop.desktop_directory", return_value=desktop),
        patch("moira.desktop.shutil.which", return_value=None),
    ):
        target, changed = create_shortcut(source)
        assert changed
        assert target.stat().st_mode & 0o111
        same_target, changed = create_shortcut(source)
        assert same_target == target
        assert not changed
        assert remove_shortcut()
        assert not remove_shortcut()
