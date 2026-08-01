import json
import signal
from unittest.mock import patch

from moira.collectors import CodexCollector
from moira.models import QuotaStatus


class FakePipe:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def write(self, value: str) -> int:
        self.events.append("write:" + json.loads(value)["method"])
        return len(value)

    def flush(self) -> None:
        pass


class FakeProcess:
    def __init__(self, events: list[str]) -> None:
        self.stdin = FakePipe(events)
        self.stdout = object()
        self.pid = 4242
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = 0
        return 0


def weekly_response() -> dict[str, object]:
    return {
        "id": 2,
        "result": {
            "rateLimits": {
                "primary": {
                    "usedPercent": 10,
                    "windowDurationMins": 300,
                    "resetsAt": 1785600000,
                },
                "secondary": {
                    "usedPercent": 40,
                    "windowDurationMins": 10080,
                    "resetsAt": 1786204800,
                },
            }
        },
    }


def test_handshake_is_ordered_before_rate_limit_request() -> None:
    events: list[str] = []
    process = FakeProcess(events)

    def response(_process: object, request_id: int, _deadline: float) -> dict[str, object]:
        events.append(f"read:{request_id}")
        return {"id": 1, "result": {}} if request_id == 1 else weekly_response()

    with (
        patch("moira.collectors.shutil.which", return_value="/usr/bin/codex"),
        patch("moira.collectors.subprocess.Popen", return_value=process),
        patch("moira.collectors._read_response", side_effect=response),
        patch("moira.collectors.os.killpg"),
    ):
        readings = CodexCollector().collect()
    assert events == [
        "write:initialize",
        "read:1",
        "write:initialized",
        "write:account/rateLimits/read",
        "read:2",
    ]
    assert readings[0].status is QuotaStatus.AVAILABLE
    assert readings[0].quota_label == "Weekly"


def test_timeout_terminates_process_group_and_sanitizes_error() -> None:
    events: list[str] = []
    process = FakeProcess(events)
    with (
        patch("moira.collectors.shutil.which", return_value="/secret/path/codex"),
        patch("moira.collectors.subprocess.Popen", return_value=process),
        patch("moira.collectors._read_response", side_effect=TimeoutError),
        patch("moira.collectors.os.killpg") as kill,
    ):
        reading = CodexCollector().collect()[0]
    assert reading.status is QuotaStatus.ERROR
    assert reading.detail == "Codex app-server request failed"
    kill.assert_called_once_with(4242, signal.SIGTERM)
