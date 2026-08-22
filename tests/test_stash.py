"""Tests for stash.py, the one-item parking spot for the last dictation.

The stash exists so a dictation is never lost when an insert cannot be
confirmed (PASTE_UX_PLAN section 2). Pure Python, no Windows APIs, so this
runs anywhere.
"""
import threading

import pytest

import stash


@pytest.fixture(autouse=True)
def clean_stash():
    """Module-level state, so every test starts and ends empty."""
    stash.set_change_callback(None)
    stash._item = None
    yield
    stash.set_change_callback(None)
    stash._item = None


class TestLifecycle:
    def test_empty_by_default(self):
        assert stash.get() is None
        assert not stash.has_unconsumed()

    def test_put_parks_text_and_target(self):
        stash.put("hello world", 4242)
        item = stash.get()
        assert item["text"] == "hello world"
        assert item["target_hwnd"] == 4242
        assert item["consumed"] is False
        assert item["at"] > 0
        assert stash.has_unconsumed()

    def test_target_hwnd_is_optional(self):
        stash.put("no target")
        assert stash.get()["target_hwnd"] == 0

    def test_put_replaces_the_previous_item(self):
        """Exactly one item, deliberately: history keeps the rest."""
        stash.put("first", 1)
        stash.put("second", 2)
        assert stash.get()["text"] == "second"

    def test_put_rearms_after_a_consume(self):
        stash.put("first", 1)
        stash.mark_consumed()
        stash.put("second", 2)
        assert stash.has_unconsumed()

    def test_mark_consumed_disarms_without_dropping_the_text(self):
        stash.put("hello", 1)
        stash.mark_consumed()
        assert not stash.has_unconsumed()
        assert stash.get()["text"] == "hello"
        assert stash.get()["consumed"] is True

    def test_clear_drops_the_item(self):
        stash.put("hello", 1)
        stash.clear()
        assert stash.get() is None
        assert not stash.has_unconsumed()

    def test_mark_consumed_and_clear_on_an_empty_stash_do_not_raise(self):
        stash.mark_consumed()
        stash.clear()
        assert stash.get() is None

    def test_blank_text_never_counts_as_unconsumed(self):
        """A blank item would show as an empty tray entry."""
        stash.put("   \n ", 1)
        assert not stash.has_unconsumed()

    def test_get_returns_a_copy(self):
        stash.put("hello", 1)
        item = stash.get()
        item["text"] = "tampered"
        assert stash.get()["text"] == "hello"


class TestChangeCallback:
    def test_fires_on_put_consume_and_clear(self):
        calls = []
        stash.set_change_callback(lambda: calls.append(len(calls)))
        stash.put("hello", 1)
        stash.mark_consumed()
        stash.clear()
        assert len(calls) == 3

    def test_does_not_fire_when_nothing_changed(self):
        calls = []
        stash.put("hello", 1)
        stash.mark_consumed()
        stash.set_change_callback(lambda: calls.append(1))
        stash.mark_consumed()   # already consumed
        stash.clear()
        stash.clear()           # already empty
        assert len(calls) == 1

    def test_callback_runs_outside_the_lock(self):
        """The tray callback re-enters the module to build its menu; firing it
        under the lock would deadlock the whole app."""
        seen = []

        def _cb():
            seen.append((stash.has_unconsumed(), stash.get()["text"]))

        stash.set_change_callback(_cb)
        stash.put("hello", 1)
        assert seen == [(True, "hello")]

    def test_a_raising_callback_never_breaks_the_stash(self):
        def _boom():
            raise RuntimeError("tray is gone")

        stash.set_change_callback(_boom)
        stash.put("hello", 1)      # must not raise
        assert stash.get()["text"] == "hello"

    def test_callback_can_be_cleared(self):
        calls = []
        stash.set_change_callback(lambda: calls.append(1))
        stash.set_change_callback(None)
        stash.put("hello", 1)
        assert calls == []


class TestThreadSafety:
    def test_concurrent_writers_leave_one_coherent_item(self):
        errors = []

        def _worker(n):
            try:
                for i in range(50):
                    stash.put(f"worker-{n}-{i}", n)
                    stash.get()
                    stash.has_unconsumed()
            except Exception as exc:      # pragma: no cover - failure detail
                errors.append(exc)

        threads = [threading.Thread(target=_worker, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5.0)
        assert errors == []
        item = stash.get()
        assert item["text"].startswith("worker-")
        assert item["target_hwnd"] in range(4)
