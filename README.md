# Moira

Moira is a compact Ubuntu GTK4/Libadwaita utility for Claude and Codex quotas. It shows Claude five-hour and weekly usage, Codex weekly usage, local reset times, countdowns, refresh state, and optional deduplicated NTFY alerts.

## Data sources and behavior

- Claude: the installed `claude` CLI's `/usage` view is captured in an isolated pseudo-terminal and parsed strictly. Claude Code must already be logged in.
- Codex: the installed `codex app-server` read-only `account/rateLimits/read` method returns structured windows. Moira selects only the declared weekly window. It intentionally does not model or display a Codex five-hour quota.
- A missing CLI produces **Unavailable**. An unexpected provider format produces **Parse error**. When a refresh fails after a success, Moira retains the last successful values as **Stale**.

Provider CLI interfaces can change. Parsers intentionally fail closed instead of treating changed or missing data as 0%.

## Install on Ubuntu

Build and install the Debian package:

```sh
./scripts/build-deb.sh
sudo apt install ./dist/moira_0.1.0_all.deb
```

The package depends on Python 3, PyGObject, GTK4, Libadwaita, and libsecret GI. Run `moira` or launch **Moira** from the application menu. Install the same `.deb` on the second PC; authenticate each provider CLI separately on that machine.

Uninstall without deleting per-user settings:

```sh
sudo apt remove moira
```

Optional user data can be removed manually from `~/.config/moira` and `~/.local/state/moira`. The NTFY token is a GNOME Keyring item named “Moira NTFY token” and is not in those directories.

## Notifications and configuration

The Notifications view configures an HTTP(S) NTFY server, one topic segment, enabled state, comma-separated thresholds, reset alerts, error alerts, and login autostart. “Send test notification” is the only explicit live delivery test. A non-empty token field updates GNOME Keyring; leaving it blank preserves the existing token.

Configuration is versioned JSON at `$XDG_CONFIG_HOME/moira/config.json` (normally `~/.config/moira/config.json`). Last readings and alert deduplication keys are at `$XDG_STATE_HOME/moira/state.json`. Both are written with mode `0600`. Enabling autostart creates `$XDG_CONFIG_HOME/autostart/io.github.moira.QuotaMonitor.desktop` at runtime.

Threshold notifications require a crossing from below to at-or-above a threshold in the same quota window. Reset and parse/error notifications are deduplicated. Delivery failures are not marked sent and may retry after the next qualifying refresh.

## Development

```sh
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e . pytest ruff mypy build
.venv/bin/pytest
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
- **Parse error:** update the provider CLI and Moira. The CLI output/protocol may have changed; Moira keeps old successful values stale rather than inventing usage.
- **No Claude values:** open Claude Code normally, sign in, and verify `/usage` works for the current account. Some account types may not expose these windows.
- **No Codex weekly value:** run `codex doctor`, confirm login, and update Codex. Moira requires a rate-limit window with a declared duration of at least seven days.
- **Keyring error:** ensure GNOME Keyring/libsecret is installed and unlocked. Moira never falls back to a plaintext token.
- **NTFY test fails:** verify the server URL, topic, network, server certificate, and token permissions. The UI displays the exact local exception but never the token.
- **Stale countdown reaches zero:** refresh again after connectivity/authentication is restored. Stale timestamps remain the last provider-reported values.

See [ARCHITECTURE.md](ARCHITECTURE.md) for module boundaries and source rationale.

