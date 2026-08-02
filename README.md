# Moira

Moira is a compact Ubuntu GTK4/Libadwaita utility for Claude and Codex quotas. It shows Claude five-hour and weekly usage, Codex weekly usage, local reset times, countdowns, refresh state, weekly exhaustion semantics, and optional deduplicated NTFY alerts including exhaustion and recovery events.

## Data sources and behavior

- Claude Code sends structured `rate_limits.five_hour` and `rate_limits.seven_day` data to its configured status-line command after a response. Moira chains the existing command and reads its own minimal cache; it never scrapes the rendered `/usage` screen.
- The installed `codex app-server` read-only `account/rateLimits/read` method returns structured windows. Moira completes the initialize handshake first and accepts only a window declared as exactly 10,080 minutes (seven days). It intentionally does not model or display a Codex five-hour quota.
- Missing data produces **Unavailable**. Invalid provider data produces **Parse error**. When a refresh fails after a success, Moira retains the last successful values as **Stale**.

Provider interfaces can change. Validation fails closed instead of treating changed or missing data as 0%.

## Weekly exhaustion

An AVAILABLE weekly reading at ≥100% means the service is exhausted until its weekly reset. STALE, missing, error, or sub-100 readings must not newly establish exhaustion. When exhausted:

- **Claude**: the weekly card shows a critical state and the five-hour row is visually disabled. The UI states that weekly exhaustion blocks usage and shows the weekly reset time and countdown, not the five-hour reset. Stored readings remain unchanged.
- **Codex**: the weekly card renders as critical with an unavailable-until-reset message, reset time, and countdown. Moira never adds a Codex five-hour quota.

## Internationalization

Moira detects the locale from the environment (LANG/LC_ALL/LC_MESSAGES) and displays French for French locales with English fallback for all other locales. All visible strings — UI labels, error messages, About dialog, NTFY notifications, dates, and countdowns — are localized. No manual language selector is needed.

## Refresh

Moira refreshes at startup, on focus regain (with a monotonic debounce and overlap guard), and at a configurable interval. Refresh choices are 1, 2, 5, 10, 15, or 30 minutes. New configurations default to 2 minutes. Saving a new interval immediately replaces the GLib provider timer without a restart or duplicates. The last and next refresh times are displayed.

Countdowns and next-refresh times are recomputed locally every 30 seconds without collectors. Claude data changes only after a Claude Code response; cache rereads are not fresh provider events.

## Install on Ubuntu

Build and install the Debian package:

```sh
./scripts/build-deb.sh
sudo apt install ./dist/moira_0.2.0_all.deb
```

The package depends on Python 3, PyGObject, GTK4, Libadwaita, and libsecret GI. Run `moira` or launch **Moira** from the application menu. Install the same `.deb` on another PC and authenticate each provider CLI separately there.

In Notifications, select **Set up Claude integration**. Moira first validates `~/.claude/settings.json`. If a command status line already exists, Moira stores its complete status-line object and substitutes `/usr/bin/moira-claude-statusline`; the wrapper passes the original JSON input to the old command and preserves its output and exit status. Setup writes atomically and creates `settings.json.moira-backup`. **Remove Claude integration** restores only the saved status-line field, preserving unrelated settings changed since setup. Removal refuses to act if another program changed the status line in the meantime.

Complete one minimal Claude response after setup if no recent status-line event exists. Until both structured limits arrive, Moira shows waiting/unavailable (or retains an older successful reading as stale), never 0%.

Uninstall without deleting per-user settings:

```sh
sudo apt remove moira
```

Optional user data can be removed manually from `~/.config/moira` and `~/.local/state/moira`. The NTFY token is a GNOME Keyring item named "Moira NTFY token" and is not in those directories.

## Notifications, privacy, and desktop integration

The Notifications view configures an HTTP(S) NTFY server, one topic segment, enabled state, comma-separated thresholds, reset alerts, error alerts, refresh interval, and login autostart. **Send test notification** performs the explicit live delivery test. A non-empty token field updates GNOME Keyring; leaving it blank preserves the existing token.

Exhaustion and recovery NTFY events are deduplicated once per service/window and are independent from threshold alerts. A duplicate generic 100% threshold alert is suppressed when an exhaustion event fires. Threshold notifications require a crossing from below to at-or-above a threshold in the same quota window. Reset and parse/error notifications are deduplicated. Delivery failures are not marked sent and may retry after the next qualifying refresh.

Configuration is versioned JSON at `$XDG_CONFIG_HOME/moira/config.json` (normally `~/.config/moira/config.json`). Last readings and alert deduplication keys are at `$XDG_STATE_HOME/moira/state.json`. Both are written with mode `0600`. Enabling autostart creates `$XDG_CONFIG_HOME/autostart/io.github.moira.QuotaMonitor.desktop` at runtime.

Claude's separate `$XDG_STATE_HOME/moira/claude-rate-limits.json` cache contains only `five_hour` and `seven_day` objects. Each has `service`, `percentage`, `reset_epoch`, and `retrieved_at`; status-line input, prompts, transcript paths, workspaces, model names, account data, and secrets are never written by Moira. Chained third-party status-line commands still receive the original input and retain responsibility for their own privacy behavior.

**Create desktop shortcut** and **Remove desktop shortcut** resolve `XDG_DESKTOP_DIR` through `xdg-user-dir`, copy the packaged launcher, mark it executable, and request GNOME's trusted metadata when `gio` supports it. Both operations are idempotent. If the session has no distinct XDG desktop directory, desktop files are unsupported and Moira explains that instead of guessing an English or localized folder name.

## Development

```sh
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e . pytest ruff mypy build
.venv/bin/python -m pytest
.venv/bin/ruff format --check .
.venv/bin/ruff check .
.venv/bin/mypy
PYTHONPATH=src xvfb-run -a python3 -m moira.app --smoke-test
./scripts/build-deb.sh
desktop-file-validate data/io.github.moira.QuotaMonitor.desktop
appstreamcli validate --no-net data/io.github.moira.QuotaMonitor.metainfo.xml
```

Tests use sanitized fixtures and mocks. They do not require provider accounts, network access, NTFY, or a running keyring.

## Troubleshooting

- **Unavailable / CLI not found:** install the official provider CLI and ensure `claude` or `codex` is on the desktop session's `PATH`.
- **No Claude values:** set up the integration, use Claude Code normally, and complete a response. Some account types may not expose both windows.
- **Claude integration conflict:** Moira chains only a valid command-type status line. Inspect the non-secret `statusLine` field and decide which integration should own it; Moira does not overwrite an unknown change.
- **No Codex weekly value:** confirm login and update Codex. Moira requires a rate-limit window declared as exactly seven days.
- **Keyring error:** ensure GNOME Keyring/libsecret is installed and unlocked. Moira never falls back to a plaintext token.
- **NTFY test fails:** verify the server URL, topic, network, certificate, and token permissions. The token is never included in errors.
- **Desktop shortcut unavailable:** confirm `xdg-user-dir DESKTOP` points to a separate existing directory. Use the application menu if the shell hides desktop files.
- **Stale countdown reaches zero:** refresh again after connectivity/authentication is restored. Stale timestamps remain the last provider-reported values.
- **Weekly exhaustion:** when a weekly reading reaches 100%, Moira blocks usage display and shows the weekly reset countdown. Exhaustion is cleared only by a new AVAILABLE reading below 100% or a new reset window.

See [ARCHITECTURE.md](ARCHITECTURE.md) for module boundaries and [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for recovery and live checks.
