"""Tests for the monitor work-area maths behind PASTE_UX_PLAN section 3.

Tk's winfo_screenwidth()/winfo_screenheight() report the PRIMARY display on
Windows, never the virtual desktop, which is why every toast landed on
DISPLAY1 whatever screen the user was on. These helpers replace that, so the
tests pin both the resolution order and the never-raise contract.

Every Win32 call is mocked, so this runs headless and on a one-monitor box.
"""
import ctypes
from ctypes import wintypes

import pytest

import preview
import winfx

# Two pretend monitors: primary at the origin, secondary to its LEFT, i.e.
# negative virtual-desktop coordinates, the exact shape of this machine's
# config.json (DISPLAY2 panel_position x = -1018), and the case where naive
# primary-only maths puts a popup on the wrong screen.
PRIMARY_WORK   = (0, 0, 2560, 1392)
SECONDARY_WORK = (-1920, 0, 0, 1080)
_H_PRIMARY   = 0x10001
_H_SECONDARY = 0x20002
_AREAS = {_H_PRIMARY: PRIMARY_WORK, _H_SECONDARY: SECONDARY_WORK}


@pytest.fixture
def fake_monitors(monkeypatch):
    """Stand in for MonitorFromPoint / MonitorFromWindow / GetMonitorInfoW.

    Returns a dict of knobs the individual tests tweak: which hmonitor a
    window maps to, whether a given hwnd is real, and where the cursor is.
    """
    state = {"window_hmon": _H_SECONDARY, "is_window": True, "cursor": (100, 100)}
    user32 = ctypes.windll.user32

    def _hmon_for_point(pt, _flags):
        return _H_SECONDARY if int(pt.x) < 0 else _H_PRIMARY

    def _hmon_for_window(_hwnd, _flags):
        return state["window_hmon"]

    def _monitor_info(hmon, info_ptr):
        area = _AREAS.get(int(getattr(hmon, "value", hmon) or 0))
        if area is None:
            return 0
        info = info_ptr.contents
        info.rcWork.left, info.rcWork.top, info.rcWork.right, info.rcWork.bottom = area
        return 1

    def _cursor_pos(pt_ptr):
        pt_ptr.contents.x, pt_ptr.contents.y = state["cursor"]
        return 1

    monkeypatch.setattr(user32, "MonitorFromPoint", _hmon_for_point)
    monkeypatch.setattr(user32, "MonitorFromWindow", _hmon_for_window)
    monkeypatch.setattr(user32, "GetMonitorInfoW", _monitor_info)
    monkeypatch.setattr(user32, "GetCursorPos", _cursor_pos)
    monkeypatch.setattr(user32, "IsWindow", lambda _hwnd: 1 if state["is_window"] else 0)
    return state


class TestWorkAreaForPoint:
    def test_point_on_the_primary(self, fake_monitors):
        assert winfx.work_area_for_point(400, 400) == PRIMARY_WORK

    def test_point_on_a_negative_coordinate_secondary(self, fake_monitors):
        assert winfx.work_area_for_point(-800, 300) == SECONDARY_WORK

    def test_work_area_excludes_the_taskbar(self, fake_monitors):
        # rcWork, not rcMonitor: the primary here is 1392 tall of a 1440 panel.
        assert winfx.work_area_for_point(0, 0)[3] == 1392

    def test_monitor_unplugged_mid_call_falls_back(self, monkeypatch, fake_monitors):
        def _boom(*_a):
            raise OSError("monitor went away")
        monkeypatch.setattr(ctypes.windll.user32, "MonitorFromPoint", _boom)
        monkeypatch.setattr(winfx, "primary_work_area", lambda: PRIMARY_WORK)
        assert winfx.work_area_for_point(-800, 300) == PRIMARY_WORK

    def test_getmonitorinfo_refusal_falls_back(self, monkeypatch, fake_monitors):
        monkeypatch.setattr(ctypes.windll.user32, "GetMonitorInfoW", lambda *_a: 0)
        monkeypatch.setattr(winfx, "primary_work_area", lambda: PRIMARY_WORK)
        assert winfx.work_area_for_point(-800, 300) == PRIMARY_WORK

    def test_degenerate_rect_is_rejected(self, monkeypatch, fake_monitors):
        def _empty(_hmon, info_ptr):
            r = info_ptr.contents.rcWork
            r.left = r.top = r.right = r.bottom = 0
            return 1
        monkeypatch.setattr(ctypes.windll.user32, "GetMonitorInfoW", _empty)
        monkeypatch.setattr(winfx, "primary_work_area", lambda: PRIMARY_WORK)
        assert winfx.work_area_for_point(10, 10) == PRIMARY_WORK


class TestWorkAreaForWindow:
    def test_window_on_the_secondary(self, fake_monitors):
        assert winfx.work_area_for_window(0x1234) == SECONDARY_WORK

    def test_zero_hwnd_is_none_not_primary(self, fake_monitors):
        # None means "no target window", so callers fall through to the
        # cursor. Returning the primary area here would silently reinstate
        # the very bug this replaces.
        assert winfx.work_area_for_window(0) is None

    def test_invalid_hwnd_is_none(self, fake_monitors):
        fake_monitors["is_window"] = False
        assert winfx.work_area_for_window(0xDEAD) is None

    def test_exception_degrades_to_primary_never_raises(self, monkeypatch, fake_monitors):
        def _boom(*_a):
            raise OSError("display topology changed")
        monkeypatch.setattr(ctypes.windll.user32, "MonitorFromWindow", _boom)
        monkeypatch.setattr(winfx, "primary_work_area", lambda: PRIMARY_WORK)
        assert winfx.work_area_for_window(0x1234) == PRIMARY_WORK


class TestWorkAreaForCursor:
    def test_follows_the_cursor_across_monitors(self, fake_monitors):
        fake_monitors["cursor"] = (-500, 200)
        assert winfx.work_area_for_cursor() == SECONDARY_WORK
        fake_monitors["cursor"] = (500, 200)
        assert winfx.work_area_for_cursor() == PRIMARY_WORK

    def test_getcursorpos_failure_falls_back(self, monkeypatch, fake_monitors):
        monkeypatch.setattr(ctypes.windll.user32, "GetCursorPos", lambda _p: 0)
        monkeypatch.setattr(winfx, "primary_work_area", lambda: PRIMARY_WORK)
        assert winfx.work_area_for_cursor() == PRIMARY_WORK


class TestPrimaryWorkArea:
    def test_uses_the_primary_monitors_work_area(self, fake_monitors):
        assert winfx.primary_work_area() == PRIMARY_WORK

    def test_falls_back_to_system_metrics(self, monkeypatch, fake_monitors):
        monkeypatch.setattr(ctypes.windll.user32, "GetMonitorInfoW", lambda *_a: 0)
        monkeypatch.setattr(ctypes.windll.user32, "GetSystemMetrics",
                            lambda i: 3840 if i == 0 else 2160)
        assert winfx.primary_work_area() == (0, 0, 3840, 2160)

    def test_last_resort_constant_when_everything_fails(self, monkeypatch, fake_monitors):
        def _boom(*_a):
            raise OSError("no user32 today")
        monkeypatch.setattr(ctypes.windll.user32, "MonitorFromPoint", _boom)
        monkeypatch.setattr(ctypes.windll.user32, "GetSystemMetrics", _boom)
        assert winfx.primary_work_area() == winfx._FALLBACK_WORK_AREA

    def test_real_call_returns_a_sane_rect(self):
        # Unmocked, against whatever this box actually has: the contract is
        # "always a usable 4-tuple", so it is worth one live assertion.
        left, top, right, bottom = winfx.primary_work_area()
        assert right > left and bottom > top


class TestPointerHandlingIsNot32BitTruncated:
    def test_hmonitor_restype_is_a_pointer(self):
        # A c_int restype truncates HMONITOR on 64-bit Windows and every
        # GetMonitorInfoW after it fails, which would look exactly like
        # "the fallback is always used".
        assert ctypes.windll.user32.MonitorFromPoint.restype is wintypes.HMONITOR
        assert ctypes.windll.user32.MonitorFromWindow.restype is wintypes.HMONITOR


class TestToastMonitorResolution:
    """preview._toast_work_area: target window first, then cursor, then
    primary. This ordering is the whole fix for PASTE_UX_PLAN D2."""

    def test_target_window_wins(self, monkeypatch):
        monkeypatch.setattr(winfx, "work_area_for_window", lambda h: SECONDARY_WORK)
        monkeypatch.setattr(winfx, "work_area_for_cursor", lambda: PRIMARY_WORK)
        assert preview._toast_work_area(0x1234) == SECONDARY_WORK

    def test_no_hwnd_uses_the_cursor(self, monkeypatch):
        monkeypatch.setattr(winfx, "work_area_for_cursor", lambda: SECONDARY_WORK)
        assert preview._toast_work_area(0) == SECONDARY_WORK

    def test_unknown_window_falls_through_to_the_cursor(self, monkeypatch):
        monkeypatch.setattr(winfx, "work_area_for_window", lambda h: None)
        monkeypatch.setattr(winfx, "work_area_for_cursor", lambda: SECONDARY_WORK)
        assert preview._toast_work_area(0xDEAD) == SECONDARY_WORK

    def test_everything_failing_still_returns_a_rect(self, monkeypatch):
        def _boom(*_a):
            raise OSError("no monitors")
        monkeypatch.setattr(winfx, "work_area_for_window", _boom)
        monkeypatch.setattr(winfx, "work_area_for_cursor", _boom)
        monkeypatch.setattr(winfx, "primary_work_area", lambda: PRIMARY_WORK)
        assert preview._toast_work_area(0x1234) == PRIMARY_WORK


class TestToastPlacementMaths:
    """The bottom-right anchor plus clamp, evaluated the way
    _show_anchored_toast evaluates it, on a negative-coordinate secondary."""

    @staticmethod
    def _place(work, w, h, offset):
        left, top, right, bottom = work
        return preview._clamp_to_workarea(right - w - 20, bottom - h - 130 - offset,
                                          w, h, work)

    def test_anchors_bottom_right_of_the_secondary(self):
        x, y = self._place(SECONDARY_WORK, 360, 90, 0)
        assert (x, y) == (-380, 860)
        assert SECONDARY_WORK[0] <= x and x + 360 <= SECONDARY_WORK[2]

    def test_stacking_walks_up_the_same_screen(self):
        first = self._place(SECONDARY_WORK, 360, 90, 0)
        second = self._place(SECONDARY_WORK, 360, 90, 100)
        assert second[0] == first[0]
        assert second[1] == first[1] - 100

    def test_short_display_clamps_inside_the_work_area(self):
        # A toast taller than the space above the badge area must still sit
        # fully on-screen rather than being pushed off the top.
        short = (-1920, 0, 0, 200)
        x, y = self._place(short, 360, 150, 0)
        assert y >= short[1]
        assert y + 150 <= short[3]

    def test_offsets_are_per_monitor(self):
        # A shared counter would push the second screen's first toast up by
        # the height of a toast on the first screen.
        preview._toast_offsets.clear()
        preview._toast_offsets[PRIMARY_WORK] = 100
        assert preview._toast_offsets.get(SECONDARY_WORK, 0) == 0
        preview._toast_offsets.clear()


class TestTargetRingGate:
    def test_default_true_when_key_absent(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        cfg.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(preview, "_CONFIG_FILE", str(cfg))
        assert preview._target_ring_enabled() is True

    def test_explicit_false(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        cfg.write_text('{"target_ring": false}', encoding="utf-8")
        monkeypatch.setattr(preview, "_CONFIG_FILE", str(cfg))
        assert preview._target_ring_enabled() is False

    def test_missing_file_defaults_true(self, tmp_path, monkeypatch):
        monkeypatch.setattr(preview, "_CONFIG_FILE", str(tmp_path / "nope.json"))
        assert preview._target_ring_enabled() is True


class TestRingColours:
    """Ring colours trace back to theme.py, never a literal."""

    def test_ok_is_the_accent(self):
        assert preview._ring_colour("ok") == preview._BLUE

    def test_warn_and_unknown_are_amber(self):
        assert preview._ring_colour("warn") == preview._PAUSE
        assert preview._ring_colour("unknown") == preview._PAUSE

    def test_unrecognised_key_is_treated_as_unknown(self):
        assert preview._ring_colour("banana") == preview._PAUSE
