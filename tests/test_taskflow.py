"""Tests for taskflow.py. Pure-function + monkeypatched-urlopen style,
matching test_config_validation.py's conventions (plain monkeypatch, no
unittest.mock) — taskflow.py has no heavy native deps to fake.
"""
import json

import sys
sys.modules.pop("taskflow", None)

import taskflow

DEFAULT_PHRASES = ["add this to TaskFlow", "add to my to-do list", "add a task"]


class FakeResponse:
    """Minimal context-manager stand-in for urllib.request.urlopen()'s return value."""

    def __init__(self, status: int, body: dict):
        self.status = status
        self._body = json.dumps(body).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestMatchTrigger:
    def test_matches_primary_phrase_case_insensitive(self):
        result = taskflow.match_trigger("ADD THIS TO TASKFLOW buy milk", DEFAULT_PHRASES)
        assert result == ("add this to TaskFlow", "buy milk")

    def test_no_match_returns_none(self):
        assert taskflow.match_trigger("just a normal sentence", DEFAULT_PHRASES) is None

    def test_matches_only_at_start(self):
        assert taskflow.match_trigger("please add this to TaskFlow now", DEFAULT_PHRASES) is None

    def test_first_matching_phrase_in_list_order_wins(self):
        phrases = ["add a task", "add a task to buy"]
        result = taskflow.match_trigger("add a task to buy milk", phrases)
        assert result == ("add a task", "to buy milk")

    def test_strips_leading_punctuation_from_remainder(self):
        result = taskflow.match_trigger("add a task: buy milk", ["add a task"])
        assert result[1] == "buy milk"

    def test_empty_remainder_when_phrase_is_whole_text(self):
        result = taskflow.match_trigger("add a task", ["add a task"])
        assert result[1] == ""

    def test_empty_phrase_list_returns_none(self):
        assert taskflow.match_trigger("add a task buy milk", []) is None

    def test_blank_phrase_entries_skipped(self):
        result = taskflow.match_trigger("add a task buy milk", ["", "  ", "add a task"])
        assert result == ("add a task", "buy milk")


class TestReadPortAndAppPath:
    def test_read_port_missing_file_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(taskflow, "_PORT_FILE", str(tmp_path / "nope.json"))
        assert taskflow._read_port() is None

    def test_read_port_valid_file(self, tmp_path, monkeypatch):
        f = tmp_path / "port.json"
        f.write_text('{"port": 5123, "pid": 1, "startedAt": "x"}')
        monkeypatch.setattr(taskflow, "_PORT_FILE", str(f))
        assert taskflow._read_port() == 5123

    def test_read_port_corrupt_json_returns_none(self, tmp_path, monkeypatch):
        f = tmp_path / "port.json"
        f.write_text("not json")
        monkeypatch.setattr(taskflow, "_PORT_FILE", str(f))
        assert taskflow._read_port() is None

    def test_read_app_path_missing_file_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(taskflow, "_APPPATH_FILE", str(tmp_path / "nope.json"))
        assert taskflow._read_app_path() is None

    def test_read_app_path_valid_file(self, tmp_path, monkeypatch):
        f = tmp_path / "app-path.json"
        f.write_text('{"exePath": "C:\\\\TaskFlow\\\\TaskFlow.exe"}')
        monkeypatch.setattr(taskflow, "_APPPATH_FILE", str(f))
        assert taskflow._read_app_path() == "C:\\TaskFlow\\TaskFlow.exe"

    def test_is_installed_false_when_apppath_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(taskflow, "_APPPATH_FILE", str(tmp_path / "nope.json"))
        assert taskflow.is_installed() is False

    def test_is_installed_true_when_apppath_exists(self, tmp_path, monkeypatch):
        f = tmp_path / "app-path.json"
        f.write_text('{"exePath": "x"}')
        monkeypatch.setattr(taskflow, "_APPPATH_FILE", str(f))
        assert taskflow.is_installed() is True


class TestCheckHealth:
    def test_false_when_no_port_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(taskflow, "_PORT_FILE", str(tmp_path / "nope.json"))
        assert taskflow.check_health() is False

    def test_true_on_200_ok(self, tmp_path, monkeypatch):
        f = tmp_path / "port.json"
        f.write_text('{"port": 5123}')
        monkeypatch.setattr(taskflow, "_PORT_FILE", str(f))
        monkeypatch.setattr(
            taskflow.urllib.request, "urlopen",
            lambda url, timeout=None: FakeResponse(200, {"status": "ok"}),
        )
        assert taskflow.check_health() is True

    def test_false_on_bad_body(self, tmp_path, monkeypatch):
        f = tmp_path / "port.json"
        f.write_text('{"port": 5123}')
        monkeypatch.setattr(taskflow, "_PORT_FILE", str(f))
        monkeypatch.setattr(
            taskflow.urllib.request, "urlopen",
            lambda url, timeout=None: FakeResponse(200, {"status": "down"}),
        )
        assert taskflow.check_health() is False

    def test_false_on_connection_error(self, tmp_path, monkeypatch):
        f = tmp_path / "port.json"
        f.write_text('{"port": 5123}')
        monkeypatch.setattr(taskflow, "_PORT_FILE", str(f))

        def raise_url_error(url, timeout=None):
            raise taskflow.urllib.error.URLError("refused")

        monkeypatch.setattr(taskflow.urllib.request, "urlopen", raise_url_error)
        assert taskflow.check_health() is False


class TestCreateTask:
    def test_returns_none_when_no_port(self, tmp_path, monkeypatch):
        monkeypatch.setattr(taskflow, "_PORT_FILE", str(tmp_path / "nope.json"))
        assert taskflow.create_task("buy milk") is None

    def test_posts_with_source_voice_dictation(self, tmp_path, monkeypatch):
        f = tmp_path / "port.json"
        f.write_text('{"port": 5123}')
        monkeypatch.setattr(taskflow, "_PORT_FILE", str(f))
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data)
            return FakeResponse(201, {"id": "abc", "title": "buy milk"})

        monkeypatch.setattr(taskflow.urllib.request, "urlopen", fake_urlopen)
        result = taskflow.create_task("buy milk")
        assert captured["body"]["source"] == "voice-dictation"
        assert captured["body"]["title"] == "buy milk"
        assert result == {"id": "abc", "title": "buy milk"}

    def test_includes_optional_fields_when_given(self, tmp_path, monkeypatch):
        f = tmp_path / "port.json"
        f.write_text('{"port": 5123}')
        monkeypatch.setattr(taskflow, "_PORT_FILE", str(f))
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data)
            return FakeResponse(201, {"id": "abc"})

        monkeypatch.setattr(taskflow.urllib.request, "urlopen", fake_urlopen)
        taskflow.create_task("buy milk", notes="2%", priority="high")
        assert captured["body"]["notes"] == "2%"
        assert captured["body"]["priority"] == "high"

    def test_returns_none_on_non_201(self, tmp_path, monkeypatch):
        f = tmp_path / "port.json"
        f.write_text('{"port": 5123}')
        monkeypatch.setattr(taskflow, "_PORT_FILE", str(f))
        monkeypatch.setattr(
            taskflow.urllib.request, "urlopen",
            lambda req, timeout=None: FakeResponse(500, {}),
        )
        assert taskflow.create_task("buy milk") is None

    def test_returns_none_on_url_error(self, tmp_path, monkeypatch):
        f = tmp_path / "port.json"
        f.write_text('{"port": 5123}')
        monkeypatch.setattr(taskflow, "_PORT_FILE", str(f))

        def raise_url_error(req, timeout=None):
            raise taskflow.urllib.error.URLError("refused")

        monkeypatch.setattr(taskflow.urllib.request, "urlopen", raise_url_error)
        assert taskflow.create_task("buy milk") is None


class TestEnsureRunning:
    def test_noop_when_already_healthy(self, monkeypatch):
        monkeypatch.setattr(taskflow, "check_health", lambda **k: True)
        launch_called = []
        monkeypatch.setattr(taskflow, "launch_hidden", lambda: launch_called.append(1) or True)
        taskflow.ensure_running()
        assert launch_called == []

    def test_skips_silently_when_not_installed(self, monkeypatch):
        monkeypatch.setattr(taskflow, "check_health", lambda **k: False)
        monkeypatch.setattr(taskflow, "is_installed", lambda: False)
        launch_called = []
        monkeypatch.setattr(taskflow, "launch_hidden", lambda: launch_called.append(1) or True)
        taskflow.ensure_running()
        assert launch_called == []

    def test_gives_up_when_launch_fails(self, monkeypatch):
        monkeypatch.setattr(taskflow, "check_health", lambda **k: False)
        monkeypatch.setattr(taskflow, "is_installed", lambda: True)
        monkeypatch.setattr(taskflow, "launch_hidden", lambda: False)
        sleeps = []
        monkeypatch.setattr(taskflow.time, "sleep", lambda s: sleeps.append(s))
        taskflow.ensure_running()
        assert sleeps == []

    def test_retries_with_backoff_then_gives_up(self, monkeypatch):
        monkeypatch.setattr(taskflow, "is_installed", lambda: True)
        monkeypatch.setattr(taskflow, "launch_hidden", lambda: True)
        calls = {"n": 0}

        def fake_health(**k):
            calls["n"] += 1
            return False

        monkeypatch.setattr(taskflow, "check_health", fake_health)
        monkeypatch.setattr(taskflow.time, "sleep", lambda s: None)
        taskflow.ensure_running(max_attempts=3)
        # 1 initial check_health() in ensure_running + 3 retry checks = 4
        assert calls["n"] == 4

    def test_succeeds_partway_through_retries(self, monkeypatch):
        monkeypatch.setattr(taskflow, "is_installed", lambda: True)
        monkeypatch.setattr(taskflow, "launch_hidden", lambda: True)
        calls = {"n": 0}

        def fake_health(**k):
            calls["n"] += 1
            return calls["n"] == 3

        monkeypatch.setattr(taskflow, "check_health", fake_health)
        monkeypatch.setattr(taskflow.time, "sleep", lambda s: None)
        taskflow.ensure_running(max_attempts=4)
        assert calls["n"] == 3
