import json
import signal
from unittest.mock import patch

from moira.collectors import CodexCollector
from moira.models import CodexSummary, CollectorResult, HistoryStatus, QuotaStatus, Service


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
            "summary": {
                "lifetimeTokens": 50000,
                "peakDailyTokens": 3000,
                "currentStreakDays": 7,
                "longestStreakDays": 14,
                "longestRunningTurnSec": 1500,
            },
            "dailyUsageBuckets": [
                {"startDate": "2026-08-04", "tokens": 1700},
            ],
        },
    }


def test_handshake_requests_both_surfaces_independently() -> None:
    """Both rate limits and usage are requested independently with separate deadlines."""
    events: list[str] = []
    process = FakeProcess(events)

    requests_seen: list[int] = []

    def response(_process: object, request_id: int, _deadline: float) -> dict[str, object]:
        events.append(f"read:{request_id}")
        requests_seen.append(request_id)
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
    assert result.token_readings[0].tokens == 1700
    assert result.token_readings[0].status is HistoryStatus.AVAILABLE_EXACT
    # codex_summary is a typed immutable CodexSummary with official fields
    assert result.codex_summary is not None
    assert isinstance(result.codex_summary, CodexSummary)
    assert result.codex_summary.lifetime_tokens == 50000
    assert result.codex_summary.peak_daily_tokens == 3000
    assert result.codex_summary.current_streak_days == 7
    assert result.codex_summary.longest_streak_days == 14
    assert result.codex_summary.longest_running_turn_sec == 1500
    assert result.codex_summary.service == Service.CODEX
    assert result.codex_summary.source == CodexCollector.USAGE_SOURCE
    # Token availability is always emitted — one per provider attempt
    assert result.token_availability_records[0] is not None
    assert result.token_availability_records[0].status is HistoryStatus.AVAILABLE_EXACT


def test_usage_failure_preserves_quota() -> None:
    """When usage request fails, quota survives. Availability is TEMPORARILY_UNAVAILABLE."""
    events: list[str] = []
    process = FakeProcess(events)

    def response(_process: object, request_id: int, _deadline: float) -> dict[str, object]:
        if request_id == 1:
            return {"id": 1, "result": {}}
        if request_id == 2:
            return weekly_response()
        # request_id == 3: simulate transport failure
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
    # Usage failure produces token_availability, not TokenReadings
    assert len(result.token_readings) == 0
    assert result.token_availability_records[0] is not None
    assert result.token_availability_records[0].status is HistoryStatus.TEMPORARILY_UNAVAILABLE
    assert result.token_availability_records[0].detail == "Codex usage request failed"


def test_usage_rpc_error_preserves_quota() -> None:
    """When usage returns an RPC error, quota survives. Availability is TEMPORARILY_UNAVAILABLE."""
    events: list[str] = []
    process = FakeProcess(events)

    def response(_process: object, request_id: int, _deadline: float) -> dict[str, object]:
        if request_id == 1:
            return {"id": 1, "result": {}}
        if request_id == 2:
            return weekly_response()
        # RPC-level error
        return {"id": 3, "error": {"code": -32000, "message": "not authorized"}}

    with (
        patch("moira.collectors.shutil.which", return_value="/usr/bin/codex"),
        patch("moira.collectors.subprocess.Popen", return_value=process),
        patch("moira.collectors._read_response", side_effect=response),
        patch("moira.collectors.os.killpg"),
    ):
        result = CodexCollector().collect()

    assert result.quota_readings[0].status is QuotaStatus.AVAILABLE
    assert len(result.token_readings) == 0
    assert result.token_availability_records[0] is not None
    assert result.token_availability_records[0].status is HistoryStatus.TEMPORARILY_UNAVAILABLE
    assert result.token_availability_records[0].detail == "Codex usage request rejected"


def test_usage_parse_error_produces_invalid() -> None:
    """Malformed usage response body produces an INVALID availability observation."""
    events: list[str] = []
    process = FakeProcess(events)

    def response(_process: object, request_id: int, _deadline: float) -> dict[str, object]:
        if request_id == 1:
            return {"id": 1, "result": {}}
        if request_id == 2:
            return weekly_response()
        # Malformed success body
        return {"id": 3, "result": {"dailyUsageBuckets": "not-a-list"}}

    with (
        patch("moira.collectors.shutil.which", return_value="/usr/bin/codex"),
        patch("moira.collectors.subprocess.Popen", return_value=process),
        patch("moira.collectors._read_response", side_effect=response),
        patch("moira.collectors.os.killpg"),
    ):
        result = CodexCollector().collect()

    assert result.quota_readings[0].status is QuotaStatus.AVAILABLE
    assert len(result.token_readings) == 0
    assert result.token_availability_records[0] is not None
    assert result.token_availability_records[0].status is HistoryStatus.INVALID
    assert result.token_availability_records[0].detail == "Codex usage response malformed"


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
    # Availability shows temporarily_unavailable
    assert len(result.token_readings) == 0
    assert result.token_availability_records[0] is not None
    assert result.token_availability_records[0].status is HistoryStatus.TEMPORARILY_UNAVAILABLE
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
    assert result.token_readings[0].tokens == 1700
    assert result.token_readings[0].status == "available_exact"
    # Availability is AVAILABLE_EXACT
    assert result.token_availability_records[0] is not None
    assert result.token_availability_records[0].status is HistoryStatus.AVAILABLE_EXACT


def test_independent_deadlines() -> None:
    """Rate limits and usage get independent deadlines — neither can consume the other."""
    events: list[str] = []
    process = FakeProcess(events)

    rate_deadlines: list[float] = []
    usage_deadlines: list[float] = []

    def response(_process: object, request_id: int, deadline: float) -> dict[str, object]:
        if request_id == 1:
            return {"id": 1, "result": {}}
        if request_id == 2:
            rate_deadlines.append(deadline)
            return weekly_response()
        usage_deadlines.append(deadline)
        return usage_response()

    with (
        patch("moira.collectors.shutil.which", return_value="/usr/bin/codex"),
        patch("moira.collectors.subprocess.Popen", return_value=process),
        patch("moira.collectors._read_response", side_effect=response),
        patch("moira.collectors.os.killpg"),
        patch("moira.collectors.time.monotonic", side_effect=[0.0, 0.0, 0.1, 0.1, 0.2, 0.2]),
    ):
        result = CodexCollector().collect()

    assert len(rate_deadlines) == 1
    assert len(usage_deadlines) == 1
    # Each surface has its own independent deadline
    assert rate_deadlines[0] != usage_deadlines[0]
    assert result.quota_readings[0].status is QuotaStatus.AVAILABLE
    assert result.token_readings[0].status == "available_exact"
    assert result.token_availability_records[0] is not None
    assert result.token_availability_records[0].status is HistoryStatus.AVAILABLE_EXACT


def test_codex_not_found_produces_unsupported() -> None:
    """When codex CLI is not found, quota is UNAVAILABLE and availability is UNSUPPORTED."""
    with patch("moira.collectors.shutil.which", return_value=None):
        result = CodexCollector().collect()

    assert len(result.quota_readings) == 1
    assert result.quota_readings[0].status is QuotaStatus.UNAVAILABLE
    assert len(result.token_readings) == 0
    assert result.token_availability_records[0] is not None
    assert result.token_availability_records[0].status is HistoryStatus.UNSUPPORTED
