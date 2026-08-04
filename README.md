# Moira

Moira is a compact Ubuntu GTK4/Libadwaita utility for Claude and Codex quotas. It shows Claude five-hour and weekly usage, Codex weekly usage, local reset times, countdowns, refresh state, weekly exhaustion semantics, optional native and NTFY notifications, and a History tab with selectable ranges, exact Codex token statistics, sanitized diagnostics, and CSV/JSON export.

## Quotas

- Claude Code sends structured `rate_limits.five_hour` and `rate_limits.seven_day` data to its configured status-line command after a response. Moira chains the existing command and reads its own minimal cache; it never scrapes the rendered `/usage` screen.
- The installed `codex app-server` read-only `account/rateLimits/read` method returns structured windows. Moira completes the initialize handshake first and accepts only a window declared as exactly 10,080 minutes (seven days). It intentionally does not model or display a Codex five-hour quota.
- Missing data produces **Unavailable**. Invalid provider data produces **Parse error**. When a refresh fails after a success, Moira retains the last successful values as **Stale**.

Provider interfaces can change. Validation fails closed instead of treating changed or missing data as 0%.

## Exact token usage

Exact daily token statistics are computed **only from official Codex account usage** (`codex app-server` `account/usage/read`): typed daily totals, reported days, average per reported day, and range peak day, always labeled per service and range. The official Codex account summary is displayed separately and labeled account-wide; it is never duplicated onto daily buckets. Claude remains percentage-only — Moira never derives, renders, or sums token values for Claude.

## History

The History tab shows quota series with reset transitions and availability states over selectable ranges (**24h, 7d, 30d, 90d**) filtered by service (**All, Claude, Codex**). Exact daily token statistics and the official Codex summary appear when the selected range contains exact Codex data. History is stored locally in a versioned SQLite database (schema **v4**) at `$XDG_STATE_HOME/moira/history.sqlite3` (normally `~/.local/state/moira/history.sqlite3`) and written by a bounded background writer that never blocks the UI. **Delete all history…** (with confirmation) clears the stored rows.

## Weekly exhaustion

An AVAILABLE weekly reading at ≥100% means the service is exhausted until its weekly reset. STALE, missing, error, or sub-100 readings must not newly establish exhaustion. When exhausted:

- **Claude**: the weekly card shows a critical state and the five-hour row is visually disabled. The UI states that weekly exhaustion blocks usage and shows the weekly reset time and countdown, not the five-hour reset. Stored readings remain unchanged.
- **Codex**: the weekly card renders as critical with an unavailable-until-reset message, reset time, and countdown. Moira never adds a Codex five-hour quota.

## Internationalization

Moira detects the locale from the environment (LANG/LC_ALL/LC_MESSAGES) and displays French for French locales with English fallback for all other locales. All visible strings — UI labels, error messages, About dialog, native and NTFY notifications, dates, and countdowns — are localized. No manual language selector is needed.

## Refresh and display

Moira refreshes at startup, on focus regain (with a monotonic debounce and overlap guard), and at a configurable interval. Refresh choices are 1, 2, 5, 10, 15, or 30 minutes. New configurations default to 2 minutes. The v1-to-v2 migration preserves every valid refresh value including 10; the 2-minute default is used only when the value is absent or invalid. Saving a new interval immediately replaces the GLib provider timer without a restart or duplicates. The last and next refresh times are displayed.

Countdowns and next-refresh times are recomputed locally every 30 seconds without collectors. Claude data changes only after a Claude Code response; cache rereads are not fresh provider events.

**Compact mode** keeps provider, status, exhaustion and reset visible while dropping the per-reading progress bars. Quota cards show used/remaining percentages (100 − used) plus a reset countdown.

## Configuration and privacy

Configuration is versioned JSON (**config v3**) at `$XDG_CONFIG_HOME/moira/config.json` (normally `~/.config/moira/config.json`). Last readings and alert deduplication keys are at `$XDG_STATE_HOME/moira/state.json`. Both are written with mode `0600`. Enabling autostart creates `$XDG_CONFIG_HOME/autostart/io.github.moira.QuotaMonitor.desktop` at runtime. Versionless (v1) and v2 configuration files migrate automatically to v3 on load, preserving every user setting; invalid persisted versions fail closed to complete defaults without partial preservation.

Claude's separate `$XDG_STATE_HOME/moira/claude-rate-limits.json` cache contains only `five_hour` and `seven_day` objects. Each has `service`, `percentage`, `reset_epoch`, and `retrieved_at`; status-line input, prompts, transcript paths, workspaces, model names, account data, and secrets are never written by Moira. Chained third-party status-line commands still receive the original input and retain responsibility for their own privacy behavior. The History database holds only typed quota, token, availability, and summary records — never secrets, raw payloads, exceptions, or paths.

## Agent activity

An **Agent activity** panel below the quota cards shows, independently from quotas, whether Claude Code, Codex CLI or Hermes is working: a spinner while a session is active (with a count when more than one session runs and the latest sanitized model label), then the correct symbolic state — completed, failed or interrupted — for exactly five minutes after the last active turn ends, then nothing. Missing terminal events expire through a watchdog to **interrupted**, never to success.

Activity is recorded through Moira-owned, reversible integrations configured in the Settings view (Set up / Remove / Test per agent):

- **Claude Code**: Moira installs `UserPromptSubmit` (start), `Stop` (completed), `StopFailure` (failed) and `SessionEnd` (interrupted) hook entries alongside the existing status line. Existing hooks and settings are preserved exactly, backups are atomic, and removal deletes only Moira-owned entries — ambiguous ownership is refused instead of guessed.
- **Hermes**: a shell-hook adapter installs `pre_llm_call` (start), `post_llm_call` (completed) and `on_session_end` (interrupt/complete) entries in `$HERMES_HOME/config.yaml` (by default `~/.hermes`), merged without a YAML dependency by a strict subset editor that fails closed outside the supported shape; the installed version is probed first and unsupported versions fail closed with a translated status. Hermes hooks fire after their first-use consent prompt.
- **Codex CLI**: only the documented app-server turn events are used. A full `turn/started` → `turn/completed` sequence is valid only for an app-server session Moira owns; terminal notifications for other sessions are recorded as completions and never synthesize RUNNING. The capability probe reports full, completion-only or unsupported.

Hooks are network-free, bounded-input (≤256 KiB), fixed-output (empty stdout, exit 0) and nonblocking — a failure never touches the agent. The store at `$XDG_STATE_HOME/moira/activity.json` (mode 0600, atomic, process-safe locking) holds only the runtime, sanitized model labels, SHA-256 hashed session identities and UTC timestamps — never prompts, responses, transcripts, paths, raw errors, accounts or secrets. The panel watches the file live with `Gio.FileMonitor` (bursts coalesced, deletion/corruption tolerated) and bounded self-cancelling timers; there is no polling, `pgrep`, terminal scraping, daemon or tray.

## Notifications

The Notifications view configures **native desktop notifications** (via GNOME) and/or an HTTP(S) NTFY server with one topic segment, enabled state, thresholds, reset alerts, error alerts, refresh interval, and login autostart. **Send test notification** performs the explicit live delivery test on the selected channel. A non-empty NTFY token field updates GNOME Keyring; leaving it blank preserves the existing token.

Alert rules are typed **per provider** (config v3): each of Claude and Codex carries its own thresholds, reset alerts, and error alerts, and each provider has its own collection toggle. Disabled providers are never started, never alert, never write fresh history, and show a translated *Disabled* state while old History stays readable.

Exhaustion and recovery events are deduplicated once per service/window and are independent from threshold alerts. Only the generic 100% threshold alert is suppressed when an exhaustion event fires; lower-threshold crossings are governed normally. Threshold notifications require a crossing from below to at-or-above a threshold in the same quota window. Reset and parse/error notifications are deduplicated. NTFY and native channels are deduplicated independently per channel, and a key is persisted only after the channel reports success. Delivery failures are not marked sent and may retry after the next qualifying refresh.

## Diagnostics and copy

A sanitized diagnostics report shows provider state, channels, refresh and History-writer status — with no secrets, server URLs, topics, paths, or raw errors. **Copy** actions export quota status, diagnostics, and History summaries to the clipboard, all sanitized.

## Export

History can be exported as deterministic **CSV or JSON** to a chosen destination (atomic UTF-8 writes, stable column order, rows sorted by observed time). Only sanitized typed fields are exported — never secrets, raw payloads, exceptions, or paths. Failures return a fixed sanitized outcome.

## Manual update checks

The Settings view offers a manual **Check for updates** action that queries the configured repository's latest GitHub release (default `ThibSama/moira`). There is no startup check, no telemetry, no token, and no auto-download or install. Comparison is strict SemVer 2.0; an invalid current version fails the check closed, and every outcome is a fixed sanitized status string.

## Install, upgrade, and uninstall on Ubuntu

Build and install the Debian package:

```sh
./scripts/build-deb.sh
sudo apt install ./dist/moira_0.3.0_all.deb
```

The package depends on Python 3, PyGObject, GTK4, Libadwaita, and libsecret GI. Run `moira` or launch **Moira** from the application menu. Install the same `.deb` on another PC and authenticate each provider CLI separately there.

**Upgrading from 0.2.2**: installing `dist/moira_0.3.0_all.deb` over the previous release upgrades in place. User files are preserved: `~/.config/moira`, `~/.local/state/moira` (including the History database) and the keyring item are untouched, and older v1/v2 configuration files migrate to config v3 automatically on first load.

**Uninstalling** without deleting per-user settings:

```sh
sudo apt remove moira
```

Optional user data can be removed manually from `~/.config/moira` and `~/.local/state/moira`. The NTFY token is a GNOME Keyring item named "Moira NTFY token" and is not in those directories.

### Claude integration setup

In Notifications, select **Set up Claude integration**. Moira first validates `~/.claude/settings.json`. If a command status line already exists, Moira stores its complete status-line object and substitutes `/usr/bin/moira-claude-statusline`; the wrapper passes the original JSON input to the old command and preserves its output and exit status. Setup writes atomically and creates `settings.json.moira-backup`. **Remove Claude integration** restores only the saved status-line field, preserving unrelated settings changed since setup. Removal refuses to act if another program changed the status line in the meantime.

Complete one minimal Claude response after setup if no recent status-line event exists. Until both structured limits arrive, Moira shows waiting/unavailable (or retains an older successful reading as stale), never 0%.

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
.venv/bin/python -m build
./scripts/build-deb.sh
desktop-file-validate data/io.github.moira.QuotaMonitor.desktop
appstreamcli validate --no-net data/io.github.moira.QuotaMonitor.metainfo.xml
```

Tests use sanitized fixtures and mocks. They do not require provider accounts, network access, NTFY, or a running keyring. The release-consistency tests assert that every release surface (Python package, `pyproject.toml`, Debian control and build output, README commands, AppStream, and runtime User-Agents/clientInfo) agrees with the single authoritative `src/moira/__init__.py::__version__` and fail on stale supported-version literals.

## Troubleshooting

- **Unavailable / CLI not found:** install the official provider CLI and ensure `claude` or `codex` is on the desktop session's `PATH`.
- **No Claude values:** set up the integration, use Claude Code normally, and complete a response. Some account types may not expose both windows.
- **Claude integration conflict:** Moira chains only a valid command-type status line. Inspect the non-secret `statusLine` field and decide which integration should own it; Moira does not overwrite an unknown change.
- **No Codex weekly value:** confirm login and update Codex. Moira requires a rate-limit window declared as exactly seven days.
- **No exact token statistics:** exact daily statistics require official Codex account usage data in the selected range. Claude never produces token values.
- **Keyring error:** ensure GNOME Keyring/libsecret is installed and unlocked. Moira never falls back to a plaintext token.
- **NTFY test fails:** verify the server URL, topic, network, certificate, and token permissions. The token is never included in errors.
- **Desktop shortcut unavailable:** confirm `xdg-user-dir DESKTOP` points to a separate existing directory. Use the application menu if the shell hides desktop files.
- **Stale countdown reaches zero:** refresh again after connectivity/authentication is restored. Stale timestamps remain the last provider-reported values.
- **Weekly exhaustion:** when a weekly reading reaches 100%, Moira blocks usage display and shows the weekly reset countdown. Exhaustion is cleared only by a new AVAILABLE reading below 100% or a new reset window. An expired 100% reading from a past window does not establish exhaustion.

See [ARCHITECTURE.md](ARCHITECTURE.md) for module boundaries and [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for recovery and live checks.
