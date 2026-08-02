# Troubleshooting and live acceptance

## Claude status-line integration

Use **Set up Claude integration** in Moira. Setup stops without changing Claude settings when `statusLine` is not a valid command object. A successful setup keeps these recovery files at mode `0600`:

- `~/.claude/settings.json.moira-backup`: the full pre-setup settings snapshot;
- `$XDG_CONFIG_HOME/moira/claude-integration.json`: the original status-line object needed for surgical removal.

**Remove Claude integration** restores only that object and keeps unrelated current Claude settings. It refuses removal if the active command is no longer Moira's wrapper. In that situation, compare only the `statusLine` objects; do not blindly overwrite current settings with the full backup.

Moira updates its cache only when both `rate_limits.five_hour` and `rate_limits.seven_day` contain a numeric `used_percentage` in 0–100 and a valid `resets_at`. Missing limits do not overwrite a prior cache. Before the first qualifying Claude response, waiting/unavailable is expected. After 15 minutes without a new status-line event, cached readings are stale.

## Weekly exhaustion

An AVAILABLE weekly reading at ≥100% establishes exhaustion until the weekly reset. STALE, missing, error, or sub-100 readings do not establish exhaustion. When exhausted:

- **Claude**: the weekly card shows a critical state and the five-hour row is disabled. The UI shows the weekly reset/countdown, not the five-hour reset. Stored readings are unchanged.
- **Codex**: the weekly card shows an unavailable-until-reset message with reset time and countdown.

Exhaustion is cleared by a new AVAILABLE reading below 100% or a new reset window. NTFY exhaustion and recovery events are deduplicated once per service/window.

## Codex app-server

Codex must be logged in and available on the desktop session `PATH`. Moira rejects a response when initialization fails, the 12-second exchange times out, fields are malformed, or no exact 10,080-minute window exists. It never substitutes the common five-hour primary window.

## Refresh interval

Moira refreshes at startup, on focus regain (with monotonic debounce and overlap guard), and at a configurable interval. Choices are 1, 2, 5, 10, 15, or 30 minutes. New configs default to 2 minutes. Saving a new interval replaces the GLib timer immediately without restart or duplicates. Countdowns recompute locally every 30 seconds without collectors. Claude data changes only after a Claude Code response; cache rereads are not fresh provider events.

## NTFY

Enter the existing HTTP(S) server and single-segment topic in Notifications. Put an access token, if required, in the password field and save once; the field clears after GNOME Keyring accepts it. Moira never writes the token to JSON. Select **Send test notification** and confirm both the success message and receipt in a subscribed client.

Exhaustion and recovery events are deduplicated once per service/window, independent from threshold alerts. A duplicate generic 100% threshold alert is suppressed when an exhaustion event fires.

Older Claude/Codex notifier scripts and user timers are outside Moira and remain intact. If they target the same topic, they may duplicate Moira's messages. Disable one source deliberately if duplicates are undesirable.

## Localization

Moira detects the locale from the environment (LANG/LC_ALL/LC_MESSAGES). French locales get French UI; all others get English. No manual selector. Dates and countdowns are also localized. To verify:

```sh
LANG=fr_FR.UTF-8 moira --smoke-test
LANG=en_US.UTF-8 moira --smoke-test
```

## Desktop and package checks

After installing the `.deb`, launch `io.github.moira.QuotaMonitor.desktop` from the application menu. **Create desktop shortcut** copies that installed entry to the directory reported by `xdg-user-dir DESKTOP`, makes it executable, and asks `gio` to mark it trusted. Repeating creation or removal is safe. A session with no separate configured desktop directory, or a shell that intentionally hides desktop files, is unsupported; use the application menu instead.

Useful non-secret checks:

```sh
dpkg-query -W -f='${Status} ${Version}\n' moira
desktop-file-validate /usr/share/applications/io.github.moira.QuotaMonitor.desktop
appstreamcli validate --no-net /usr/share/metainfo/io.github.moira.QuotaMonitor.metainfo.xml
xdg-user-dir DESKTOP
```

## Local history

Moira stores quota observations in `$XDG_STATE_HOME/moira/history.sqlite3` (mode `0600`). Only fresh AVAILABLE readings with a percentage and `reset_at` are stored. STALE, error, unavailable, and parse-error readings are never recorded. No raw payloads, prompts, responses, transcript text, private paths, account identifiers, or secrets are retained. Token counts are never estimated or derived from percentages.

Quota changes are recorded immediately and every distinct percentage or reset transition is preserved, including multiple changes inside one 15-minute bucket. Earlier change points are never overwritten. Unchanged values retain at most one periodic sample per service/quota per 15-minute bucket, making repeated refresh completions idempotent. Rows older than 90 days are purged after successful writes.

History writes run on a `HistoryCoordinator` daemon worker thread with a newest-wins overflow policy. Refresh rendering and alerts never await history I/O. If the worker is busy, the newest pending batch replaces any older pending batch, and a sanitized `backlog saturated` status is set — cleared only after a later successful write. The coordinator exposes an idempotent `shutdown` connected to the window `close-request` signal. Shutdown rejects new work, signals the worker, and joins with a bounded timeout (2 seconds) that never blocks GTK for the SQLite five-second lock. History failure returns only a sanitized diagnostic string and does not disrupt quota state, display, or alerts. `SchemaVersionError` distinguishes schema-version failures from domain validation failures; `write_history_safely` returns exactly `ok`, `database unavailable`, `schema mismatch`, `invalid observation`, or `backlog saturated`.

Domain validation is fail-closed: timestamps must be timezone-aware and are normalized to UTC; labels and sources must be non-empty; percentages must be 0–100; token counts must be non-negative integers. `AVAILABLE_EXACT` requires `total_tokens` and at least one breakdown field; non-available statuses must not carry any token counts.
