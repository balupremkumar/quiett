"""Row identity in history.json.

An open History page holds a list of indices. Every new dictation is inserted
at the front, so those indices shift by one the moment the user speaks — and
acting on a stale index deletes or pins the wrong dictation. Entries are
therefore addressed by timestamp, with the index only as a fast path.
"""
import json
import os

import pytest

import history


@pytest.fixture
def hist_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    entries = [
        {"timestamp": f"2026-07-27T1{i}:00:00.000001", "text": f"entry {i}"}
        for i in range(3)
    ]
    (tmp_path / "history.json").write_text(json.dumps(entries), encoding="utf-8")
    return tmp_path


def _texts():
    return [e["text"] for e in history.load()]


class TestResolveIndex:
    def test_matching_index_is_the_fast_path(self):
        entries = [{"timestamp": "a"}, {"timestamp": "b"}]
        assert history.resolve_index(entries, 1, "b") == 1

    def test_shifted_entry_is_found_by_stamp(self):
        entries = [{"timestamp": "new"}, {"timestamp": "a"}, {"timestamp": "b"}]
        assert history.resolve_index(entries, 1, "b") == 2

    def test_missing_stamp_resolves_to_nothing(self):
        entries = [{"timestamp": "a"}]
        assert history.resolve_index(entries, 0, "gone") == -1

    def test_no_stamp_falls_back_to_the_index(self):
        entries = [{"timestamp": "a"}, {"timestamp": "b"}]
        assert history.resolve_index(entries, 1, "") == 1
        assert history.resolve_index(entries, 9, "") == -1


class TestPinAfterShift:
    def test_pin_follows_the_entry_not_the_slot(self, hist_dir):
        stamp = "2026-07-27T11:00:00.000001"  # "entry 1", at index 1
        history.save("a dictation that just landed")  # everything shifts down
        assert history.set_pinned(1, True, stamp) is True
        pinned = [e["text"] for e in history.load() if e.get("pinned")]
        assert pinned == ["entry 1"]

    def test_index_alone_would_have_hit_the_wrong_row(self, hist_dir):
        """Documents the bug: without the stamp, the same call pins whatever
        has slid into that slot."""
        history.save("a dictation that just landed")
        assert history.set_pinned(1, True) is True
        pinned = [e["text"] for e in history.load() if e.get("pinned")]
        assert pinned == ["entry 0"]

    def test_deleted_entry_reports_failure(self, hist_dir):
        assert history.set_pinned(0, True, "2026-07-27T09:99:99") is False


class TestTransaction:
    def test_lock_file_is_released(self, hist_dir):
        with history.transaction():
            pass
        with history.transaction():  # a leaked lock would hang then proceed late
            history._write([])
        assert history.load() == []

    def test_save_still_works_when_the_lock_file_cannot_be_made(
            self, hist_dir, monkeypatch):
        """A read-only directory or an AV hold on the lock file must degrade to
        the old thread-only behaviour, never break saving."""
        real_open = open

        def _no_lock_file(path, *args, **kwargs):
            if os.path.basename(str(path)) == history._LOCK_FILE:
                raise OSError("locked out")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", _no_lock_file)
        history.save("still saved")
        assert _texts()[0] == "still saved"
