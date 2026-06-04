"""
Custom Tk widgets: animated toggle switch and live mic level meter.

Used by the settings UI to replace stock Checkbuttons and offer a
real-time mic-level preview when picking an input device.
"""
from __future__ import annotations

import threading
import tkinter as tk

import numpy as np
import sounddevice as sd


class Toggle(tk.Canvas):
    """
    Animated on/off toggle switch.

    Drives a tk.BooleanVar. Click anywhere on the canvas (or its label)
    to toggle. Smoothly slides the knob between off/on states.
    """

    W = 38
    H = 20
    PAD = 2

    def __init__(self, parent, variable: tk.BooleanVar,
                 bg: str = "#1a1a1d",
                 off_bg: str = "#3a3a40",
                 on_bg:  str = "#3b82f6",
                 knob_fg: str = "#f3f4f6") -> None:
        super().__init__(parent, width=self.W, height=self.H,
                         bg=bg, bd=0, highlightthickness=0)
        self._var = variable
        self._off_bg = off_bg
        self._on_bg = on_bg
        self._knob_fg = knob_fg

        r = self.H / 2
        # Pill background (use two ovals + a rect for cross-Tk-version rounded look)
        self._track = self.create_oval(0, 0, self.H, self.H, fill=off_bg, outline="")
        self.create_oval(self.W - self.H, 0, self.W, self.H, fill=off_bg, outline="",
                         tags=("track2",))
        self.create_rectangle(r, 0, self.W - r, self.H, fill=off_bg, outline="",
                              tags=("trackmid",))
        # Knob
        kr = r - self.PAD
        self._knob = self.create_oval(
            self.PAD, self.PAD, self.PAD + kr * 2, self.PAD + kr * 2,
            fill=knob_fg, outline="",
        )

        self._pos = 1.0 if variable.get() else 0.0
        self._target = self._pos
        self._render()

        self.bind("<Button-1>", lambda e: self.toggle())
        variable.trace_add("write", lambda *_: self._sync_from_var())

    def toggle(self) -> None:
        self._var.set(not self._var.get())

    def _sync_from_var(self) -> None:
        self._target = 1.0 if self._var.get() else 0.0
        self._animate()

    def _animate(self) -> None:
        delta = self._target - self._pos
        if abs(delta) < 0.02:
            self._pos = self._target
            self._render()
            return
        self._pos += delta * 0.35
        self._render()
        self.after(12, self._animate)

    def _render(self) -> None:
        # Track colour interpolates between off/on
        c = _blend_hex(self._off_bg, self._on_bg, self._pos)
        for tag_id in (self._track,):
            self.itemconfig(tag_id, fill=c)
        for tag in ("track2", "trackmid"):
            self.itemconfig(tag, fill=c)
        # Knob slides left → right
        r = self.H / 2
        kr = r - self.PAD
        x0 = self.PAD + self._pos * (self.W - 2 * self.PAD - 2 * kr)
        self.coords(self._knob, x0, self.PAD, x0 + 2 * kr, self.PAD + 2 * kr)


def _blend_hex(a: str, b: str, t: float) -> str:
    ar, ag, ab = int(a[1:3], 16), int(a[3:5], 16), int(a[5:7], 16)
    br, bg, bb = int(b[1:3], 16), int(b[3:5], 16), int(b[5:7], 16)
    r = int(ar + (br - ar) * t)
    g = int(ag + (bg - ag) * t)
    bl = int(ab + (bb - ab) * t)
    return f"#{r:02x}{g:02x}{bl:02x}"


class MicMeter(tk.Canvas):
    """
    Live audio-level meter for previewing the selected input device.

    Opens its own short-lived InputStream (separate from the recording stream),
    polled at 30 fps. Re-call set_device() to switch mics on the fly.
    """

    W = 220
    H = 18
    SEGMENTS = 24

    def __init__(self, parent, bg: str = "#1a1a1d",
                 idle: str = "#3a3a40",
                 active_lo: str = "#22c55e",
                 active_hi: str = "#ef4444") -> None:
        super().__init__(parent, width=self.W, height=self.H,
                         bg=bg, bd=0, highlightthickness=0)
        self._idle = idle
        self._active_lo = active_lo
        self._active_hi = active_hi
        self._level = 0.0
        self._device = None
        self._stream = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

        seg_w = (self.W - self.SEGMENTS) / self.SEGMENTS
        self._segs = []
        for i in range(self.SEGMENTS):
            x = i * (seg_w + 1)
            r = self.create_rectangle(x, 0, x + seg_w, self.H,
                                      fill=idle, outline="")
            self._segs.append(r)

        self.after(33, self._tick)
        self.bind("<Destroy>", lambda e: self.stop())

    def set_device(self, device) -> None:
        with self._lock:
            self._device = device
            self._restart()

    def _restart(self) -> None:
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
        except Exception:
            pass
        self._stream = None
        try:
            self._stream = sd.InputStream(
                samplerate=16000, channels=1, dtype="float32",
                device=self._device, blocksize=512,
                callback=self._cb,
            )
            self._stream.start()
        except Exception:
            self._stream = None

    def _cb(self, indata, frames, time_info, status) -> None:
        if self._stop.is_set():
            return
        rms = float(np.sqrt(np.mean(indata ** 2)))
        # Fast attack, slow decay
        if rms > self._level:
            self._level = self._level * 0.4 + rms * 0.6
        else:
            self._level = self._level * 0.85 + rms * 0.15

    def _tick(self) -> None:
        if self._stop.is_set():
            return
        try:
            n_lit = int(min(1.0, self._level / 0.20) ** 0.6 * self.SEGMENTS)
            for i, seg in enumerate(self._segs):
                if i < n_lit:
                    t = i / max(1, self.SEGMENTS - 1)
                    self.itemconfig(seg, fill=_blend_hex(self._active_lo, self._active_hi, t))
                else:
                    self.itemconfig(seg, fill=self._idle)
        except Exception:
            return
        self.after(33, self._tick)

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
        except Exception:
            pass
        self._stream = None


class Toast:
    """
    Brief in-app toast that fades in/out at the bottom of a parent window.

    Used for click-to-copy confirmations etc. Self-destructs.
    """

    def __init__(self, parent: tk.Misc, text: str,
                 bg: str = "#26262a", fg: str = "#f3f4f6",
                 duration_ms: int = 1400) -> None:
        try:
            x = parent.winfo_rootx()
            y = parent.winfo_rooty()
            w = parent.winfo_width()
            h = parent.winfo_height()
        except Exception:
            return

        self._win = tk.Toplevel(parent)
        self._win.overrideredirect(True)
        self._win.attributes("-topmost", True)
        self._win.attributes("-alpha", 0.0)
        self._win.configure(bg="#4a4a52")

        inner = tk.Frame(self._win, bg=bg, padx=14, pady=8)
        inner.pack(padx=1, pady=1)
        tk.Label(inner, text=text, bg=bg, fg=fg,
                 font=("Segoe UI Variable Text", 9)).pack()

        self._win.update_idletasks()
        tw = self._win.winfo_reqwidth()
        th = self._win.winfo_reqheight()
        tx = x + (w - tw) // 2
        ty = y + h - th - 20
        self._win.geometry(f"{tw}x{th}+{tx}+{ty}")

        self._fade(0.0, 0.95, 8, lambda: self._win.after(duration_ms, self._dismiss))

    def _fade(self, start, end, steps, done) -> None:
        delta = (end - start) / steps
        def step(i, val):
            try:
                if not self._win.winfo_exists():
                    return
                self._win.attributes("-alpha", val)
            except Exception:
                return
            if i < steps:
                self._win.after(16, step, i + 1, val + delta)
            else:
                if done:
                    done()
        step(0, start)

    def _dismiss(self) -> None:
        def done():
            try:
                self._win.destroy()
            except Exception:
                pass
        self._fade(0.95, 0.0, 8, done)
