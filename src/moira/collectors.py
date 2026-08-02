from __future__ import annotations

import json
import os
import select
import shutil
import signal
import subprocess
import time

from .models import QuotaReading, QuotaStatus, Service, utc_now
from .parsers import ParseError, parse_codex_rate_limits


class ClaudeCollector:
    def collect(self) -> list[QuotaReading]:
        from .claude_integration import load_cached_readings

        return load_cached_readings()


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
    def __init__(self) -> None:
        self.binary = "codex"

    def collect(self) -> list[QuotaReading]:
        now = utc_now()
        executable = shutil.which(self.binary)
        source = "codex-app-server:account/rateLimits/read"
        if not executable:
            return [
                QuotaReading(
                    Service.CODEX,
                    "Weekly",
                    None,
                    None,
                    now,
                    source,
                    QuotaStatus.UNAVAILABLE,
                    "codex CLI not found",
                )
            ]
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
            _send_message(
                process,
                {
                    "id": 1,
                    "method": "initialize",
                    "params": {"clientInfo": {"name": "moira", "version": "0.2.0"}},
                },
            )
            initialized = _read_response(process, 1, deadline)
            if "error" in initialized or not isinstance(initialized.get("result"), dict):
                raise OSError("Codex app-server initialization rejected")
            _send_message(process, {"method": "initialized", "params": {}})
            _send_message(process, {"id": 2, "method": "account/rateLimits/read", "params": None})
            response = _read_response(process, 2, deadline)
            if "error" in response:
                raise OSError("Codex app-server rate-limit request rejected")
            return parse_codex_rate_limits(response, now)
        except ParseError as exc:
            return [
                QuotaReading(
                    Service.CODEX,
                    "Weekly",
                    None,
                    None,
                    now,
                    source,
                    QuotaStatus.PARSE_ERROR,
                    str(exc),
                )
            ]
        except (OSError, TimeoutError, subprocess.SubprocessError, json.JSONDecodeError):
            return [
                QuotaReading(
                    Service.CODEX,
                    "Weekly",
                    None,
                    None,
                    now,
                    source,
                    QuotaStatus.ERROR,
                    "Codex app-server request failed",
                )
            ]
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
