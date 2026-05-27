"""Tests for inject.py — verifies keybd_event Ctrl+V is used, not WM_PASTE."""
import sys
from unittest.mock import MagicMock, call

import pytest

# Stub win32 modules before importing inject
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
        _win32api.keybd_event.assert_not_called()

    def test_skips_invalid_window(self):
        _win32gui.IsWindow.return_value = False
        inject.inject_text("hello", 9999)
        _win32api.keybd_event.assert_not_called()

    def test_uses_keybd_event_not_wm_paste(self):
        _win32gui.IsWindow.return_value = True
        _pyperclip.paste.return_value = "original"
        inject.inject_text("test text", 1234)
        # keybd_event must have been called (Ctrl+V sequence)
        assert _win32api.keybd_event.call_count >= 4
        # WM_PASTE must NOT be sent
        _win32gui.PostMessage.assert_not_called()

    def test_copies_text_to_clipboard_before_paste(self):
        _win32gui.IsWindow.return_value = True
        _pyperclip.paste.return_value = "old"
        inject.inject_text("new text", 1234)
        _pyperclip.copy.assert_any_call("new text")

    def test_configure_sets_restore_delay(self):
        inject.configure(restore_delay_ms=300)
        assert inject._restore_delay_ms == 300
