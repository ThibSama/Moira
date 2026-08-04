from __future__ import annotations

import json
import os
import select
import shutil
import signal
import subprocess
import time

from .models import CollectorResult, QuotaReading, QuotaStatus, Service, TokenReading, utc_now
from .parsers import ParseError, parse_codex_rate_limits, parse_codex_usage


class ClaudeCollector:
    def collect(self) -> CollectorResult:
        from .claude_integration import load_cached_readings

        return CollectorResult(
            quota_readings=tuple(load_cached_readings()),
            token_readings=(),
        )


def _send_message(process: subprocess.Popen[str], message: dict[str, object]) -> None:
    if process.stdin is None:
        raise OSError("Codex app-server input unavailable")
    process.stdin.write(json.dumps(message) + "\n")
    process.stdin.flush()


def _read_response(
    process: subprocess.Popen[str], request_id: int, deadline: float
) -> dict[str, object]:
    if process.stdout is None:
        raise OSError("Codex app-server output unavailable")
    while time.monotonic() < deadline:
        remaining = max(0.0, deadline - time.monotonic())
        ready, _, _ = select.select([process.stdout], [], [], min(0.25, remaining))
        if not ready:
            continue
        line = process.stdout.readline()
        if not line:
            raise OSError("Codex app-server stopped unexpectedly")
        message = json.loads(line)
        if not isinstance(message, dict):
            raise OSError("Codex app-server response malformed")
        if message.get("id") == request_id:
            return message
    raise TimeoutError("Codex app-server request timed out")


class CodexCollector:
    """Collects both quota and token data from the Codex app-server.

    Reuses one initialized app-server process per refresh. Requests rate
    limits and usage independently — failure of one surface does not
    discard valid data from the other.
    """

    def __init__(self) -> None:
        self.binary = "codex"

    def collect(self) -> CollectorResult:
        now = utc_now()
        executable = shutil.which(self.binary)
        if not executable:
            return CollectorResult(
                quota_readings=(
                    QuotaReading(
                        Service.CODEX,
                        "Weekly",
                        None,
                        None,
                        now,
                        "codex-app-server",
                        QuotaStatus.UNAVAILABLE,
                        "codex CLI not found",
                    ),
                ),
                token_readings=(),
            )

        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(
                [executable, "app-server", "--stdio"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                start_new_session=True,
            )
            if process.stdin is None or process.stdout is None:
                raise OSError("Codex app-server pipes unavailable")

            deadline = time.monotonic() + 12

            # Initialize the app-server
            _send_message(
                process,
                {
                    "id": 1,
                    "method": "initialize",
                    "params": {"clientInfo": {"name": "moira", "version": "0.2.2"}},
                },
            )
            initialized = _read_response(process, 1, deadline)
            if "error" in initialized or not isinstance(initialized.get("result"), dict):
                raise OSError("Codex app-server initialization rejected")
            _send_message(process, {"method": "initialized", "params": {}})

            # Request rate limits and usage independently
            quota_readings: list[QuotaReading] = []
            token_readings: list[TokenReading] = []

            # Rate limits request
            _send_message(process, {"id": 2, "method": "account/rateLimits/read", "params": None})
            try:
                rate_response = _read_response(process, 2, deadline)
                if "error" not in rate_response:
                    quota_readings.extend(parse_codex_rate_limits(rate_response, now))
                else:
                    quota_readings.append(
                        QuotaReading(
                            Service.CODEX,
                            "Weekly",
                            None,
                            None,
                            now,
                            "codex-app-server:account/rateLimits/read",
                            QuotaStatus.ERROR,
                            "Codex app-server rate-limit request rejected",
                        )
                    )
            except ParseError:
                quota_readings.append(
                    QuotaReading(
                        Service.CODEX,
                        "Weekly",
                        None,
                        None,
                        now,
                        "codex-app-server:account/rateLimits/read",
                        QuotaStatus.PARSE_ERROR,
                        "Codex rate-limit response malformed",
                    )
                )
            except (OSError, TimeoutError, json.JSONDecodeError):
                quota_readings.append(
                    QuotaReading(
                        Service.CODEX,
                        "Weekly",
                        None,
                        None,
                        now,
                        "codex-app-server:account/rateLimits/read",
                        QuotaStatus.ERROR,
                        "Codex rate-limit request failed",
                    )
                )

            # Usage request (token data)
            _send_message(process, {"id": 3, "method": "account/usage/read", "params": None})
            try:
                usage_response = _read_response(process, 3, deadline)
                if "error" not in usage_response:
                    token_readings.extend(parse_codex_usage(usage_response, now))
                # On error, silently produce no token readings (not a quota failure)
            except ParseError:
                # Malformed usage data — no token readings, but quotas survive
                pass
            except (OSError, TimeoutError, json.JSONDecodeError):
                # Usage request failed — no token readings, quotas survive
                pass

            return CollectorResult(
                quota_readings=tuple(quota_readings),
                token_readings=tuple(token_readings),
            )

        except (OSError, TimeoutError, subprocess.SubprocessError, json.JSONDecodeError):
            return CollectorResult(
                quota_readings=(
                    QuotaReading(
                        Service.CODEX,
                        "Weekly",
                        None,
                        None,
                        now,
                        "codex-app-server",
                        QuotaStatus.ERROR,
                        "Codex app-server request failed",
                    ),
                ),
                token_readings=(),
            )
        finally:
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=2)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
