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
    def test_skips_zero_hwnd(self):
        inject.inject_text("hello", 0)
        _pyperclip.copy.assert_not_called()

    def test_skips_invalid_window(self):
        _win32gui.IsWindow.return_value = False
        inject.inject_text("hello", 9999)
        _pyperclip.copy.assert_not_called()

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
                            lambda mods, key: keystroke_calls.append((mods, key)))
        inject.inject_text("ls -la", 1234)
        assert clipboard_calls == ["ls -la"]
        assert keystroke_calls and keystroke_calls[0][1] == inject._VK_V

    def test_configure_sets_restore_delay(self):
        inject.configure(restore_delay_ms=300)
        assert inject._restore_delay_ms == 300
