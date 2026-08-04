import json
import signal
from unittest.mock import patch

from moira.collectors import CodexCollector
from moira.models import CollectorResult, QuotaStatus


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


def usage_response() -> dict[str, object]:
    return {
        "id": 3,
        "result": {
            "usage": [
                {
                    "day": "2026-08-04",
                    "inputTokens": 1000,
                    "cachedInputTokens": 200,
                    "outputTokens": 500,
                    "reasoningOutputTokens": 0,
                    "totalTokens": 1700,
                }
            ]
        },
    }


def test_handshake_requests_both_surfaces_independently() -> None:
    events: list[str] = []
    process = FakeProcess(events)

    def response(_process: object, request_id: int, _deadline: float) -> dict[str, object]:
        events.append(f"read:{request_id}")
        if request_id == 1:
            return {"id": 1, "result": {}}
        if request_id == 2:
            return weekly_response()
        return usage_response()

    with (
        patch("moira.collectors.shutil.which", return_value="/usr/bin/codex"),
        patch("moira.collectors.subprocess.Popen", return_value=process),
        patch("moira.collectors._read_response", side_effect=response),
        patch("moira.collectors.os.killpg"),
    ):
        result = CodexCollector().collect()

    assert events == [
        "write:initialize",
        "read:1",
        "write:initialized",
        "write:account/rateLimits/read",
        "read:2",
        "write:account/usage/read",
        "read:3",
    ]
    assert isinstance(result, CollectorResult)
    assert result.quota_readings[0].status is QuotaStatus.AVAILABLE
    assert result.quota_readings[0].quota_label == "Weekly"
    assert len(result.token_readings) == 1
    assert result.token_readings[0].total_tokens == 1700
    assert result.token_readings[0].input_tokens == 1000


def test_usage_failure_preserves_quota() -> None:
    """When usage request fails, quota readings still survive."""
    events: list[str] = []
    process = FakeProcess(events)

    call_count = 0

    def response(_process: object, request_id: int, _deadline: float) -> dict[str, object]:
        nonlocal call_count
        call_count += 1
        if request_id == 1:
            return {"id": 1, "result": {}}
        if request_id == 2:
            return weekly_response()
        # request_id == 3: simulate failure
        raise TimeoutError("usage timeout")

    with (
        patch("moira.collectors.shutil.which", return_value="/usr/bin/codex"),
        patch("moira.collectors.subprocess.Popen", return_value=process),
        patch("moira.collectors._read_response", side_effect=response),
        patch("moira.collectors.os.killpg"),
    ):
        result = CodexCollector().collect()

    assert result.quota_readings[0].status is QuotaStatus.AVAILABLE
    assert result.quota_readings[0].quota_label == "Weekly"
    assert len(result.token_readings) == 0


def test_timeout_terminates_process_group_and_sanitizes_error() -> None:
    events: list[str] = []
    process = FakeProcess(events)
    with (
        patch("moira.collectors.shutil.which", return_value="/secret/path/codex"),
        patch("moira.collectors.subprocess.Popen", return_value=process),
        patch("moira.collectors._read_response", side_effect=TimeoutError),
        patch("moira.collectors.os.killpg") as kill,
    ):
        result = CodexCollector().collect()
    assert result.quota_readings[0].status is QuotaStatus.ERROR
    assert result.quota_readings[0].detail == "Codex app-server request failed"
    assert len(result.token_readings) == 0
    kill.assert_called_once_with(4242, signal.SIGTERM)


def test_rate_limit_parse_error_preserves_tokens() -> None:
    """When rate-limit parsing fails, token readings from usage still succeed."""
    events: list[str] = []
    process = FakeProcess(events)

    def response(_process: object, request_id: int, _deadline: float) -> dict[str, object]:
        if request_id == 1:
            return {"id": 1, "result": {}}
        if request_id == 2:
            return {"id": 2, "result": {"rateLimits": []}}  # no valid windows
        return usage_response()

    with (
        patch("moira.collectors.shutil.which", return_value="/usr/bin/codex"),
        patch("moira.collectors.subprocess.Popen", return_value=process),
        patch("moira.collectors._read_response", side_effect=response),
        patch("moira.collectors.os.killpg"),
    ):
        result = CodexCollector().collect()

    # Quota parse error produces a PARSE_ERROR reading
    assert len(result.quota_readings) == 1
    assert result.quota_readings[0].status is QuotaStatus.PARSE_ERROR
    # Token readings survive
    assert len(result.token_readings) == 1
    assert result.token_readings[0].total_tokens == 1700
