"""Tests for fitness.py (Local FitnessPal client). Same conventions as
test_taskflow.py: pure functions + monkeypatched urlopen, no unittest.mock.
"""
import json
import urllib.error

import sys
sys.modules.pop("fitness", None)

import fitness

DEFAULT_PHRASES = ["food log", "log food", "macro log"]


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
    """fitness re-exports taskflow.match_trigger — verify the semantics that
    matter for food logging hold through the alias."""

    def test_matches_at_start_case_insensitive(self):
        result = fitness.match_trigger("Food log meal 1, 4 eggs", DEFAULT_PHRASES)
        assert result == ("food log", "meal 1, 4 eggs")

    def test_mid_utterance_mention_does_not_match(self):
        assert fitness.match_trigger("I forgot to food log today", DEFAULT_PHRASES) is None

    def test_plain_dictation_does_not_match(self):
        assert fitness.match_trigger("let's meet at the food court", DEFAULT_PHRASES) is None


class TestLogRaw:
    def test_success_returns_response_dict(self, monkeypatch):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data)
            captured["timeout"] = timeout
            return FakeResponse(200, {"summary": "Logged 2 items to meal 1"})

        monkeypatch.setattr(fitness.urllib.request, "urlopen", fake_urlopen)
        result = fitness.log_raw("food log meal 1, 4 eggs, 250 grams rice")
        assert result == {"summary": "Logged 2 items to meal 1"}
        assert captured["url"] == "http://127.0.0.1:8091/api/log/raw"
        assert captured["body"]["transcript"] == "food log meal 1, 4 eggs, 250 grams rice"
        assert captured["body"]["source"] == "voice-dictation"
        assert captured["timeout"] == fitness._LOG_TIMEOUT_S

    def test_unreachable_returns_none(self, monkeypatch):
        def fake_urlopen(req, timeout=None):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(fitness.urllib.request, "urlopen", fake_urlopen)
        assert fitness.log_raw("food log meal 1, 4 eggs") is None

    def test_unexpected_status_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            fitness.urllib.request, "urlopen",
            lambda req, timeout=None: FakeResponse(500, {"detail": "boom"}),
        )
        assert fitness.log_raw("food log meal 1, 4 eggs") is None

    def test_garbage_body_returns_none(self, monkeypatch):
        class GarbageResponse(FakeResponse):
            def __init__(self):
                self.status = 200
                self._body = b"not json"

        monkeypatch.setattr(
            fitness.urllib.request, "urlopen",
            lambda req, timeout=None: GarbageResponse(),
        )
        assert fitness.log_raw("food log meal 1, 4 eggs") is None


class TestCheckHealth:
    def test_healthy(self, monkeypatch):
        monkeypatch.setattr(
            fitness.urllib.request, "urlopen",
            lambda url, timeout=None: FakeResponse(200, {"status": "ok"}),
        )
        assert fitness.check_health() is True

    def test_down(self, monkeypatch):
        def fake_urlopen(url, timeout=None):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(fitness.urllib.request, "urlopen", fake_urlopen)
        assert fitness.check_health() is False


class TestFormatResult:
    def test_uses_server_summary(self):
        assert fitness.format_result({"summary": "Logged 3 items to meal 2"}) \
            == "Logged 3 items to meal 2"

    def test_fallback_when_summary_missing(self):
        assert fitness.format_result({}) == "Food log saved."

    def test_fallback_when_summary_blank(self):
        assert fitness.format_result({"summary": "  "}) == "Food log saved."
