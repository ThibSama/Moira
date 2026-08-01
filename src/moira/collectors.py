from __future__ import annotations

import errno
import json
import os
import pty
import re
import select
import shutil
import signal
import subprocess
import time
from collections.abc import Callable
from datetime import datetime

from .models import QuotaReading, QuotaStatus, Service, utc_now
from .parsers import ParseError, parse_claude_usage, parse_codex_rate_limits

Parser = Callable[[str, datetime], list[QuotaReading]]


def capture_slash_command(binary: str, command: str, timeout: float = 18.0) -> str:
    """Capture a supported CLI slash command in an isolated pseudo-terminal."""
    master, slave = pty.openpty()
    env = os.environ.copy()
    env.update({"TERM": "dumb", "NO_COLOR": "1"})
    process = subprocess.Popen(
        [binary, "--no-alt-screen"] if os.path.basename(binary) == "codex" else [binary],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=env,
        start_new_session=True,
        close_fds=True,
    )
    os.close(slave)
    chunks: list[bytes] = []
    started = time.monotonic()
    deadline = started + timeout
    send_at = started + 1.5
    sent = False
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.25)
            if ready:
                try:
                    part = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise
                if not part:
                    break
                chunks.append(part)
            if not sent and time.monotonic() >= send_at:
                os.write(master, (command + "\n").encode())
                sent = True
            joined = b"".join(chunks).lower()
            if sent and (b"resets" in joined or b"reset at" in joined) and b"%" in joined:
                time.sleep(0.4)
                try:
                    chunks.append(os.read(master, 65536))
                except OSError:
                    pass
                break
    finally:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
        os.close(master)
    return b"".join(chunks).decode("utf-8", errors="replace")


class CliCollector:
    def __init__(self, service: Service, binary: str, command: str, parser: Parser) -> None:
        self.service = service
        self.binary = binary
        self.command = command
        self.parser = parser

    def collect(self) -> list[QuotaReading]:
        now = utc_now()
        executable = shutil.which(self.binary)
        label = "Weekly" if self.service is Service.CODEX else "Five-hour"
        source = f"{self.binary}-cli:{self.command}"
        if not executable:
            return [
                QuotaReading(
                    self.service,
                    label,
                    None,
                    None,
                    now,
                    source,
                    QuotaStatus.UNAVAILABLE,
                    f"{self.binary} CLI not found",
                )
            ]
        try:
            output = capture_slash_command(executable, self.command)
            return self.parser(output, now)
        except ParseError as exc:
            return [
                QuotaReading(
                    self.service, label, None, None, now, source, QuotaStatus.PARSE_ERROR, str(exc)
                )
            ]
        except (OSError, subprocess.SubprocessError) as exc:
            detail = re.sub(r"/[^\s:]+", "<path>", str(exc))[:160]
            return [
                QuotaReading(
                    self.service, label, None, None, now, source, QuotaStatus.ERROR, detail
                )
            ]


class ClaudeCollector(CliCollector):
    def __init__(self) -> None:
        super().__init__(Service.CLAUDE, "claude", "/usage", parse_claude_usage)


class CodexCollector(CliCollector):
    def __init__(self) -> None:
        super().__init__(Service.CODEX, "codex", "account/rateLimits/read", lambda _text, _now: [])

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
            requests = [
                {
                    "id": 1,
                    "method": "initialize",
                    "params": {"clientInfo": {"name": "moira", "version": "0.1.0"}},
                },
                {"method": "initialized", "params": {}},
                {"id": 2, "method": "account/rateLimits/read", "params": None},
            ]
            for request in requests:
                process.stdin.write(json.dumps(request) + "\n")
            process.stdin.flush()
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                ready, _, _ = select.select([process.stdout], [], [], 0.25)
                if not ready:
                    continue
                line = process.stdout.readline()
                if not line:
                    break
                message = json.loads(line)
                if message.get("id") == 2:
                    if "error" in message:
                        raise OSError("Codex app-server rejected rate-limit request")
                    return parse_codex_rate_limits(message, now)
            raise OSError("Codex app-server rate-limit request timed out")
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
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            return [
                QuotaReading(
                    Service.CODEX,
                    "Weekly",
                    None,
                    None,
                    now,
                    source,
                    QuotaStatus.ERROR,
                    type(exc).__name__,
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
