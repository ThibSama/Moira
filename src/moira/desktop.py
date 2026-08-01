from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

DESKTOP_ID = "io.github.moira.QuotaMonitor.desktop"
PACKAGED_ENTRY = Path("/usr/share/applications") / DESKTOP_ID


class DesktopShortcutError(RuntimeError):
    pass


def desktop_directory() -> Path:
    executable = shutil.which("xdg-user-dir")
    if executable:
        try:
            result = subprocess.run(
                [executable, "DESKTOP"],
                check=True,
                capture_output=True,
                text=True,
                timeout=3,
            )
            value = result.stdout.strip()
            if value:
                resolved = Path(value).expanduser()
                if resolved != Path.home():
                    return resolved
        except (OSError, subprocess.SubprocessError):
            pass
    config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    user_dirs = config / "user-dirs.dirs"
    try:
        text = user_dirs.read_text(encoding="utf-8")
    except OSError as exc:
        raise DesktopShortcutError("XDG desktop directory is unavailable") from exc
    match = re.search(r'^XDG_DESKTOP_DIR="([^"\n]+)"$', text, re.MULTILINE)
    if not match:
        raise DesktopShortcutError("XDG desktop directory is not configured")
    value = match.group(1).replace("$HOME", str(Path.home()), 1)
    resolved = Path(value)
    if not resolved.is_absolute() or resolved == Path.home():
        raise DesktopShortcutError("this environment has no separate desktop directory")
    return resolved


def shortcut_path() -> Path:
    return desktop_directory() / DESKTOP_ID


def create_shortcut(source: Path = PACKAGED_ENTRY) -> tuple[Path, bool]:
    if not source.is_file():
        raise DesktopShortcutError("the packaged Moira desktop entry is not installed")
    desktop = desktop_directory()
    if not desktop.is_dir():
        raise DesktopShortcutError("the configured XDG desktop directory does not exist")
    target = desktop / source.name
    content = source.read_bytes()
    changed = not target.exists() or target.read_bytes() != content
    if changed:
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        try:
            temporary.write_bytes(content)
            os.chmod(temporary, 0o755)
            os.replace(temporary, target)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    else:
        target.chmod(target.stat().st_mode | 0o111)
    gio = shutil.which("gio")
    if gio:
        try:
            subprocess.run(
                [gio, "set", str(target), "metadata::trusted", "true"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    return target, changed


def remove_shortcut() -> bool:
    target = shortcut_path()
    if not target.exists():
        return False
    target.unlink()
    return True
