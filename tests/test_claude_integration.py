import io
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from moira.claude_integration import (
    load_cached_readings,
    minimal_cache,
    remove,
    setup,
    statusline_main,
    update_cache,
)
from moira.models import QuotaStatus


def payload() -> dict[str, object]:
    return {
        "session_id": "must-not-be-cached",
        "workspace": {"current_dir": "/private/project"},
        "transcript_path": "/private/transcript",
        "rate_limits": {
            "five_hour": {"used_percentage": 12.5, "resets_at": 1785600000},
            "seven_day": {"used_percentage": 34, "resets_at": "2026-08-08T12:00:00Z"},
        },
    }


def test_minimal_cache_contains_only_allowed_fields() -> None:
    value = minimal_cache(payload(), 1785585600)
    assert set(value) == {"five_hour", "seven_day"}
    assert all(
        set(window) == {"percentage", "reset_epoch", "retrieved_at", "service"}
        for window in value.values()
    )
    assert "private" not in json.dumps(value)


def test_missing_limits_do_not_replace_cache(tmp_path: Path) -> None:
    with patch.dict("os.environ", {"XDG_STATE_HOME": str(tmp_path)}):
        assert update_cache(payload(), 1785585600)
        path = tmp_path / "moira/claude-rate-limits.json"
        before = path.read_bytes()
        assert not update_cache({"rate_limits": {"five_hour": {}}})
        assert path.read_bytes() == before


def test_missing_and_stale_cache_never_report_zero(tmp_path: Path) -> None:
    with patch.dict("os.environ", {"XDG_STATE_HOME": str(tmp_path)}):
        missing = load_cached_readings(datetime(2026, 8, 1, tzinfo=UTC))
        assert missing[0].status is QuotaStatus.UNAVAILABLE
        assert missing[0].percentage is None
        assert update_cache(payload(), 1_785_585_600)
        stale = load_cached_readings(datetime.fromtimestamp(1_785_587_000, UTC))
        assert all(item.status is QuotaStatus.STALE for item in stale)


def test_setup_chains_idempotently_and_remove_restores(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    metadata = tmp_path / "integration.json"
    original = {
        "theme": "dark",
        "statusLine": {
            "type": "command",
            "command": "/existing/status-line",
            "padding": 2,
            "refreshInterval": 60,
        },
    }
    settings_path.write_text(json.dumps(original))
    assert setup(settings_path, metadata, "/installed/moira-statusline")
    installed = json.loads(settings_path.read_text())
    assert installed["statusLine"]["command"] == "/installed/moira-statusline"
    assert installed["statusLine"]["padding"] == 2
    assert json.loads((tmp_path / "settings.json.moira-backup").read_text()) == original
    assert not setup(settings_path, metadata, "/installed/moira-statusline")
    installed["model"] = "new-setting"
    settings_path.write_text(json.dumps(installed))
    assert remove(settings_path, metadata, "/installed/moira-statusline")
    restored = json.loads(settings_path.read_text())
    assert restored["statusLine"] == original["statusLine"]
    assert restored["model"] == "new-setting"
    assert not remove(settings_path, metadata, "/installed/moira-statusline")


def test_statusline_delegates_original_input(tmp_path: Path) -> None:
    metadata = tmp_path / "integration.json"
    metadata.write_text(
        json.dumps({"original_status_line": {"type": "command", "command": "old-status"}})
    )
    raw = json.dumps(payload()).encode()
    fake_stdin = type("Input", (), {"buffer": io.BytesIO(raw)})()
    completed = type("Completed", (), {"returncode": 7})()
    with (
        patch.object(sys, "stdin", fake_stdin),
        patch("moira.claude_integration.integration_path", return_value=metadata),
        patch("moira.claude_integration.update_cache", return_value=True),
        patch("moira.claude_integration.subprocess.run", return_value=completed) as run,
    ):
        assert statusline_main() == 7
    assert run.call_args.kwargs["input"] == raw
    assert run.call_args.args[0] == "old-status"
