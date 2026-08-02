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
- `ui.py` owns GTK widgets and schedules collectors on worker threads. A refresh guard prevents overlap. Refresh occurs at startup, on focus regain (monotonic debounce), and at the configured interval. Countdowns recompute locally every 30 seconds without collectors. Saving a new interval immediately replaces the GLib timer without restart or duplicates. The History tab (`history_page.py`) uses a genuinely bounded `HistoryReader` with at most one running read and one pending newest request. Results are published through an injected dispatcher (GLib.idle_add) so no callback can update a destroyed page. Stale generations never publish. The chart (`history_chart.py`) uses DrawingArea/Cairo with a shared time axis across all series, dash-pattern differentiation for non-color-only identification, and theme-adaptive background/foreground. The History tab refreshes when it becomes visible and after a successful history write (via `HistoryCoordinator.set_write_success_callback`).

No database, rendered dashboard/terminal scraping, private endpoint, background service, tray, or legacy helper dependency is used. An existing user status-line command remains an independent delegate when Moira chaining is enabled.

## Local history

`history.py` defines typed domain objects separating quota observations (`QuotaObservation`) from optional exact-token observations (`TokenObservation`). Each record carries service, UTC time, source, and a `HistoryStatus`: `AVAILABLE_EXACT`, `UNSUPPORTED`, `TEMPORARILY_UNAVAILABLE`, or `INVALID`. Token fields support input, cached input, output, reasoning output, and total when exposed.

`history_db.py` stores observations in a stdlib SQLite database at `$XDG_STATE_HOME/moira/history.sqlite3` (mode `0600`). Config and current-state JSON are unchanged. A transactional schema version (`schema_meta` table, currently v2) guards future migrations. Schema v2 removes the v1 `UNIQUE(service, quota_label, bucket)` constraint that destroyed earlier change points; it preserves every distinct percentage or reset transition, including multiple changes inside one 15-minute bucket. Unchanged values retain at most one periodic sample per service/quota/bucket. Exact replay (same `observed_at`) is a no-op. A transactional forward migration from v1 to v2 preserves all existing rows. Rows older than 90 days are purged after successful writes using an injected clock. Ordered read APIs cover 24h, 7d, 30d, and 90d with optional service/metric filters, plus a delete-all operation.

History writes are integrated into refresh completion (`_record_history`) via a `HistoryCoordinator` that models capacity as exactly one in-flight batch plus one pending batch. If neither exists, accept the batch and return True. If a batch is in-flight but pending is empty, accept the new pending batch, return True, and do not report saturation. If pending is already occupied, replace it with the newest generation, return False, and latch `backlog saturated`. All transitions are atomic under a single `Condition`. The saturation latch is separate from the latest write diagnostic: while saturation remains unresolved, public status stays `backlog saturated` even when the retained write fails. The sanitized diagnostic is stored internally via `last_write_diagnostic` for the future Diagnostic view. Generation tracking ensures an old in-flight result cannot resolve a newer saturation; only successful completion of the retained or a later generation clears the latch. The coordinator exposes an idempotent `shutdown` connected to the window `close-request` signal. The constructor validates `0 < db_timeout < shutdown_timeout`; defaults are `db_timeout=1.0`, `shutdown_timeout=3.0`. `shutdown(timeout=...)` validates the effective timeout against `db_timeout` before mutating lifecycle state. Lifecycle states: NEW, RUNNING, SHUTTING_DOWN, TERMINATED. Shutdown-before-start transitions directly to TERMINATED and rejects future work. Start-after-terminal-shutdown is a documented no-op. The worker wakes on `Condition.notify_all()` from enqueue and shutdown, not periodic polling. `_thread` is never set to None while the captured thread is still alive. `write_history_safely` returns only a bounded `HistoryWriteResult` with a sanitized diagnostic string (`ok`, `database unavailable`, `schema mismatch`, `invalid observation`, `backlog saturated`). `SchemaVersionError` distinguishes schema-version failures from domain `ValueError` validation failures so that `invalid observation` is returned only for the latter. History failure does not disrupt quota state, display, or alerts. Missing token telemetry does not affect quota collection, display, alerts, or quota-history recording. No raw payloads, prompts, responses, transcript text, private paths, account identifiers, or secrets are stored.

Domain validation is fail-closed: timestamps must be timezone-aware and are normalized to UTC; labels and sources must be non-empty; percentages must be 0–100; token counts must be non-negative integers. `AVAILABLE_EXACT` requires `total_tokens` and at least one breakdown field; non-available statuses must not carry any token counts. `QuotaObservation` always exposes `AVAILABLE_EXACT` status.

## Provider token capability matrix

| Surface | Method/Source | Exact tokens? | Fields |
|---|---|---|---|
| Codex app-server | `account/rateLimits/read` | No | `usedPercent` (0–100), `resetsAt` (epoch), `windowDurationMins` |
| Codex app-server | `account/usage/read` | Partial (aggregate) | `summary.lifetimeTokens`, `summary.peakDailyTokens`, `dailyUsageBuckets[].tokens` (no per-window input/output/cached/reasoning breakdown) |
| Codex app-server | `thread/tokenUsage/updated` (notification) | Yes (turn-level) | `TokenUsageBreakdown`: `inputTokens`, `cachedInputTokens`, `outputTokens`, `reasoningOutputTokens`, `totalTokens`, `cacheWriteInputTokens` |
| Claude status-line | `rate_limits` structure | No | `used_percentage` (0–100), `resets_at` (epoch/ISO) for `five_hour` and `seven_day` |
| Claude CLI `--print --output-format json` | `usage` object | Yes (turn-level) | `input_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, `output_tokens`, plus `server_tool_use`, `service_tier`, `cache_creation` sub-objects |

Capability assessment (read-only probes, no message content read):
- No structured surface currently provides exact per-window token counts alongside a matching quota window at refresh time. The Codex `account/usage/read` returns aggregate/lifetime tokens but not a per-window input/output breakdown. The Codex `thread/tokenUsage/updated` notification carries per-turn exact counts but is a per-thread event, not an account-wide refresh.
- Claude's `--print --output-format json` exposes turn-level exact token counts but no per-window account quota token breakdown.
- Therefore, `TokenObservation` records are stored as `UNSUPPORTED` until a future package wires a structured token surface. Quota history recording proceeds independently.

Privacy decisions:
- Only fresh AVAILABLE readings with percentage and reset_at are stored.
- STALE, error, unavailable, and parse-error readings are never stored.
- No raw payloads, prompts, responses, transcript text, private paths, account identifiers, or secrets are retained.
- Token counts are never estimated or derived from percentages.
