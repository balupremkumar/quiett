"""Tests for taskflow.py. Pure-function + monkeypatched-urlopen style,
matching test_config_validation.py's conventions (plain monkeypatch, no
unittest.mock) — taskflow.py has no heavy native deps to fake.
"""
import json
import re
from datetime import date

import sys
sys.modules.pop("taskflow", None)

import taskflow
import transcribe

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


LEADING = ["add this to my to-do list", "add to my to-do list", "add to my list",
           "add a task", "add task", "add this to TaskFlow"]
TRAILING = ["add that to my to-do list", "add that to my list", "add that as a task",
            "add that to TaskFlow"]


class TestExtractTasks:
    def test_no_match_returns_text_unchanged(self):
        tasks, remainder = taskflow.extract_tasks(
            "Just a normal sentence about my day.", LEADING, TRAILING)
        assert tasks == []
        assert remainder == "Just a normal sentence about my day."

    def test_whole_utterance_single_match(self):
        tasks, remainder = taskflow.extract_tasks(
            "Add this to my to-do list buy milk.", LEADING, TRAILING)
        assert tasks == ["buy milk"]
        assert remainder == ""

    def test_embedded_mid_utterance_leading(self):
        text = ("I was thinking about the project. Add this to my to-do list "
                 "call the dentist. Anyway, let's keep going with the plan.")
        tasks, remainder = taskflow.extract_tasks(text, LEADING, TRAILING)
        assert tasks == ["call the dentist"]
        assert "project" in remainder
        assert "keep going" in remainder
        assert "dentist" not in remainder

    def test_embedded_trailing_phrase(self):
        text = "Buy milk, add that to my list. Then let's continue talking."
        tasks, remainder = taskflow.extract_tasks(text, LEADING, TRAILING)
        assert tasks == ["Buy milk"]
        assert "continue talking" in remainder

    def test_multiple_tasks_in_one_utterance(self):
        text = "Add a task buy milk. Add a task call the dentist."
        tasks, remainder = taskflow.extract_tasks(text, LEADING, TRAILING)
        assert tasks == ["buy milk", "call the dentist"]
        assert remainder == ""

    def test_negated_trigger_not_extracted(self):
        text = "Don't add this to my to-do list, I'm just talking about it."
        tasks, remainder = taskflow.extract_tasks(text, LEADING, TRAILING)
        assert tasks == []
        assert "to-do list" in remainder

    def test_meta_discussion_about_to_do_lists_not_triggered(self):
        text = "I want to create myself a to-do list app that runs on boot."
        tasks, remainder = taskflow.extract_tasks(text, LEADING, TRAILING)
        assert tasks == []
        assert remainder == text

    def test_empty_text_returns_empty(self):
        tasks, remainder = taskflow.extract_tasks("", LEADING, TRAILING)
        assert tasks == []
        assert remainder == ""


class TestParseDueDate:
    def test_today(self):
        anchor = date(2026, 6, 25)
        cleaned, due = taskflow.parse_due_date("buy milk today", today=anchor)
        assert due == "2026-06-25"
        assert "today" not in cleaned

    def test_tomorrow_with_connector(self):
        anchor = date(2026, 6, 25)
        cleaned, due = taskflow.parse_due_date("call the dentist by tomorrow", today=anchor)
        assert due == "2026-06-26"
        assert cleaned == "call the dentist"

    def test_next_weekday(self):
        anchor = date(2026, 6, 25)  # a Thursday
        cleaned, due = taskflow.parse_due_date("submit report next monday", today=anchor)
        assert due == "2026-07-06"
        assert "monday" not in cleaned.lower()

    def test_bare_weekday_rolls_to_next_occurrence(self):
        anchor = date(2026, 6, 25)  # Thursday
        cleaned, due = taskflow.parse_due_date("submit report friday", today=anchor)
        assert due == "2026-06-26"

    def test_in_n_days(self):
        anchor = date(2026, 6, 25)
        cleaned, due = taskflow.parse_due_date("renew passport in 5 days", today=anchor)
        assert due == "2026-06-30"

    def test_no_date_returns_none(self):
        cleaned, due = taskflow.parse_due_date("buy milk")
        assert due is None
        assert cleaned == "buy milk"


class TestParsePriority:
    def test_urgent_maps_to_high(self):
        cleaned, pri = taskflow.parse_priority("this is urgent fix the server")
        assert pri == "high"
        assert "urgent" not in cleaned

    def test_low_rush_maps_to_low(self):
        cleaned, pri = taskflow.parse_priority("clean the garage, no rush")
        assert pri == "low"

    def test_no_priority_returns_none(self):
        cleaned, pri = taskflow.parse_priority("buy milk")
        assert pri is None
        assert cleaned == "buy milk"


class TestParseProject:
    PROJECTS = [{"id": "p1", "name": "Work"}, {"id": "p2", "name": "Personal"}]

    def test_exact_project_name_match(self):
        cleaned, pid = taskflow.parse_project("send the invoice to my Work list", self.PROJECTS)
        assert pid == "p1"
        assert "work" not in cleaned.lower()

    def test_fuzzy_project_name_match(self):
        cleaned, pid = taskflow.parse_project("email client in my personal project", self.PROJECTS)
        assert pid == "p2"

    def test_no_project_phrase_returns_none(self):
        cleaned, pid = taskflow.parse_project("buy milk", self.PROJECTS)
        assert pid is None

    def test_no_projects_available_returns_none(self):
        cleaned, pid = taskflow.parse_project("to my Work list", None)
        assert pid is None


class TestSplitTitleNotes:
    def test_short_text_no_split(self):
        title, notes = taskflow.split_title_notes("buy milk")
        assert title == "buy milk"
        assert notes is None

    def test_splits_on_comma(self):
        title, notes = taskflow.split_title_notes("call the dentist, ask about the appointment time")
        assert title == "call the dentist"
        assert notes == "ask about the appointment time"

    def test_long_text_without_punctuation_splits_on_word_count(self):
        text = "this is a really quite long rambling task description with extra detail"
        title, notes = taskflow.split_title_notes(text, max_title_words=5)
        assert title == "this is a really quite"
        assert notes == "long rambling task description with extra detail"


class TestBuildTaskSpec:
    def test_plain_title_only(self):
        spec = taskflow.build_task_spec("buy milk")
        assert spec == {"title": "buy milk"}

    def test_due_date_and_priority_combined(self):
        spec = taskflow.build_task_spec("fix the server urgent tomorrow")
        assert spec["priority"] == "high"
        assert re.match(r"\d{4}-\d{2}-\d{2}", spec["dueDate"])
        assert "urgent" not in spec["title"].lower()
        assert "tomorrow" not in spec["title"].lower()

    def test_project_lookup_only_called_when_relevant(self, monkeypatch):
        called = {"n": 0}

        def fake_list_projects(timeout=1.5):
            called["n"] += 1
            return []

        monkeypatch.setattr(taskflow, "list_projects", fake_list_projects)
        taskflow.build_task_spec("buy milk")
        assert called["n"] == 0

    def test_project_lookup_called_when_phrase_present(self, monkeypatch):
        called = {"n": 0}

        def fake_list_projects(timeout=1.5):
            called["n"] += 1
            return [{"id": "p1", "name": "Work"}]

        monkeypatch.setattr(taskflow, "list_projects", fake_list_projects)
        spec = taskflow.build_task_spec("send invoice to my Work list")
        assert called["n"] == 1
        assert spec.get("projectId") == "p1"


class TestListUpdateDeleteProjects:
    def test_list_tasks_no_port_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(taskflow, "_PORT_FILE", str(tmp_path / "nope.json"))
        assert taskflow.list_tasks() is None

    def test_list_tasks_success(self, tmp_path, monkeypatch):
        f = tmp_path / "port.json"
        f.write_text('{"port": 5123}')
        monkeypatch.setattr(taskflow, "_PORT_FILE", str(f))
        monkeypatch.setattr(
            taskflow.urllib.request, "urlopen",
            lambda url, timeout=None: FakeResponse(200, [{"id": "1", "title": "buy milk"}]),
        )
        result = taskflow.list_tasks(completed=False)
        assert result == [{"id": "1", "title": "buy milk"}]

    def test_update_task_success(self, tmp_path, monkeypatch):
        f = tmp_path / "port.json"
        f.write_text('{"port": 5123}')
        monkeypatch.setattr(taskflow, "_PORT_FILE", str(f))
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data)
            captured["method"] = req.get_method()
            return FakeResponse(200, {"id": "1", "completed": True})

        monkeypatch.setattr(taskflow.urllib.request, "urlopen", fake_urlopen)
        result = taskflow.update_task("1", completed=True)
        assert captured["method"] == "PATCH"
        assert captured["body"] == {"completed": True}
        assert result == {"id": "1", "completed": True}

    def test_update_task_404_returns_none(self, tmp_path, monkeypatch):
        f = tmp_path / "port.json"
        f.write_text('{"port": 5123}')
        monkeypatch.setattr(taskflow, "_PORT_FILE", str(f))
        monkeypatch.setattr(
            taskflow.urllib.request, "urlopen",
            lambda req, timeout=None: FakeResponse(404, {"error": "Task not found"}),
        )
        assert taskflow.update_task("nope", completed=True) is None

    def test_delete_task_success(self, tmp_path, monkeypatch):
        f = tmp_path / "port.json"
        f.write_text('{"port": 5123}')
        monkeypatch.setattr(taskflow, "_PORT_FILE", str(f))

        def fake_urlopen(req, timeout=None):
            assert req.get_method() == "DELETE"
            return FakeResponse(200, {"deleted": True})

        monkeypatch.setattr(taskflow.urllib.request, "urlopen", fake_urlopen)
        assert taskflow.delete_task("1") is True

    def test_delete_task_404_returns_false(self, tmp_path, monkeypatch):
        f = tmp_path / "port.json"
        f.write_text('{"port": 5123}')
        monkeypatch.setattr(taskflow, "_PORT_FILE", str(f))
        monkeypatch.setattr(
            taskflow.urllib.request, "urlopen",
            lambda req, timeout=None: FakeResponse(404, {"error": "Task not found"}),
        )
        assert taskflow.delete_task("nope") is False

    def test_list_projects_success(self, tmp_path, monkeypatch):
        f = tmp_path / "port.json"
        f.write_text('{"port": 5123}')
        monkeypatch.setattr(taskflow, "_PORT_FILE", str(f))
        monkeypatch.setattr(
            taskflow.urllib.request, "urlopen",
            lambda url, timeout=None: FakeResponse(200, [{"id": "p1", "name": "Work"}]),
        )
        assert taskflow.list_projects() == [{"id": "p1", "name": "Work"}]


class TestFindOpenTaskByTitle:
    def test_exact_match(self, monkeypatch):
        monkeypatch.setattr(taskflow, "list_tasks",
                             lambda completed=None: [{"id": "1", "title": "buy milk"}])
        result = taskflow.find_open_task_by_title("buy milk")
        assert result["id"] == "1"

    def test_fuzzy_match(self, monkeypatch):
        monkeypatch.setattr(taskflow, "list_tasks",
                             lambda completed=None: [{"id": "1", "title": "buy milk and eggs"}])
        result = taskflow.find_open_task_by_title("buy milk")
        assert result["id"] == "1"

    def test_no_tasks_returns_none(self, monkeypatch):
        monkeypatch.setattr(taskflow, "list_tasks", lambda completed=None: [])
        assert taskflow.find_open_task_by_title("buy milk") is None

    def test_unrelated_title_returns_none(self, monkeypatch):
        monkeypatch.setattr(taskflow, "list_tasks",
                             lambda completed=None: [{"id": "1", "title": "completely different thing"}])
        assert taskflow.find_open_task_by_title("buy milk") is None


class TestFormatTaskList:
    def test_empty_list(self):
        assert taskflow.format_task_list([]) == "Your to-do list is empty."

    def test_single_task(self):
        assert taskflow.format_task_list([{"title": "buy milk"}]) == "You have 1 task: buy milk."

    def test_multiple_tasks(self):
        result = taskflow.format_task_list([{"title": "buy milk"}, {"title": "call dentist"}])
        assert result == "You have 2 tasks: buy milk; call dentist."


class TestMatchCompleteCommand:
    def test_matches_mark_as_done(self):
        assert taskflow.match_complete_command("mark buy milk as done") == "buy milk"

    def test_matches_complete_as_finished(self):
        assert taskflow.match_complete_command("complete call the dentist as finished") == "call the dentist"

    def test_bare_mark_without_suffix_does_not_match(self):
        # Deliberately conservative — avoids false-positiving on "Mark is calling me."
        assert taskflow.match_complete_command("Mark is going to call me") is None

    def test_unrelated_sentence_does_not_match(self):
        assert taskflow.match_complete_command("I finished the report yesterday") is None


class TestDuplicateGuard:
    def test_not_duplicate_before_creation(self):
        assert taskflow.is_duplicate("a brand new unique title xyz") is False

    def test_duplicate_within_window(self, monkeypatch):
        monkeypatch.setattr(taskflow, "_recent_titles", {})
        taskflow._remember_title("buy milk")
        assert taskflow.is_duplicate("Buy Milk") is True

    def test_not_duplicate_after_window_expires(self, monkeypatch):
        monkeypatch.setattr(taskflow, "_recent_titles", {})
        monkeypatch.setattr(taskflow.time, "time", lambda: 1000.0)
        taskflow._remember_title("buy milk")
        monkeypatch.setattr(taskflow.time, "time", lambda: 1000.0 + taskflow._DUP_WINDOW_S + 1)
        assert taskflow.is_duplicate("buy milk") is False


class TestRecordHealthCheck:
    def test_resets_on_success(self, monkeypatch):
        monkeypatch.setattr(taskflow, "_consecutive_health_failures", 5)
        assert taskflow.record_health_check(True) is False
        assert taskflow._consecutive_health_failures == 0

    def test_fires_exactly_once_at_threshold(self, monkeypatch):
        monkeypatch.setattr(taskflow, "_consecutive_health_failures", 0)
        results = [taskflow.record_health_check(False) for _ in range(5)]
        assert results == [False, False, True, False, False]


class TestPostprocessIntegration:
    """transcribe.py's postprocess runs once, before any taskflow parsing —
    verify the combined pipeline (capitalisation, trailing-period, filler
    removal) doesn't break trigger matching or break content parsing."""

    def test_realistic_postprocessed_text_matches_and_parses(self):
        raw = "add a task buy milk by tomorrow"
        postprocessed = transcribe._postprocess(raw, filler_words=[], profile_rules={})
        # _postprocess capitalises the first letter and appends a trailing space
        assert postprocessed[0].isupper()

        task_texts, remainder = taskflow.extract_tasks(
            postprocessed, ["add a task"], [])
        assert len(task_texts) == 1
        assert remainder == ""

        spec = taskflow.build_task_spec(task_texts[0])
        assert spec["title"] == "buy milk"
        assert re.match(r"\d{4}-\d{2}-\d{2}", spec["dueDate"])

    def test_embedded_trigger_survives_postprocess_capitalisation(self):
        raw = "i was thinking. add this to my to-do list call the dentist. anyway let's continue"
        postprocessed = transcribe._postprocess(raw, filler_words=[], profile_rules={})
        task_texts, remainder = taskflow.extract_tasks(
            postprocessed, ["add this to my to-do list"], [])
        assert task_texts == ["call the dentist"]
        assert "continue" in remainder.lower()
