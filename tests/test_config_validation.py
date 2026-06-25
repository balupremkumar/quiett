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


class TestTaskflowConfig:
    def test_defaults_applied(self):
        result = main._validate_config({})
        assert result["taskflow_enabled"] is True
        assert result["taskflow_trigger_phrases"] == [
            "add this to my to-do list", "add to my to-do list",
            "add to my list", "add a task", "add task",
            "add this to TaskFlow",
        ]

    def test_enabled_coerced_to_bool(self):
        assert main._validate_config({"taskflow_enabled": 0})["taskflow_enabled"] is False
        assert main._validate_config({"taskflow_enabled": 1})["taskflow_enabled"] is True

    def test_non_list_phrases_falls_back_to_default(self):
        result = main._validate_config({"taskflow_trigger_phrases": "not a list"})
        assert result["taskflow_trigger_phrases"] == main._CONFIG_DEFAULTS["taskflow_trigger_phrases"]

    def test_non_string_and_blank_entries_filtered(self):
        result = main._validate_config({"taskflow_trigger_phrases": ["ok phrase", 5, "", "  "]})
        assert result["taskflow_trigger_phrases"] == ["ok phrase"]

    def test_custom_phrases_preserved(self):
        result = main._validate_config({"taskflow_trigger_phrases": ["log a task"]})
        assert result["taskflow_trigger_phrases"] == ["log a task"]
