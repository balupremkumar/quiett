"""Tests for winfx.apply_backdrop — the QUIETT_UI_PLAN P4 acrylic call.
Must never raise or hold up a window build; DWM itself is mocked since this
runs on whatever Windows version the test box has, not necessarily Win11."""
import ctypes

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
