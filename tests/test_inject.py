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

    def test_copies_text_to_clipboard_before_paste(self):
        _win32gui.IsWindow.return_value = True
        _pyperclip.paste.return_value = "old"
        inject.inject_text("new text", 1234)
        _pyperclip.copy.assert_any_call("new text")

    def test_configure_sets_restore_delay(self):
        inject.configure(restore_delay_ms=300)
        assert inject._restore_delay_ms == 300
