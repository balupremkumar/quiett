"""Tests for _validate_config in main.py.
Imports main directly — all app deps are installed in the venv so no mocking needed.
"""
import sys

# Remove any prior mock of "main" so we get the real module
sys.modules.pop("main", None)

import main


class TestValidateConfig:
    def test_defaults_applied_for_empty_config(self):
        result = main._validate_config({})
        assert result["max_record_seconds"] == 120.0
        assert result["preview_position"]   == "cursor"
        assert result["corrections"]        == {}
        assert result["filler_words"]       == []

    def test_max_record_clamped_low(self):
        result = main._validate_config({"max_record_seconds": 1})
        assert result["max_record_seconds"] == 5.0

    def test_max_record_clamped_high(self):
        result = main._validate_config({"max_record_seconds": 9999})
        assert result["max_record_seconds"] == 300.0

    def test_invalid_max_record_falls_back(self):
        result = main._validate_config({"max_record_seconds": "bad"})
        assert result["max_record_seconds"] == 120.0

    def test_invalid_position_falls_back(self):
        result = main._validate_config({"preview_position": "moon"})
        assert result["preview_position"] == "cursor"

    def test_valid_positions_kept(self):
        for pos in ("top-right", "bottom-right", "top-left", "bottom-left", "center"):
            result = main._validate_config({"preview_position": pos})
            assert result["preview_position"] == pos

    def test_non_list_filler_words_replaced(self):
        result = main._validate_config({"filler_words": "not a list"})
        assert result["filler_words"] == []

    def test_non_dict_corrections_replaced(self):
        result = main._validate_config({"corrections": ["not", "a", "dict"]})
        assert result["corrections"] == {}

    def test_silence_timeout_clamped_negative(self):
        result = main._validate_config({"silence_auto_stop_seconds": -5})
        assert result["silence_auto_stop_seconds"] == 0.0

    def test_user_values_preserved(self):
        result = main._validate_config({
            "language": "fr",
            "max_record_seconds": 90,
            "corrections": {"gonna": "going to"},
        })
        assert result["language"]            == "fr"
        assert result["max_record_seconds"]  == 90.0
        assert result["corrections"]         == {"gonna": "going to"}


class TestPanelAcrylicConfig:
    def test_default_on(self):
        assert main._validate_config({})["panel_acrylic"] is True

    def test_explicit_off_kept(self):
        assert main._validate_config({"panel_acrylic": False})["panel_acrylic"] is False

    def test_non_bool_coerced(self):
        assert main._validate_config({"panel_acrylic": 0})["panel_acrylic"] is False
        assert main._validate_config({"panel_acrylic": 1})["panel_acrylic"] is True


class TestLearnFromEditsConfig:
    def test_default_on(self):
        assert main._validate_config({})["learn_from_edits"] is True

    def test_explicit_off_kept(self):
        assert main._validate_config({"learn_from_edits": False})["learn_from_edits"] is False


class TestDashboardPrewarmConfig:
    def test_default_on(self):
        assert main._validate_config({})["dashboard_prewarm"] is True

    def test_explicit_off_kept(self):
        assert main._validate_config({"dashboard_prewarm": False})["dashboard_prewarm"] is False

    def test_non_bool_coerced(self):
        assert main._validate_config({"dashboard_prewarm": 0})["dashboard_prewarm"] is False
        assert main._validate_config({"dashboard_prewarm": "yes"})["dashboard_prewarm"] is True


class TestReadAloudConfig:
    def test_defaults_applied(self):
        result = main._validate_config({})
        assert result["tts_speed"] == 1.0
        assert result["tts_max_chunk_chars"] == 120

    def test_speed_clamped_to_range(self):
        assert main._validate_config({"tts_speed": 0.1})["tts_speed"] == 0.5
        assert main._validate_config({"tts_speed": 9})["tts_speed"] == 2.0

    def test_invalid_speed_falls_back(self):
        assert main._validate_config({"tts_speed": "fast"})["tts_speed"] == 1.0

    def test_chunk_chars_never_negative(self):
        assert main._validate_config({"tts_max_chunk_chars": -5})["tts_max_chunk_chars"] == 0
        assert main._validate_config({"tts_max_chunk_chars": "x"})["tts_max_chunk_chars"] == 120

