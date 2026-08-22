"""Tests for winfx.apply_backdrop — the QUIETT_UI_PLAN P4 acrylic call.
Must never raise or hold up a window build; DWM itself is mocked since this
runs on whatever Windows version the test box has, not necessarily Win11."""
import ctypes

import pytest
import win32con

import winfx


class TestApplyBackdrop:
    def test_success_returns_true(self, monkeypatch):
        monkeypatch.setattr(ctypes.windll.dwmapi, "DwmSetWindowAttribute",
                             lambda *a, **kw: 0)  # S_OK
        assert winfx.apply_backdrop(12345) is True

    def test_dwm_refusal_is_a_clean_false(self, monkeypatch):
        monkeypatch.setattr(ctypes.windll.dwmapi, "DwmSetWindowAttribute",
                             lambda *a, **kw: 1)  # any non-zero HRESULT
        assert winfx.apply_backdrop(12345) is False

    def test_exception_is_a_clean_false_not_a_raise(self, monkeypatch):
        def _boom(*a, **kw):
            raise OSError("no DWM on this box")
        monkeypatch.setattr(ctypes.windll.dwmapi, "DwmSetWindowAttribute", _boom)
        assert winfx.apply_backdrop(12345) is False


class TestApplyClickThrough:
    """The target ring must be invisible to the mouse. Getting these bits
    wrong puts a sheet over the user's target that eats every click, which is
    worse than having no ring, so the caller checks the returned style."""

    @pytest.fixture
    def fake_window(self, monkeypatch):
        state = {"style": win32con.WS_EX_TOPMOST | win32con.WS_EX_LAYERED}
        monkeypatch.setattr(winfx, "_toplevel_hwnd", lambda _win: 4242)
        monkeypatch.setattr(winfx.win32gui, "GetWindowLong",
                            lambda _hwnd, _idx: state["style"])

        def _set(_hwnd, _idx, value):
            state["style"] = value
            return 1
        monkeypatch.setattr(winfx.win32gui, "SetWindowLong", _set)
        return state

    def test_sets_all_four_bits(self, fake_window):
        style = winfx.apply_click_through(object())
        for bit in (win32con.WS_EX_TRANSPARENT, win32con.WS_EX_LAYERED,
                    win32con.WS_EX_NOACTIVATE, win32con.WS_EX_TOOLWINDOW):
            assert style & bit, f"missing ex-style bit {hex(bit)}"

    def test_keeps_the_bits_the_window_already_had(self, fake_window):
        style = winfx.apply_click_through(object())
        assert style & win32con.WS_EX_TOPMOST

    def test_failure_returns_zero_so_callers_can_bail(self, monkeypatch):
        monkeypatch.setattr(winfx, "_toplevel_hwnd", lambda _win: 4242)

        def _boom(*_a):
            raise OSError("window already gone")
        monkeypatch.setattr(winfx.win32gui, "GetWindowLong", _boom)
        assert winfx.apply_click_through(object()) == 0
