"""Place mode and the corrected escalation policy (PASTE_UX_PLAN sections 4-5).

Everything here is headless: no Tk root, no real mouse hook, no live desktop.
The state machine, the timeout and the escalation table are all reachable as
plain functions, which is the point of keeping them out of the widget code.
"""
import ctypes
import json
import threading
import time

import pytest

import hotkey
import main
import preview
import stash


# ---------------------------------------------------------------------------
# Escalation decision table (PASTE_UX_PLAN section 5, corrected 2026-08-22)
# ---------------------------------------------------------------------------

class TestEscalationTable:
    """Pure function, so the table is testable as a table. The rule it
    encodes: silence is not the same as failure."""

    def test_confirmed_landed_consumes_the_stash_and_says_nothing(self):
        d = main.escalation_for("inserted")
        assert d["consume_stash"] is True
        assert d["ui"] == main.ESCALATE_NONE
        assert d["reason"] == ""

    def test_no_signal_leaves_the_stash_armed_and_says_nothing(self):
        """Terminals, RDP and every app with no accessibility layer land here.
        A notice on all of them would train the user to ignore it."""
        d = main.escalation_for("inserted_unconfirmed")
        assert d["consume_stash"] is False
        assert d["ui"] == main.ESCALATE_NONE

    def test_confirmed_miss_escalates_to_the_recovery_panel(self):
        d = main.escalation_for("clipboard")
        assert d["consume_stash"] is False
        assert d["ui"] == main.ESCALATE_RECOVERY
        assert "clipboard" in d["reason"].lower()

    def test_hard_failure_admits_the_clipboard_copy_failed_too(self):
        d = main.escalation_for("failed")
        assert d["ui"] == main.ESCALATE_RECOVERY
        assert "clipboard copy" in d["reason"].lower()

    def test_preflight_refusal_names_the_real_reason(self):
        d = main.escalation_for(main._REFUSED_NOT_EDITABLE)
        assert d["ui"] == main.ESCALATE_RECOVERY
        assert "no text field" in d["reason"].lower()

    def test_clipboard_only_mode_is_never_a_failure(self):
        """Copying instead of inserting is the whole point of that mode."""
        for status in ("clipboard", "failed", "inserted_unconfirmed"):
            d = main.escalation_for(status, paste_mode="clipboard_only")
            assert d["ui"] == main.ESCALATE_NONE
            assert d["consume_stash"] is False

    def test_clipboard_only_does_not_consume_the_stash_on_a_confirmed_insert(self):
        assert main.escalation_for("inserted", "clipboard_only")["consume_stash"] is False

    def test_an_unrecognised_status_escalates_rather_than_going_quiet(self):
        """Failing loud on something unexpected keeps a future inject status
        from silently losing a dictation."""
        d = main.escalation_for("something_new")
        assert d["ui"] == main.ESCALATE_RECOVERY

    def test_only_a_confirmed_insert_ever_consumes_the_stash(self):
        for status in ("inserted_unconfirmed", "clipboard", "failed",
                       main._REFUSED_NOT_EDITABLE, "something_new"):
            assert main.escalation_for(status)["consume_stash"] is False


# ---------------------------------------------------------------------------
# Place mode
# ---------------------------------------------------------------------------

class FakePreview:
    def __init__(self):
        self.toasts = []
        self.overlays = []        # text on arm, None on disarm
        self.rings = []           # hwnd, or None on hide
        self.recoveries = []      # (text, reason) per recovery panel

    def show_toast(self, message, kind="info", action_label="", action_cb=None,
                   dwell_ms=0, target_hwnd=0):
        self.toasts.append({"message": message, "kind": kind})

    def show_place_overlay(self, text):
        self.overlays.append(text)

    def hide_place_overlay(self):
        self.overlays.append(None)

    def show_ring_for_window(self, hwnd):
        self.rings.append(hwnd)

    def hide_target_ring(self):
        self.rings.append(None)

    def hide_badge(self):
        pass

    def show_recovery_panel(self, text, reason, on_place, on_dismiss):
        self.recoveries.append((text, reason))


class FakeHotkey:
    def __init__(self, hook_ok=True):
        self.hook_ok = hook_ok
        self.taps = {}
        self.click_cb = None
        self.capture_calls = 0
        self.cancel_calls = 0

    def register_tap(self, keys, callback):
        self.taps[keys] = callback
        return True

    def unregister_tap(self, keys):
        self.taps.pop(keys, None)

    def capture_next_click(self, on_click):
        self.capture_calls += 1
        if not self.hook_ok:
            return False
        self.click_cb = on_click
        return True

    def cancel_click_capture(self):
        self.cancel_calls += 1
        self.click_cb = None

    def set_external_recording(self, active):
        pass


class FakeAudio:
    def __init__(self, recording=False):
        self.recording = recording
        self.cancelled = False

    def is_recording(self):
        return self.recording

    def cancel(self):
        self.cancelled = True
        self.recording = False


class FakeTray:
    def __init__(self):
        self.states = []

    def set_state(self, state):
        self.states.append(state)


class FakeInject:
    def __init__(self, status="inserted"):
        self.calls = []
        self.status = status
        self.done = threading.Event()

    def capture_foreground(self, quiet=False):
        return 0

    def is_usable_target(self, hwnd):
        return False

    def inject_text(self, text, hwnd):
        self.calls.append((text, hwnd))
        self.done.set()
        return self.status

    INSERTED = "inserted"
    INSERTED_UNCONFIRMED = "inserted_unconfirmed"
    CLIPBOARD = "clipboard"
    FAILED = "failed"
    REFUSED_NOT_EDITABLE = "refused_not_editable"

    @staticmethod
    def landed(status):
        return status in ("inserted", "inserted_unconfirmed")


@pytest.fixture
def place(monkeypatch):
    fp, fh, fa, ft, fi = FakePreview(), FakeHotkey(), FakeAudio(), FakeTray(), FakeInject()
    monkeypatch.setattr(main, "preview", fp)
    monkeypatch.setattr(main, "hotkey", fh)
    monkeypatch.setattr(main, "audio", fa)
    monkeypatch.setattr(main, "tray", ft)
    monkeypatch.setattr(main, "inject", fi)
    monkeypatch.setattr(main, "_cfg", {})
    monkeypatch.setattr(main, "_place", {"armed": False, "text": "", "timer": None})
    monkeypatch.setattr(stash, "_item", None)
    stash.set_change_callback(None)
    yield fp, fh, fa, ft, fi
    main.disarm_place_mode("test teardown")


class TestArming:
    def test_nothing_stashed_says_so_and_arms_nothing(self, place):
        fp, fh, _, _, _ = place
        assert main.toggle_place_mode() == "empty"
        assert fp.toasts == [{"message": "Nothing to place yet", "kind": "info"}]
        assert fp.overlays == []
        assert fh.capture_calls == 0
        assert not main.place_mode_armed()

    def test_a_consumed_item_counts_as_nothing_stashed(self, place):
        fp, _, _, _, _ = place
        stash.put("already placed", 4242)
        stash.mark_consumed()
        assert main.toggle_place_mode() == "empty"
        assert fp.toasts[0]["message"] == "Nothing to place yet"

    def test_a_blank_stash_item_counts_as_nothing_stashed(self, place):
        fp, _, _, _, _ = place
        stash.put("   ", 4242)
        assert main.toggle_place_mode() == "empty"
        assert fp.toasts[0]["message"] == "Nothing to place yet"

    def test_arming_shows_the_overlay_and_installs_the_hook(self, place):
        fp, fh, _, _, _ = place
        stash.put("remember the milk", 4242)
        assert main.toggle_place_mode() == "armed"
        assert main.place_mode_armed()
        assert fp.overlays == ["remember the milk"]
        assert fh.capture_calls == 1
        assert fh.click_cb is main._on_place_click

    def test_escape_is_registered_only_while_armed(self, place):
        _, fh, _, _, _ = place
        assert "esc" not in fh.taps
        stash.put("parked", 4242)
        main.toggle_place_mode()
        assert "esc" in fh.taps
        fh.taps["esc"]()                       # the user presses Esc
        assert not main.place_mode_armed()
        assert "esc" not in fh.taps

    def test_a_second_press_disarms(self, place):
        fp, fh, _, _, _ = place
        stash.put("parked", 4242)
        main.toggle_place_mode()
        assert main.toggle_place_mode() == "disarmed"
        assert not main.place_mode_armed()
        assert fp.overlays[-1] is None
        assert fh.cancel_calls >= 1

    def test_disarm_is_idempotent(self, place):
        fp, _, _, _, _ = place
        stash.put("parked", 4242)
        main.toggle_place_mode()
        main.disarm_place_mode("first")
        overlays = len(fp.overlays)
        main.disarm_place_mode("second")
        assert len(fp.overlays) == overlays     # nothing fired twice

    def test_no_mouse_hook_means_no_armed_state_left_behind(self, place, monkeypatch):
        fp, fh, _, _, _ = place
        fh.hook_ok = False
        stash.put("parked", 4242)
        main.toggle_place_mode()
        assert not main.place_mode_armed()
        assert fp.overlays[-1] is None
        assert any("mouse hook" in t["message"] for t in fp.toasts)

    def test_arming_cancels_the_recording_its_own_modifiers_started(self, place):
        """ctrl+alt+v shares the ctrl+alt record hold, so the modifiers alone
        have already started a recording by the time the V lands."""
        _, _, fa, ft, _ = place
        fa.recording = True
        stash.put("parked", 4242)
        assert main.toggle_place_mode() == "armed"
        assert fa.cancelled is True
        assert ft.states == ["idle"]

    def test_nothing_is_cancelled_when_nothing_is_recording(self, place):
        _, _, fa, ft, _ = place
        stash.put("parked", 4242)
        main.toggle_place_mode()
        assert fa.cancelled is False
        assert ft.states == []


class TestTimeout:
    def test_the_default_timeout_is_thirty_seconds(self):
        assert main._PLACE_TIMEOUT_SECONDS == 30.0

    def test_armed_and_forgotten_disarms_itself(self, place, monkeypatch):
        fp, fh, _, _, _ = place
        monkeypatch.setattr(main, "_PLACE_TIMEOUT_SECONDS", 0.05)
        stash.put("parked", 4242)
        main.toggle_place_mode()
        assert main.place_mode_armed()
        deadline = time.monotonic() + 2.0
        while main.place_mode_armed() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not main.place_mode_armed(), "the 30s timeout never fired"
        assert fp.overlays[-1] is None
        assert fh.cancel_calls >= 1

    def test_disarming_cancels_the_pending_timer(self, place, monkeypatch):
        monkeypatch.setattr(main, "_PLACE_TIMEOUT_SECONDS", 30.0)
        stash.put("parked", 4242)
        main.toggle_place_mode()
        timer = main._place["timer"]
        main.disarm_place_mode("test")
        assert timer is not None
        time.sleep(0.05)
        assert not timer.is_alive()
        assert main._place["timer"] is None


class TestClickToPlace:
    def test_the_click_inserts_the_parked_text_at_the_new_target(self, place, monkeypatch):
        _, fh, _, _, fi = place
        monkeypatch.setattr(main, "_PLACE_SETTLE_SECONDS", 0.0)
        stash.put("remember the milk", 4242)
        main.toggle_place_mode()
        fh.click_cb()                           # the user clicks a field
        assert fi.done.wait(2.0), "the insert never ran"
        # hwnd 0: the target is resolved at click time, not at arm time, which
        # is the entire point of picking the field by clicking it.
        assert fi.calls == [("remember the milk", 0)]

    def test_the_click_disarms(self, place, monkeypatch):
        fp, fh, _, _, _ = place
        monkeypatch.setattr(main, "_PLACE_SETTLE_SECONDS", 0.0)
        stash.put("parked", 4242)
        main.toggle_place_mode()
        fh.click_cb()
        assert not main.place_mode_armed()
        assert fp.overlays[-1] is None

    def test_a_click_arriving_after_a_disarm_does_nothing(self, place, monkeypatch):
        _, fh, _, _, fi = place
        monkeypatch.setattr(main, "_PLACE_SETTLE_SECONDS", 0.0)
        stash.put("parked", 4242)
        main.toggle_place_mode()
        cb = fh.click_cb
        main.disarm_place_mode("esc")
        cb()
        assert fi.calls == []

    def test_the_settle_delay_is_not_zero_in_production(self):
        """The click has been delivered but the app has not necessarily
        finished moving focus into the control it hit."""
        assert main._PLACE_SETTLE_SECONDS > 0


# ---------------------------------------------------------------------------
# preview.py helpers behind the overlay and the ring (no Tk root needed)
# ---------------------------------------------------------------------------

class FakeTarget:
    def __init__(self, verdict="EDITABLE", rect=(10, 20, 300, 60), label="Notepad"):
        self.verdict = verdict
        self.rect = rect
        self.label = label


@pytest.fixture
def probe_config(tmp_path, monkeypatch):
    path = tmp_path / "config.json"

    def _write(data: dict) -> None:
        path.write_text(json.dumps(data), encoding="utf-8")

    monkeypatch.setattr(preview, "_CONFIG_FILE", str(path))
    _write({})
    return _write


class TestPlaceOverlayText:
    def test_first_line_only(self):
        assert preview._place_preview_line("one\ntwo\nthree") == "one"

    def test_truncated_with_an_ellipsis(self):
        line = preview._place_preview_line("x" * 200)
        assert len(line) == preview._PLACE_TEXT_CHARS
        assert line.endswith("…")

    def test_blank_text_yields_nothing_to_draw(self):
        assert preview._place_preview_line("   \n  ") == ""
        assert preview._place_preview_line("") == ""

    def test_short_text_is_left_alone(self):
        assert preview._place_preview_line("  hello  ") == "hello"


class TestRingSpec:
    def test_editable_earns_the_accent(self, probe_config, monkeypatch):
        monkeypatch.setattr(preview, "targetprobe",
                            type("P", (), {"probe": staticmethod(lambda h: FakeTarget())}))
        rect, colour, label = preview._ring_spec(4242)
        assert rect == (10, 20, 300, 60)
        assert colour == "ok"
        assert label == "Notepad"

    def test_not_editable_and_unknown_both_get_amber(self, probe_config, monkeypatch):
        for verdict in ("NOT_EDITABLE", "UNKNOWN", "something else"):
            monkeypatch.setattr(
                preview, "targetprobe",
                type("P", (), {"probe": staticmethod(lambda h, v=verdict: FakeTarget(verdict=v))}))
            assert preview._ring_spec(4242)[1] == "warn"

    def test_no_element_rect_falls_back_to_the_window(self, probe_config, monkeypatch):
        monkeypatch.setattr(preview, "_window_ring",
                            lambda h: ((0, 0, 800, 600), "unknown", ""))
        monkeypatch.setattr(
            preview, "targetprobe",
            type("P", (), {"probe": staticmethod(lambda h: FakeTarget(rect=None))}))
        rect, colour, _ = preview._ring_spec(4242)
        assert rect == (0, 0, 800, 600)
        assert colour == "ok"          # the verdict still stands, only the rect is coarse

    def test_a_missing_probe_module_still_outlines_the_window(self, probe_config, monkeypatch):
        """targetprobe.py is written in parallel and may not be importable."""
        monkeypatch.setattr(preview, "targetprobe", None)
        monkeypatch.setattr(preview, "_window_ring",
                            lambda h: ((0, 0, 800, 600), "unknown", ""))
        assert preview._ring_spec(4242) == ((0, 0, 800, 600), "unknown", "")

    def test_a_probe_that_raises_never_takes_the_ring_down(self, probe_config, monkeypatch):
        def _boom(hwnd):
            raise RuntimeError("UIA fell over")

        monkeypatch.setattr(preview, "targetprobe",
                            type("P", (), {"probe": staticmethod(_boom)}))
        monkeypatch.setattr(preview, "_window_ring",
                            lambda h: ((0, 0, 800, 600), "unknown", ""))
        assert preview._ring_spec(4242)[1] == "unknown"

    def test_probe_switched_off_outlines_the_window_and_claims_nothing(self, probe_config, monkeypatch):
        probe_config({"target_probe": False})
        monkeypatch.setattr(preview, "_window_ring",
                            lambda h: ((0, 0, 800, 600), "unknown", ""))
        monkeypatch.setattr(preview, "targetprobe",
                            type("P", (), {"probe": staticmethod(lambda h: FakeTarget())}))
        assert preview._ring_spec(4242)[1] == "unknown"

    def test_no_window_means_no_ring(self, probe_config):
        assert preview._ring_spec(0) is None


class TestRingConfigGates:
    def test_ring_switched_off_makes_show_ring_for_window_a_no_op(self, probe_config, monkeypatch):
        probe_config({"target_ring": False})
        spawned = []
        monkeypatch.setattr(preview.threading, "Thread",
                            lambda *a, **k: spawned.append(k) or pytest.fail("spawned a worker"))
        preview.show_ring_for_window(4242)
        assert spawned == []


# ---------------------------------------------------------------------------
# hotkey.py: tap registration and the one-shot click capture
# ---------------------------------------------------------------------------

class FakeKeyboard:
    KEY_DOWN = "down"

    def __init__(self, fail=False):
        self.fail = fail
        self.registered = []
        self.removed = []
        self._next = 0

    def add_hotkey(self, combo, fn, suppress=False):
        if self.fail:
            raise ValueError("nope")
        self._next += 1
        self.registered.append((combo, fn, suppress))
        return self._next

    def remove_hotkey(self, handle):
        self.removed.append(handle)


@pytest.fixture
def fake_kb(monkeypatch):
    kb = FakeKeyboard()
    monkeypatch.setattr(hotkey, "keyboard", kb)
    monkeypatch.setattr(hotkey, "_tap_handles", {})
    return kb


class TestTapHotkeys:
    def test_registers_and_reports_success(self, fake_kb):
        assert hotkey.register_tap("ctrl+alt+v", lambda: None) is True
        assert fake_kb.registered[0][0] == "ctrl+alt+v"

    def test_never_suppresses_the_combo(self, fake_kb):
        """Swallowing it would hide the keystroke from the app the user is in."""
        hotkey.register_tap("ctrl+alt+v", lambda: None)
        assert fake_kb.registered[0][2] is False

    def test_normalises_the_combo(self, fake_kb):
        hotkey.register_tap("  Ctrl+Alt+V  ", lambda: None)
        assert fake_kb.registered[0][0] == "ctrl+alt+v"
        assert "ctrl+alt+v" in hotkey._tap_handles

    def test_re_registering_replaces_the_previous_binding(self, fake_kb):
        hotkey.register_tap("ctrl+alt+v", lambda: None)
        first = hotkey._tap_handles["ctrl+alt+v"]
        hotkey.register_tap("ctrl+alt+v", lambda: None)
        assert fake_kb.removed == [first]
        assert len(hotkey._tap_handles) == 1

    def test_unregister_removes_it(self, fake_kb):
        hotkey.register_tap("esc", lambda: None)
        hotkey.unregister_tap("esc")
        assert fake_kb.removed
        assert "esc" not in hotkey._tap_handles

    def test_unregistering_something_that_was_never_registered_is_safe(self, fake_kb):
        hotkey.unregister_tap("esc")
        assert fake_kb.removed == []

    def test_blank_keys_register_nothing(self, fake_kb):
        assert hotkey.register_tap("", lambda: None) is False
        assert hotkey.register_tap("esc", None) is False
        assert fake_kb.registered == []

    def test_a_failed_registration_reports_false(self, monkeypatch):
        kb = FakeKeyboard(fail=True)
        monkeypatch.setattr(hotkey, "keyboard", kb)
        monkeypatch.setattr(hotkey, "_tap_handles", {})
        assert hotkey.register_tap("ctrl+alt+v", lambda: None) is False

    def test_a_raising_callback_never_escapes_the_hook_thread(self, fake_kb):
        def _boom():
            raise RuntimeError("callback exploded")

        hotkey.register_tap("ctrl+alt+v", _boom)
        fake_kb.registered[0][1]()      # must not raise

    def test_the_hold_path_state_is_untouched(self, fake_kb):
        """The hold-to-record hook keeps its own state; taps go nowhere near it."""
        before_mods = list(hotkey._mod_sets)
        before_held = list(hotkey._held)
        hotkey.register_tap("ctrl+alt+v", lambda: None)
        hotkey.unregister_tap("ctrl+alt+v")
        assert hotkey._mod_sets == before_mods
        assert hotkey._held == before_held


class TestClickCapture:
    def test_nothing_is_captured_by_default(self):
        assert hotkey.click_capture_active() is False

    def test_cancelling_with_nothing_captured_is_safe(self):
        hotkey.cancel_click_capture()       # must not raise
        assert hotkey.click_capture_active() is False

    def test_a_null_callback_installs_nothing(self):
        assert hotkey.capture_next_click(None) is False

    def test_a_button_up_fires_the_callback_off_the_hook_thread(self, monkeypatch):
        """The hook itself must return immediately: everything after this
        point takes hundreds of ms, and a low-level hook that blocks that long
        is torn down by Windows."""
        fired = threading.Event()
        monkeypatch.setattr(hotkey, "_click_cb", lambda: fired.set())
        result = hotkey._click_hook_proc(0, hotkey._WM_LBUTTONUP, None)
        assert isinstance(result, int)
        assert fired.wait(2.0)
        assert hotkey.click_capture_active() is False   # one shot only

    def test_a_raising_callback_never_escapes_the_hook(self, monkeypatch):
        done = threading.Event()

        def _boom():
            done.set()
            raise RuntimeError("callback exploded")

        monkeypatch.setattr(hotkey, "_click_cb", _boom)
        hotkey._click_hook_proc(0, hotkey._WM_LBUTTONUP, None)
        assert done.wait(2.0)

    def test_moves_and_button_downs_are_ignored(self, monkeypatch):
        fired = threading.Event()
        monkeypatch.setattr(hotkey, "_click_cb", lambda: fired.set())
        hotkey._click_hook_proc(0, 0x0200, None)    # WM_MOUSEMOVE
        hotkey._click_hook_proc(0, 0x0201, None)    # WM_LBUTTONDOWN
        assert not fired.wait(0.2)
        assert hotkey.click_capture_active() is True
        monkeypatch.setattr(hotkey, "_click_cb", None)

    def test_a_negative_ncode_is_passed_straight_through(self, monkeypatch):
        fired = threading.Event()
        monkeypatch.setattr(hotkey, "_click_cb", lambda: fired.set())
        hotkey._click_hook_proc(-1, hotkey._WM_LBUTTONUP, None)
        assert not fired.wait(0.2)
        monkeypatch.setattr(hotkey, "_click_cb", None)


# ---------------------------------------------------------------------------
# The reserved place key (one key, no modifiers)
# ---------------------------------------------------------------------------

class TestPlaceKey:
    """The key places into the field that already has focus. Arming is the
    fallback, not the default, because by the time the user hits the key they
    have already clicked where they want the text."""

    def test_nothing_stashed_says_so_and_inserts_nothing(self, place):
        fp, _, _, _, fi = place
        assert main.place_key_pressed() == "empty"
        assert fp.toasts == [{"message": "Nothing to place yet", "kind": "info"}]
        assert fi.calls == []
        assert not main.place_mode_armed()

    def test_a_consumed_item_counts_as_nothing_stashed(self, place):
        fp, _, _, _, fi = place
        stash.put("already placed", 4242)
        stash.mark_consumed()
        assert main.place_key_pressed() == "empty"
        assert fp.toasts[0]["message"] == "Nothing to place yet"
        assert fi.calls == []

    def test_a_blank_stash_item_counts_as_nothing_stashed(self, place):
        fp, _, _, _, fi = place
        stash.put("   ", 4242)
        assert main.place_key_pressed() == "empty"
        assert fi.calls == []

    def test_a_stashed_item_goes_straight_into_the_focused_field(self, place):
        fp, fh, _, _, fi = place
        stash.put("remember the milk", 4242)
        assert main.place_key_pressed() == "placed"
        # hwnd 0: resolved at press time, which is what "put it HERE" means.
        assert fi.calls == [("remember the milk", 0)]
        assert not main.place_mode_armed()
        assert fh.capture_calls == 0        # no overlay, no mouse hook
        assert fp.overlays == []
        assert not stash.has_unconsumed()   # confirmed landed

    def test_a_confident_refusal_arms_place_mode_instead(self, place):
        """Only a CONFIDENT "there is no field here" earns the overlay."""
        fp, fh, _, _, fi = place
        fi.status = main._REFUSED_NOT_EDITABLE
        stash.put("parked", 4242)
        assert main.place_key_pressed() == "armed"
        assert main.place_mode_armed()
        assert fp.overlays == ["parked"]
        assert fh.capture_calls == 1
        assert stash.has_unconsumed()       # nothing landed, nothing consumed

    def test_a_refusal_gives_one_notice_not_two(self, place):
        """The overlay IS the notice. A recovery panel on top of it is the
        double-notice the design rules out."""
        fp, _, _, _, fi = place
        fi.status = main._REFUSED_NOT_EDITABLE
        stash.put("parked", 4242)
        main.place_key_pressed()
        assert fp.recoveries == []
        assert fp.toasts == []

    def test_a_real_miss_still_escalates_and_does_not_arm(self, place):
        """Clipboard fallback is evidence the text missed, not evidence that
        there was no field, so the recovery panel is still the right answer."""
        fp, _, _, _, fi = place
        fi.status = "clipboard"
        stash.put("parked", 4242)
        assert main.place_key_pressed() == "placed"
        assert len(fp.recoveries) == 1
        assert not main.place_mode_armed()

    def test_an_unconfirmed_insert_stays_silent(self, place):
        """Terminals, RDP and anything with no accessibility layer land here."""
        fp, _, _, _, fi = place
        fi.status = "inserted_unconfirmed"
        stash.put("parked", 4242)
        assert main.place_key_pressed() == "placed"
        assert fp.recoveries == []
        assert fp.toasts == []
        assert stash.has_unconsumed()

    def test_pressing_it_again_while_armed_disarms(self, place):
        fp, _, _, _, fi = place
        fi.status = main._REFUSED_NOT_EDITABLE
        stash.put("parked", 4242)
        main.place_key_pressed()
        assert main.place_key_pressed() == "disarmed"
        assert not main.place_mode_armed()
        assert fp.overlays[-1] is None
        assert len(fi.calls) == 1       # the second press placed nothing

    def test_the_chord_and_the_key_share_one_armed_state(self, place):
        _, _, _, _, fi = place
        fi.status = main._REFUSED_NOT_EDITABLE
        stash.put("parked", 4242)
        main.place_key_pressed()
        assert main.toggle_place_mode() == "disarmed"


class TestInsertEscalationOptOut:
    """escalate=False exists for exactly one caller: the place key, which has
    its own answer to a refusal."""

    def test_it_shows_nothing(self, place):
        fp, _, _, _, fi = place
        fi.status = "clipboard"
        assert main.insert_text_now("parked", escalate=False) == "clipboard"
        assert fp.recoveries == []

    def test_it_still_consumes_a_confirmed_insert(self, place):
        stash.put("parked", 4242)
        main.insert_text_now("parked", escalate=False)
        assert not stash.has_unconsumed()

    def test_escalation_is_still_the_default(self, place):
        fp, _, _, _, fi = place
        fi.status = "clipboard"
        main.insert_text_now("parked")
        assert len(fp.recoveries) == 1


# ---------------------------------------------------------------------------
# hotkey.py: the dedicated WH_KEYBOARD_LL hook behind the reserved key
#
# All headless. The scan-code/extended-flag decision is a pure function and
# the hook proc is driven with a real KBDLLHOOKSTRUCT, so nothing here needs a
# human to press anything.
# ---------------------------------------------------------------------------

def _kbd_event(scan_code, flags=0, vk=110):
    """A real KBDLLHOOKSTRUCT, so the hook proc's ctypes.cast is exercised
    rather than mocked away."""
    return hotkey._KBDLLHOOKSTRUCT(vkCode=vk, scanCode=scan_code, flags=flags,
                                   time=0, dwExtraInfo=0)


class TestReservedKeyMatching:
    """The Delete regression lives here. The numpad "." and the nav-cluster
    Delete share base scan code 83; only the extended flag tells them apart."""

    def test_the_numpad_dot_matches(self):
        spec = hotkey.RESERVED_KEYS["numpad_decimal"]
        assert hotkey.reserved_key_matches(83, 0, spec) is True

    def test_the_real_delete_key_is_never_matched(self):
        """Swallowing scan 83 with the extended bit set would break Delete
        system-wide. This is the regression test for that."""
        spec = hotkey.RESERVED_KEYS["numpad_decimal"]
        assert hotkey.reserved_key_matches(83, hotkey._LLKHF_EXTENDED, spec) is False

    def test_alt_down_does_not_stop_a_match(self):
        """Bit 5 (0x20, alt-down) rides along on perfectly real presses and
        must not stop the match. Bit 0 is the extended flag and must."""
        spec = hotkey.RESERVED_KEYS["numpad_decimal"]
        assert hotkey.reserved_key_matches(83, 0x20, spec) is True
        assert hotkey.reserved_key_matches(83, 0x21, spec) is False

    def test_our_own_injected_capital_S_is_never_a_place_key(self):
        """The 2026-08-23 repeat storm. inject.py types via SendInput
        KEYEVENTF_UNICODE, which carries the UTF-16 code unit in wScan, and
        ord("S") == 83 — the numpad "." scan code. Matching that made the app
        swallow the S out of its own insert (so verification failed and the
        stash stayed unconsumed) and re-fire place mode, typing the same text
        again. One press produced 14 inserts."""
        spec = hotkey.RESERVED_KEYS["numpad_decimal"]
        assert hotkey.reserved_key_matches(83, hotkey._LLKHF_INJECTED, spec) is False
        assert hotkey.reserved_key_matches(83, 0x30, spec) is False

    def test_no_reserved_key_can_be_hit_by_our_own_typing(self):
        """Every reserved key's scan code collides with a printable ASCII
        character (78='N', 74='J', 55='7', 83='S', 82='R', 69='E'), so the
        injected gate has to hold for all of them, not just the default."""
        for name, spec in hotkey.RESERVED_KEYS.items():
            scan = spec[0]
            assert hotkey.reserved_key_matches(
                scan, hotkey._LLKHF_INJECTED, spec) is False, name

    def test_a_different_scan_code_never_matches(self):
        spec = hotkey.RESERVED_KEYS["numpad_decimal"]
        for scan in (82, 78, 74, 55, 69, 30):
            assert hotkey.reserved_key_matches(scan, 0, spec) is False

    def test_numpad_star_is_told_apart_from_printscreen(self):
        spec = hotkey.RESERVED_KEYS["numpad_multiply"]
        assert hotkey.reserved_key_matches(55, 0, spec) is True
        assert hotkey.reserved_key_matches(55, hotkey._LLKHF_EXTENDED, spec) is False

    def test_no_spec_matches_nothing(self):
        assert hotkey.reserved_key_matches(83, 0, None) is False
        assert hotkey.reserved_key_matches(83, 0, ()) is False

    def test_every_supported_name_carries_a_scan_code_and_a_flag(self):
        assert "numpad_decimal" in hotkey.RESERVED_KEYS
        for name, (scan, extended) in hotkey.RESERVED_KEYS.items():
            assert isinstance(scan, int) and scan > 0, name
            assert isinstance(extended, bool), name

    def test_no_two_names_claim_the_same_key(self):
        specs = list(hotkey.RESERVED_KEYS.values())
        assert len(set(specs)) == len(specs)


class TestReservedKeyDebounce:
    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        monkeypatch.setattr(hotkey, "_reserved_last_fire", 0.0)

    def test_the_window_is_300ms(self):
        assert hotkey._RESERVED_DEBOUNCE_SECONDS == 0.30

    def test_the_first_press_always_fires(self):
        assert hotkey._reserved_debounced(1000.0) is False

    def test_auto_repeat_inside_the_window_is_swallowed(self):
        assert hotkey._reserved_debounced(1000.0) is False
        assert hotkey._reserved_debounced(1000.05) is True
        assert hotkey._reserved_debounced(1000.29) is True

    def test_a_deliberate_second_press_fires(self):
        hotkey._reserved_debounced(1000.0)
        assert hotkey._reserved_debounced(1000.31) is False
        assert hotkey._reserved_debounced(1000.62) is False

    def test_a_swallowed_repeat_does_not_extend_the_window(self):
        """Holding the key down must not push the next real press out."""
        hotkey._reserved_debounced(1000.0)
        hotkey._reserved_debounced(1000.2)      # repeat, swallowed
        assert hotkey._reserved_debounced(1000.31) is False


class TestReservedKeyHookProc:
    @pytest.fixture(autouse=True)
    def _reserved(self, monkeypatch):
        monkeypatch.setattr(hotkey, "_reserved_last_fire", 0.0)
        monkeypatch.setattr(hotkey, "_reserved_spec",
                            hotkey.RESERVED_KEYS["numpad_decimal"])
        monkeypatch.setattr(hotkey, "_reserved_cb", None)
        monkeypatch.setattr(hotkey, "_reserved_down_swallowed", False)
        # The hook proc must never create a thread, so it signals a worker that
        # is already running. Start a real one here: these tests then exercise
        # the production path end to end rather than a stubbed shortcut.
        signal, stop = threading.Event(), threading.Event()
        monkeypatch.setattr(hotkey, "_reserved_signal", signal)
        monkeypatch.setattr(hotkey, "_reserved_stop", stop)
        worker = threading.Thread(
            target=hotkey._reserved_worker_loop,
            args=(signal, stop, lambda: (hotkey._reserved_cb or (lambda: None))()),
            daemon=True)
        worker.start()
        yield
        stop.set()
        signal.set()
        worker.join(timeout=1.0)

    def test_the_key_down_is_swallowed_and_fires_off_the_hook_thread(self, monkeypatch):
        fired = threading.Event()
        monkeypatch.setattr(hotkey, "_reserved_cb", lambda: fired.set())
        ev = _kbd_event(83)
        assert hotkey._reserved_hook_proc(0, hotkey._WM_KEYDOWN, ctypes.addressof(ev)) == 1
        assert fired.wait(2.0)

    def test_the_key_up_is_swallowed_when_its_down_was(self, monkeypatch):
        """Both halves of a press we own are eaten, so no stray key-up reaches
        the focused app."""
        fired = threading.Event()
        monkeypatch.setattr(hotkey, "_reserved_cb", lambda: fired.set())
        ev = _kbd_event(83)
        assert hotkey._reserved_hook_proc(0, hotkey._WM_KEYDOWN, ctypes.addressof(ev)) == 1
        assert hotkey._reserved_hook_proc(0, hotkey._WM_KEYUP, ctypes.addressof(ev)) == 1

    def test_an_unpaired_key_up_passes_through(self, monkeypatch):
        """THE STUCK-KEY REGRESSION. If the hook is installed between a key
        going down and coming up, swallowing that up leaves the focused app
        believing the key is still held. Balu reported exactly that symptom
        ("it looks like it's being held even though I'm not"), so an up whose
        down we did not swallow must reach the app."""
        fired = threading.Event()
        monkeypatch.setattr(hotkey, "_reserved_cb", lambda: fired.set())
        monkeypatch.setattr(hotkey, "_reserved_down_swallowed", False)
        ev = _kbd_event(83)
        assert hotkey._reserved_hook_proc(0, hotkey._WM_KEYUP, ctypes.addressof(ev)) != 1
        assert not fired.wait(0.2)

    def test_auto_repeat_fires_once_per_press(self, monkeypatch):
        """A held key repeats WM_KEYDOWN with no keyup in between. The 300ms
        time debounce alone let a held key fire every 300ms; only the leading
        edge of a press is a press."""
        fires = []
        monkeypatch.setattr(hotkey, "_reserved_cb", lambda: fires.append(1))
        ev = _kbd_event(83)
        for _ in range(20):
            assert hotkey._reserved_hook_proc(0, hotkey._WM_KEYDOWN, ctypes.addressof(ev)) == 1
            monkeypatch.setattr(hotkey, "_reserved_last_fire", 0.0)  # defeat the time debounce
        time.sleep(0.4)
        assert len(fires) == 1
        assert hotkey._reserved_hook_proc(0, hotkey._WM_KEYUP, ctypes.addressof(ev)) == 1

    def test_our_own_injected_text_never_fires_the_key(self, monkeypatch):
        """THE REPEAT-STORM REGRESSION. inject.py types via SendInput
        KEYEVENTF_UNICODE with the code unit in wScan, and ord("S") == 83.
        A matching injected event both swallowed the S out of the insert and
        re-fired place mode, which typed the same text again."""
        fired = threading.Event()
        monkeypatch.setattr(hotkey, "_reserved_cb", lambda: fired.set())
        ev = _kbd_event(83, flags=hotkey._LLKHF_INJECTED)
        assert hotkey._reserved_hook_proc(0, hotkey._WM_KEYDOWN, ctypes.addressof(ev)) != 1
        assert hotkey._reserved_hook_proc(0, hotkey._WM_KEYUP, ctypes.addressof(ev)) != 1
        assert not fired.wait(0.3)

    def test_the_hook_proc_creates_no_threads(self, monkeypatch):
        """A low-level keyboard hook blocks ALL keyboard input on the machine
        until it returns, and Windows silently removes one that overruns
        LowLevelHooksTimeout. Creating a thread in there under GIL contention
        is exactly that risk, and was the original cause of the freeze."""
        import threading as _t
        before = _t.active_count()
        ev = _kbd_event(83)
        for _ in range(50):
            hotkey._reserved_hook_proc(0, hotkey._WM_KEYDOWN, ctypes.addressof(ev))
            hotkey._reserved_hook_proc(0, hotkey._WM_KEYUP, ctypes.addressof(ev))
        assert _t.active_count() <= before + 1

    def test_the_syskey_variants_are_handled_the_same(self, monkeypatch):
        fired = threading.Event()
        monkeypatch.setattr(hotkey, "_reserved_cb", lambda: fired.set())
        ev = _kbd_event(83)
        assert hotkey._reserved_hook_proc(0, hotkey._WM_SYSKEYDOWN, ctypes.addressof(ev)) == 1
        assert fired.wait(2.0)
        assert hotkey._reserved_hook_proc(0, hotkey._WM_SYSKEYUP, ctypes.addressof(ev)) == 1

    def test_the_real_delete_key_passes_straight_through(self, monkeypatch):
        fired = threading.Event()
        monkeypatch.setattr(hotkey, "_reserved_cb", lambda: fired.set())
        ev = _kbd_event(83, flags=hotkey._LLKHF_EXTENDED, vk=0x2E)
        for msg in (hotkey._WM_KEYDOWN, hotkey._WM_KEYUP):
            assert hotkey._reserved_hook_proc(0, msg, ctypes.addressof(ev)) != 1
        assert not fired.wait(0.2)

    def test_every_other_key_passes_straight_through(self, monkeypatch):
        fired = threading.Event()
        monkeypatch.setattr(hotkey, "_reserved_cb", lambda: fired.set())
        ev = _kbd_event(30)     # "a"
        assert hotkey._reserved_hook_proc(0, hotkey._WM_KEYDOWN, ctypes.addressof(ev)) != 1
        assert not fired.wait(0.2)

    def test_nothing_reserved_means_nothing_swallowed(self, monkeypatch):
        monkeypatch.setattr(hotkey, "_reserved_spec", None)
        ev = _kbd_event(83)
        assert hotkey._reserved_hook_proc(0, hotkey._WM_KEYDOWN, ctypes.addressof(ev)) != 1

    def test_a_negative_ncode_passes_straight_through(self, monkeypatch):
        fired = threading.Event()
        monkeypatch.setattr(hotkey, "_reserved_cb", lambda: fired.set())
        ev = _kbd_event(83)
        assert hotkey._reserved_hook_proc(-1, hotkey._WM_KEYDOWN, ctypes.addressof(ev)) != 1
        assert not fired.wait(0.2)

    def test_a_garbage_lparam_never_raises(self, monkeypatch):
        """A hook proc that throws is torn down by Windows, taking every
        keystroke after it with it."""
        monkeypatch.setattr(hotkey, "_reserved_cb", lambda: None)
        assert isinstance(hotkey._reserved_hook_proc(0, hotkey._WM_KEYDOWN, None), int)

    def test_a_raising_callback_never_escapes_the_hook(self, monkeypatch):
        done = threading.Event()

        def _boom():
            done.set()
            raise RuntimeError("callback exploded")

        monkeypatch.setattr(hotkey, "_reserved_cb", _boom)
        ev = _kbd_event(83)
        assert hotkey._reserved_hook_proc(0, hotkey._WM_KEYDOWN, ctypes.addressof(ev)) == 1
        assert done.wait(2.0)

    def test_auto_repeat_fires_once_but_is_swallowed_every_time(self, monkeypatch):
        calls = []
        first = threading.Event()
        monkeypatch.setattr(hotkey, "_reserved_cb",
                            lambda: (calls.append(1), first.set()))
        ev = _kbd_event(83)
        for _ in range(6):
            assert hotkey._reserved_hook_proc(0, hotkey._WM_KEYDOWN, ctypes.addressof(ev)) == 1
        assert first.wait(2.0)
        time.sleep(0.15)
        assert len(calls) == 1


class TestReservedKeyRegistration:
    def test_an_unknown_name_reserves_nothing(self):
        assert hotkey.register_reserved_key("f13", lambda: None) is False
        assert hotkey.reserved_key_active() is False

    def test_a_blank_name_reserves_nothing(self):
        assert hotkey.register_reserved_key("", lambda: None) is False
        assert hotkey.register_reserved_key(None, lambda: None) is False

    def test_a_null_callback_reserves_nothing(self):
        assert hotkey.register_reserved_key("numpad_decimal", None) is False
        assert hotkey.reserved_key_active() is False

    def test_unregistering_with_nothing_reserved_is_safe(self):
        hotkey.unregister_reserved_key()        # must not raise
        assert hotkey.reserved_key_active() is False

    def test_simulating_a_press_with_nothing_reserved_does_nothing(self):
        assert hotkey.simulate_reserved_key_press() is False

    def test_install_fire_and_release(self, monkeypatch):
        """Installs the real hook. The key is reserved only for the
        milliseconds between these two calls."""
        monkeypatch.setattr(hotkey, "_reserved_last_fire", 0.0)
        fired = threading.Event()
        if not hotkey.register_reserved_key("numpad_decimal", lambda: fired.set()):
            pytest.skip("Windows would not give this process a keyboard hook")
        try:
            assert hotkey.reserved_key_active() is True
            assert hotkey.reserved_key_name() == "numpad_decimal"
            assert hotkey._reserved_spec == (83, False)
            assert hotkey._reserved_proc is not None    # kept alive, or Windows calls freed memory
            assert hotkey.simulate_reserved_key_press() is True
            assert fired.wait(2.0)
        finally:
            hotkey.unregister_reserved_key()
        assert hotkey.reserved_key_active() is False
        assert hotkey.reserved_key_name() == ""
        assert hotkey.simulate_reserved_key_press() is False
