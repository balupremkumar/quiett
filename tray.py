"""
System tray icon via pystray. Must run on the main thread (call tray.run()).

States
------
loading    grey   — Whisper model is downloading / initialising
idle       green  — ready, waiting for hotkey
recording  red    — mic is active
processing yellow — transcribing audio
"""

import os
import threading

import pystray
from PIL import Image, ImageDraw

_state = "loading"
_icon: pystray.Icon | None = None
_on_view_history = None
_on_toggle_pause = None
_paused = False
_lock = threading.Lock()

# Shown in the greyed-out status menu item
_LABELS = {
    "loading":    "Loading model...",
    "idle":       "Idle — ready to dictate",
    "recording":  "Recording...",
    "processing": "Processing...",
}

# Shown as the tray tooltip on hover
_TOOLTIPS = {
    "loading":    "VoiceDictate — Loading...",
    "idle":       "VoiceDictate — Ready",
    "recording":  "VoiceDictate — Recording",
    "processing": "VoiceDictate — Processing...",
}

_BG = {
    "loading":    (110, 110, 110),
    "idle":       (30,  140,  30),
    "recording":  (200,  30,  30),
    "processing": (200, 160,   0),
}


def _make_icon(bg: tuple) -> Image.Image:
    """64×64 RGBA icon: coloured circle with a white microphone silhouette."""
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


def configure(on_view_history, on_toggle_pause=None) -> None:
    """Wire optional callbacks before calling run()."""
    global _on_view_history, _on_toggle_pause
    _on_view_history = on_view_history
    _on_toggle_pause = on_toggle_pause


def _label() -> str:
    return _LABELS.get(_state, _state)


def set_state(state: str) -> None:
    """Update icon colour and tooltip. Safe to call from any thread."""
    global _state
    with _lock:
        _state = state
        if _icon is not None:
            _icon.icon = _ICONS[state]
            _icon.title = _TOOLTIPS.get(state, f"VoiceDictate — {state}")
            _icon.update_menu()


def notify(title: str, message: str) -> None:
    """Show a Windows tray notification. Safe to call from any thread."""
    if _icon is not None:
        try:
            _icon.notify(message, title)
        except Exception:
            pass  # notify unsupported on some pystray backends


def run() -> None:
    """Block the calling thread on the pystray event loop."""
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

    menu = pystray.Menu(
        pystray.MenuItem(lambda _: _label(), lambda icon, item: None, enabled=False),
        pystray.MenuItem("View History", _view_history),
        pystray.MenuItem(lambda _: "Resume" if _paused else "Pause", _toggle_pause),
        pystray.MenuItem("Open Config", _open_config),
        pystray.MenuItem("Quit", lambda icon, item: icon.stop()),
    )
    _icon = pystray.Icon(
        name="dictation",
        icon=_ICONS["loading"],
        title="VoiceDictate — Loading...",
        menu=menu,
    )
    _icon.run()
