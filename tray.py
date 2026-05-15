"""
System tray icon via pystray. Must run on the main thread (call tray.run()).

States
------
loading    grey   — Whisper model is downloading / initialising
idle       green  — ready, waiting for hotkey
recording  red    — mic is active
processing yellow — transcribing audio
"""

import pystray
from PIL import Image, ImageDraw

_state = "loading"
_icon: pystray.Icon | None = None
_on_view_history = None

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


def configure(on_view_history) -> None:
    """Wire optional callbacks before calling run()."""
    global _on_view_history
    _on_view_history = on_view_history


def _label() -> str:
    return _LABELS.get(_state, _state)


def set_state(state: str) -> None:
    """Update icon colour and tooltip. Safe to call from any thread."""
    global _state
    _state = state
    if _icon is not None:
        _icon.icon = _ICONS[state]
        _icon.title = _TOOLTIPS.get(state, f"VoiceDictate — {state}")
        _icon.update_menu()


def run() -> None:
    """Block the calling thread on the pystray event loop."""
    global _icon

    def _view_history(icon, item):
        if _on_view_history:
            _on_view_history()

    menu = pystray.Menu(
        pystray.MenuItem(lambda _: _label(), lambda icon, item: None, enabled=False),
        pystray.MenuItem("View History", _view_history),
        pystray.MenuItem("Quit", lambda icon, item: icon.stop()),
    )
    _icon = pystray.Icon(
        name="dictation",
        icon=_ICONS["loading"],
        title="VoiceDictate — Loading...",
        menu=menu,
    )
    _icon.run()
