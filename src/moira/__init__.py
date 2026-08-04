"""Moira quota monitor.

``__version__`` is the single authoritative runtime version source. Every
other Python surface (UI/About dialog, Codex app-server clientInfo, NTFY
User-Agent, update-check current/User-Agent) derives from it; Debian,
AppStream, README and packaging metadata are verified against it by the
release-consistency tests and ``scripts/build-deb.sh``.
"""

__version__ = "0.3.0"
