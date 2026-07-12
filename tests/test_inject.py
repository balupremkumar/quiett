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


@pytest.fixture(autouse=True)
def reset_mocks():
    _win32gui.reset_mock()
    _win32api.reset_mock()
    _pyperclip.reset_mock()


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
        _win32gui.GetWindowText.return_value = "VoiceDictate"
        monkeypatch.setattr(inject, "_user32", self._fake_user32(99999))
        monkeypatch.setattr(inject, "_get_exe_name", lambda h: "pythonw3.13.exe")
        assert inject.capture_foreground() == 0

    def test_foreign_window_passes_through(self, monkeypatch):
        _win32gui.GetForegroundWindow.return_value = 4242
        _win32gui.GetWindowText.return_value = "Untitled - Notepad"
        monkeypatch.setattr(inject, "_user32", self._fake_user32(99999))
        monkeypatch.setattr(inject, "_get_exe_name", lambda h: "notepad.exe")
        assert inject.capture_foreground() == 4242
