"""
System tray icon via pystray. Must run on the main thread (call tray.run()).

States
------
loading    grey   — Whisper model is downloading / initialising
idle       green  — ready, waiting for hotkey
recording  red    — mic is active (icon pulses)
processing yellow — transcribing audio
"""

import os
import threading

import pystray
from PIL import Image, ImageDraw

_state = "loading"
_icon: pystray.Icon | None = None
_on_view_history  = None
_on_toggle_pause  = None
_on_view_profile  = None
_paused = False
_lock = threading.Lock()

_LABELS = {
    "loading":    "Loading model...",
    "idle":       "Idle — ready to dictate",
    "recording":  "Recording...",
    "processing": "Processing...",
}

_TOOLTIPS = {
    "loading":    "VoiceDictate — Loading...",
    "idle":       "VoiceDictate — Ready",
    "recording":  "VoiceDictate — Recording",
    "processing": "VoiceDictate — Processing...",
}

_BG = {
    "loading":      (110, 110, 110),
    "idle":         (30,  140,  30),
    "recording":    (210,  30,  30),
    "recording_dim":(140,  18,  18),
    "processing":   (200, 160,   0),
}


def _make_icon(bg: tuple) -> Image.Image:
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([2, 2, 61, 61], fill=bg)
    cx, fg = 32, (255, 255, 255)
    d.rounded_rectangle([cx - 8, 12, cx + 8, 36], radius=7, fill=fg)
    d.arc([cx - 14, 26, cx + 14, 46], start=0, end=180, fill=fg, width=3)
    d.line([cx, 46, cx, 54], fill=fg, width=3)
    d.line([cx - 8, 54, cx + 8, 54], fill=fg, width=3)
    return img


_ICONS = {state: _make_icon(bg) for state, bg in _BG.items()}

# Pulse state
_pulse_timer: threading.Timer | None = None
_pulse_bright = True


def _pulse_step() -> None:
    global _pulse_timer, _pulse_bright
    if _state != "recording" or _icon is None:
        return
    _pulse_bright = not _pulse_bright
    key = "recording" if _pulse_bright else "recording_dim"
    try:
        _icon.icon = _ICONS[key]
    except Exception:
        pass
    _pulse_timer = threading.Timer(0.6, _pulse_step)
    _pulse_timer.daemon = True
    _pulse_timer.start()


def _start_pulse() -> None:
    global _pulse_bright
    _pulse_bright = True
    _pulse_step()


def _stop_pulse() -> None:
    global _pulse_timer
    if _pulse_timer:
        _pulse_timer.cancel()
        _pulse_timer = None


def configure(on_view_history, on_toggle_pause=None, on_view_profile=None) -> None:
    global _on_view_history, _on_toggle_pause, _on_view_profile
    _on_view_history = on_view_history
    _on_toggle_pause = on_toggle_pause
    _on_view_profile = on_view_profile


def _label() -> str:
    return _LABELS.get(_state, _state)


def set_state(state: str) -> None:
    global _state
    with _lock:
        prev = _state
        _state = state
        if state != "recording" and prev == "recording":
            _stop_pulse()
        if _icon is not None:
            if state == "recording":
                _icon.icon = _ICONS["recording"]
                _start_pulse()
            else:
                _icon.icon = _ICONS.get(state, _ICONS["idle"])
            _icon.title = _TOOLTIPS.get(state, f"VoiceDictate — {state}")
            _icon.update_menu()


def notify(title: str, message: str) -> None:
    if _icon is not None:
        try:
            _icon.notify(message, title)
        except Exception:
            pass


def run() -> None:
    global _icon

    def _view_history(icon, item):
        if _on_view_history:
            _on_view_history()

    def _toggle_pause(icon, item):
        global _paused
        _paused = not _paused
        if _on_toggle_pause:
            _on_toggle_pause(_paused)
        icon.update_menu()

    def _open_config(icon, item):
        try:
            os.startfile("config.json")
        except Exception:
            pass

    def _view_profile(icon, item):
        if _on_view_profile:
            _on_view_profile()

    menu = pystray.Menu(
        pystray.MenuItem(lambda _: _label(), lambda icon, item: None, enabled=False),
        pystray.MenuItem("View History",    _view_history),
        pystray.MenuItem("Speech Profile",  _view_profile),
        pystray.MenuItem(lambda _: "Resume" if _paused else "Pause", _toggle_pause),
        pystray.MenuItem("Open Config",     _open_config),
        pystray.MenuItem("Quit",            lambda icon, item: icon.stop()),
    )
    _icon = pystray.Icon(
        name="dictation",
        icon=_ICONS["loading"],
        title="VoiceDictate — Loading...",
        menu=menu,
    )
    _icon.run()
