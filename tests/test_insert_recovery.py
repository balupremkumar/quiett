"""Insert-failure recovery (BACKLOG item 139).

When a paste does not land, the text must never be stranded: main shows a warn
toast with an "Insert again" action that re-resolves the target at click time,
and the tray offers the same recovery for the newest dictation.

main is imported directly (all app deps live in the venv); inject/preview are
swapped for fakes so nothing touches the real clipboard or Tk.
"""
import sys
import threading

import pytest

sys.modules.pop("main", None)

import main  # noqa: E402
import tray  # noqa: E402


class FakeInject:
    INSERTED  = "inserted"
    CLIPBOARD = "clipboard"
    FAILED    = "failed"

    def __init__(self, status="inserted", foreground=0, usable=False):
        self.status = status
        self.foreground = foreground
        self.usable = usable
        self.calls = []          # (text, hwnd)
        self.submit_calls = []
        self.done = threading.Event()

    def capture_foreground(self, quiet=False):
        return self.foreground

    def is_usable_target(self, hwnd):
        return bool(hwnd) and self.usable

    def inject_text(self, text, hwnd):
        self.calls.append((text, hwnd))
        self.done.set()
        return self.status

    def inject_text_and_submit(self, text, hwnd):
        self.submit_calls.append((text, hwnd))
        return self.inject_text(text, hwnd)


class FakePreview:
    def __init__(self):
        self.toasts = []

    def show_toast(self, message, kind="info", action_label="", action_cb=None,
                   dwell_ms=0):
        self.toasts.append({"message": message, "kind": kind,
                            "action_label": action_label, "action_cb": action_cb,
                            "dwell_ms": dwell_ms})


@pytest.fixture
def fakes(monkeypatch):
    fi = FakeInject()
    fp = FakePreview()
    monkeypatch.setattr(main, "inject", fi)
    monkeypatch.setattr(main, "preview", fp)
    monkeypatch.setattr(main, "_cfg", {})
    monkeypatch.setattr(main, "_recording_target", {"hwnd": 0})
    return fi, fp


class TestResolvePasteTarget:
    def test_foreground_window_wins(self, fakes):
        fi, _ = fakes
        fi.foreground = 4242
        main._recording_target["hwnd"] = 111
        assert main._resolve_paste_target() == 4242

    def test_falls_back_to_hotkey_down_window(self, fakes):
        """Our own badge or panel holding focus at release must not mean
        'no paste target'."""
        fi, _ = fakes
        fi.foreground = 0          # capture_foreground rejects our own windows
        fi.usable = True
        main._recording_target["hwnd"] = 111
        assert main._resolve_paste_target() == 111

    def test_dead_fallback_window_is_not_used(self, fakes):
        fi, _ = fakes
        fi.foreground = 0
        fi.usable = False
        main._recording_target["hwnd"] = 111
        assert main._resolve_paste_target() == 0


class TestInsertTextNow:
    def test_successful_insert_shows_no_toast(self, fakes):
        fi, fp = fakes
        fi.foreground = 4242
        assert main.insert_text_now("hello") == FakeInject.INSERTED
        assert fi.calls == [("hello", 4242)]
        assert fp.toasts == []

    def test_explicit_target_is_used_as_given(self, fakes):
        fi, _ = fakes
        fi.foreground = 4242
        main.insert_text_now("hello", 999)
        assert fi.calls == [("hello", 999)]

    def test_empty_text_never_injects(self, fakes):
        fi, fp = fakes
        assert main.insert_text_now("   ") == FakeInject.INSERTED
        assert fi.calls == []
        assert fp.toasts == []

    def test_leading_space_is_preserved(self, fakes):
        """A panel insert with "append" on carries a leading space."""
        fi, _ = fakes
        fi.foreground = 4242
        main.insert_text_now(" appended")
        assert fi.calls == [(" appended", 4242)]

    def test_submit_uses_the_insert_and_send_path(self, fakes):
        fi, _ = fakes
        fi.foreground = 4242
        main.insert_text_now("hello", submit=True)
        assert fi.submit_calls == [("hello", 4242)]

    def test_clipboard_fallback_shows_retry_toast(self, fakes):
        fi, fp = fakes
        fi.status = FakeInject.CLIPBOARD
        fi.foreground = 4242
        assert main.insert_text_now("hello") == FakeInject.CLIPBOARD
        assert len(fp.toasts) == 1
        toast = fp.toasts[0]
        assert toast["kind"] == "warn"
        assert toast["action_label"] == "Insert again"
        assert toast["action_cb"] is not None
        assert toast["dwell_ms"] == main._INSERT_RETRY_DWELL_MS
        assert "clipboard" in toast["message"].lower()

    def test_hard_failure_shows_retry_toast(self, fakes):
        fi, fp = fakes
        fi.status = FakeInject.FAILED
        fi.foreground = 4242
        assert main.insert_text_now("hello") == FakeInject.FAILED
        assert len(fp.toasts) == 1
        assert fp.toasts[0]["action_label"] == "Insert again"

    def test_clipboard_only_mode_stays_quiet(self, fakes, monkeypatch):
        """Copying instead of inserting is the whole point of that mode."""
        fi, fp = fakes
        fi.status = FakeInject.CLIPBOARD
        monkeypatch.setattr(main, "_cfg", {"paste_mode": "clipboard_only"})
        main.insert_text_now("hello")
        assert fp.toasts == []


class TestRetryAction:
    def test_retry_reinserts_into_the_window_focused_at_click_time(self, fakes):
        fi, fp = fakes
        fi.status = FakeInject.CLIPBOARD
        fi.foreground = 4242
        main.insert_text_now("hello")

        # User clicks into a different field, then presses "Insert again".
        fi.foreground = 777
        fi.status = FakeInject.INSERTED
        fi.done.clear()
        fp.toasts[0]["action_cb"]()
        assert fi.done.wait(2.0), "retry never ran"
        assert fi.calls[-1] == ("hello", 777)

    def test_retry_that_fails_again_offers_another_retry(self, fakes):
        fi, fp = fakes
        fi.status = FakeInject.CLIPBOARD
        fi.foreground = 4242
        main.insert_text_now("hello")
        fi.done.clear()
        fp.toasts[0]["action_cb"]()
        assert fi.done.wait(2.0)
        assert len(fp.toasts) == 2
        assert fp.toasts[1]["action_label"] == "Insert again"


class TestPreviewWiring:
    def test_preview_failure_callback_signature_matches(self):
        """preview calls back with (text, status) — main's toast helper takes
        exactly that."""
        import inspect
        assert list(inspect.signature(main.show_insert_retry_toast).parameters) == \
            ["text", "status"]

    def test_setter_stores_the_callback(self):
        import preview
        marker = object()
        previous = preview._on_insert_failed
        try:
            preview.set_insert_failed_callback(marker)
            assert preview._on_insert_failed is marker
        finally:
            preview.set_insert_failed_callback(previous)


class TestInsertLastDictation:
    def test_helper_runs_the_insert_off_the_menu_thread(self, fakes):
        fi, _ = fakes
        fi.foreground = 4242
        main.insert_last_dictation("older text")
        assert fi.done.wait(2.0), "insert never ran"
        assert fi.calls == [("older text", 4242)]

    def test_tray_item_sends_the_newest_entry(self, monkeypatch):
        import history
        monkeypatch.setattr(history, "load",
                            lambda: [{"text": "newest"}, {"text": "older"}])
        sent = []
        monkeypatch.setattr(tray, "_on_insert_last", lambda t: sent.append(t))
        tray._insert_last(None, None)
        assert sent == ["newest"]

    def test_tray_item_skips_blank_entries(self, monkeypatch):
        import history
        monkeypatch.setattr(history, "load",
                            lambda: [{"text": "   "}, {"text": "real one"}])
        sent = []
        monkeypatch.setattr(tray, "_on_insert_last", lambda t: sent.append(t))
        tray._insert_last(None, None)
        assert sent == ["real one"]

    def test_empty_history_notifies_instead_of_inserting(self, monkeypatch):
        import history
        monkeypatch.setattr(history, "load", lambda: [])
        notes, sent = [], []
        monkeypatch.setattr(tray, "notify", lambda title, msg: notes.append(msg))
        monkeypatch.setattr(tray, "_on_insert_last", lambda t: sent.append(t))
        tray._insert_last(None, None)
        assert sent == []
        assert notes == ["No recent dictations"]

    def test_unreadable_history_does_not_raise(self, monkeypatch):
        import history

        def _boom():
            raise OSError("history file locked")

        monkeypatch.setattr(history, "load", _boom)
        monkeypatch.setattr(tray, "notify", lambda title, msg: None)
        monkeypatch.setattr(tray, "_on_insert_last", lambda t: None)
        tray._insert_last(None, None)  # must not raise
