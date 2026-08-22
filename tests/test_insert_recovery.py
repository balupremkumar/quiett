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
import stash  # noqa: E402
import tray  # noqa: E402


class FakeInject:
    INSERTED  = "inserted"
    INSERTED_UNCONFIRMED = "inserted_unconfirmed"
    CLIPBOARD = "clipboard"
    FAILED    = "failed"

    @staticmethod
    def landed(status):
        return status in (FakeInject.INSERTED, FakeInject.INSERTED_UNCONFIRMED)

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
        self.recoveries = []

    def show_toast(self, message, kind="info", action_label="", action_cb=None,
                   dwell_ms=0, target_hwnd=0):
        self.toasts.append({"message": message, "kind": kind,
                            "action_label": action_label, "action_cb": action_cb,
                            "dwell_ms": dwell_ms})

    def show_recovery_panel(self, text, reason, on_place, on_dismiss):
        self.recoveries.append({"text": text, "reason": reason,
                                "on_place": on_place, "on_dismiss": on_dismiss})


@pytest.fixture
def fakes(monkeypatch):
    fi = FakeInject()
    fp = FakePreview()
    monkeypatch.setattr(main, "inject", fi)
    monkeypatch.setattr(main, "preview", fp)
    monkeypatch.setattr(main, "_cfg", {})
    monkeypatch.setattr(main, "_recording_target", {"hwnd": 0})
    monkeypatch.setattr(stash, "_item", None)
    stash.set_change_callback(None)
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
        assert fp.recoveries == []

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

    def test_clipboard_fallback_opens_the_recovery_panel(self, fakes):
        fi, fp = fakes
        fi.status = FakeInject.CLIPBOARD
        fi.foreground = 4242
        assert main.insert_text_now("hello") == FakeInject.CLIPBOARD
        assert fp.toasts == []          # the corner toast is for notices now
        assert len(fp.recoveries) == 1
        panel = fp.recoveries[0]
        assert panel["text"] == "hello"
        assert "clipboard" in panel["reason"].lower()
        assert panel["on_place"] is not None
        assert panel["on_dismiss"] is not None

    def test_hard_failure_opens_the_recovery_panel(self, fakes):
        fi, fp = fakes
        fi.status = FakeInject.FAILED
        fi.foreground = 4242
        assert main.insert_text_now("hello") == FakeInject.FAILED
        assert len(fp.recoveries) == 1
        assert "clipboard copy" in fp.recoveries[0]["reason"].lower()

    def test_unconfirmed_insert_says_nothing(self, fakes):
        """Corrected escalation policy (PASTE_UX_PLAN section 5): "no signal"
        is not failure. Terminals, RDP and every app without an accessibility
        layer land here, so escalating would put a failure notice on most
        successful inserts. Retention plus the tray dot is the whole response."""
        fi, fp = fakes
        fi.status = FakeInject.INSERTED_UNCONFIRMED
        fi.foreground = 4242
        assert main.insert_text_now("hello") == FakeInject.INSERTED_UNCONFIRMED
        assert fp.toasts == []
        assert fp.recoveries == []

    def test_unconfirmed_insert_stays_quiet_when_retention_is_off(self, fakes, monkeypatch):
        """Unconfirmed is silent either way; retention only decides whether the
        previous clipboard comes back, which is inject's call, not this one."""
        fi, fp = fakes
        fi.status = FakeInject.INSERTED_UNCONFIRMED
        fi.foreground = 4242
        monkeypatch.setattr(main, "_cfg", {"clipboard_retain_on_unconfirmed": False})
        main.insert_text_now("hello")
        assert fp.toasts == []
        assert fp.recoveries == []

    def test_unconfirmed_insert_leaves_the_stash_armed(self, fakes):
        fi, _ = fakes
        fi.status = FakeInject.INSERTED_UNCONFIRMED
        fi.foreground = 4242
        stash.put("hello", 4242)
        main.insert_text_now("hello")
        assert stash.has_unconsumed()

    def test_confirmed_insert_consumes_the_stash(self, fakes):
        fi, _ = fakes
        fi.status = FakeInject.INSERTED
        fi.foreground = 4242
        stash.put("hello", 4242)
        main.insert_text_now("hello")
        assert not stash.has_unconsumed()

    def test_clipboard_only_mode_stays_quiet(self, fakes, monkeypatch):
        """Copying instead of inserting is the whole point of that mode."""
        fi, fp = fakes
        fi.status = FakeInject.CLIPBOARD
        monkeypatch.setattr(main, "_cfg", {"paste_mode": "clipboard_only"})
        main.insert_text_now("hello")
        assert fp.toasts == []
        assert fp.recoveries == []


class TestRetryAction:
    def test_place_it_reinserts_into_the_window_focused_at_click_time(self, fakes):
        fi, fp = fakes
        fi.status = FakeInject.CLIPBOARD
        fi.foreground = 4242
        main.insert_text_now("hello")

        # User clicks into a different field, then presses "Place it".
        fi.foreground = 777
        fi.status = FakeInject.INSERTED
        fi.done.clear()
        fp.recoveries[0]["on_place"]()
        assert fi.done.wait(2.0), "retry never ran"
        assert fi.calls[-1] == ("hello", 777)

    def test_a_retry_that_fails_again_reopens_the_panel(self, fakes):
        fi, fp = fakes
        fi.status = FakeInject.CLIPBOARD
        fi.foreground = 4242
        main.insert_text_now("hello")
        fi.done.clear()
        fp.recoveries[0]["on_place"]()
        assert fi.done.wait(2.0)
        assert len(fp.recoveries) == 2

    def test_dismiss_drops_the_parked_copy(self, fakes):
        fi, fp = fakes
        fi.status = FakeInject.CLIPBOARD
        fi.foreground = 4242
        stash.put("hello", 4242)
        main.insert_text_now("hello")
        assert stash.has_unconsumed()      # armed until the user says otherwise
        fp.recoveries[0]["on_dismiss"]()
        assert stash.get() is None


class TestPreviewWiring:
    def test_preview_failure_callback_signature_matches(self):
        """preview calls back with (text, status), which is exactly what
        main's escalation entry point takes."""
        import inspect
        assert list(inspect.signature(main.escalate_insert).parameters) == \
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


class TestTrayStashItem:
    """The tray reflects a parked dictation: dot on the icon, "Place last
    dictation" as the first menu item."""

    @pytest.fixture(autouse=True)
    def clean_stash(self, monkeypatch):
        monkeypatch.setattr(stash, "_item", None)
        stash.set_change_callback(None)
        yield
        stash.set_change_callback(None)
        stash._item = None

    def test_hidden_until_something_is_parked(self):
        assert not tray._has_stashed()
        stash.put("parked text", 4242)
        assert tray._has_stashed()

    def test_consumed_item_hides_it_again(self):
        stash.put("parked text", 4242)
        stash.mark_consumed()
        assert not tray._has_stashed()

    def test_label_carries_the_text_inline(self):
        stash.put("remember the milk", 4242)
        assert tray._place_last_label() == "Place last dictation: remember the milk"

    def test_label_truncates_long_text(self):
        stash.put("x" * 200, 4242)
        label = tray._place_last_label()
        body = label.split(": ", 1)[1]
        assert len(body) == tray._STASH_LABEL_TRUNCATE
        assert body.endswith("\u2026")

    def test_label_collapses_newlines(self):
        """A multi-line dictation must not break the menu row."""
        stash.put("first line\nsecond line", 4242)
        assert tray._place_last_label() == "Place last dictation: first line second line"

    def test_placing_sends_the_stashed_text_not_the_newest_history_entry(self, monkeypatch):
        import history
        monkeypatch.setattr(history, "load", lambda: [{"text": "some newer thing"}])
        stash.put("the parked one", 4242)
        sent = []
        monkeypatch.setattr(tray, "_on_insert_last", lambda t: sent.append(t))
        tray._place_last(None, None)
        assert sent == ["the parked one"]

    def test_placing_an_empty_stash_notifies_instead(self, monkeypatch):
        notes, sent = [], []
        monkeypatch.setattr(tray, "notify", lambda title, msg: notes.append(msg))
        monkeypatch.setattr(tray, "_on_insert_last", lambda t: sent.append(t))
        tray._place_last(None, None)
        assert sent == []
        assert notes == ["Nothing to place"]

    def test_icon_carries_a_dot_while_parked(self):
        assert tray._icon_for("idle") is tray._ICONS["idle"]
        stash.put("parked text", 4242)
        assert tray._icon_for("idle") is tray._STASH_ICONS["idle"]

    def test_a_broken_stash_never_takes_the_menu_down(self, monkeypatch):
        def _boom():
            raise RuntimeError("stash exploded")

        monkeypatch.setattr(stash, "has_unconsumed", _boom)
        assert tray._has_stashed() is False
