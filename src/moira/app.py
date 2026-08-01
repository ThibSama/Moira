from __future__ import annotations

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib  # noqa: E402

from .ui import MainWindow


class MoiraApplication(Adw.Application):
    def __init__(self, smoke_test: bool = False) -> None:
        super().__init__(application_id="io.github.moira.QuotaMonitor")
        self.smoke_test = smoke_test

    def do_activate(self) -> None:
        window = self.get_active_window() or MainWindow(self, self.smoke_test)
        window.present()
        if self.smoke_test:
            GLib.timeout_add(500, self._quit_smoke)

    def _quit_smoke(self) -> bool:
        self.quit()
        return False


def main() -> int:
    smoke = "--smoke-test" in sys.argv
    args = [arg for arg in sys.argv if arg != "--smoke-test"]
    return int(MoiraApplication(smoke).run(args))


if __name__ == "__main__":
    raise SystemExit(main())
