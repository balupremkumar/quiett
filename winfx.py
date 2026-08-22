"""
Windows-only window-effects helpers for borderless Tk Toplevels.

Provides rounded corners (via SetWindowRgn) and smooth fade in/out
(via wm_attributes alpha + tk.after).
"""
from __future__ import annotations

import ctypes
import math
from ctypes import wintypes

import win32api
import win32con
import win32gui

_DWMWA_WINDOW_CORNER_PREFERENCE = 33
_DWMWCP_ROUND = 2
_DWMWA_SYSTEMBACKDROP_TYPE = 38
_DWMSBT_TRANSIENTWINDOW = 3

# ---------------------------------------------------------------------------
# Motion policy (BACKLOG item 47) — every animation in this module is capped
# to a 100-400ms window (state-confirming, never a spectacle) and the whole
# app treats Windows' own "Show animations" accessibility setting the same as
# an explicit animations:false — reduce_motion() is the single check callers
# (preview._animations_enabled) combine with the config kill-switch.
# ---------------------------------------------------------------------------
_MOTION_MIN_MS = 100
_MOTION_MAX_MS = 400
_SPI_GETCLIENTAREAANIMATION = 0x1042


def _clamp_duration(duration_ms: int) -> int:
    return max(_MOTION_MIN_MS, min(_MOTION_MAX_MS, int(duration_ms)))


def reduce_motion() -> bool:
    """True when Windows' Settings > Accessibility > Visual effects >
    "Animation effects" is off. Checked via the legacy client-area-animation
    SPI, which Windows still keeps in sync with that setting."""
    try:
        val = ctypes.c_int(1)
        ok = ctypes.windll.user32.SystemParametersInfoW(
            _SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(val), 0)
        if ok:
            return val.value == 0
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Monitor geometry (PASTE_UX_PLAN section 3): Tk's winfo_screenwidth() on
# Windows reports the PRIMARY display, never the virtual desktop, so anything
# placed from it lands on the wrong screen for a two-monitor user. These
# answer "which work area is this point / window / cursor on", work area
# meaning the monitor minus the taskbar. Every one degrades to the primary
# work area instead of raising: a monitor can be unplugged between two calls
# and no popup is worth taking the app down for.
# ---------------------------------------------------------------------------
MONITOR_DEFAULTTONULL = 0
MONITOR_DEFAULTTOPRIMARY = 1
MONITOR_DEFAULTTONEAREST = 2

_SM_CXSCREEN = 0
_SM_CYSCREEN = 1
_FALLBACK_WORK_AREA = (0, 0, 1920, 1080)


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD)]


try:
    # HMONITOR is a pointer; ctypes' default c_int return truncates it on
    # 64-bit Windows and every GetMonitorInfoW that follows then fails.
    ctypes.windll.user32.MonitorFromPoint.restype = wintypes.HMONITOR
    ctypes.windll.user32.MonitorFromWindow.restype = wintypes.HMONITOR
except Exception:
    pass


def _work_area_of(hmon) -> tuple[int, int, int, int] | None:
    """(left, top, right, bottom) work area of an HMONITOR. None when the
    handle is null/stale or GetMonitorInfoW refuses."""
    if not hmon:
        return None
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(_MONITORINFO)
    if not ctypes.windll.user32.GetMonitorInfoW(
            wintypes.HMONITOR(hmon), ctypes.pointer(info)):
        return None
    r = info.rcWork
    if r.right <= r.left or r.bottom <= r.top:
        return None
    return int(r.left), int(r.top), int(r.right), int(r.bottom)


def primary_work_area() -> tuple[int, int, int, int]:
    """Work area of the primary display, the last-resort fallback for every
    other helper here, and never allowed to fail."""
    try:
        area = _work_area_of(ctypes.windll.user32.MonitorFromPoint(
            wintypes.POINT(0, 0), MONITOR_DEFAULTTOPRIMARY))
        if area:
            return area
    except Exception:
        pass
    try:
        w = int(ctypes.windll.user32.GetSystemMetrics(_SM_CXSCREEN))
        h = int(ctypes.windll.user32.GetSystemMetrics(_SM_CYSCREEN))
        if w > 0 and h > 0:
            return 0, 0, w, h
    except Exception:
        pass
    return _FALLBACK_WORK_AREA


def work_area_for_point(x: int, y: int) -> tuple[int, int, int, int]:
    """Work area of the monitor containing (x, y) in virtual-desktop
    coordinates. Nearest monitor when the point is off every screen."""
    try:
        area = _work_area_of(ctypes.windll.user32.MonitorFromPoint(
            wintypes.POINT(int(x), int(y)), MONITOR_DEFAULTTONEAREST))
        if area:
            return area
    except Exception:
        pass
    return primary_work_area()


def work_area_for_window(hwnd: int) -> tuple[int, int, int, int] | None:
    """Work area of the monitor holding most of `hwnd`. None (not the
    primary area) for a falsy or invalid hwnd, so callers can tell "no target
    window" apart from "target window is on the primary screen" and fall
    through to the cursor instead."""
    if not hwnd:
        return None
    try:
        if not ctypes.windll.user32.IsWindow(wintypes.HWND(int(hwnd))):
            return None
        hmon = ctypes.windll.user32.MonitorFromWindow(
            wintypes.HWND(int(hwnd)), MONITOR_DEFAULTTONEAREST)
        return _work_area_of(hmon) or primary_work_area()
    except Exception:
        return primary_work_area()


def work_area_for_cursor() -> tuple[int, int, int, int]:
    """Work area of the monitor the mouse pointer is currently on."""
    try:
        pt = wintypes.POINT()
        if ctypes.windll.user32.GetCursorPos(ctypes.pointer(pt)):
            return work_area_for_point(int(pt.x), int(pt.y))
    except Exception:
        pass
    return primary_work_area()


class _MARGINS(ctypes.Structure):
    _fields_ = [("cxLeftWidth", ctypes.c_int), ("cxRightWidth", ctypes.c_int),
                ("cyTopHeight", ctypes.c_int), ("cyBottomHeight", ctypes.c_int)]


def _toplevel_hwnd(win) -> int:
    # winfo_id on Tk returns the inner widget HWND; walk up to the actual top-level
    hwnd = int(win.winfo_id())
    parent = win32gui.GetParent(hwnd)
    while parent:
        hwnd = parent
        parent = win32gui.GetParent(hwnd)
    return hwnd


def apply_rounded_region(win, radius: int = 12) -> None:
    """Round the window's corners. Safe to call once after geometry is set.

    Win11 path: DWMWA_WINDOW_CORNER_PREFERENCE — compositor-drawn, antialiased,
    with a native shadow (the fixed system radius wins over `radius`).
    Fallback (Win10 / DWM refusal): the old CreateRoundRectRgn hard mask."""
    try:
        win.update_idletasks()
        hwnd = _toplevel_hwnd(win)

        w = win.winfo_width()
        h = win.winfo_height()
        if w <= 0 or h <= 0:
            return
        try:
            pref = ctypes.c_int(_DWMWCP_ROUND)
            hr = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                wintypes.HWND(hwnd), _DWMWA_WINDOW_CORNER_PREFERENCE,
                ctypes.byref(pref), ctypes.sizeof(pref))
            if hr == 0:
                _apply_drop_shadow(hwnd)
                return
        except Exception:
            pass
        try:
            radius = max(1, round(radius * ctypes.windll.user32.GetDpiForWindow(hwnd) / 96))
        except Exception:
            pass
        rgn = win32gui.CreateRoundRectRgn(0, 0, w + 1, h + 1, radius * 2, radius * 2)
        win32gui.SetWindowRgn(hwnd, rgn, True)
    except Exception:
        pass


def apply_no_activate(win) -> None:
    """Stop a floating popup from stealing the foreground when it is clicked.

    Toasts need this: the recovery action re-inserts into whatever window the
    user has focused, so activating our own toast on click would move the
    target out from under it. Buttons still receive clicks without activation."""
    try:
        hwnd = _toplevel_hwnd(win)
        style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE,
                               style | win32con.WS_EX_NOACTIVATE)
    except Exception:
        pass


def apply_click_through(win) -> int:
    """Make an overlay invisible to the mouse and to the focus chain, for
    windows that only ever decorate the screen (the target ring).

    WS_EX_TRANSPARENT drops the window out of hit-testing so every click goes
    to whatever is underneath, WS_EX_LAYERED is what lets it be drawn without
    a hit region at all (Tk also needs it for -transparentcolor/-alpha),
    WS_EX_NOACTIVATE keeps it out of the foreground, WS_EX_TOOLWINDOW keeps
    it out of Alt+Tab. Returns the resulting ex-style, 0 on failure. An
    overlay that eats the user's clicks is worse than no overlay, so callers
    can assert the bits actually landed."""
    try:
        hwnd = _toplevel_hwnd(win)
        style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        style |= (win32con.WS_EX_TRANSPARENT | win32con.WS_EX_LAYERED
                  | win32con.WS_EX_NOACTIVATE | win32con.WS_EX_TOOLWINDOW)
        win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, style)
        return win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
    except Exception:
        return 0


def _apply_drop_shadow(hwnd: int) -> None:
    """Extend a 1px DWM frame margin into the client area — the documented
    minimal-margins trick that makes the compositor draw its native drop
    shadow around a borderless window, without a real "sheet of glass"
    effect. Only called after DWMWA_WINDOW_CORNER_PREFERENCE succeeds, since
    that's the Win11 compositor path; the Win10 region-mask fallback would
    just clip the shadow away."""
    try:
        margins = _MARGINS(0, 0, 0, 1)
        ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(
            wintypes.HWND(hwnd), ctypes.byref(margins))
    except Exception:
        pass


def apply_backdrop(hwnd: int) -> bool:
    """Win11 acrylic-style backdrop (DWMSBT_TRANSIENTWINDOW) behind a
    borderless Toplevel — QUIETT_UI_PLAN P4. Silent no-op returning False on
    Win10 or any DWM refusal: callers must never depend on this for
    visibility, only polish. Tk still paints every widget solid on top, so
    this only shows through wherever the window's own background colour
    would otherwise be — the existing solid fill is the fallback that always
    works, unchanged, when this returns False."""
    try:
        pref = ctypes.c_int(_DWMSBT_TRANSIENTWINDOW)
        hr = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            wintypes.HWND(hwnd), _DWMWA_SYSTEMBACKDROP_TYPE,
            ctypes.byref(pref), ctypes.sizeof(pref))
        return hr == 0
    except Exception:
        return False


def fade_to(win, target: float, duration_ms: int = 180, steps: int = 9,
            on_done=None) -> None:
    """
    Smoothly ramp the window's -alpha attribute to `target` over `duration_ms`.
    Cubic ease-in-out. Safely no-ops if the window is destroyed mid-fade.
    """
    duration_ms = _clamp_duration(duration_ms)
    try:
        start = float(win.attributes("-alpha"))
    except Exception:
        start = 1.0

    delta = target - start
    if abs(delta) < 0.01:
        if on_done:
            on_done()
        return

    step_ms = max(1, duration_ms // steps)

    def ease(t: float) -> float:
        # cubic in-out
        return 4 * t * t * t if t < 0.5 else 1 - pow(-2 * t + 2, 3) / 2

    def tick(i: int) -> None:
        try:
            if not win.winfo_exists():
                return
        except Exception:
            return
        t = i / steps
        a = start + delta * ease(t)
        try:
            win.attributes("-alpha", max(0.0, min(1.0, a)))
        except Exception:
            return
        if i >= steps:
            if on_done:
                try:
                    on_done()
                except Exception:
                    pass
            return
        win.after(step_ms, lambda: tick(i + 1))

    tick(1)


def fade_in(win, target: float = 0.94, duration_ms: int = 180) -> None:
    try:
        win.attributes("-alpha", 0.0)
    except Exception:
        return
    fade_to(win, target, duration_ms)


def fade_out_then_destroy(win, duration_ms: int = 160) -> None:
    fade_to(win, 0.0, duration_ms, on_done=lambda: _safe_destroy(win))


def _safe_destroy(win) -> None:
    try:
        win.destroy()
    except Exception:
        pass


def _blend_hex(a: str, b: str, t: float) -> str:
    ar, ag, ab = int(a[1:3], 16), int(a[3:5], 16), int(a[5:7], 16)
    br, bg, bb = int(b[1:3], 16), int(b[3:5], 16), int(b[5:7], 16)
    return f"#{int(ar + (br - ar) * t):02x}{int(ag + (bg - ag) * t):02x}{int(ab + (bb - ab) * t):02x}"


def ease_color(widget, option: str, start_hex: str, end_hex: str,
                duration_ms: int = 150, steps: int = 8, on_done=None) -> None:
    """Animate a single colour option (e.g. 'bg', 'highlightbackground', 'fg')
    on `widget` from `start_hex` to `end_hex`. Safe to call if widget is destroyed mid-animation."""
    duration_ms = _clamp_duration(duration_ms)
    step_ms = max(1, duration_ms // steps)

    def tick(i: int) -> None:
        try:
            if not widget.winfo_exists():
                return
        except Exception:
            return
        try:
            widget.configure(**{option: _blend_hex(start_hex, end_hex, i / steps)})
        except Exception:
            return
        if i >= steps:
            if on_done:
                try:
                    on_done()
                except Exception:
                    pass
            return
        widget.after(step_ms, lambda: tick(i + 1))

    tick(1)


def ease_place_y(widget, start_y: int, end_y: int,
                  duration_ms: int = 150, steps: int = 10) -> None:
    """Animate a place()-managed widget's y coordinate (e.g. a sliding tab indicator)."""
    if start_y == end_y:
        return
    duration_ms = _clamp_duration(duration_ms)
    step_ms = max(1, duration_ms // steps)

    def ease(t: float) -> float:
        return 1 - pow(1 - t, 3)

    def tick(i: int) -> None:
        try:
            if not widget.winfo_exists():
                return
        except Exception:
            return
        t = ease(i / steps)
        y = int(start_y + (end_y - start_y) * t)
        try:
            widget.place(y=y)
        except Exception:
            return
        if i < steps:
            widget.after(step_ms, lambda: tick(i + 1))

    tick(1)


def slide_in(win, dx: int = 0, dy: int = 10, duration_ms: int = 200,
             alpha_target: float = 1.0) -> None:
    """Move window from (x+dx, y+dy) to (x, y) while fading alpha in. Eased."""
    duration_ms = _clamp_duration(duration_ms)
    try:
        win.update_idletasks()
        geo = win.geometry()  # "WxH+X+Y"
    except Exception:
        return

    try:
        size_part, x_part, y_part = geo.split("+", 2)
        x_end = int(x_part)
        y_end = int(y_part)
        w, h = size_part.split("x")
    except Exception:
        return

    x_start = x_end + dx
    y_start = y_end + dy
    steps = 10
    step_ms = max(1, duration_ms // steps)

    try:
        win.attributes("-alpha", 0.0)
    except Exception:
        pass

    def ease(t: float) -> float:
        return 1 - pow(1 - t, 3)

    def tick(i: int) -> None:
        try:
            if not win.winfo_exists():
                return
        except Exception:
            return
        t = i / steps
        e = ease(t)
        x = int(x_start + (x_end - x_start) * e)
        y = int(y_start + (y_end - y_start) * e)
        try:
            win.geometry(f"{w}x{h}+{x}+{y}")
            win.attributes("-alpha", alpha_target * e)
        except Exception:
            return
        if i < steps:
            win.after(step_ms, lambda: tick(i + 1))

    tick(1)
