# Troubleshooting and live acceptance

## Claude status-line integration

Use **Set up Claude integration** in Moira. Setup stops without changing Claude settings when `statusLine` is not a valid command object. A successful setup keeps these recovery files at mode `0600`:

- `~/.claude/settings.json.moira-backup`: the full pre-setup settings snapshot;
- `$XDG_CONFIG_HOME/moira/claude-integration.json`: the original status-line object needed for surgical removal.

**Remove Claude integration** restores only that object and keeps unrelated current Claude settings. It refuses removal if the active command is no longer Moira's wrapper. In that situation, compare only the `statusLine` objects; do not blindly overwrite current settings with the full backup.

Moira updates its cache only when both `rate_limits.five_hour` and `rate_limits.seven_day` contain a numeric `used_percentage` in 0–100 and a valid `resets_at`. Missing limits do not overwrite a prior cache. Before the first qualifying Claude response, waiting/unavailable is expected. After 15 minutes without a new status-line event, cached readings are stale.

## Codex app-server

Codex must be logged in and available on the desktop session `PATH`. Moira rejects a response when initialization fails, the 12-second exchange times out, fields are malformed, or no exact 10,080-minute window exists. It never substitutes the common five-hour primary window.

## NTFY

Enter the existing HTTP(S) server and single-segment topic in Notifications. Put an access token, if required, in the password field and save once; the field clears after GNOME Keyring accepts it. Moira never writes the token to JSON. Select **Send test notification** and confirm both the success message and receipt in a subscribed client.

Older Claude/Codex notifier scripts and user timers are outside Moira and remain intact. If they target the same topic, they may duplicate Moira's threshold, reset, task-completion, or error messages. Disable one source deliberately if duplicates are undesirable.

## Desktop and package checks

After installing the `.deb`, launch `io.github.moira.QuotaMonitor.desktop` from the application menu. **Create desktop shortcut** copies that installed entry to the directory reported by `xdg-user-dir DESKTOP`, makes it executable, and asks `gio` to mark it trusted. Repeating creation or removal is safe. A session with no separate configured desktop directory, or a shell that intentionally hides desktop files, is unsupported; use the application menu instead.

Useful non-secret checks:

```sh
dpkg-query -W -f='${Status} ${Version}\n' moira
desktop-file-validate /usr/share/applications/io.github.moira.QuotaMonitor.desktop
xdg-user-dir DESKTOP
```
