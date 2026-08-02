# Architecture

Moira keeps provider-specific behavior at the boundary and passes only validated `QuotaReading` values inward.

```text
Claude response ─> status-line chain ─> minimal atomic cache ─┐
                                                              ├─> typed readings ─> stale merge ─> exhaustion derivation ─> GTK cards
Codex app-server ─> ordered JSON-RPC handshake ─> validator ──┘                                                         │
                                                                                                                        ├─> alert rules ─> NTFY
                                                                                                                        └─> 30s local recompute
```

- `models.py` defines the normalized quota model and rejects invalid percentages or naive timestamps.
- `claude_integration.py` validates structured status-line limits, writes the privacy-minimal cache, and atomically installs/removes a reversible chain around an existing command status line. Full status-line input exists only in process memory while it is passed to the original command.
- `parsers.py` validates Codex percentages, reset epochs, and the exact 10,080-minute weekly duration. Unknown formats are errors, never zeroes.
- `collectors.py` reads the Claude cache and owns the Codex lifecycle. Codex sends `initialize`, validates its response, sends `initialized`, and only then calls `account/rateLimits/read`. A shared 12-second deadline bounds the exchange; exits terminate and reap the app-server process group. Errors never include provider payloads, paths, or stderr.
- `exhaustion.py` is the pure weekly-exhaustion rule: an AVAILABLE weekly reading at ≥100% means exhausted until its weekly reset. STALE, missing, error, or sub-100 readings must not newly establish exhaustion. Derives `ServiceSnapshot` state outside GTK widgets.
- `alerts.py` merges failed refreshes with last successful readings as stale and generates deduplicated threshold, reset, error, exhaustion, and recovery events. The duplicate generic 100% threshold alert is suppressed when an exhaustion event fires. Exhaustion and recovery events are deduplicated once per service/window, independent from thresholds.
- `i18n.py` detects the locale from the environment (LANG/LC_ALL/LC_MESSAGES) and provides French translations with English fallback. No manual selector, no compiled .mo files. Functionally equivalent to gettext.
- `persistence.py` stores versioned JSON configuration (v2) and a small last-known-state cache under XDG directories with mode `0600`. Config v1 is additively migrated to v2 preserving user settings. Refresh choices are 1, 2, 5, 10, 15, or 30 minutes with a 2-minute default.
- `secrets.py` is the only token boundary and uses libsecret/GNOME Keyring. Tokens never enter JSON state.
- `ntfy.py` validates and constructs requests with the standard library.
- `desktop.py` resolves the localized XDG desktop directory and idempotently copies/removes the installed desktop entry.
- `ui.py` owns GTK widgets and schedules collectors on worker threads. A refresh guard prevents overlap. Refresh occurs at startup, on focus regain (monotonic debounce), and at the configured interval. Countdowns recompute locally every 30 seconds without collectors. Saving a new interval immediately replaces the GLib timer without restart or duplicates.

No database, rendered dashboard/terminal scraping, private endpoint, background service, tray, or legacy helper dependency is used. An existing user status-line command remains an independent delegate when Moira chaining is enabled.
