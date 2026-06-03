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


def _make_icon(bg: tuple, target_size: int = 64) -> Image.Image:
    """Render the tray icon. Drawn at 4x then downsampled with LANCZOS for clean edges."""
    scale = 4
    size  = target_size * scale
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Background circle with subtle inner shadow for depth
    pad = 2 * scale
    d.ellipse([pad, pad, size - pad, size - pad], fill=bg)

    fg = (255, 255, 255)
    stroke = max(2, int(2.5 * scale))

    # ── Microphone (shifted left, centred vertically) ─────────────────────
    cx_mic = int(size * 0.34)
    body_top    = int(size * 0.20)
    body_bottom = int(size * 0.55)
    body_half_w = int(size * 0.11)
    body_radius = body_half_w
    d.rounded_rectangle(
        [cx_mic - body_half_w, body_top, cx_mic + body_half_w, body_bottom],
        radius=body_radius, fill=fg,
    )
    # Mic stand arc (U-shape under the body)
    arc_half_w = int(size * 0.16)
    arc_top    = int(size * 0.45)
    arc_bottom = int(size * 0.70)
    d.arc(
        [cx_mic - arc_half_w, arc_top, cx_mic + arc_half_w, arc_bottom],
        start=0, end=180, fill=fg, width=stroke,
    )
    # Vertical stem from arc to foot
    stem_top    = int(size * 0.70)
    stem_bottom = int(size * 0.82)
    d.line([cx_mic, stem_top, cx_mic, stem_bottom], fill=fg, width=stroke)
    # Foot
    foot_half_w = int(size * 0.11)
    d.line(
        [cx_mic - foot_half_w, stem_bottom, cx_mic + foot_half_w, stem_bottom],
        fill=fg, width=stroke,
    )

    # ── Waveform — 3 rounded vertical bars (short-tall-short) ─────────────
    bar_width = max(2, int(2 * scale))
    bar_centre_y = int(size * 0.50)
    bar_xs = [int(size * 0.66), int(size * 0.76), int(size * 0.86)]
    bar_half_hs = [int(size * 0.11), int(size * 0.20), int(size * 0.14)]
    for bx, bh in zip(bar_xs, bar_half_hs):
        d.rounded_rectangle(
            [bx - bar_width, bar_centre_y - bh, bx + bar_width, bar_centre_y + bh],
            radius=bar_width, fill=fg,
        )

    # Downsample with LANCZOS for crisp anti-aliasing at 16/24/32 px tray sizes
    return img.resize((target_size, target_size), Image.LANCZOS)


_ICONS = {state: _make_icon(bg) for state, bg in _BG.items()}


def export_ico(path: str) -> None:
    """Write a multi-resolution .ico file for use as a desktop shortcut icon."""
    icon = _make_icon(_BG["idle"], target_size=256)
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    icon.save(path, format="ICO", sizes=sizes)

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


_on_open_settings = None
_on_toggle_vibe   = None
_vibe_mode        = False


def configure(on_view_history, on_toggle_pause=None,
              on_view_profile=None, on_open_settings=None,
              on_toggle_vibe=None, vibe_mode: bool = False) -> None:
    global _on_view_history, _on_toggle_pause, _on_view_profile, _on_open_settings
    global _on_toggle_vibe, _vibe_mode
    _on_view_history  = on_view_history
    _on_toggle_pause  = on_toggle_pause
    _on_view_profile  = on_view_profile
    _on_open_settings = on_open_settings
    _on_toggle_vibe   = on_toggle_vibe
    _vibe_mode        = vibe_mode


def set_vibe_mode(enabled: bool) -> None:
    """Keep tray menu in sync when vibe_mode changes externally (e.g. hot-reload)."""
    global _vibe_mode
    _vibe_mode = enabled
    if _icon is not None:
        _icon.update_menu()


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

    def _open_settings(icon, item):
        if _on_open_settings:
            _on_open_settings()

    def _toggle_vibe(icon, item):
        global _vibe_mode
        _vibe_mode = not _vibe_mode
        if _on_toggle_vibe:
            _on_toggle_vibe(_vibe_mode)
        icon.update_menu()

    menu = pystray.Menu(
        pystray.MenuItem(lambda _: _label(), lambda icon, item: None, enabled=False),
        pystray.MenuItem("Vibe Mode", _toggle_vibe, checked=lambda item: _vibe_mode),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("View History",    _view_history),
        pystray.MenuItem("Speech Profile",  _view_profile),
        pystray.MenuItem("Settings",        _open_settings),
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
