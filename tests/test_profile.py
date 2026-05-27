"""Tests for profile.py — speech profile correction learning."""
import pytest
import profile as p


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    """Redirect the SQLite file to a temp directory and reset module state."""
    monkeypatch.setattr(p, "DB_FILE", str(tmp_path / "test_profile.db"))
    monkeypatch.setattr(p, "_initialized", False)
    p.init()


class TestLogCorrection:
    def test_no_entry_when_unchanged(self):
        p.log_correction("hello world", "hello world")
        assert p.get_all_rules() == []

    def test_single_word_substitution(self):
        p.log_correction("hes here", "he's here")
        rules = p.get_all_rules()
        assert any(r["whisper_out"] == "hes" for r in rules)

    def test_deletion_logged_as_empty_replacement(self):
        p.log_correction("hello um world", "hello world")
        rules = p.get_all_rules()
        assert any(r["whisper_out"] == "um" and r["correct_out"] == "" for r in rules)


class TestRulePromotion:
    def test_below_threshold_not_active(self):
        for _ in range(p.MIN_OCCURRENCES - 1):
            p.log_correction("hes here", "he's here")
        assert "hes" not in p.get_active_rules()

    def test_at_threshold_becomes_active(self):
        for _ in range(p.MIN_OCCURRENCES):
            p.log_correction("hes here", "he's here")
        assert p.get_active_rules().get("hes") == "he's"

    def test_count_increments(self):
        for _ in range(5):
            p.log_correction("hes here", "he's here")
        rules = p.get_all_rules()
        rule = next(r for r in rules if r["whisper_out"] == "hes")
        assert rule["count"] == 5


class TestDeleteRule:
    def test_delete_removes_from_active(self):
        for _ in range(p.MIN_OCCURRENCES):
            p.log_correction("recieve", "receive")
        p.delete_rule("recieve", "receive")
        assert "recieve" not in p.get_active_rules()

    def test_delete_removes_from_all_rules(self):
        p.log_correction("hes here", "he's here")
        p.delete_rule("hes", "he's")
        assert all(r["whisper_out"] != "hes" for r in p.get_all_rules())


class TestGetAllRules:
    def test_returns_active_flag(self):
        # 1 correction → pending
        p.log_correction("hes here", "he's here")
        rules = p.get_all_rules()
        rule = next(r for r in rules if r["whisper_out"] == "hes")
        assert rule["active"] is False

        # reach threshold → active
        for _ in range(p.MIN_OCCURRENCES - 1):
            p.log_correction("hes here", "he's here")
        rules = p.get_all_rules()
        rule = next(r for r in rules if r["whisper_out"] == "hes")
        assert rule["active"] is True
