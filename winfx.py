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


def fade_to(win, target: float, duration_ms: int = 180, steps: int = 9,
            on_done=None) -> None:
    """
    Smoothly ramp the window's -alpha attribute to `target` over `duration_ms`.
    Cubic ease-in-out. Safely no-ops if the window is destroyed mid-fade.
    """
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
