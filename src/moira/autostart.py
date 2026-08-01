from __future__ import annotations

import os
from pathlib import Path

DESKTOP = """[Desktop Entry]
Type=Application
Name=Moira
Comment=Monitor Claude and Codex quotas
Exec=moira
Icon=io.github.moira.QuotaMonitor
Terminal=false
Categories=Utility;
X-GNOME-Autostart-enabled=true
"""


def path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "autostart/io.github.moira.QuotaMonitor.desktop"


def set_enabled(enabled: bool) -> None:
    target = path()
    if enabled:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(DESKTOP, encoding="utf-8")
    elif target.exists():
        target.unlink()
