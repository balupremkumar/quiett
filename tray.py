"""
System tray icon via pystray. Must run on the main thread (call tray.run()).

States
------
loading    grey   — Whisper model is downloading / initialising
idle       green  — ready, waiting for hotkey
recording  red    — mic is active (icon pulses with sine-eased brightness)
processing yellow — transcribing audio
"""

import math
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
    "idle":         (34,  170,  84),
    "recording":    (220,  38,  38),
    "processing":   (217, 152,  10),
}

# Number of brightness steps for the sine-eased recording pulse
_PULSE_STEPS = 14


def _system_light_taskbar() -> bool:
    """Taskbar theme (SystemUsesLightTheme — distinct from the apps theme)."""
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return bool(winreg.QueryValueEx(k, "SystemUsesLightTheme")[0])
    except Exception:
        return False


_DOT = {  # state → indicator dot colour; idle is the bare glyph
    "loading":    (110, 110, 110),
    "recording":  (220, 38, 38),
    "processing": (217, 152, 10),
}


def _make_badge_icon(bg: tuple, target_size: int = 32) -> Image.Image:
    """Coloured-disc badge icon — kept for the desktop/installer .ico, where a
    bare monochrome glyph would vanish against the wallpaper."""
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


def _make_icon(state: str, target_size: int = 32, dot: tuple | None = None) -> Image.Image:
    """Monochrome Win11-style tray glyph that follows the taskbar theme; the
    app state is a small colour dot so the glyph itself stays theme-neutral.
    Drawn at 4x then LANCZOS-downsampled."""
    scale = 4
    size  = target_size * scale
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    fg = (28, 28, 28, 255) if _system_light_taskbar() else (255, 255, 255, 255)
    stroke = max(2, int(2.5 * scale))

    # ── Microphone (shifted left, centred vertically) ─────────────────────
    cx_mic = int(size * 0.34)
    body_top    = int(size * 0.20)
    body_bottom = int(size * 0.55)
    body_half_w = int(size * 0.11)
    d.rounded_rectangle(
        [cx_mic - body_half_w, body_top, cx_mic + body_half_w, body_bottom],
        radius=body_half_w, fill=fg,
    )
    arc_half_w = int(size * 0.16)
    d.arc([cx_mic - arc_half_w, int(size * 0.45), cx_mic + arc_half_w, int(size * 0.70)],
          start=0, end=180, fill=fg, width=stroke)
    d.line([cx_mic, int(size * 0.70), cx_mic, int(size * 0.82)], fill=fg, width=stroke)
    foot_half_w = int(size * 0.11)
    d.line([cx_mic - foot_half_w, int(size * 0.82), cx_mic + foot_half_w, int(size * 0.82)],
           fill=fg, width=stroke)

    # ── Waveform — 3 rounded vertical bars (short-tall-short) ─────────────
    bar_width = max(2, int(2 * scale))
    bar_centre_y = int(size * 0.50)
    for bx, bh in zip([int(size * 0.66), int(size * 0.76), int(size * 0.86)],
                      [int(size * 0.11), int(size * 0.20), int(size * 0.14)]):
        d.rounded_rectangle(
            [bx - bar_width, bar_centre_y - bh, bx + bar_width, bar_centre_y + bh],
            radius=bar_width, fill=fg,
        )

    dot = dot if dot is not None else _DOT.get(state)
    if dot:
        r = int(size * 0.15)
        d.ellipse([size - 2 * r, size - 2 * r, size, size], fill=dot)

    return img.resize((target_size, target_size), Image.LANCZOS)


def _blend_rgb(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _build_pulse_frames() -> list[Image.Image]:
    """Sine-eased pulse on the recording dot — the glyph itself can't pulse
    since a black glyph on a light taskbar has no brightness headroom."""
    frames = []
    for i in range(_PULSE_STEPS):
        t = i / _PULSE_STEPS  # 0..1
        b = 0.5 - 0.5 * math.cos(2 * math.pi * t)  # 0 → 1 → 0
        frames.append(_make_icon(
            "recording", dot=_blend_rgb(_DOT["recording"], (255, 170, 170), b)))
    return frames


_theme_light: bool | None = None
_ICONS: dict = {}
_PULSE_FRAMES: list = []


def _rebuild_icons() -> None:
    global _ICONS, _PULSE_FRAMES, _theme_light
    _theme_light = _system_light_taskbar()
    _ICONS = {s: _make_icon(s) for s in _TOOLTIPS}
    _PULSE_FRAMES = _build_pulse_frames()


_rebuild_icons()


def refresh_theme() -> None:
    """Rebuild the tray icons when the Windows taskbar theme flips; reapply state."""
    if _system_light_taskbar() == _theme_light:
        return
    _rebuild_icons()
    if _icon is not None:
        try:
            _icon.icon = _ICONS.get(_state, _ICONS["idle"])
        except Exception:
            pass


def make_logo(target_size: int = 48,
              bg: tuple = (210, 30, 30),
              ring: tuple = (255, 255, 255, 90)) -> Image.Image:
    """
    Render a polished VoiceDictate logo for the recording overlay.

    Mic body + emanating sound arcs inside a circular badge.
    Drawn at 4x and LANCZOS-downsampled.
    """
    scale = 4
    size = target_size * scale
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    pad = 2 * scale
    d.ellipse([pad, pad, size - pad, size - pad], fill=bg)

    # Inner highlight ring for depth
    inner = int(size * 0.08)
    d.ellipse(
        [inner, inner, size - inner, size - inner],
        outline=ring, width=max(1, int(0.6 * scale)),
    )

    fg = (255, 255, 255)
    stroke = max(2, int(2.5 * scale))

    # Centred microphone
    cx = size // 2
    body_top    = int(size * 0.22)
    body_bottom = int(size * 0.58)
    body_half_w = int(size * 0.12)
    body_radius = body_half_w
    d.rounded_rectangle(
        [cx - body_half_w, body_top, cx + body_half_w, body_bottom],
        radius=body_radius, fill=fg,
    )

    # U-shaped stand
    arc_half_w = int(size * 0.18)
    arc_top    = int(size * 0.46)
    arc_bottom = int(size * 0.72)
    d.arc(
        [cx - arc_half_w, arc_top, cx + arc_half_w, arc_bottom],
        start=0, end=180, fill=fg, width=stroke,
    )
    stem_top    = int(size * 0.72)
    stem_bottom = int(size * 0.82)
    d.line([cx, stem_top, cx, stem_bottom], fill=fg, width=stroke)
    foot_half_w = int(size * 0.11)
    d.line(
        [cx - foot_half_w, stem_bottom, cx + foot_half_w, stem_bottom],
        fill=fg, width=stroke,
    )

    # Symmetric sound-wave arcs left & right of the mic
    arc_stroke = max(2, int(1.8 * scale))
    for i, off in enumerate([0.20, 0.28]):
        ax_l = int(cx - size * (0.16 + off))
        ax_r = int(cx + size * (0.16 + off))
        ay_t = int(size * (0.30 - i * 0.04))
        ay_b = int(size * (0.62 + i * 0.04))
        d.arc([ax_l - int(size * 0.05), ay_t, ax_l + int(size * 0.05), ay_b],
              start=300, end=60, fill=fg, width=arc_stroke)
        d.arc([ax_r - int(size * 0.05), ay_t, ax_r + int(size * 0.05), ay_b],
              start=120, end=240, fill=fg, width=arc_stroke)

    return img.resize((target_size, target_size), Image.LANCZOS)


def export_ico(path: str) -> None:
    """Write a multi-resolution .ico file for use as a desktop shortcut icon."""
    icon = _make_badge_icon(_BG["idle"], target_size=256)
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    icon.save(path, format="ICO", sizes=sizes)

# Pulse state — sine-eased breathing at ~14 frames * 100ms = 1.4s/cycle
_pulse_timer: threading.Timer | None = None
_pulse_idx = 0


def _pulse_step() -> None:
    global _pulse_timer, _pulse_idx
    if _state != "recording" or _icon is None:
        return
    _pulse_idx = (_pulse_idx + 1) % _PULSE_STEPS
    try:
        _icon.icon = _PULSE_FRAMES[_pulse_idx]
    except Exception:
        pass
    _pulse_timer = threading.Timer(0.10, _pulse_step)
    _pulse_timer.daemon = True
    _pulse_timer.start()


def _start_pulse() -> None:
    global _pulse_idx
    _pulse_idx = _PULSE_STEPS // 2  # start at brightest peak so transition feels instant
    _pulse_step()


def _stop_pulse() -> None:
    global _pulse_timer
    if _pulse_timer:
        _pulse_timer.cancel()
        _pulse_timer = None


_on_open_settings = None
_on_toggle_clipboard_only = None
_clipboard_only   = False
_on_relaunch_taskflow = None
_taskflow_status  = "unknown"   # "running" | "down" | "unknown"
_task_count       = 0           # tasks added to TaskFlow this session
_on_toggle_agent_command_mode = None
_agent_command_mode = False
_on_rebuild_voice_profile = None


def configure(on_view_history, on_toggle_pause=None,
              on_view_profile=None, on_open_settings=None,
              on_toggle_clipboard_only=None, clipboard_only: bool = False,
              on_relaunch_taskflow=None,
              on_toggle_agent_command_mode=None, agent_command_mode: bool = False,
              on_rebuild_voice_profile=None) -> None:
    global _on_view_history, _on_toggle_pause, _on_view_profile, _on_open_settings
    global _on_toggle_clipboard_only, _clipboard_only, _on_relaunch_taskflow
    global _on_toggle_agent_command_mode, _agent_command_mode
    global _on_rebuild_voice_profile
    _on_view_history  = on_view_history
    _on_toggle_pause  = on_toggle_pause
    _on_view_profile  = on_view_profile
    _on_open_settings = on_open_settings
    _on_toggle_clipboard_only = on_toggle_clipboard_only
    _clipboard_only   = clipboard_only
    _on_relaunch_taskflow = on_relaunch_taskflow
    _on_toggle_agent_command_mode = on_toggle_agent_command_mode
    _agent_command_mode = agent_command_mode
    _on_rebuild_voice_profile = on_rebuild_voice_profile


def increment_task_count() -> None:
    """Call once per task successfully created via voice this session."""
    global _task_count
    _task_count += 1
    if _icon is not None:
        _icon.update_menu()


def set_taskflow_status(running: bool) -> None:
    """Keep the tray's TaskFlow status line in sync (called from the health
    checks already made when creating/reading tasks — no extra polling)."""
    global _taskflow_status
    new_status = "running" if running else "down"
    if new_status != _taskflow_status:
        _taskflow_status = new_status
        if _icon is not None:
            _icon.update_menu()


def set_clipboard_only(enabled: bool) -> None:
    """Keep tray menu in sync when paste_mode changes externally (e.g. hot-reload)."""
    global _clipboard_only
    _clipboard_only = enabled
    if _icon is not None:
        _icon.update_menu()


def set_agent_command_mode(enabled: bool) -> None:
    """Keep tray menu in sync when agent_command_mode_enabled changes externally."""
    global _agent_command_mode
    _agent_command_mode = enabled
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

    def _toggle_clipboard_only(icon, item):
        global _clipboard_only
        _clipboard_only = not _clipboard_only
        if _on_toggle_clipboard_only:
            _on_toggle_clipboard_only(_clipboard_only)
        icon.update_menu()

    def _toggle_agent_command_mode(icon, item):
        global _agent_command_mode
        _agent_command_mode = not _agent_command_mode
        if _on_toggle_agent_command_mode:
            _on_toggle_agent_command_mode(_agent_command_mode)
        icon.update_menu()

    def _relaunch_taskflow(icon, item):
        if _on_relaunch_taskflow:
            _on_relaunch_taskflow()

    def _rebuild_voice_profile(icon, item):
        if _on_rebuild_voice_profile:
            _on_rebuild_voice_profile()

    def _taskflow_label(_item) -> str:
        if _taskflow_status == "unknown":
            return "TaskFlow: —"
        return f"TaskFlow: {'Running' if _taskflow_status == 'running' else 'Down — click to relaunch'}"

    def _task_count_label(_item) -> str:
        return f"Tasks added this session: {_task_count}"

    menu = pystray.Menu(
        pystray.MenuItem(lambda _: _label(), lambda icon, item: None, enabled=False),
        pystray.MenuItem("Agent Command Mode", _toggle_agent_command_mode, checked=lambda item: _agent_command_mode),
        pystray.MenuItem("Clipboard Only", _toggle_clipboard_only, checked=lambda item: _clipboard_only),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(_taskflow_label, _relaunch_taskflow),
        pystray.MenuItem(_task_count_label, lambda icon, item: None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("View History",    _view_history),
        pystray.MenuItem("Speech Profile",  _view_profile),
        pystray.MenuItem("Rebuild Voice Profile", _rebuild_voice_profile),
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
