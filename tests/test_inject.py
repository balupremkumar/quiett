"""Tests for inject.py — verifies clipboard is set and target window is validated.

inject.py uses SendInput (via ctypes) rather than win32api.keybd_event, so we
verify behaviour at the clipboard + hwnd-validation boundary, not the low-level
keystroke API which is exercised via ctypes calls that aren't easily mockable.
"""
import sys
from unittest.mock import MagicMock

import pytest

_win32gui  = MagicMock()
_win32api  = MagicMock()
_pyperclip = MagicMock()

sys.modules["win32gui"]  = _win32gui
sys.modules["win32api"]  = _win32api
sys.modules["win32con"]  = MagicMock()
sys.modules["pyperclip"] = _pyperclip
sys.modules.pop("inject", None)

import inject  # noqa: E402


class _FakeTime:
    """Stand-in for inject's `time` module: sleeps advance a fake clock instead of
    the wall clock, so timeout loops (the 4s modifier watcher) finish instantly
    and the sleep durations themselves become assertable."""

    def __init__(self, recorder: list | None = None):
        self._now = 1000.0
        self.slept: list = []
        self._recorder = recorder

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        if self._recorder is not None:
            self._recorder.append(("sleep", seconds))
        self._now += seconds

    def monotonic(self) -> float:
        return self._now


@pytest.fixture(autouse=True)
def reset_mocks():
    _win32gui.reset_mock()
    _win32api.reset_mock()
    _pyperclip.reset_mock()
    # reset_mock() keeps side_effect; a leftover scripted foreground sequence
    # would raise StopIteration inside an unrelated test.
    _win32gui.GetForegroundWindow.side_effect = None


class TestInjectText:
    def test_zero_hwnd_falls_back_to_clipboard(self, monkeypatch):
        """No target window: text must land on the clipboard, never be dropped."""
        copied, infos = [], []
        monkeypatch.setattr(inject, "_clipboard_set_text", lambda t: copied.append(t) or True)
        monkeypatch.setattr(inject, "_notify_info", lambda m: infos.append(m))
        inject.inject_text("hello", 0)
        assert copied == ["hello"]
        assert infos

    def test_invalid_window_falls_back_to_clipboard(self, monkeypatch):
        _win32gui.IsWindow.return_value = False
        copied = []
        monkeypatch.setattr(inject, "_clipboard_set_text", lambda t: copied.append(t) or True)
        monkeypatch.setattr(inject, "_notify_info", lambda m: None)
        inject.inject_text("hello", 9999)
        assert copied == ["hello"]

    def test_default_path_types_text(self, monkeypatch):
        """Default (non-terminal) path types text via Unicode SendInput."""
        _win32gui.IsWindow.return_value = True
        typed = []
        monkeypatch.setattr(inject, "_decide_method", lambda h: "type")
        monkeypatch.setattr(inject, "_is_higher_integrity_target", lambda h: False)
        monkeypatch.setattr(inject, "_force_foreground", lambda h: None)
        monkeypatch.setattr(inject, "_wait_modifiers_released", lambda timeout_ms=400: True)
        monkeypatch.setattr(inject, "_send_unicode_text",
                            lambda t, batch=32, batch_delay_ms=2: typed.append(t) or len(t))
        inject.inject_text("new text", 1234)
        assert typed == ["new text"]

    def test_terminal_path_uses_clipboard(self, monkeypatch):
        """Terminal targets use Ctrl+Shift+V clipboard paste, not typing."""
        _win32gui.IsWindow.return_value = True
        clipboard_calls = []
        keystroke_calls = []
        monkeypatch.setattr(inject, "_decide_method", lambda h: "ctrl_shift_v")
        monkeypatch.setattr(inject, "_is_higher_integrity_target", lambda h: False)
        monkeypatch.setattr(inject, "_force_foreground", lambda h: None)
        monkeypatch.setattr(inject, "_wait_modifiers_released", lambda timeout_ms=400: True)
        monkeypatch.setattr(inject, "_clipboard_get_text", lambda: "old")
        monkeypatch.setattr(inject, "_clipboard_set_text",
                            lambda t: clipboard_calls.append(t) or True)
        monkeypatch.setattr(inject, "_send_keystroke",
                            lambda mods, key: keystroke_calls.append((mods, key)) or 4)
        # inject_text() spawns a real background thread to restore the clipboard
        # after a delay. If it's left to run for real, it fires after this test
        # (and its monkeypatches) have already torn down, hitting the *actual*
        # Windows clipboard via raw ctypes from a stale thread — flaky and has
        # been observed to crash the test process. Make Thread.start() a no-op
        # so the restore never actually runs; this test only cares about the
        # synchronous clipboard-set + keystroke calls above.
        monkeypatch.setattr(inject.threading, "Thread",
                            lambda target=None, daemon=None: MagicMock(start=lambda: None))
        inject.inject_text("ls -la", 1234)
        assert clipboard_calls == ["ls -la"]
        assert keystroke_calls and keystroke_calls[0][1] == inject._VK_V

    def test_configure_sets_restore_delay(self):
        inject.configure(restore_delay_ms=300)
        assert inject._restore_delay_ms == 300

    def test_configure_sets_rdp_clipboard_timings(self):
        try:
            inject.configure(restore_delay_ms=150, rdp_clipboard_settle_ms=400,
                             rdp_clipboard_restore_delay_ms=5000)
            assert inject._rdp_clipboard_settle_ms == 400
            assert inject._rdp_clipboard_restore_delay_ms == 5000
        finally:
            inject.configure(restore_delay_ms=150, rdp_clipboard_settle_ms=250,
                             rdp_clipboard_restore_delay_ms=3000)

    def test_rdp_target_settles_before_ctrl_v(self, monkeypatch):
        """rdpclip propagates the format list asynchronously — Ctrl+V must not be
        sent until the settle delay has elapsed, or the remote pastes stale text."""
        _win32gui.IsWindow.return_value = True
        _win32gui.GetForegroundWindow.return_value = 1234
        calls = []
        clock = _FakeTime(recorder=calls)
        monkeypatch.setattr(inject, "time", clock)
        monkeypatch.setattr(inject, "_decide_method", lambda h: "ctrl_v")
        monkeypatch.setattr(inject, "_is_rdp", lambda h: True)
        monkeypatch.setattr(inject, "_is_higher_integrity_target", lambda h: False)
        monkeypatch.setattr(inject, "_force_foreground", lambda h: None)
        monkeypatch.setattr(inject, "_wait_modifiers_released", lambda timeout_ms=400: True)
        monkeypatch.setattr(inject, "_flush_all_modifiers", lambda force=False: None)
        monkeypatch.setattr(inject, "_clipboard_snapshot", lambda: [])
        monkeypatch.setattr(inject, "_clipboard_set_text",
                            lambda t: calls.append(("set_text", t)) or True)
        monkeypatch.setattr(inject, "_send_keystroke",
                            lambda mods, key: calls.append(("keystroke", key)) or 4)
        monkeypatch.setattr(inject.threading, "Thread",
                            lambda target=None, daemon=None: MagicMock(start=lambda: None))
        inject.configure(restore_delay_ms=150, rdp_clipboard_settle_ms=250,
                         rdp_clipboard_restore_delay_ms=3000)
        assert inject.inject_text("remote text", 1234) == inject.INSERTED
        i = [n for n, c in enumerate(calls) if c[0] == "keystroke"][0]
        assert calls[i - 1] == ("sleep", 0.25)
        assert ("set_text", "remote text") in calls[:i]


class TestInjectStatus:
    """inject_text reports what actually happened so callers can offer a retry
    instead of the text silently ending up only on the clipboard."""

    @pytest.fixture
    def live_target(self, monkeypatch):
        """A valid, non-elevated target with modifiers released."""
        _win32gui.IsWindow.return_value = True
        monkeypatch.setattr(inject, "_is_higher_integrity_target", lambda h: False)
        monkeypatch.setattr(inject, "_force_foreground", lambda h: None)
        monkeypatch.setattr(inject, "_wait_modifiers_released", lambda timeout_ms=400: True)
        monkeypatch.setattr(inject, "_notify_failure", lambda m: None)
        monkeypatch.setattr(inject, "_notify_info", lambda m: None)
        # Never let the real clipboard-restore thread outlive the test.
        monkeypatch.setattr(inject.threading, "Thread",
                            lambda target=None, daemon=None: MagicMock(start=lambda: None))

    def test_empty_text_is_a_no_op(self):
        assert inject.inject_text("", 1234) == inject.INSERTED

    def test_clipboard_only_mode_reports_clipboard(self, monkeypatch):
        monkeypatch.setattr(inject, "_paste_mode", "clipboard_only")
        monkeypatch.setattr(inject, "_clipboard_set_text", lambda t: True)
        monkeypatch.setattr(inject, "_notify_info", lambda m: None)
        assert inject.inject_text("hello", 1234) == inject.CLIPBOARD

    def test_clipboard_only_mode_copy_failure_reports_failed(self, monkeypatch):
        monkeypatch.setattr(inject, "_paste_mode", "clipboard_only")
        monkeypatch.setattr(inject, "_clipboard_set_text", lambda t: False)
        monkeypatch.setattr(inject, "_notify_failure", lambda m: None)
        assert inject.inject_text("hello", 1234) == inject.FAILED

    def test_no_target_reports_clipboard(self, monkeypatch):
        monkeypatch.setattr(inject, "_clipboard_set_text", lambda t: True)
        monkeypatch.setattr(inject, "_notify_info", lambda m: None)
        assert inject.inject_text("hello", 0) == inject.CLIPBOARD

    def test_no_target_and_copy_failure_reports_failed(self, monkeypatch):
        monkeypatch.setattr(inject, "_clipboard_set_text", lambda t: False)
        monkeypatch.setattr(inject, "_notify_failure", lambda m: None)
        assert inject.inject_text("hello", 0) == inject.FAILED

    def test_elevated_target_copies_instead_of_dropping(self, monkeypatch):
        """Regression: the UAC branch used to return without copying, so the
        dictation was lost entirely."""
        _win32gui.IsWindow.return_value = True
        copied, failures = [], []
        monkeypatch.setattr(inject, "_is_higher_integrity_target", lambda h: True)
        monkeypatch.setattr(inject, "_decide_method", lambda h: "type")
        monkeypatch.setattr(inject, "_clipboard_set_text", lambda t: copied.append(t) or True)
        monkeypatch.setattr(inject, "_notify_failure", lambda m: failures.append(m))
        assert inject.inject_text("secret", 1234) == inject.CLIPBOARD
        assert copied == ["secret"]
        assert failures

    def test_elevated_target_copy_failure_reports_failed(self, monkeypatch):
        _win32gui.IsWindow.return_value = True
        monkeypatch.setattr(inject, "_is_higher_integrity_target", lambda h: True)
        monkeypatch.setattr(inject, "_decide_method", lambda h: "type")
        monkeypatch.setattr(inject, "_clipboard_set_text", lambda t: False)
        monkeypatch.setattr(inject, "_notify_failure", lambda m: None)
        assert inject.inject_text("secret", 1234) == inject.FAILED

    def test_modifiers_still_held_reports_clipboard(self, monkeypatch):
        _win32gui.IsWindow.return_value = True
        copied = []
        monkeypatch.setattr(inject, "_is_higher_integrity_target", lambda h: False)
        monkeypatch.setattr(inject, "_decide_method", lambda h: "type")
        monkeypatch.setattr(inject, "_force_foreground", lambda h: None)
        monkeypatch.setattr(inject, "_wait_modifiers_released", lambda timeout_ms=400: False)
        monkeypatch.setattr(inject, "_clipboard_set_text", lambda t: copied.append(t) or True)
        monkeypatch.setattr(inject, "_notify_failure", lambda m: None)
        assert inject.inject_text("hello", 1234) == inject.CLIPBOARD
        assert copied == ["hello"]

    def test_typed_text_reports_inserted(self, monkeypatch, live_target):
        monkeypatch.setattr(inject, "_decide_method", lambda h: "type")
        monkeypatch.setattr(inject, "_send_unicode_text",
                            lambda t, batch=32, batch_delay_ms=2: len(t))
        assert inject.inject_text("hello", 1234) == inject.INSERTED

    def test_successful_paste_reports_inserted(self, monkeypatch, live_target):
        monkeypatch.setattr(inject, "_decide_method", lambda h: "ctrl_v")
        monkeypatch.setattr(inject, "_clipboard_snapshot", lambda: [])
        monkeypatch.setattr(inject, "_clipboard_set_text", lambda t: True)
        monkeypatch.setattr(inject, "_send_keystroke", lambda mods, key: 4)
        monkeypatch.setattr(inject, "_is_rdp", lambda h: False)
        assert inject.inject_text("hello", 1234) == inject.INSERTED

    def test_blocked_paste_reports_clipboard(self, monkeypatch, live_target):
        """SendInput blocked and WM_PASTE refused: text stays on the clipboard."""
        monkeypatch.setattr(inject, "_decide_method", lambda h: "ctrl_v")
        monkeypatch.setattr(inject, "_clipboard_snapshot", lambda: [])
        monkeypatch.setattr(inject, "_clipboard_set_text", lambda t: True)
        monkeypatch.setattr(inject, "_send_keystroke", lambda mods, key: 0)
        monkeypatch.setattr(inject, "_try_wm_paste", lambda h: False)
        assert inject.inject_text("hello", 1234) == inject.CLIPBOARD

    def test_clipboard_set_failure_on_paste_path_reports_failed(self, monkeypatch, live_target):
        monkeypatch.setattr(inject, "_decide_method", lambda h: "ctrl_v")
        monkeypatch.setattr(inject, "_clipboard_snapshot", lambda: [])
        monkeypatch.setattr(inject, "_clipboard_set_text", lambda t: False)
        assert inject.inject_text("hello", 1234) == inject.FAILED

    def test_submit_skips_enter_when_insert_did_not_land(self, monkeypatch):
        """No Enter into a window the text never reached."""
        sent = []
        monkeypatch.setattr(inject, "inject_text", lambda t, h: inject.CLIPBOARD)
        monkeypatch.setattr(inject, "_send_inputs", lambda inputs: sent.append(inputs))
        assert inject.inject_text_and_submit("hello", 1234) == inject.CLIPBOARD
        assert sent == []

    def test_submit_forwards_enter_after_a_real_insert(self, monkeypatch):
        sent = []
        monkeypatch.setattr(inject, "inject_text", lambda t, h: inject.INSERTED)
        monkeypatch.setattr(inject, "_wait_modifiers_released", lambda timeout_ms=400: True)
        monkeypatch.setattr(inject, "_flush_all_modifiers", lambda force=False: None)
        monkeypatch.setattr(inject, "_is_rdp", lambda h: False)
        monkeypatch.setattr(inject, "_send_inputs", lambda inputs: sent.append(inputs))
        assert inject.inject_text_and_submit("hello", 1234) == inject.INSERTED
        assert sent


class TestCaptureForeground:
    """Own-app windows (tray icon message window, preview, dashboard subprocess)
    must never be captured as paste targets."""

    def _fake_user32(self, pid_value: int):
        fake = MagicMock()

        def fake_gwtpi(hwnd, byref_pid):
            byref_pid._obj.value = pid_value
            return 1

        fake.GetWindowThreadProcessId.side_effect = fake_gwtpi
        return fake

    def test_own_process_window_returns_zero(self, monkeypatch):
        import os
        _win32gui.GetForegroundWindow.return_value = 4242
        monkeypatch.setattr(inject, "_user32", self._fake_user32(os.getpid()))
        assert inject.capture_foreground() == 0

    def test_dashboard_subprocess_returns_zero(self, monkeypatch):
        _win32gui.GetForegroundWindow.return_value = 4242
        _win32gui.GetWindowText.return_value = "Quiett"
        monkeypatch.setattr(inject, "_user32", self._fake_user32(99999))
        monkeypatch.setattr(inject, "_get_exe_name", lambda h: "pythonw3.13.exe")
        assert inject.capture_foreground() == 0

    def test_foreign_window_passes_through(self, monkeypatch):
        _win32gui.GetForegroundWindow.return_value = 4242
        _win32gui.GetWindowText.return_value = "Untitled - Notepad"
        monkeypatch.setattr(inject, "_user32", self._fake_user32(99999))
        monkeypatch.setattr(inject, "_get_exe_name", lambda h: "notepad.exe")
        assert inject.capture_foreground() == 4242

    def test_quiet_capture_does_not_warn(self, monkeypatch):
        """The hotkey-down capture is speculative — our own window in front
        there is normal and must not log a paste-target warning."""
        import os
        warnings = []
        monkeypatch.setattr(inject, "warn", lambda *a: warnings.append(a))
        _win32gui.GetForegroundWindow.return_value = 4242
        monkeypatch.setattr(inject, "_user32", self._fake_user32(os.getpid()))
        assert inject.capture_foreground(quiet=True) == 0
        assert warnings == []
        assert inject.capture_foreground() == 0
        assert warnings  # the real capture still reports it


class TestIsUsableTarget:
    """Guards the hotkey-down fallback target: a remembered hwnd is only
    reused while it is still a live, visible window that isn't ours."""

    def test_zero_is_not_usable(self):
        assert inject.is_usable_target(0) is False

    def test_live_foreign_window_is_usable(self, monkeypatch):
        _win32gui.IsWindow.return_value = True
        _win32gui.IsWindowVisible.return_value = True
        monkeypatch.setattr(inject, "_is_own_window", lambda h: False)
        assert inject.is_usable_target(4242) is True

    def test_closed_window_is_not_usable(self, monkeypatch):
        _win32gui.IsWindow.return_value = False
        _win32gui.IsWindowVisible.return_value = True
        monkeypatch.setattr(inject, "_is_own_window", lambda h: False)
        assert inject.is_usable_target(4242) is False

    def test_hidden_window_is_not_usable(self, monkeypatch):
        _win32gui.IsWindow.return_value = True
        _win32gui.IsWindowVisible.return_value = False
        monkeypatch.setattr(inject, "_is_own_window", lambda h: False)
        assert inject.is_usable_target(4242) is False

    def test_own_window_is_not_usable(self, monkeypatch):
        _win32gui.IsWindow.return_value = True
        _win32gui.IsWindowVisible.return_value = True
        monkeypatch.setattr(inject, "_is_own_window", lambda h: True)
        assert inject.is_usable_target(4242) is False


class TestRdpDetection:
    def test_msrdcw_is_rdp(self, monkeypatch):
        """Windows App (msrdcw.exe) hosts remote sessions too and uses neither
        the mstsc class names nor the msrdc.exe image name."""
        monkeypatch.setattr(inject, "_get_class", lambda h: "WinUIDesktopWin32WindowClass")
        monkeypatch.setattr(inject, "_get_exe_name", lambda h: "msrdcw.exe")
        assert inject._is_rdp(4242) is True

    def test_plain_window_is_not_rdp(self, monkeypatch):
        monkeypatch.setattr(inject, "_get_class", lambda h: "Notepad")
        monkeypatch.setattr(inject, "_get_exe_name", lambda h: "notepad.exe")
        assert inject._is_rdp(4242) is False


class TestModifierFlush:
    """The RDP session keeps a modifier latched when mstsc drops the key-up, and
    the local key state can't see it — so the flush must actually fire whenever an
    RDP window holds the foreground, even if it only gets it back a moment later."""

    _RELEASE_SCANS = {s for s, _ in inject._MOD_RELEASE_SCANCODES}

    def _sent_scancodes(self, sent: list) -> set:
        return {inp.ki.wScan for batch in sent for inp in batch}

    def test_watcher_flushes_when_rdp_takes_foreground_later(self, monkeypatch):
        """Regression: the preview panel steals focus first, so a single
        foreground sample sees our own window and never flushes."""
        sent = []
        monkeypatch.setattr(inject, "time", _FakeTime())
        monkeypatch.setattr(inject, "_wait_modifiers_released", lambda timeout_ms=400: True)
        monkeypatch.setattr(inject, "_send_inputs", lambda inputs: sent.append(inputs) or len(inputs))
        monkeypatch.setattr(inject, "_is_rdp", lambda h: h == 77)
        _win32gui.GetForegroundWindow.side_effect = [11, 11, 77]
        inject._flush_hotkey_modifiers()
        assert sent, "no flush sent once the RDP window regained the foreground"
        assert self._sent_scancodes(sent) == self._RELEASE_SCANS

    def test_watcher_expires_without_flushing(self, monkeypatch):
        sent = []
        monkeypatch.setattr(inject, "time", _FakeTime())
        monkeypatch.setattr(inject, "_wait_modifiers_released", lambda timeout_ms=400: True)
        monkeypatch.setattr(inject, "_send_inputs", lambda inputs: sent.append(inputs) or len(inputs))
        monkeypatch.setattr(inject, "_is_rdp", lambda h: False)
        _win32gui.GetForegroundWindow.return_value = 11
        inject._flush_hotkey_modifiers()
        assert sent == []

    def test_force_flush_sends_key_ups_only_never_downs(self, monkeypatch):
        """Regression for the 2026-08-12 down+up "tap" experiment, REVERTED same
        day: mstsc sometimes loses key-ups around focus changes, so a synthetic
        modifier DOWN in that channel can latch the remote session by our own
        hand. The force flush must only ever emit key-ups."""
        sent = []
        monkeypatch.setattr(inject, "_send_inputs", lambda inputs: sent.append(inputs) or len(inputs))
        inject._flush_all_modifiers(force=True)
        assert len(sent) == 1
        events = [(inp.ki.wScan, bool(inp.ki.dwFlags & inject._KEYEVENTF_KEYUP))
                  for inp in sent[0]]
        assert events == [(s, True) for s, _ in inject._MOD_RELEASE_SCANCODES]
        assert all(up for _, up in events), "a synthetic modifier DOWN must never be injected"

    def test_flush_rdp_if_foreground_reports_whether_it_flushed(self, monkeypatch):
        """The preview skips its focus steal entirely when this returns True —
        stealing focus from mstsc right after key events were in flight is when
        mstsc loses key-ups."""
        monkeypatch.setattr(inject, "time", _FakeTime())
        monkeypatch.setattr(inject, "_send_inputs", lambda inputs: len(inputs))
        monkeypatch.setattr(inject, "_is_rdp", lambda h: h == 77)
        _win32gui.GetForegroundWindow.return_value = 77
        assert inject.flush_rdp_if_foreground() is True
        _win32gui.GetForegroundWindow.return_value = 11
        assert inject.flush_rdp_if_foreground() is False

    def test_non_force_flush_only_releases_held_keys(self, monkeypatch):
        """Unchanged: outside RDP we never synthesise a down, and only the keys
        actually held get an up (spurious ups confuse Electron/VS Code)."""
        sent = []
        monkeypatch.setattr(inject, "_send_inputs", lambda inputs: sent.append(inputs) or len(inputs))
        monkeypatch.setattr(inject, "_modifiers_physically_down", lambda: [inject._VK_CONTROL])
        inject._flush_all_modifiers(force=False)
        events = [inp for batch in sent for inp in batch]
        assert len(events) == 1
        assert events[0].ki.dwFlags & inject._KEYEVENTF_KEYUP

    def test_flush_rdp_if_foreground_flushes_for_rdp(self, monkeypatch):
        sent = []
        monkeypatch.setattr(inject, "time", _FakeTime())
        monkeypatch.setattr(inject, "_send_inputs", lambda inputs: sent.append(inputs) or len(inputs))
        monkeypatch.setattr(inject, "_is_rdp", lambda h: True)
        _win32gui.GetForegroundWindow.return_value = 77
        inject.flush_rdp_if_foreground()
        assert self._sent_scancodes(sent) == self._RELEASE_SCANS

    def test_flush_rdp_if_foreground_is_a_no_op_otherwise(self, monkeypatch):
        sent = []
        monkeypatch.setattr(inject, "time", _FakeTime())
        monkeypatch.setattr(inject, "_send_inputs", lambda inputs: sent.append(inputs) or len(inputs))
        monkeypatch.setattr(inject, "_is_rdp", lambda h: False)
        _win32gui.GetForegroundWindow.return_value = 11
        inject.flush_rdp_if_foreground()
        assert sent == []
