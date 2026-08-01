# Architecture

Moira keeps provider-specific behavior at the boundary and passes only validated `QuotaReading` values inward.

```text
Claude CLI /usage ─> PTY capture ─> Claude parser ─┐
                                                   ├─> typed readings ─> stale merge ─> GTK cards
Codex app-server ─> JSON-RPC ─> Codex parser ─────┘                         │
                                                                           └─> alert rules ─> NTFY
```

- `models.py` defines the single normalized quota model and rejects invalid percentages or naive timestamps.
- `parsers.py` contains strict, independently testable provider parsing. Unknown formats are errors, never zeroes.
- `collectors.py` owns process discovery and capture. Claude uses its supported `/usage` interface through a PTY. Codex uses the CLI's generated app-server protocol and `account/rateLimits/read`; only a duration-declared weekly window is accepted.
- `alerts.py` merges failed refreshes with last successful readings as stale and generates deduplicated threshold, reset, and error events.
- `persistence.py` stores versioned JSON configuration and a small last-known-state cache under XDG directories with mode `0600`.
- `secrets.py` is the only token boundary and uses libsecret/GNOME Keyring. Tokens never enter JSON state.
- `ntfy.py` validates and constructs requests with the standard library.
- `ui.py` owns GTK widgets and schedules collectors on worker threads. A refresh guard prevents overlap.

No database, dashboard scraping, private endpoint, background service, tray, or legacy helper dependency is used.

