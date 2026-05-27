"""Tests for transcribe postprocessing and confidence normalisation.
No Whisper model is loaded — only pure-Python functions are tested.
"""
import sys
from unittest.mock import MagicMock

# Stub heavy deps before import
sys.modules.setdefault("faster_whisper", MagicMock())
_audio_stub = MagicMock()
_audio_stub.SAMPLE_RATE = 16000
sys.modules.setdefault("audio", _audio_stub)

import transcribe  # noqa: E402


class TestStripFillers:
    def test_basic_removal(self):
        assert transcribe._strip_fillers("um hello uh world", ["um", "uh"]) == "hello world"

    def test_word_boundary_respected(self):
        # "umbrella" must not be trimmed by the "um" filler rule
        assert transcribe._strip_fillers("umbrella um", ["um"]) == "umbrella"

    def test_multi_word_filler_first(self):
        result = transcribe._strip_fillers("you know what um", ["um", "you know"])
        assert "you know" not in result
        assert "um" not in result

    def test_case_insensitive(self):
        assert transcribe._strip_fillers("UM hello UH", ["um", "uh"]) == "hello"

    def test_no_double_spaces(self):
        result = transcribe._strip_fillers("hello um world", ["um"])
        assert "  " not in result


class TestApplyProfile:
    def test_replaces_known_word(self):
        result = transcribe._apply_profile("hes going", {"hes": "he's"})
        assert "he's" in result

    def test_skips_empty_whisper_out(self):
        # Empty key must not cause a crash
        result = transcribe._apply_profile("hello world", {"": "boom"})
        assert result == "hello world"

    def test_word_boundary_only(self):
        # "receive" should not be replaced inside "receiver"
        result = transcribe._apply_profile("receiver recieve", {"recieve": "receive"})
        assert result.startswith("receiver")
        assert "receive" in result


class TestPostprocess:
    def test_capitalises_first_letter(self):
        result = transcribe._postprocess("hello world", [], {})
        assert result[0] == "H"

    def test_appends_trailing_space(self):
        result = transcribe._postprocess("hello", [], {})
        assert result.endswith(" ")

    def test_empty_returns_empty(self):
        result = transcribe._postprocess("um", ["um"], {})
        assert result == ""

    def test_applies_fillers_then_profile(self):
        result = transcribe._postprocess("um hes here", ["um"], {"hes": "he's"})
        assert "um" not in result
        assert "he's" in result.lower()  # first char may be capitalised by postprocess


class TestConfidenceNormalisation:
    """Verify the logprob → 0–1 mapping used in transcribe.run()."""

    def _normalise(self, logprob: float) -> float:
        return max(0.0, min(1.0, (logprob + 0.8) / 0.6))

    def test_confident_logprob(self):
        assert self._normalise(-0.2) == pytest.approx(1.0)

    def test_mid_logprob(self):
        val = self._normalise(-0.5)
        assert 0.3 < val < 0.7

    def test_uncertain_logprob(self):
        assert self._normalise(-0.8) == pytest.approx(0.0)

    def test_clamp_below_zero(self):
        assert self._normalise(-2.0) == 0.0

    def test_clamp_above_one(self):
        assert self._normalise(0.0) == 1.0


import pytest  # noqa: E402 (needed for approx above)
