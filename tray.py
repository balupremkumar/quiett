"""
System tray icon via pystray. Must run on the main thread (call tray.run()).

States
------
loading    grey   — Whisper model is downloading / initialising
idle       green  — ready, waiting for hotkey
recording  red    — mic is active (icon pulses with sine-eased brightness)
processing yellow — transcribing audio
"""

import io
import json
import math
import os
import struct
import threading
import time

import pyperclip
import pystray
from PIL import Image, ImageDraw

import theme

_CONFIG_FILE = "config.json"

_state = "loading"
_icon: pystray.Icon | None = None
_on_view_history  = None
_on_toggle_pause  = None
_on_view_profile  = None
_on_open_dashboard = None
_paused = False
_pause_until: float | None = None   # epoch seconds; None = paused-until-restart (or not paused)
_pause_timer: threading.Timer | None = None
_lock = threading.Lock()

_LABELS = {
    "loading":    "Loading model...",
    "idle":       "Idle — ready to dictate",
    "recording":  "Recording...",
    "processing": "Processing...",
}

_TOOLTIPS = {
    "loading":    "Quiett — Loading...",
    "idle":       "Quiett — Ready",
    "recording":  "Quiett — Recording",
    "processing": "Quiett — Processing...",
}

def _hex_to_rgb(hexcolour: str) -> tuple:
    return (int(hexcolour[1:3], 16), int(hexcolour[3:5], 16), int(hexcolour[5:7], 16))


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


_DOT = {  # state -> indicator dot colour, from theme.py (QUIETT_UI_PLAN P5);
          # idle stays the bare glyph, no dot at all.
    "loading":    _hex_to_rgb(theme.DARK["dim"]),
    "recording":  _hex_to_rgb(theme.DARK["rec"]),
    "processing": _hex_to_rgb(theme.DARK["pause"]),
}


# ---------------------------------------------------------------------------
# The Quiett master mark (QUIETT_UI_PLAN P5) — mic capsule, open arc, stem,
# caret foot. Matches the kove.nz showcase titlebar SVG. One shared drawing
# primitive so the tray glyph, the badge overlay logo, and the desktop .ico
# are always the same shape, never three hand-drifted lookalikes.
# ---------------------------------------------------------------------------

def _draw_mic_caret_glyph(draw: "ImageDraw.ImageDraw", cx: float, cy: float,
                          glyph_h: float, fg, stroke: int | None = None,
                          include_arc: bool = True, body_scale: float = 1.0) -> None:
    """Draw the mark centred on (cx, cy), `glyph_h` pixels tall overall.
    `include_arc=False` drops the open arc under the capsule — at 16px it
    muddies the shape more than it reads as a mic stand, so legibility wins;
    `body_scale` widens the capsule relative to the stem to keep the two
    readable as separate parts once that arc (their usual visual seam) is
    gone. `stroke` overrides the auto stroke weight for hand-tuning small
    sizes."""
    stroke = stroke if stroke is not None else max(2, round(glyph_h * 0.075))

    body_half_w = glyph_h * 0.18 * body_scale
    body_h      = glyph_h * 0.56
    body_top    = cy - glyph_h * 0.48
    draw.rounded_rectangle(
        [cx - body_half_w, body_top, cx + body_half_w, body_top + body_h],
        radius=body_half_w, fill=fg,
    )

    stem_top = body_top + body_h
    if include_arc:
        arc_half_w = glyph_h * 0.26
        arc_top    = stem_top - glyph_h * 0.16
        arc_bottom = stem_top + glyph_h * 0.24
        draw.arc([cx - arc_half_w, arc_top, cx + arc_half_w, arc_bottom],
                 start=0, end=180, fill=fg, width=stroke)
        stem_top = arc_bottom

    stem_bottom = cy + glyph_h * 0.48
    draw.line([cx, stem_top, cx, stem_bottom], fill=fg, width=stroke)

    foot_half_w = glyph_h * 0.18
    draw.line([cx - foot_half_w, stem_bottom, cx + foot_half_w, stem_bottom],
              fill=fg, width=stroke)


_GRADIENT_BASE = 64  # small working resolution; a linear gradient upsamples
                     # losslessly, so this keeps icon generation fast


def _gradient_tile(px: int, corner_frac: float = 0.22) -> Image.Image:
    """Rounded-square tile in the 125deg ion -> ion-deep gradient — the
    master mark's background (QUIETT_UI_PLAN P5). Always the flagship (dark
    theme) hues: this is the coloured brand mark, not a themed UI surface."""
    c0 = _hex_to_rgb(theme.DARK["ion"])
    c1 = _hex_to_rgb(theme.DARK["ion_deep"])
    angle = math.radians(125)
    ax, ay = math.cos(angle), math.sin(angle)
    n = _GRADIENT_BASE
    corners = [(0, 0), (n, 0), (0, n), (n, n)]
    projs = [x * ax + y * ay for x, y in corners]
    lo, hi = min(projs), max(projs)
    span = (hi - lo) or 1.0
    small = Image.new("RGB", (n, n))
    sp = small.load()
    for y in range(n):
        base = y * ay
        for x in range(n):
            t = (x * ax + base - lo) / span
            sp[x, y] = _blend_rgb(c0, c1, max(0.0, min(1.0, t)))
    grad = small.resize((px, px), Image.BICUBIC)

    img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    mask = Image.new("L", (px, px), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, px - 1, px - 1], radius=max(1, int(px * corner_frac)), fill=255)
    img.paste(grad, (0, 0), mask)
    return img


def _mic_caret_tile(size: int, stroke_frac: float | None = None,
                    include_arc: bool = True, body_scale: float = 1.0) -> Image.Image:
    """One frame of the desktop .ico master mark at `size` px. Rendered at 4x
    and LANCZOS-downsampled like every other icon here, except 16/20px which
    export_ico() calls directly at their own resolution (not resized down
    from the 256px master) so their stroke/arc/body can be hand-tuned."""
    scale = 4
    px = size * scale
    img = _gradient_tile(px)
    glyph_h = px * 0.62
    stroke = None
    if stroke_frac is not None:
        stroke = max(2, round(glyph_h * stroke_frac))
    _draw_mic_caret_glyph(ImageDraw.Draw(img), px / 2, px / 2, glyph_h,
                         (255, 255, 255, 255), stroke=stroke, include_arc=include_arc,
                         body_scale=body_scale)
    return img.resize((size, size), Image.LANCZOS)


def _make_icon(state: str, target_size: int = 32, dot: tuple | None = None) -> Image.Image:
    """Monochrome Win11-style tray glyph that follows the taskbar theme; the
    app state is a small colour dot so the glyph itself stays theme-neutral.
    Drawn at 4x then LANCZOS-downsampled."""
    scale = 4
    size  = target_size * scale
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    fg = (28, 28, 28, 255) if _system_light_taskbar() else (255, 255, 255, 255)
    _draw_mic_caret_glyph(d, size * 0.5, size * 0.5, size * 0.62, fg,
                         include_arc=target_size > 16)

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
    Render the Quiett recording-overlay logo: the mic-to-caret mark on a
    coloured circular badge (state accent — see preview._badge_cfg). Drawn
    at 4x and LANCZOS-downsampled.
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

    _draw_mic_caret_glyph(d, size / 2, size / 2, size * 0.58, (255, 255, 255))

    return img.resize((target_size, target_size), Image.LANCZOS)


def _write_ico(path: str, frames: dict) -> None:
    """Write an ICO container with each size's frame kept exactly as given
    (PNG-compressed entries, supported since Vista) — unlike
    `Image.save(..., format="ICO", sizes=...)`, which only resizes one base
    image, this lets 16/20px carry their own hand-tuned render instead of
    inheriting the 256px master's stroke weight."""
    entries = sorted(frames.items(), key=lambda kv: kv[0][0])
    blobs = []
    for (w, h), img in entries:
        buf = io.BytesIO()
        img.convert("RGBA").save(buf, format="PNG")
        blobs.append(buf.getvalue())

    header = struct.pack("<HHH", 0, 1, len(entries))
    dir_entries = b""
    data_offset = 6 + 16 * len(entries)
    image_data = b""
    for ((w, h), _img), blob in zip(entries, blobs):
        w_b = 0 if w >= 256 else w
        h_b = 0 if h >= 256 else h
        dir_entries += struct.pack("<BBBBHHII", w_b, h_b, 0, 0, 1, 32, len(blob), data_offset)
        data_offset += len(blob)
        image_data += blob

    with open(path, "wb") as f:
        f.write(header)
        f.write(dir_entries)
        f.write(image_data)


ICO_SIZES = (16, 20, 24, 32, 48, 64, 128, 256)


def export_ico(path: str) -> None:
    """Write the multi-resolution desktop .ico — the QUIETT_UI_PLAN P5 master
    mark (rounded-square ion-gradient tile + mic-to-caret glyph). 16/20px are
    rendered at their own resolution with a thicker stroke (and, at 16px, the
    arc dropped) instead of being resized down from the 256px master."""
    frames = {}
    master = None
    for s in ICO_SIZES:
        if s == 16:
            frames[(s, s)] = _mic_caret_tile(s, stroke_frac=0.09, include_arc=False, body_scale=1.7)
        elif s == 20:
            frames[(s, s)] = _mic_caret_tile(s, stroke_frac=0.13, include_arc=True)
        else:
            if master is None:
                master = _mic_caret_tile(256)
            frames[(s, s)] = master if s == 256 else master.resize((s, s), Image.LANCZOS)
    _write_ico(path, frames)

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
_on_rebuild_voice_profile = None
_on_toggle_incognito = None
_incognito = False
_on_set_tts_speed = None
_tts_speed = 1.0
_on_toggle_study_mode = None
_study_mode = False
_on_insert_last = None


def configure(on_view_history, on_toggle_pause=None,
              on_view_profile=None, on_open_settings=None,
              on_toggle_clipboard_only=None, clipboard_only: bool = False,
              on_rebuild_voice_profile=None, on_open_dashboard=None,
              on_toggle_incognito=None, incognito: bool = False,
              on_set_tts_speed=None, tts_speed: float = 1.0,
              on_toggle_study_mode=None, study_mode: bool = False,
              on_insert_last=None) -> None:
    global _on_view_history, _on_toggle_pause, _on_view_profile, _on_open_settings
    global _on_toggle_clipboard_only, _clipboard_only
    global _on_rebuild_voice_profile, _on_open_dashboard
    global _on_toggle_incognito, _incognito
    global _on_set_tts_speed, _tts_speed
    global _on_toggle_study_mode, _study_mode
    global _on_insert_last
    _on_view_history  = on_view_history
    _on_toggle_pause  = on_toggle_pause
    _on_view_profile  = on_view_profile
    _on_open_settings = on_open_settings
    _on_toggle_clipboard_only = on_toggle_clipboard_only
    _clipboard_only   = clipboard_only
    _on_rebuild_voice_profile = on_rebuild_voice_profile
    _on_open_dashboard = on_open_dashboard if on_open_dashboard else on_view_profile
    _on_toggle_incognito = on_toggle_incognito
    _incognito = incognito
    _on_set_tts_speed = on_set_tts_speed
    _tts_speed = tts_speed
    _on_toggle_study_mode = on_toggle_study_mode
    _study_mode = study_mode
    _on_insert_last = on_insert_last


def set_clipboard_only(enabled: bool) -> None:
    """Keep tray menu in sync when paste_mode changes externally (e.g. hot-reload)."""
    global _clipboard_only
    _clipboard_only = enabled
    if _icon is not None:
        _icon.update_menu()


def set_incognito(enabled: bool) -> None:
    """Keep tray menu + tooltip in sync when incognito changes externally
    (Settings page or hot-reload — item 86)."""
    global _incognito
    _incognito = enabled
    if _icon is not None:
        _icon.title = _with_incognito_suffix(
            _pause_tooltip() if _paused else _TOOLTIPS.get(_state, f"Quiett — {_state}"))
        _icon.update_menu()


def set_study_mode(enabled: bool) -> None:
    """Keep the tray in sync when study_mode changes externally (Settings page
    or hot-reload)."""
    global _study_mode
    _study_mode = enabled
    if _icon is not None:
        _icon.update_menu()


def set_tts_speed(speed: float) -> None:
    """Keep the speed picker showing the live mode's rate. Called when the
    study toggle flips, since each mode carries its own speed."""
    global _tts_speed
    _tts_speed = speed
    if _icon is not None:
        _icon.update_menu()


def _label() -> str:
    return _LABELS.get(_state, _state)


def _with_incognito_suffix(title: str) -> str:
    """Eye-ish state on the tray tooltip (item 86) — small, muted marker so
    the incognito state is visible without opening the menu."""
    return f"{title}  ·  \U0001F576 Incognito" if _incognito else title


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
            _icon.title = _with_incognito_suffix(
                _pause_tooltip() if _paused else _TOOLTIPS.get(state, f"Quiett — {state}"))
            _icon.update_menu()


def _pause_tooltip() -> str:
    if _pause_until is None:
        return "Quiett — Paused"
    remaining = int(_pause_until - time.time())
    if remaining <= 0:
        return "Quiett — Paused"
    mins = max(1, round(remaining / 60))
    return f"Quiett — Paused ({mins} min left)"


def _auto_resume() -> None:
    _set_paused(False)


def _set_paused(paused: bool, resume_at: float | None = None) -> None:
    """Pause or resume dictation. resume_at (epoch seconds) schedules an
    auto-resume via threading.Timer; None + paused=True means paused until
    the app restarts."""
    global _paused, _pause_until, _pause_timer
    if _pause_timer is not None:
        _pause_timer.cancel()
        _pause_timer = None
    _paused = paused
    _pause_until = resume_at if paused else None
    if _on_toggle_pause:
        _on_toggle_pause(_paused)
    if paused and resume_at is not None:
        _pause_timer = threading.Timer(max(0.0, resume_at - time.time()), _auto_resume)
        _pause_timer.daemon = True
        _pause_timer.start()
    if _icon is not None:
        _icon.title = _with_incognito_suffix(
            _pause_tooltip() if _paused else _TOOLTIPS.get(_state, f"Quiett — {_state}"))
        _icon.update_menu()


def notify(title: str, message: str) -> None:
    if _icon is not None:
        try:
            _icon.notify(message, title)
        except Exception:
            pass


def _read_config() -> dict:
    try:
        with open(_CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_config(patch: dict) -> None:
    """Read-modify-write config.json (same pattern as main.py's toggle handlers)."""
    try:
        with open(_CONFIG_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        raw.update(patch)
        with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(raw, f, indent=2)
    except Exception:
        pass


_RECENT_HISTORY_MAX = 5
_RECENT_HISTORY_TRUNCATE = 40


def _copy_history_entry(text: str):
    def _handler(icon, item):
        try:
            pyperclip.copy(text)
        except Exception:
            return
        notify("Quiett", "Copied to clipboard")
    return _handler


def _recent_history_items():
    """Rebuilt every time the submenu opens — reads history.load() fresh so
    it never shows stale entries."""
    import history
    try:
        entries = history.load()
    except Exception:
        entries = []
    if not entries:
        yield pystray.MenuItem("No recent dictations", lambda icon, item: None, enabled=False)
        return
    for e in entries[:_RECENT_HISTORY_MAX]:
        text = (e.get("text") or "").strip()
        if not text:
            continue
        label = text if len(text) <= _RECENT_HISTORY_TRUNCATE else text[:_RECENT_HISTORY_TRUNCATE - 1] + "…"
        yield pystray.MenuItem(label, _copy_history_entry(text))


def _latest_history_text() -> str:
    """Newest saved dictation, or "" when history is empty/unreadable."""
    import history
    try:
        entries = history.load()
    except Exception:
        return ""
    for e in entries:
        text = (e.get("text") or "").strip()
        if text:
            return text
    return ""


def _insert_last(icon, item):
    """Re-insert the newest dictation into whatever is focused now — the
    recovery path when a paste never landed."""
    text = _latest_history_text()
    if not text:
        notify("Quiett", "No recent dictations")
        return
    if _on_insert_last:
        _on_insert_last(text)


_MIC_NAME_TRUNCATE = 40


def _select_input_device(device):
    def _handler(icon, item):
        _write_config({"input_device": device})
    return _handler


def _mic_menu_items():
    """Rebuilt every time the submenu opens — re-reads config.json and the
    live device list so the radio check always reflects reality."""
    import audio
    try:
        devices = audio.list_input_devices()
    except Exception:
        devices = []
    current = _read_config().get("input_device")
    yield pystray.MenuItem(
        "System default", _select_input_device(None),
        checked=lambda item, c=current: c is None, radio=True,
    )
    for d in devices:
        idx = d.get("index")
        name = d.get("name") or f"Device {idx}"
        label = name if len(name) <= _MIC_NAME_TRUNCATE else name[:_MIC_NAME_TRUNCATE - 1] + "…"
        yield pystray.MenuItem(
            label, _select_input_device(idx),
            checked=lambda item, c=current, idx=idx: c == idx, radio=True,
        )


_TTS_SPEEDS = (0.7, 0.8, 0.9, 1.0, 1.1, 1.25, 1.5)


def _select_tts_speed(speed: float):
    def _handler(icon, item):
        global _tts_speed
        _tts_speed = speed
        if _on_set_tts_speed:
            _on_set_tts_speed(speed)
        icon.update_menu()
    return _handler


def _tts_speed_items():
    """Playback rate for read-aloud (Ctrl+Shift+S). Pitch is preserved, so a
    slower rate still sounds like the same voice. The picker edits whichever
    mode is live, so setting a study pace never overwrites the normal one."""
    yield pystray.MenuItem(
        lambda _: f"Setting: {'study mode' if _study_mode else 'normal'}",
        lambda icon, item: None, enabled=False,
    )
    for speed in _TTS_SPEEDS:
        label = "Normal (1x)" if speed == 1.0 else f"{speed:g}x"
        yield pystray.MenuItem(
            label, _select_tts_speed(speed),
            checked=lambda item, s=speed: abs(_tts_speed - s) < 1e-6, radio=True,
        )


def run() -> None:
    global _icon

    def _view_history(icon, item):
        if _on_view_history:
            _on_view_history()

    def _pause_15(icon, item):
        _set_paused(True, time.time() + 15 * 60)

    def _pause_60(icon, item):
        _set_paused(True, time.time() + 60 * 60)

    def _pause_until_restart(icon, item):
        _set_paused(True, None)

    def _resume(icon, item):
        _set_paused(False)

    def _open_dashboard(icon, item):
        if _on_open_dashboard:
            _on_open_dashboard()

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

    def _rebuild_voice_profile(icon, item):
        if _on_rebuild_voice_profile:
            _on_rebuild_voice_profile()

    def _toggle_study_mode(icon, item):
        global _study_mode
        _study_mode = not _study_mode
        if _on_toggle_study_mode:
            _on_toggle_study_mode(_study_mode)
        icon.update_menu()

    def _toggle_incognito(icon, item):
        global _incognito
        _incognito = not _incognito
        if _on_toggle_incognito:
            _on_toggle_incognito(_incognito)
        icon.title = _with_incognito_suffix(
            _pause_tooltip() if _paused else _TOOLTIPS.get(_state, f"Quiett — {_state}"))
        icon.update_menu()

    # Timed-pause flyout: durations + Resume (only enabled while paused)
    pause_menu = pystray.Menu(
        pystray.MenuItem("Pause 15 min",         _pause_15),
        pystray.MenuItem("Pause 1 hour",         _pause_60),
        pystray.MenuItem("Pause until restart",  _pause_until_restart),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Resume", _resume, enabled=lambda item: _paused),
    )

    # Rebuilt on every open (callable menus) so they never show stale data
    recent_menu = pystray.Menu(_recent_history_items)
    mic_menu = pystray.Menu(_mic_menu_items)
    tts_speed_menu = pystray.Menu(_tts_speed_items)

    # Rare/utility actions tucked away so the top level stays short
    more_menu = pystray.Menu(
        pystray.MenuItem("Clipboard Only", _toggle_clipboard_only, checked=lambda item: _clipboard_only),
        pystray.MenuItem("Microphone", mic_menu),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Read-aloud Speed", tts_speed_menu),
        pystray.MenuItem("Speech Profile", _view_profile),
        pystray.MenuItem("Rebuild Voice Profile", _rebuild_voice_profile),
        pystray.MenuItem("Open Config", _open_config),
    )

    menu = pystray.Menu(
        pystray.MenuItem(lambda _: _label(), lambda icon, item: None, enabled=False),
        # Hidden default item — fires on left-click, doesn't show up in the right-click menu
        pystray.MenuItem("Open Dashboard", _open_dashboard, default=True, visible=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("View History", _view_history),
        pystray.MenuItem("Recent Dictations", recent_menu),
        pystray.MenuItem("Insert Last Dictation", _insert_last),
        pystray.MenuItem("Settings",     _open_settings),
        pystray.MenuItem(lambda _: "Paused" if _paused else "Pause", pause_menu),
        pystray.MenuItem("Study Mode", _toggle_study_mode, checked=lambda item: _study_mode),
        pystray.MenuItem("Incognito", _toggle_incognito, checked=lambda item: _incognito),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("More", more_menu),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", lambda icon, item: icon.stop()),
    )
    _icon = pystray.Icon(
        name="dictation",
        icon=_ICONS["loading"],
        title=_with_incognito_suffix("Quiett — Loading..."),
        menu=menu,
    )
    _icon.run()
