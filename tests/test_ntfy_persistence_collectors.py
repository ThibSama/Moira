import json
from pathlib import Path
from unittest.mock import patch

from moira.claude_integration import update_cache
from moira.collectors import ClaudeCollector
from moira.models import QuotaStatus
from moira.ntfy import Notification, build_request, send
from moira.persistence import Settings, load_settings, save_settings


def test_ntfy_request_construction() -> None:
    token = "fixture-token"
    request = build_request(
        "https://notify.example/base",
        "my topic",
        Notification("Title", "Body", "warning", 4),
        token,
    )
    assert request.full_url == "https://notify.example/base/my%20topic"
    assert request.data == b"Body"
    assert request.get_header("Authorization") == f"Bearer {token}"
    assert request.get_header("Title") == "Title"
    assert request.method == "POST"


def test_mocked_ntfy_send() -> None:
    response = type(
        "Response",
        (),
        {"status": 200, "__enter__": lambda self: self, "__exit__": lambda self, *args: None},
    )()
    with patch("urllib.request.urlopen", return_value=response) as opened:
        send("https://notify.example", "topic", Notification("Test", "Hello"), None)
    assert opened.call_count == 1


def test_versioned_settings_and_no_token_on_disk(tmp_path: Path, monkeypatch: object) -> None:
    patcher = patch.dict("os.environ", {"XDG_CONFIG_HOME": str(tmp_path)})
    with patcher:
        save_settings(Settings(ntfy_topic="topic"))
        raw = (tmp_path / "moira/config.json").read_text()
        assert json.loads(raw)["version"] == 2
        assert "token" not in raw.lower()
        assert load_settings().ntfy_topic == "topic"


def test_collector_unavailable(monkeypatch: object) -> None:
    with patch.dict("os.environ", {"XDG_STATE_HOME": "/nonexistent/moira-test"}):
        reading = ClaudeCollector().collect()[0]
    assert reading.status is QuotaStatus.UNAVAILABLE
    assert "not found" in reading.detail


def test_collector_parse_error() -> None:
    with patch("moira.claude_integration._read_object", return_value={"bad": "cache"}):
        reading = ClaudeCollector().collect()[0]
    assert reading.status is QuotaStatus.PARSE_ERROR


def test_collector_available(tmp_path: Path) -> None:
    payload = {
        "rate_limits": {
            "five_hour": {"used_percentage": 10, "resets_at": 1785600000},
            "seven_day": {"used_percentage": 20, "resets_at": 1786204800},
        }
    }
    with patch.dict("os.environ", {"XDG_STATE_HOME": str(tmp_path)}):
        assert update_cache(payload)
        readings = ClaudeCollector().collect()
    assert len(readings) == 2
    assert all(item.status is QuotaStatus.AVAILABLE for item in readings)
