"""Tests for profile.py — speech profile correction learning."""
import sqlite3
from datetime import datetime, timedelta

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


class TestRecentPromotions:
    def test_empty_before_promotion(self):
        for _ in range(p.MIN_OCCURRENCES - 1):
            p.log_correction("hes here", "he's here")
        assert p.recent_promotions() == []

    def test_appears_once_promoted(self):
        for _ in range(p.MIN_OCCURRENCES):
            p.log_correction("hes here", "he's here")
        rows = p.recent_promotions()
        assert len(rows) == 1
        row = rows[0]
        assert row["raw"] == "hes"
        assert row["fixed"] == "he's"
        assert row["count"] == p.MIN_OCCURRENCES
        assert row["promoted_at"]  # a real timestamp was stamped

    def test_further_corrections_do_not_duplicate_or_move_promoted_at(self):
        for _ in range(p.MIN_OCCURRENCES):
            p.log_correction("hes here", "he's here")
        first = p.recent_promotions()[0]["promoted_at"]
        p.log_correction("hes here", "he's here")  # 4th occurrence
        rows = p.recent_promotions()
        assert len(rows) == 1
        assert rows[0]["promoted_at"] == first
        assert rows[0]["count"] == p.MIN_OCCURRENCES + 1

    def test_window_filters_old_promotions(self):
        for _ in range(p.MIN_OCCURRENCES):
            p.log_correction("hes here", "he's here")
        old = (datetime.now() - timedelta(days=30)).isoformat()
        with sqlite3.connect(p.DB_FILE) as conn:
            conn.execute(
                "UPDATE substitutions SET promoted_at=? WHERE whisper_out=?",
                (old, "hes"),
            )
        assert p.recent_promotions(days=7) == []
        assert len(p.recent_promotions(days=60)) == 1

    def test_contract_keys_exact(self):
        for _ in range(p.MIN_OCCURRENCES):
            p.log_correction("hes here", "he's here")
        row = p.recent_promotions()[0]
        assert set(row.keys()) == {"raw", "fixed", "count", "promoted_at"}


class TestPromotedAtMigration:
    def test_legacy_db_without_column_gets_backfilled(self, tmp_path, monkeypatch):
        """Simulate a profile.db created before promoted_at existed: rules
        already past the threshold must be backfilled, not lost."""
        db_path = tmp_path / "legacy.db"
        with sqlite3.connect(db_path) as conn:
            conn.executescript("""
                CREATE TABLE corrections (
                    id INTEGER PRIMARY KEY, whisper_out TEXT NOT NULL,
                    user_edit TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE substitutions (
                    whisper_out TEXT NOT NULL, correct_out TEXT NOT NULL,
                    count INTEGER DEFAULT 1, last_seen TEXT NOT NULL,
                    PRIMARY KEY (whisper_out, correct_out)
                );
            """)
            for i in range(p.MIN_OCCURRENCES):
                conn.execute(
                    "INSERT INTO corrections(whisper_out, user_edit, created_at) VALUES (?,?,?)",
                    ("recieve", "receive", f"2026-01-0{i+1}T00:00:00"),
                )
            conn.execute(
                "INSERT INTO substitutions(whisper_out, correct_out, count, last_seen) "
                "VALUES (?,?,?,?)",
                ("recieve", "receive", p.MIN_OCCURRENCES, "2026-01-03T00:00:00"),
            )

        monkeypatch.setattr(p, "DB_FILE", str(db_path))
        monkeypatch.setattr(p, "_initialized", False)
        p.init()

        # Existing rule survived the migration untouched...
        assert p.get_active_rules().get("recieve") == "receive"
        # ...and now has a promoted_at derived from the Nth (3rd) correction.
        rows = p.recent_promotions(days=36500)
        assert len(rows) == 1
        assert rows[0]["promoted_at"] == "2026-01-03T00:00:00"

    def test_migration_is_idempotent(self, tmp_path, monkeypatch):
        db_path = tmp_path / "legacy2.db"
        monkeypatch.setattr(p, "DB_FILE", str(db_path))
        monkeypatch.setattr(p, "_initialized", False)
        p.init()  # first run creates the column
        monkeypatch.setattr(p, "_initialized", False)
        p.init()  # second run must not error on an already-migrated db
        assert p.recent_promotions() == []


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
