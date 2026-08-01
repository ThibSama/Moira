import json
from pathlib import Path
from unittest.mock import patch

from moira.collectors import ClaudeCollector
from moira.models import QuotaStatus
from moira.ntfy import Notification, build_request, send
from moira.persistence import Settings, load_settings, save_settings


def test_ntfy_request_construction() -> None:
    request = build_request(
        "https://notify.example/base",
        "my topic",
        Notification("Title", "Body", "warning", 4),
        "secret-placeholder",
    )
    assert request.full_url == "https://notify.example/base/my%20topic"
    assert request.data == b"Body"
    assert request.get_header("Authorization") == "Bearer secret-placeholder"
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
        assert json.loads(raw)["version"] == 1
        assert "token" not in raw.lower()
        assert load_settings().ntfy_topic == "topic"


def test_collector_unavailable(monkeypatch: object) -> None:
    with patch("moira.collectors.shutil.which", return_value=None):
        reading = ClaudeCollector().collect()[0]
    assert reading.status is QuotaStatus.UNAVAILABLE


def test_collector_parse_error() -> None:
    with (
        patch("moira.collectors.shutil.which", return_value="/bin/claude"),
        patch("moira.collectors.capture_slash_command", return_value="changed output"),
    ):
        reading = ClaudeCollector().collect()[0]
    assert reading.status is QuotaStatus.PARSE_ERROR


def test_collector_available() -> None:
    output = (
        "Five hour 10% resets at 2026-08-01T18:30:00+02:00\n"
        "Weekly 20% resets at 2026-08-08T18:30:00+02:00"
    )
    with (
        patch("moira.collectors.shutil.which", return_value="/bin/claude"),
        patch("moira.collectors.capture_slash_command", return_value=output),
    ):
        readings = ClaudeCollector().collect()
    assert len(readings) == 2
    assert all(item.status is QuotaStatus.AVAILABLE for item in readings)
