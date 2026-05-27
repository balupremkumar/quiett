"""Tests for inject.py — verifies WM_PASTE is used, not keyboard simulation."""
import sys
from unittest.mock import MagicMock, patch, call

import pytest

# Stub win32 modules before importing inject
_win32gui = MagicMock()
_win32con = MagicMock()
_win32con.WM_PASTE = 0x0302
_pyperclip = MagicMock()

sys.modules["win32gui"]  = _win32gui
sys.modules["win32con"]  = _win32con
sys.modules["pyperclip"] = _pyperclip
sys.modules.pop("inject", None)

import inject  # noqa: E402  (import after stubs)


@pytest.fixture(autouse=True)
def reset_mocks():
    _win32gui.reset_mock()
    _win32con.reset_mock()
    _pyperclip.reset_mock()


class TestInjectText:
    def test_skips_zero_hwnd(self):
        inject.inject_text("hello", 0)
        _win32gui.PostMessage.assert_not_called()

    def test_skips_invalid_window(self):
        _win32gui.IsWindow.return_value = False
        inject.inject_text("hello", 9999)
        _win32gui.PostMessage.assert_not_called()

    def test_posts_wm_paste_not_keyboard_send(self, monkeypatch):
        _win32gui.IsWindow.return_value = True
        _pyperclip.paste.return_value = "original"

        # If keyboard were still imported and keyboard.send called it would raise
        import builtins
        real_import = builtins.__import__

        def no_keyboard(name, *args, **kwargs):
            if name == "keyboard":
                raise ImportError("keyboard should not be used in inject.py")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_keyboard)
        inject.inject_text("test text", 1234)
        _win32gui.PostMessage.assert_called_once_with(1234, _win32con.WM_PASTE, 0, 0)

    def test_copies_text_to_clipboard_before_paste(self):
        _win32gui.IsWindow.return_value = True
        _pyperclip.paste.return_value = "old"
        inject.inject_text("new text", 1234)
        _pyperclip.copy.assert_any_call("new text")

    def test_configure_sets_restore_delay(self):
        inject.configure(restore_delay_ms=300)
        assert inject._restore_delay_ms == 300
