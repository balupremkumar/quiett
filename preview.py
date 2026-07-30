"""
Tkinter preview window + history viewer + speech profile viewer,
running on a dedicated worker thread.

pystray owns the main thread, so we spin up a persistent hidden Tk root here.
Queues are polled every 50 ms so all windows can coexist simultaneously.
"""

import ctypes
import json
import queue
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from datetime import date, datetime

import math

import pyperclip
import keyboard as _keyboard
import win32api
import win32con
from PIL import Image, ImageDraw, ImageEnhance, ImageTk

import audio
import chime
import hotkey
import inject
import profile
import theme
import tray
import widgets
import winfx
from logger import log, warn, error as log_error

_CONFIG_FILE = "config.json"


def _dpi_scale() -> float:
    try:
        return ctypes.windll.user32.GetDpiForSystem() / 96.0
    except Exception:
        return 1.0


_S = _dpi_scale()


def _px(v: int) -> int:
    """Scale a design-pixel value to physical pixels for the current DPI."""
    return max(1, round(v * _S))

# ---------------------------------------------------------------------------
# Public API — safe to call from any thread
# ---------------------------------------------------------------------------

_preview_q:      queue.Queue = queue.Queue()
_profile_q:      queue.Queue = queue.Queue()
_settings_q:     queue.Queue = queue.Queue()
_badge_q:        queue.Queue = queue.Queue()
_toast_q:        queue.Queue = queue.Queue()
_flash_q:        queue.Queue = queue.Queue()
_root:        tk.Tk | None = None
_ready = threading.Event()

_preview_open  = False   # only touched on the tkinter thread
_profile_open  = False
_settings_open = False

_current_preview_win: tk.Toplevel | None = None  # live preview window reference
_close_preview_requested = threading.Event()     # set from any thread to dismiss current preview

# Preview position: "cursor" | "top-right" | "bottom-right" | "top-left" | "bottom-left"
_preview_position = "cursor"

# Re-record callback — set by main.py
_on_rerecord = None

# Themes — dark / light / system. Palette lives in refreshable module globals:
# refresh_theme() re-resolves them, new windows pick the change up on open.
# QUIETT_UI_PLAN P4: every colour here now comes from theme.py's Tk-flat
# tokens (theme.TK_DARK / theme.TK_LIGHT), not a second hand-maintained
# palette — grounds -> bg/surface/elevated, hairlines -> line/line2,
# ink -> text/mid/dim, the one accent -> ion everywhere the old palette used
# its one blue (status dot, pin, waveform, countdown bar), recording -> rec,
# transcribing/paused -> pause. No new theme.py tokens were needed: the
# panel's error/warning literals (mic errors, low-confidence hints) reuse
# rec/pause, since both are already alarm-red/amber semantically.


def _blend(hex_a: str, hex_b: str, t: float) -> str:
    """Small standalone blend used to build _THEMES below, ahead of the
    fuller _hex_blend() (used elsewhere further down this file, once accent
    colours are already resolved) — kept separate so _THEMES can be built at
    the very top of the module without reordering the rest of the file."""
    a = int(hex_a[1:3], 16), int(hex_a[3:5], 16), int(hex_a[5:7], 16)
    b = int(hex_b[1:3], 16), int(hex_b[3:5], 16), int(hex_b[5:7], 16)
    r = int(a[0] + (b[0] - a[0]) * t)
    g = int(a[1] + (b[1] - a[1]) * t)
    bl = int(a[2] + (b[2] - a[2]) * t)
    return f"#{r:02x}{g:02x}{bl:02x}"


def _rgb(hex_colour: str) -> tuple:
    return (int(hex_colour[1:3], 16), int(hex_colour[3:5], 16), int(hex_colour[5:7], 16))


def _flatten_theme(t: dict) -> dict:
    """Map theme.py's Tk-flat tokens onto this module's legacy palette keys."""
    ion_hv = _blend(t["ion"], "#ffffff", 0.22)
    return {
        "BG": t["bg"], "BG2": t["surface"], "BG3": t["elevated"],
        "FG": t["text"], "FG2": t["mid"], "FG3": t["dim"],
        "BLUE": t["ion"], "BLUE_HV": ion_hv,
        "BORDER": t["line"], "BORDER2": t["line2"],
        "TASK": t["ion"], "TASK_HV": ion_hv,
        "MATCH_BG": _blend(t["bg"], t["ion"], 0.16), "MATCH_FG": t["text"],
        "REC": t["rec"], "PAUSE": t["pause"],
    }


_THEMES = {
    "dark":  _flatten_theme(theme.TK_DARK),
    "light": _flatten_theme(theme.TK_LIGHT),
}


def _system_theme() -> str:
    """Windows personalisation setting: AppsUseLightTheme 1 = light, 0 = dark."""
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return "light" if winreg.QueryValueEx(k, "AppsUseLightTheme")[0] else "dark"
    except Exception:
        return "dark"


def _resolve_theme() -> str:
    try:
        with open(_CONFIG_FILE, encoding="utf-8") as f:
            name = json.load(f).get("theme", "dark")
    except Exception:
        name = "dark"
    return _system_theme() if name == "system" else name


def _apply_palette(name: str) -> None:
    global _THEME_NAME, _T, _BG, _BG2, _BG3, _FG, _FG2, _FG3, _BLUE, _BLUE_HV
    global _BORDER, _BORDER2, _TASK, _TASK_HV, _MATCH_BG, _MATCH_FG, _REC, _PAUSE
    _THEME_NAME = name
    _T = _THEMES.get(name, _THEMES["dark"])
    _BG      = _T["BG"]
    _BG2     = _T["BG2"]
    _BG3     = _T["BG3"]
    _FG      = _T["FG"]
    _FG2     = _T["FG2"]
    _FG3     = _T["FG3"]
    _BLUE    = _T["BLUE"]
    _BLUE_HV = _T["BLUE_HV"]
    _BORDER  = _T["BORDER"]
    _BORDER2 = _T["BORDER2"]
    _TASK    = _T["TASK"]
    _TASK_HV = _T["TASK_HV"]
    _MATCH_BG = _T["MATCH_BG"]
    _MATCH_FG = _T["MATCH_FG"]
    _REC     = _T["REC"]      # recording state (theme.rec)
    _PAUSE   = _T["PAUSE"]    # processing/warning state (theme.pause)


def refresh_theme() -> bool:
    """Re-resolve the palette from config + Windows theme. Returns True when it
    changed; open windows keep their colours, new ones use the fresh palette."""
    name = _resolve_theme()
    if name == _THEME_NAME:
        return False
    _apply_palette(name)
    return True


_apply_palette(_resolve_theme())


def _animations_enabled() -> bool:
    """Kill-switch for motion (items 18, 47). Off when the config key is
    false, or when Windows' own "Show animations" accessibility setting is
    off — an OS-level opt-out is honoured the same as an explicit one."""
    try:
        with open(_CONFIG_FILE, encoding="utf-8") as f:
            cfg_on = bool(json.load(f).get("animations", True))
    except Exception:
        cfg_on = True
    return cfg_on and not winfx.reduce_motion()


def _panel_acrylic_enabled() -> bool:
    """Config kill-switch for the Win11 acrylic backdrop (QUIETT_UI_PLAN P4,
    BACKLOG item 14), default on. Reading config.json directly matches the
    other display-only toggles in this module (animations, badge_animation)."""
    try:
        with open(_CONFIG_FILE, encoding="utf-8") as f:
            return bool(json.load(f).get("panel_acrylic", True))
    except Exception:
        return True


def _learn_from_edits_enabled() -> bool:
    """Dictionary auto-learn gate (QUIETT_UI_PLAN P6, config key
    learn_from_edits, default on). When off, corrections stop being logged —
    existing promoted rules keep applying via profile.get_active_rules(),
    which this doesn't touch."""
    try:
        with open(_CONFIG_FILE, encoding="utf-8") as f:
            return bool(json.load(f).get("learn_from_edits", True))
    except Exception:
        return True


def _apply_backdrop(win) -> None:
    """Best-effort acrylic behind a floating Toplevel — silent no-op on Win10
    or when the config key is off; the solid _BG fill underneath is unaffected
    either way, so this never regresses the always-worked solid look."""
    if not _panel_acrylic_enabled():
        return
    try:
        winfx.apply_backdrop(winfx._toplevel_hwnd(win))
    except Exception:
        pass


def _play_entrance(win, target_alpha: float, dy: int = 14, duration_ms: int = 200) -> None:
    """Slide-up + fade-in entrance (item 18): small upward drift combined with
    the existing alpha fade, eased out. Honours the `animations` config key —
    falls back to an instant snap to the target alpha when motion is off."""
    if _animations_enabled():
        winfx.slide_in(win, dx=0, dy=dy, duration_ms=duration_ms, alpha_target=target_alpha)
    else:
        try:
            win.attributes("-alpha", target_alpha)
        except Exception:
            pass


# Typography ladder — sourced from theme.FONT_UI / FONT_DISPLAY / FONT_MONO,
# each a (preferred, fallback) tuple. The real installed-font check happens
# once in _tk_main() once a Tk interpreter exists to query; these module-level
# defaults (first choice) are what's live until then, and match what almost
# every machine actually has since Segoe UI Variable ships with Win11.
_FONT_FAM_TEXT    = theme.FONT_UI[0]
_FONT_FAM_DISPLAY = theme.FONT_DISPLAY[0]
_FONT_FAM_MONO    = theme.FONT_MONO[0]
_FONT_BODY  = (_FONT_FAM_TEXT,    11, "normal")
_FONT_HINT  = (_FONT_FAM_TEXT,     9, "normal")
_FONT_META  = (_FONT_FAM_TEXT,     9, "normal")
_FONT_BTN   = (_FONT_FAM_DISPLAY, 10, "normal")
_FONT_CHIP  = (_FONT_FAM_TEXT,     8, "normal")


def start() -> None:
    """Spawn the tkinter worker thread and block until its root is live."""
    t = threading.Thread(target=_tk_main, daemon=True)
    t.start()
    _ready.wait()


def show(text: str, hwnd: int, empty: bool = False,
         confidence: float | None = None,
         words: list | None = None,
         auto_dismiss: float = 0.0,
         raw: str | None = None,
         duration: float = 0.0,
         incognito: bool = False) -> None:
    """Queue a dictation preview window. raw= is the unmodified Whisper
    transcript before filler-stripping/punctuation/correction cleanup;
    `text` is the cleaned version shown by default. When the two differ,
    the panel shows a Raw/Cleaned toggle (item 9).

    duration: seconds of recorded audio, when known — powers the speaking-pace
    (WPM) footer metric. 0.0 when unavailable.

    incognito: when True, this dictation was not saved to history — shows a
    small muted indicator in the panel header so the state is visible at
    dictation time.
    """
    _preview_q.put({"text": text, "hwnd": hwnd, "empty": empty,
                    "confidence": confidence, "words": words,
                    "auto_dismiss": auto_dismiss, "raw": raw,
                    "duration": duration, "incognito": incognito})


def show_profile() -> None:
    _profile_q.put(True)


def show_badge(state: str) -> None:
    """Show the recording/processing status badge. state: 'recording'|'processing'|'too_short'|'not_ready'"""
    _badge_q.put(state)


def hide_badge() -> None:
    """Remove the status badge."""
    _badge_q.put(None)


def show_toast(message: str, kind: str = "info", action_label: str = "",
               action_cb=None) -> None:
    """Show a custom in-app toast. kind: info|warn|error. Optional action button."""
    _toast_q.put({"message": message, "kind": kind,
                  "action_label": action_label, "action_cb": action_cb})


def flash_screen_edge(colour: str = _BLUE) -> None:
    """Quick screen-edge flash to confirm a hotkey press registered."""
    _flash_q.put(colour)


def close_current_preview() -> None:
    """Close any open preview window. Safe to call from any thread."""
    _close_preview_requested.set()


def show_settings() -> None:
    _settings_q.put(True)


def configure_position(position: str) -> None:
    """Update preview window placement. Safe to call from any thread."""
    global _preview_position
    _preview_position = position


def set_rerecord_callback(fn) -> None:
    """Set the callback invoked when user presses Ctrl+R in the preview panel."""
    global _on_rerecord
    _on_rerecord = fn


_partial_q: queue.Queue = queue.Queue()


def set_partial_text(text: str) -> None:
    """Live partial transcription shown under the recording badge. Any thread."""
    _partial_q.put(text or "")


# ---------------------------------------------------------------------------
# Internal — everything below runs exclusively on the tkinter worker thread
# ---------------------------------------------------------------------------

def _tk_main() -> None:
    global _root
    global _FONT_FAM_TEXT, _FONT_FAM_DISPLAY, _FONT_FAM_MONO
    global _FONT_BODY, _FONT_HINT, _FONT_META, _FONT_BTN, _FONT_CHIP
    _root = tk.Tk()
    try:
        # points-per-pixel ratio so point-sized fonts track the real DPI
        _root.tk.call("tk", "scaling", ctypes.windll.user32.GetDpiForSystem() / 72.0)
    except Exception:
        pass
    try:
        # Graceful fallback (QUIETT_UI_PLAN P4): prefer theme.py's first choice,
        # drop to its fallback entry if this Windows install doesn't have it.
        installed = set(tkfont.families(_root))
        _FONT_FAM_TEXT    = next((f for f in theme.FONT_UI if f in installed), theme.FONT_UI[-1])
        _FONT_FAM_DISPLAY = next((f for f in theme.FONT_DISPLAY if f in installed), theme.FONT_DISPLAY[-1])
        _FONT_FAM_MONO    = next((f for f in theme.FONT_MONO if f in installed), theme.FONT_MONO[-1])
        _FONT_BODY = (_FONT_FAM_TEXT,    11, "normal")
        _FONT_HINT = (_FONT_FAM_TEXT,     9, "normal")
        _FONT_META = (_FONT_FAM_MONO,     9, "normal")
        _FONT_BTN  = (_FONT_FAM_DISPLAY, 10, "normal")
        _FONT_CHIP = (_FONT_FAM_TEXT,     8, "normal")
    except Exception:
        pass
    _root.withdraw()
    _ready.set()
    _root.after(50, _tick)
    _root.mainloop()


_badge_win:    tk.Toplevel | None = None
_badge_label:  tk.Label | None = None     # used for non-recording transient states (too_short / not_ready)
_badge_dot:    tk.Label | None = None
_badge_canvas: tk.Canvas | None = None    # waveform + logo canvas for recording/processing
_badge_gradient_img: "ImageTk.PhotoImage | None" = None  # composite wave frame — kept alive against GC
_badge_gradient_id:  int | None = None    # canvas image item id, reused across frames
_badge_time:   tk.Label | None = None
_badge_logo_lbl: tk.Label | None = None
_badge_logo_variants: list = []           # PhotoImage list indexed by brightness level
_badge_logo_idx: int = 0
_badge_state:  str | None = None
_badge_anim_phase: float = 0.0            # drives the processing sweep
_badge_smoothed: list[float] = []         # interpolated bar heights for ease-out decay
_badge_partial_lbl: tk.Text | None = None  # live partial transcription line (word-stabilised, item 51)
_badge_status_lbl:  tk.Label | None = None  # dedicated status line — never shares space with transcript text
_badge_stop_lbl:    tk.Label | None = None  # hover-reveal "finish now" control (item 8)
_badge_cancel_lbl:  tk.Label | None = None  # hover-reveal "discard" control (item 8)
_badge_hover_visible: bool = False           # are the hover controls currently faded in?
_badge_hover_job = None                      # pending after() id for the leave-debounce

# Live-partial word stabilisation (item 51) — a word that lands at the same
# position in two consecutive partials is "committed" (stops changing colour);
# everything past that point is still shifting under whisper's revisions.
_partial_prev_words: list[str] = []
_partial_committed_n: int = 0
_partial_font_cache: "tkfont.Font | None" = None
_PARTIAL_MAX_LINES = 2


def _tick() -> None:
    global _preview_open, _profile_open, _settings_open
    global _current_preview_win

    # Close any open preview if a new recording started (signal from any thread)
    if _close_preview_requested.is_set():
        _close_preview_requested.clear()
        if _preview_open and _current_preview_win is not None:
            try:
                _current_preview_win.destroy()
            except Exception:
                pass
            _preview_open = False
            _current_preview_win = None

    # Drain the entire preview queue — keep only the latest item.
    # If a new transcription arrives while a preview is open, replace it.
    latest_preview = None
    while True:
        try:
            latest_preview = _preview_q.get_nowait()
        except queue.Empty:
            break

    if latest_preview is not None:
        # Close existing preview (replace-in-place instead of queuing behind it)
        if _preview_open and _current_preview_win is not None:
            try:
                _current_preview_win.destroy()
            except Exception:
                pass
            _preview_open = False
            _current_preview_win = None

        if not _preview_open:
            _preview_open = True
            try:
                _open_window(
                    latest_preview["text"], latest_preview["hwnd"],
                    latest_preview.get("empty", False),
                    latest_preview.get("confidence"),
                    latest_preview.get("auto_dismiss", 0.0),
                    latest_preview.get("words"),
                    latest_preview.get("raw"),
                    latest_preview.get("duration", 0.0),
                    latest_preview.get("incognito", False),
                )
            except Exception as e:
                log_error("preview", f"preview window failed to open, falling back to edge flash: {e}")
                _preview_open = False
                _current_preview_win = None
                _show_edge_flash(_BLUE)

    # Live-update waveform badge
    if _badge_state in ("recording", "processing") and _badge_alive():
        _draw_badge_frame()

    # Always drain these queues so items don't accumulate while a window is open
    # and immediately reopen it the moment the user closes it.
    profile_requested = False
    while True:
        try:
            _profile_q.get_nowait()
            profile_requested = True
        except queue.Empty:
            break
    if profile_requested and not _profile_open:
        _profile_open = True
        try:
            _open_profile()
        except Exception as e:
            print(f"profile window error: {e}")
            _profile_open = False

    settings_requested = False
    while True:
        try:
            _settings_q.get_nowait()
            settings_requested = True
        except queue.Empty:
            break
    if settings_requested and not _settings_open:
        _settings_open = True
        try:
            _open_settings()
        except Exception as e:
            print(f"settings window error: {e}")
            _settings_open = False

    # Drain the entire badge queue each tick so a late "processing" item
    # cannot reappear after a None already cleared the badge.
    while True:
        try:
            badge_cmd = _badge_q.get_nowait()
            _handle_badge(badge_cmd)
        except queue.Empty:
            break

    # Drain partial-transcription queue — keep only the latest
    latest_partial = None
    while True:
        try:
            latest_partial = _partial_q.get_nowait()
        except queue.Empty:
            break
    if latest_partial is not None:
        _update_badge_partial(latest_partial)

    # Drain toast queue
    while True:
        try:
            t = _toast_q.get_nowait()
            _show_anchored_toast(t)
        except queue.Empty:
            break

    # Drain flash queue
    while True:
        try:
            colour = _flash_q.get_nowait()
            _show_edge_flash(colour)
        except queue.Empty:
            break

    _root.after(50, _tick)


# ---------------------------------------------------------------------------
# Anchored toast (bottom-right of screen) + action button
# ---------------------------------------------------------------------------

_toast_offset: int = 0  # stack toasts vertically when several arrive together


def _show_anchored_toast(t: dict) -> None:
    global _toast_offset
    msg = t.get("message", "")
    kind = t.get("kind", "info")
    action_label = t.get("action_label", "")
    action_cb = t.get("action_cb")

    accent = {"info": _BLUE, "warn": _PAUSE, "error": _REC}.get(kind, _BLUE)

    if kind == "error":
        # Audible cue only for genuine errors (item 45/48) — warn/info stay
        # visual-only so routine notices (undo, clipboard fallback) don't add noise.
        chime.play_error()

    win = tk.Toplevel(_root)
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    win.attributes("-alpha", 0.0)
    win.configure(bg=_BORDER2)

    inner = tk.Frame(win, bg=_BG, padx=14, pady=10)
    inner.pack(padx=1, pady=1)

    dot_lbl = tk.Label(inner, text="●", bg=_BG, fg=accent,
                       font=(_FONT_FAM_TEXT, 10))
    dot_lbl.pack(side=tk.LEFT, padx=(0, 8))
    tk.Label(inner, text=msg, bg=_BG, fg=_FG,
             font=(_FONT_FAM_TEXT, 10), wraplength=320,
             justify="left").pack(side=tk.LEFT)

    if action_label and action_cb:
        def _do_action():
            try:
                action_cb()
            except Exception:
                pass
            winfx.fade_out_then_destroy(win, duration_ms=140)

        btn = tk.Button(inner, text=action_label, command=_do_action,
                        bg=accent, fg="#ffffff",
                        activebackground=accent, activeforeground="#ffffff",
                        relief="flat", bd=0,
                        font=(_FONT_FAM_DISPLAY, 9, "bold"),
                        padx=10, pady=4, cursor="hand2")
        btn.pack(side=tk.LEFT, padx=(12, 0))

    win.update_idletasks()
    w = win.winfo_reqwidth()
    h = win.winfo_reqheight()
    sw = win.winfo_screenwidth()
    sh = win.winfo_screenheight()
    # Stack above the badge area
    y = sh - h - 130 - _toast_offset
    win.geometry(f"{w}x{h}+{sw - w - 20}+{y}")
    _toast_offset += h + 10

    winfx.apply_rounded_region(win, radius=10)
    _apply_backdrop(win)
    winfx.fade_in(win, target=0.96, duration_ms=160)

    if kind in ("warn", "error"):
        # Brief urgency pulse on the accent dot so warn/error toasts stand out
        # from routine info ones, not just by colour but by motion.
        bright = _hex_blend(accent, "#ffffff", 0.6)

        def _pulse(n: int = 0) -> None:
            if n >= 4:
                return
            start, end = (accent, bright) if n % 2 == 0 else (bright, accent)
            winfx.ease_color(dot_lbl, "fg", start, end, duration_ms=160, steps=6,
                             on_done=lambda: _pulse(n + 1))
        _pulse()

    # Auto-dismiss after 4.5s (or stay if action button present)
    dismiss_ms = 7000 if action_label else 4500

    def _dismiss():
        global _toast_offset
        _toast_offset = max(0, _toast_offset - h - 10)
        winfx.fade_out_then_destroy(win, duration_ms=180)

    win.after(dismiss_ms, _dismiss)


# ---------------------------------------------------------------------------
# Screen-edge flash (item 21)
# ---------------------------------------------------------------------------

def _show_edge_flash(colour: str) -> None:
    """Thin horizontal bar at the top of the screen, fades in then out fast."""
    win = tk.Toplevel(_root)
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    win.attributes("-alpha", 0.0)
    win.configure(bg=colour)

    sw = win.winfo_screenwidth()
    win.geometry(f"{sw}x3+0+0")

    # Fade in fast, hold briefly, fade out
    winfx.fade_to(win, 0.65, duration_ms=80,
                  on_done=lambda: win.after(
                      120, lambda: winfx.fade_out_then_destroy(win, duration_ms=180)))


# ---------------------------------------------------------------------------
# Status badge (recording / processing / feedback)
# ---------------------------------------------------------------------------

def _badge_cfg(state: str) -> dict:
    """Per-state badge config, theme-aware (QUIETT_UI_PLAN P4: recording ->
    rec, transcribing/processing -> pause; the waveform bars themselves stay
    ion regardless of state — see _draw_badge_frame). Built fresh per call so
    a theme switch is picked up immediately, same as every other colour in
    this module."""
    cfgs = {
        "recording":   {"accent": _REC, "logo_bg": _rgb(_REC), "status": "Recording…"},
        "processing":  {"accent": _PAUSE, "logo_bg": _rgb(_PAUSE), "status": "Processing…"},
        "too_short":   {"accent": _FG2, "text": "Hold longer to record"},
        "not_ready":   {"accent": _FG2, "text": "Model loading, please wait"},
        # Designed error states (BACKLOG item 48) — red accent + a plain-English
        # next step, never a raw exception. "error": True marks these for the
        # audible cue in _handle_badge, distinct from the benign grey hints above.
        "mic_error":   {"accent": _REC, "error": True,
                         "text": "No microphone found. Check Settings → Microphone"},
        "model_error": {"accent": _REC, "error": True,
                         "text": "Whisper couldn't start. Check Settings or app.log"},
    }
    return cfgs.get(state, cfgs["processing"])

# Visual layout for the recording badge
_BADGE_W       = _px(320)
_BADGE_H       = _px(64)
_LOGO_SIZE     = _px(44)
_WAVE_BAR_N    = 28
_WAVE_BAR_W    = _px(4)
_WAVE_BAR_GAP  = _px(2)


def _badge_alive() -> bool:
    """Return True only if _badge_win is a live Tkinter window."""
    global _badge_win, _badge_label, _badge_dot, _badge_canvas
    global _badge_time, _badge_logo_lbl, _badge_logo_variants
    global _badge_partial_lbl, _badge_status_lbl
    global _badge_gradient_img, _badge_gradient_id
    global _badge_stop_lbl, _badge_cancel_lbl, _badge_hover_visible, _badge_hover_job
    if _badge_win is None:
        return False
    try:
        _badge_win.winfo_exists()
        return True
    except Exception:
        _badge_win = None
        _badge_label = None
        _badge_dot = None
        _badge_canvas = None
        _badge_time = None
        _badge_logo_lbl = None
        _badge_logo_variants = []
        _badge_partial_lbl = None
        _badge_status_lbl = None
        _badge_gradient_img = None
        _badge_gradient_id = None
        _badge_stop_lbl = None
        _badge_cancel_lbl = None
        _badge_hover_visible = False
        _badge_hover_job = None
        return False


_LOGO_PULSE_LEVELS = 6  # number of brightness variants for the logo pulse


def _build_logo_variants(bg: tuple) -> list[ImageTk.PhotoImage]:
    """Pre-render the overlay logo at several brightness levels for the energy pulse."""
    base = tray.make_logo(target_size=_LOGO_SIZE, bg=bg)
    variants = []
    for i in range(_LOGO_PULSE_LEVELS):
        factor = 1.0 + (i / (_LOGO_PULSE_LEVELS - 1)) * 0.30  # 1.00 → 1.30
        if i == 0:
            img = base
        else:
            img = ImageEnhance.Brightness(base).enhance(factor)
        variants.append(ImageTk.PhotoImage(img))
    return variants


# ---------------------------------------------------------------------------
# Hover-to-reveal stop/cancel controls on the recording badge (item 8)
# ---------------------------------------------------------------------------

def _badge_stop_clicked(_event=None) -> None:
    """Finish the recording now — identical to releasing the hotkey.
    Dispatched off the Tk thread since audio.stop() calls straight back into
    main.py's transcription pipeline."""
    if _badge_state != "recording":
        return
    threading.Thread(target=audio.stop, daemon=True).start()


def _badge_cancel_clicked(_event=None) -> None:
    """Discard the recording — no transcription, mirrors a too-short release
    without the "hold longer" toast, since this is a deliberate cancel."""
    if _badge_state != "recording":
        return

    def _do() -> None:
        audio.cancel()
        hotkey.set_external_recording(False)
        try:
            tray.set_state("idle")
        except Exception:
            pass
        hide_badge()

    threading.Thread(target=_do, daemon=True).start()


def _badge_hover_set(visible: bool) -> None:
    global _badge_hover_visible
    if visible and _badge_state != "recording":
        return
    if _badge_hover_visible == visible:
        return
    _badge_hover_visible = visible
    stop_target = _FG2 if visible else _BG
    cancel_target = _REC if visible else _BG
    if _badge_stop_lbl is not None:
        try:
            winfx.ease_color(_badge_stop_lbl, "fg", _badge_stop_lbl.cget("fg"),
                             stop_target, duration_ms=150, steps=6)
        except Exception:
            pass
    if _badge_cancel_lbl is not None:
        try:
            winfx.ease_color(_badge_cancel_lbl, "fg", _badge_cancel_lbl.cget("fg"),
                             cancel_target, duration_ms=150, steps=6)
        except Exception:
            pass


def _badge_hover_force_hide() -> None:
    """Snap the hover controls invisible with no animation — used when the
    badge state moves away from recording (e.g. into processing) so a stale
    hover doesn't carry over onto a badge that can no longer be stopped."""
    global _badge_hover_visible, _badge_hover_job
    _badge_hover_visible = False
    if _badge_hover_job is not None:
        try:
            _badge_win.after_cancel(_badge_hover_job)
        except Exception:
            pass
        _badge_hover_job = None
    for lbl in (_badge_stop_lbl, _badge_cancel_lbl):
        if lbl is not None:
            try:
                lbl.config(fg=_BG)
            except Exception:
                pass


def _badge_on_enter(_event=None) -> None:
    global _badge_hover_job
    if _badge_hover_job is not None:
        try:
            _badge_win.after_cancel(_badge_hover_job)
        except Exception:
            pass
        _badge_hover_job = None
    _badge_hover_set(True)


def _badge_on_leave(_event=None) -> None:
    global _badge_hover_job

    def _apply() -> None:
        global _badge_hover_job
        _badge_hover_job = None
        _badge_hover_set(False)

    _badge_hover_job = _badge_win.after(120, _apply)


def _build_recording_badge(cfg: dict) -> None:
    """Create the large recording badge with logo + waveform canvas + elapsed timer."""
    global _badge_win, _badge_canvas, _badge_time
    global _badge_label, _badge_dot, _badge_logo_lbl
    global _badge_logo_variants, _badge_logo_idx, _badge_smoothed
    global _badge_partial_lbl, _badge_status_lbl
    global _badge_gradient_img, _badge_gradient_id
    global _badge_stop_lbl, _badge_cancel_lbl, _badge_hover_visible, _badge_hover_job

    _badge_win = tk.Toplevel(_root)
    _badge_win.overrideredirect(True)
    _badge_win.attributes("-topmost", True)
    _badge_win.configure(bg=_BG)
    _badge_win.attributes("-alpha", 0.0)  # winfx fades in

    # Outer frame draws a faint 1-px ring on _BG2 against _BG fill — pseudo-depth
    outer = tk.Frame(_badge_win, bg=_BORDER, bd=0)
    outer.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
    inner = tk.Frame(outer, bg=_BG, padx=12, pady=10)
    inner.pack(fill=tk.BOTH, expand=True)

    row = tk.Frame(inner, bg=_BG)
    row.pack(fill=tk.X)

    _badge_logo_variants = _build_logo_variants(cfg["logo_bg"])
    _badge_logo_idx = 0
    _badge_logo_lbl = tk.Label(row, image=_badge_logo_variants[0], bg=_BG, bd=0)
    _badge_logo_lbl.pack(side=tk.LEFT, padx=(0, 12))

    wave_w = _WAVE_BAR_N * (_WAVE_BAR_W + _WAVE_BAR_GAP)
    _badge_canvas = tk.Canvas(
        row, width=wave_w, height=_LOGO_SIZE,
        bg=_BG, bd=0, highlightthickness=0,
    )
    _badge_canvas.pack(side=tk.LEFT)

    _badge_time = tk.Label(
        row, text="0:00", bg=_BG, fg=_FG2,
        font=("Segoe UI Variable Display", 11, "normal"),
        width=4, anchor="e",
    )
    _badge_time.pack(side=tk.LEFT, padx=(12, 0))

    # Hover-reveal controls (item 8) — reserved in the layout at all times
    # (fg starts matching the background, i.e. invisible) so fading them in
    # on hover never resizes or shifts the badge. Click actions call straight
    # into audio.stop()/audio.cancel(), the same functions the hotkey release
    # already uses (hotkey.configure(on_stop=audio.stop, on_cancel=audio.cancel)
    # in main.py) — no new plumbing needed.
    _badge_stop_lbl = tk.Label(
        row, text="⏹", bg=_BG, fg=_BG, font=(_FONT_FAM_TEXT, 11), cursor="hand2",
    )
    _badge_stop_lbl.pack(side=tk.LEFT, padx=(14, 0))
    _badge_stop_lbl.bind("<Button-1>", _badge_stop_clicked)

    _badge_cancel_lbl = tk.Label(
        row, text="✕", bg=_BG, fg=_BG, font=(_FONT_FAM_TEXT, 11), cursor="hand2",
    )
    _badge_cancel_lbl.pack(side=tk.LEFT, padx=(8, 0))
    _badge_cancel_lbl.bind("<Button-1>", _badge_cancel_clicked)

    _badge_hover_visible = False
    _badge_hover_job = None
    _badge_win.bind("<Enter>", _badge_on_enter)
    _badge_win.bind("<Leave>", _badge_on_leave)

    # Dedicated status line — own zone below the waveform row, distinct from
    # the partial-transcript line below it so state text and dictated text
    # never occupy or overwrite the same space.
    _badge_status_lbl = tk.Label(
        inner, text=cfg.get("status", ""), bg=_BG, fg=_FG3,
        font=(_FONT_FAM_TEXT, 8), anchor="w",
    )
    _badge_status_lbl.pack(fill=tk.X, pady=(6, 0))

    # Live partial transcription line — packed lazily when text first arrives.
    # A Text widget (not Label) so the stabilised prefix and the still-changing
    # tail can render in different colours without a full-line redraw each
    # tick (item 51). Wrapped ourselves (see _wrap_words_to_lines) against a
    # fixed, DPI-scaled pixel width so height can grow 1 -> 2 lines exactly
    # with the content, then cap there.
    _partial_frame = tk.Frame(inner, bg=_BG, width=_px(300))
    _partial_frame.pack_propagate(False)
    _badge_partial_lbl = tk.Text(
        _partial_frame, height=1, bg=_BG, fg=_FG3, bd=0, highlightthickness=0,
        font=(_FONT_FAM_TEXT, 9), wrap="none", cursor="arrow",
        padx=0, pady=0,
    )
    _badge_partial_lbl.pack(fill=tk.BOTH, expand=True)
    _badge_partial_lbl.tag_configure("committed", foreground=_FG2)
    _badge_partial_lbl.tag_configure("tail", foreground=_FG3)
    _badge_partial_lbl.configure(state="disabled")
    _reset_partial_stability()

    _badge_label = None
    _badge_dot = None
    _badge_smoothed = [0.0] * _WAVE_BAR_N
    _badge_gradient_img = None
    _badge_gradient_id = None

    _badge_win.update_idletasks()
    w = _badge_win.winfo_reqwidth()
    h = _badge_win.winfo_reqheight()
    sw = _badge_win.winfo_screenwidth()
    sh = _badge_win.winfo_screenheight()
    _badge_win.geometry(f"{w}x{h}+{sw - w - 20}+{sh - h - 60}")

    winfx.apply_rounded_region(_badge_win, radius=14)
    _apply_backdrop(_badge_win)
    _play_entrance(_badge_win, 0.96, dy=_px(16), duration_ms=200)


def _build_text_badge(cfg: dict) -> None:
    """Lightweight text badge for too_short / not_ready toasts."""
    global _badge_win, _badge_label, _badge_dot
    global _badge_canvas, _badge_time, _badge_logo_lbl, _badge_logo_variants
    global _badge_partial_lbl, _badge_status_lbl
    global _badge_stop_lbl, _badge_cancel_lbl
    _badge_partial_lbl = None
    _badge_status_lbl = None

    _badge_win = tk.Toplevel(_root)
    _badge_win.overrideredirect(True)
    _badge_win.attributes("-topmost", True)
    _badge_win.configure(bg=_BG)
    _badge_win.attributes("-alpha", 0.0)

    outer = tk.Frame(_badge_win, bg=_BORDER, bd=0)
    outer.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
    inner = tk.Frame(outer, bg=_BG, padx=12, pady=8)
    inner.pack(fill=tk.BOTH, expand=True)

    _badge_dot = tk.Label(inner, text="●", bg=_BG,
                          font=("Segoe UI", 9), fg=cfg["accent"])
    _badge_dot.pack(side=tk.LEFT, padx=(0, 6))

    _badge_label = tk.Label(inner, text=cfg.get("text", ""), bg=_BG,
                            fg=_FG, font=("Segoe UI Variable Text", 10))
    _badge_label.pack(side=tk.LEFT)

    _badge_canvas = None
    _badge_time = None
    _badge_logo_lbl = None
    _badge_logo_variants = []
    _badge_stop_lbl = None
    _badge_cancel_lbl = None

    _badge_win.update_idletasks()
    w = _badge_win.winfo_reqwidth()
    h = _badge_win.winfo_reqheight()
    sw = _badge_win.winfo_screenwidth()
    sh = _badge_win.winfo_screenheight()
    _badge_win.geometry(f"{w}x{h}+{sw - w - 20}+{sh - h - 60}")

    winfx.apply_rounded_region(_badge_win, radius=10)
    _apply_backdrop(_badge_win)
    _play_entrance(_badge_win, 0.94, dy=_px(16), duration_ms=200)


def _hex_blend(hex_a: str, hex_b: str, t: float) -> str:
    """Blend two #rrggbb colours. t=0 → a, t=1 → b."""
    a = int(hex_a[1:3], 16), int(hex_a[3:5], 16), int(hex_a[5:7], 16)
    b = int(hex_b[1:3], 16), int(hex_b[3:5], 16), int(hex_b[5:7], 16)
    r = int(a[0] + (b[0] - a[0]) * t)
    g = int(a[1] + (b[1] - a[1]) * t)
    bl = int(a[2] + (b[2] - a[2]) * t)
    return f"#{r:02x}{g:02x}{bl:02x}"


# Real vertical gradient (item 17) — a single column is rendered once into a
# PIL image per (accent, theme background, height) and reused every frame via
# crop + paste, instead of re-blending stacked colour bands on the fly.
_gradient_col_cache: dict[tuple, "Image.Image"] = {}


def _gradient_column(accent: str, height: int, width: int) -> "Image.Image":
    """One smooth vertical gradient column, row 0 = brightest (nearest the
    waveform's centreline) fading toward the panel background at the far row.
    Cropped from the top for shorter bars, so nearby bars always agree on
    colour regardless of how tall any one of them currently is."""
    height = max(1, height)
    width = max(1, width)
    key = (accent, _BG, height, width)
    cached = _gradient_col_cache.get(key)
    if cached is not None:
        return cached
    bright = _hex_blend(accent, "#ffffff", 0.18)
    faded  = _hex_blend(accent, _BG, 0.55)
    img = Image.new("RGB", (width, height))
    for y in range(height):
        t = y / max(1, height - 1)
        img.paste(_hex_blend(bright, faded, t), (0, y, width, y + 1))
    _gradient_col_cache[key] = img
    return img


# Full-width symmetric gradient (item 7) — bright at the waveform's
# centreline, fading toward the panel background at the top and bottom
# edges. Built from the same per-row blend as the bar gradient above, just
# spanning the whole canvas so a continuous waveform shape can be cut out of
# it with an alpha mask instead of cropping/pasting per discrete bar.
_gradient_full_cache: dict[tuple, "Image.Image"] = {}


def _gradient_full(accent: str, width: int, height: int) -> "Image.Image":
    width = max(1, width)
    height = max(1, height)
    key = (accent, _BG, width, height)
    cached = _gradient_full_cache.get(key)
    if cached is not None:
        return cached
    mid_i = height // 2
    half_h = max(mid_i, height - mid_i) + 1
    col = _gradient_column(accent, half_h, width)
    img = Image.new("RGB", (width, height), _BG)
    top = col.crop((0, 0, width, min(half_h, mid_i))).transpose(Image.FLIP_TOP_BOTTOM)
    img.paste(top, (0, mid_i - top.height))
    bottom = col.crop((0, 0, width, min(half_h, height - mid_i)))
    img.paste(bottom, (0, mid_i))
    _gradient_full_cache[key] = img
    return img


def _badge_animation_style() -> str:
    """Which of _BADGE_ANIM_RENDERERS draws the badge's live level meter
    (item 12, config key `badge_animation`). Unknown/missing values fall
    back to the default "waveform" style. Dashboard picker UI is out of
    scope here — this just reads whatever config.json holds."""
    try:
        with open(_CONFIG_FILE, encoding="utf-8") as f:
            style = json.load(f).get("badge_animation", "waveform")
    except Exception:
        style = "waveform"
    return style if style in _BADGE_ANIM_RENDERERS else "waveform"


def _render_waveform_frame(smoothed: list, accent: str, canvas_w: int, canvas_h: int) -> "Image.Image":
    """Default style (item 7): the per-bar amplitude history is upsampled via
    a PIL grayscale resize — BICUBIC interpolates between samples for a
    smooth curve instead of a stair-step — then rasterised as a filled
    mirrored band at 3x supersample and downscaled with LANCZOS for
    antialiasing, then cut from a vertical gradient with an alpha mask."""
    max_half = canvas_h * 0.44
    SS = 3  # supersample factor
    big_w, big_h = canvas_w * SS, canvas_h * SS
    mid_big = big_h / 2.0
    max_half_big = max_half * SS

    small = Image.new("L", (len(smoothed), 1))
    small.putdata([int(max(0.0, min(1.0, v)) * 255) for v in smoothed])
    curve_vals = small.resize((big_w, 1), Image.BICUBIC).getdata()

    mask_big = Image.new("L", (big_w, big_h), 0)
    top_pts, bottom_pts = [], []
    for x in range(big_w):
        amp = max(0.0, min(1.0, curve_vals[x] / 255.0))
        h_big = max(SS, amp * max_half_big)
        top_pts.append((x, mid_big - h_big))
        bottom_pts.append((x, mid_big + h_big))
    ImageDraw.Draw(mask_big).polygon(top_pts + bottom_pts[::-1], fill=255)
    mask = mask_big.resize((canvas_w, canvas_h), Image.LANCZOS)

    grad = _gradient_full(accent, canvas_w, canvas_h)
    bg_layer = Image.new("RGB", (canvas_w, canvas_h), _BG)
    return Image.composite(grad, bg_layer, mask)


def _render_bars_frame(smoothed: list, accent: str, canvas_w: int, canvas_h: int) -> "Image.Image":
    """"bars" style (item 12): the classic discrete equalizer bars, mirrored
    from the centreline with rounded caps — simpler and punchier than the
    continuous waveform, closer to a classic voice-recorder meter."""
    img = Image.new("RGB", (canvas_w, canvas_h), _BG)
    draw = ImageDraw.Draw(img)
    max_half = canvas_h * 0.44
    mid = canvas_h / 2.0
    radius = max(1, _WAVE_BAR_W // 2)
    step = _WAVE_BAR_W + _WAVE_BAR_GAP
    for i, amp in enumerate(smoothed):
        amp = max(0.0, min(1.0, amp))
        half_h = max(radius, amp * max_half)
        x0 = i * step
        x1 = x0 + _WAVE_BAR_W
        draw.rounded_rectangle([x0, mid - half_h, x1, mid + half_h],
                               radius=radius, fill=accent)
    return img


_pulse_falloff_cache: dict[tuple, "Image.Image"] = {}


def _pulse_falloff(canvas_w: int, canvas_h: int) -> "Image.Image":
    """Cached radial falloff (grayscale, 255 at centre fading to 0 at the
    corners) reused every frame for the "pulse" style — built once per
    canvas size rather than per tick."""
    key = (canvas_w, canvas_h)
    cached = _pulse_falloff_cache.get(key)
    if cached is not None:
        return cached
    cx, cy = canvas_w / 2.0, canvas_h / 2.0
    max_dist = math.hypot(cx, cy) or 1.0
    img = Image.new("L", (canvas_w, canvas_h))
    px = img.load()
    for y in range(canvas_h):
        for x in range(canvas_w):
            dist = math.hypot(x - cx, y - cy) / max_dist
            px[x, y] = int(max(0.0, 1.0 - dist) * 255)
    _pulse_falloff_cache[key] = img
    return img


def _render_pulse_frame(smoothed: list, accent: str, canvas_w: int, canvas_h: int) -> "Image.Image":
    """"pulse" style (item 12): a soft breathing glow of the accent colour,
    centred on the badge, that brightens and widens with the recording's
    overall energy instead of reading out per-bar detail."""
    energy = max(0.0, min(1.0, sum(smoothed) / max(1, len(smoothed))))
    falloff = _pulse_falloff(canvas_w, canvas_h)
    factor = 0.5 + 1.5 * energy
    mask = falloff.point(lambda v: min(255, int(v * factor)))
    glow = Image.new("RGB", (canvas_w, canvas_h), accent)
    bg_layer = Image.new("RGB", (canvas_w, canvas_h), _BG)
    return Image.composite(glow, bg_layer, mask)


# One function per style (item 12) — a future style is just one more entry
# here plus a line in the dashboard Settings select (not built in this pass;
# dashboard.py is owned elsewhere right now).
_BADGE_ANIM_RENDERERS = {
    "waveform": _render_waveform_frame,
    "pulse":    _render_pulse_frame,
    "bars":     _render_bars_frame,
}


def _draw_badge_frame() -> None:
    """Render one frame of the badge's live level meter + update the timer + pulse logo."""
    global _badge_anim_phase, _badge_logo_idx, _badge_gradient_img, _badge_gradient_id
    if _badge_canvas is None:
        return

    cfg = _badge_cfg(_badge_state or "recording")
    # The waveform itself is always ion (QUIETT_UI_PLAN P4) — cfg["accent"]
    # (rec while recording, pause while processing) instead tints the logo
    # pulse built at badge-open time, so the state read is still visible
    # without recolouring the level meter people are actually watching.
    accent = _BLUE

    try:
        canvas_h = _LOGO_SIZE
        motion_on = _animations_enabled()

        if not motion_on:
            # Motion policy (item 47/12): every style renders a static frame
            # when animations are off — no phase advance, no live audio
            # levels, just a fixed neutral amplitude.
            targets = [0.4] * _WAVE_BAR_N
        elif _badge_state == "recording":
            levels = audio.get_recent_levels(_WAVE_BAR_N)
            silence = audio.get_silence_elapsed()
            timeout = audio.get_silence_timeout()
            is_silent_warn = timeout > 0 and silence > 1.5
            if is_silent_warn:
                accent = _PAUSE

            def norm(v: float) -> float:
                v = min(1.0, v / 0.20)
                return v ** 0.6

            targets = [norm(v) for v in levels]
            _badge_anim_phase += 0.25
            shimmer = 0.05
            targets = [
                max(h, shimmer + 0.04 * math.sin(_badge_anim_phase + i * 0.45))
                for i, h in enumerate(targets)
            ]
        else:
            _badge_anim_phase += 0.22
            targets = [
                0.22 + 0.45 * (0.5 + 0.5 * math.sin(_badge_anim_phase - i * 0.35))
                for i in range(_WAVE_BAR_N)
            ]

        # Smooth interpolation: snap up fast, ease down slow (ease-out decay).
        # Skipped when motion is off so the static frame doesn't ease its way
        # to flat over the first few ticks — it's just flat immediately.
        if not motion_on:
            _badge_smoothed[:] = targets
        elif len(_badge_smoothed) != _WAVE_BAR_N:
            _badge_smoothed[:] = list(targets)
        else:
            for i, t in enumerate(targets):
                cur = _badge_smoothed[i]
                if t > cur:
                    _badge_smoothed[i] = cur * 0.45 + t * 0.55     # fast attack
                else:
                    _badge_smoothed[i] = cur * 0.78 + t * 0.22     # slow release

        canvas_w = _WAVE_BAR_N * (_WAVE_BAR_W + _WAVE_BAR_GAP)
        canvas_h_i = int(canvas_h)

        renderer = _BADGE_ANIM_RENDERERS.get(_badge_animation_style(), _render_waveform_frame)
        frame_img = renderer(_badge_smoothed, accent, canvas_w, canvas_h_i)
        _badge_gradient_img = ImageTk.PhotoImage(frame_img)
        if _badge_gradient_id is None:
            _badge_gradient_id = _badge_canvas.create_image(0, 0, anchor="nw", image=_badge_gradient_img)
        else:
            _badge_canvas.itemconfig(_badge_gradient_id, image=_badge_gradient_img)

        # Logo brightness pulse based on overall energy
        if _badge_logo_lbl is not None and _badge_logo_variants:
            energy = sum(_badge_smoothed) / max(1, len(_badge_smoothed))
            idx = max(0, min(_LOGO_PULSE_LEVELS - 1,
                             int(energy * (_LOGO_PULSE_LEVELS - 0.5))))
            if idx != _badge_logo_idx:
                _badge_logo_idx = idx
                try:
                    _badge_logo_lbl.config(image=_badge_logo_variants[idx])
                except Exception:
                    pass

        if _badge_time is not None and _badge_state == "recording":
            elapsed = int(audio.get_elapsed())
            new_t = f"{elapsed // 60}:{elapsed % 60:02d}"
            if _badge_time.cget("text") != new_t:
                _badge_time.config(text=new_t)
        elif _badge_time is not None and _badge_state != "recording":
            if _badge_time.cget("text") != "":
                _badge_time.config(text="")
    except Exception:
        pass


def _reset_partial_stability() -> None:
    """Clear word-stabilisation state. Call whenever a fresh recording badge
    is built so leftover text from the previous dictation can't bleed into
    the next one's stabilisation comparison."""
    global _partial_prev_words, _partial_committed_n
    _partial_prev_words = []
    _partial_committed_n = 0


def _partial_font() -> "tkfont.Font":
    """Lazily-created Font matching the partial line's rendering, used purely
    for pixel-width measurement (no live window needed, so this is safe to
    call and reason about without visually running the app)."""
    global _partial_font_cache
    if _partial_font_cache is None:
        _partial_font_cache = tkfont.Font(family=_FONT_FAM_TEXT, size=9)
    return _partial_font_cache


def _wrap_words_to_lines(fnt: "tkfont.Font", words_tags: list,
                         max_px: int) -> list:
    """Greedy word-wrap of [(word, tag), ...] to lines no wider than max_px
    (measured with `fnt`). Returns a list of lines, each a list of
    (word, tag) pairs. Wrapping is computed ourselves (rather than relying on
    the Text widget's own wrap engine) so the line count used to size the
    widget always matches exactly what gets rendered."""
    lines: list = [[]]
    cur_width = 0
    space_w = fnt.measure(" ")
    for word, tag in words_tags:
        w = fnt.measure(word)
        if lines[-1] and cur_width + space_w + w > max_px:
            lines.append([])
            cur_width = 0
        if lines[-1]:
            cur_width += space_w
        lines[-1].append((word, tag))
        cur_width += w
    return lines


def _update_badge_partial(text: str) -> None:
    """Show/refresh the live partial transcription line under the waveform.

    Stabilises the display instead of re-rendering the whole line every tick:
    a word that lands at the same position in this partial and the previous
    one has now been seen unchanged twice in a row, so it's rendered as
    "committed" (normal muted colour); anything after that point is still
    shifting under whisper's revisions and renders extra-muted. Wraps to a
    DPI-scaled fixed width, growing 1 -> 2 lines with content and capping
    there — once full, older (committed) words drop off the front behind a
    leading ellipsis so the live edge (the tail) is always visible.
    """
    global _partial_prev_words, _partial_committed_n
    if not _badge_alive() or _badge_partial_lbl is None:
        return
    try:
        new_words = text.split()

        # Longest common prefix with the previous partial: a word only
        # counts as committed once it's shown up unchanged at the same
        # position twice in a row. Recomputed fresh each tick (not carried
        # forward) so a later revision to an earlier word correctly drops it
        # back to the muted "tail" style instead of leaving stale text
        # looking falsely locked-in.
        n = 0
        while (n < len(new_words) and n < len(_partial_prev_words)
               and new_words[n] == _partial_prev_words[n]):
            n += 1
        _partial_committed_n = n
        _partial_prev_words = new_words

        frame = _badge_partial_lbl.master
        if not new_words:
            frame.pack_forget()
            return

        words_tags = ([(w, "committed") for w in new_words[:_partial_committed_n]]
                      + [(w, "tail") for w in new_words[_partial_committed_n:]])
        box_w = _px(300)
        lines = _wrap_words_to_lines(_partial_font(), words_tags, box_w)
        truncated = len(lines) > _PARTIAL_MAX_LINES
        visible = lines[-_PARTIAL_MAX_LINES:] if truncated else lines

        if not frame.winfo_ismapped():
            frame.pack(fill=tk.X, pady=(6, 0))

        _badge_partial_lbl.configure(state="normal")
        _badge_partial_lbl.delete("1.0", "end")
        if truncated:
            _badge_partial_lbl.insert("end", "…", "committed")
        for i, line in enumerate(visible):
            if truncated and i == 0 and line:
                _badge_partial_lbl.insert("end", " ", "committed")
            for j, (word, tag) in enumerate(line):
                if j > 0:
                    _badge_partial_lbl.insert("end", " ")
                _badge_partial_lbl.insert("end", word, tag)
            if i < len(visible) - 1:
                _badge_partial_lbl.insert("end", "\n")
        n_lines = max(1, len(visible))
        _badge_partial_lbl.configure(state="disabled", height=n_lines)
        # pack_propagate(False) on the wrapper frame means it won't auto-fit
        # its child's height, so it must be given an explicit pixel height —
        # otherwise the frame (and the badge) stays stuck at its very first
        # size no matter how many lines the text wraps to.
        frame.configure(height=_partial_font().metrics("linespace") * n_lines)

        # Badge grows with the text — recompute size and keep it anchored
        # to the bottom-right corner.
        _badge_win.update_idletasks()
        w = _badge_win.winfo_reqwidth()
        h = _badge_win.winfo_reqheight()
        sw = _badge_win.winfo_screenwidth()
        sh = _badge_win.winfo_screenheight()
        _badge_win.geometry(f"{w}x{h}+{sw - w - 20}+{sh - h - 60}")
        winfx.apply_rounded_region(_badge_win, radius=14)
    except Exception:
        pass


def _handle_badge(cmd: str | None) -> None:
    global _badge_win, _badge_state, _badge_logo_variants, _badge_logo_idx
    prev = _badge_state
    _badge_state = cmd
    if cmd is not None and not _badge_alive():
        refresh_theme()  # catch theme flips before drawing a fresh badge

    if cmd is None:
        _reset_partial_stability()  # belt-and-suspenders: don't let a late-
                                     # queued partial bleed into the next recording
        if _badge_alive():
            win = _badge_win
            _badge_win = None  # null first so reference is released
            winfx.fade_out_then_destroy(win, duration_ms=160)
        return

    cfg = _badge_cfg(cmd)
    needs_wave = cmd in ("recording", "processing")

    # Rebuild if widget type doesn't match the new state
    have_wave = _badge_canvas is not None
    if _badge_alive() and have_wave != needs_wave:
        try:
            _badge_win.destroy()
        except Exception:
            pass
        _badge_win = None

    if not _badge_alive():
        # Screen-edge flash is the fallback signal for this state change —
        # only fired if the badge itself fails to build (item 46). In the
        # normal flow the badge's own entrance animation is the signal, so
        # main.py no longer flashes unconditionally on recording start.
        try:
            if needs_wave:
                _build_recording_badge(cfg)
                _draw_badge_frame()
            else:
                _build_text_badge(cfg)
                if cfg.get("error"):
                    chime.play_error()
        except Exception as exc:
            log_error("preview", f"badge build failed, falling back to edge flash: {exc}")
            _badge_win = None
            _show_edge_flash(cfg.get("accent", _BLUE))
        return

    # Live update existing badge
    if needs_wave:
        if prev != cmd:
            if cmd != "recording":
                # e.g. recording -> processing: stale hover controls from the
                # recording state shouldn't linger on a badge that can no
                # longer be stopped/cancelled (item 8).
                _badge_hover_force_hide()
            _badge_logo_variants = _build_logo_variants(cfg["logo_bg"])
            _badge_logo_idx = 0
            if _badge_logo_lbl is not None:
                try:
                    _badge_logo_lbl.config(image=_badge_logo_variants[0])
                except Exception:
                    pass
            if _badge_status_lbl is not None:
                try:
                    _badge_status_lbl.config(text=cfg.get("status", ""))
                except Exception:
                    pass
        _draw_badge_frame()
    else:
        try:
            if _badge_dot:
                _badge_dot.config(fg=cfg["accent"])
            if _badge_label:
                _badge_label.config(text=cfg.get("text", ""))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Dictation preview window
# ---------------------------------------------------------------------------

def _calc_position(cx: int, cy: int, w: int, h: int,
                   sw: int, sh: int, mode: str) -> tuple[int, int]:
    pad = 16
    positions = {
        "cursor":       (max(0, min(cx + 12, sw - w)), max(0, min(cy + 12, sh - h))),
        "top-right":    (sw - w - pad,   pad),
        "bottom-right": (sw - w - pad,   sh - h - 56),
        "top-left":     (pad,            pad),
        "bottom-left":  (pad,            sh - h - 56),
        "center":       ((sw - w) // 2,  (sh - h) // 2),
    }
    return positions.get(mode, positions["cursor"])


# ---------------------------------------------------------------------------
# Per-monitor drag position persistence (item 54)
# ---------------------------------------------------------------------------

def _monitor_key_and_workarea(x: int, y: int) -> tuple[str, tuple[int, int, int, int]]:
    """Device name + work-area rect (left, top, right, bottom) of the monitor
    containing point (x, y). Falls back to a generic key/rect on failure."""
    try:
        hmon = win32api.MonitorFromPoint((x, y), win32con.MONITOR_DEFAULTTONEAREST)
        info = win32api.GetMonitorInfo(hmon)
        return info.get("Device", "default"), info.get("Work", (0, 0, 1920, 1080))
    except Exception:
        return "default", (0, 0, 1920, 1080)


def _clamp_to_workarea(x: int, y: int, w: int, h: int,
                       work: tuple[int, int, int, int]) -> tuple[int, int]:
    left, top, right, bottom = work
    x = max(left, min(x, right - w))
    y = max(top, min(y, bottom - h))
    return x, y


def _load_panel_position(monitor_key: str) -> tuple[int, int] | None:
    try:
        with open(_CONFIG_FILE, encoding="utf-8") as f:
            saved = json.load(f).get("panel_position", {}).get(monitor_key)
        if isinstance(saved, dict):
            return int(saved["x"]), int(saved["y"])
    except Exception:
        pass
    return None


def _save_panel_position(monitor_key: str, x: int, y: int) -> None:
    try:
        with open(_CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    positions = cfg.get("panel_position")
    if not isinstance(positions, dict):
        positions = {}
    positions[monitor_key] = {"x": x, "y": y}
    cfg["panel_position"] = positions
    try:
        with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


_APP_NAMES = {
    "code.exe": "VS Code", "chrome.exe": "Chrome", "msedge.exe": "Edge",
    "firefox.exe": "Firefox", "explorer.exe": "Explorer",
    "windowsterminal.exe": "Terminal", "wt.exe": "Terminal",
    "notepad.exe": "Notepad", "outlook.exe": "Outlook",
    "winword.exe": "Word", "excel.exe": "Excel", "slack.exe": "Slack",
    "discord.exe": "Discord", "mstsc.exe": "Remote Desktop",
}


def _target_app_label(hwnd: int) -> str:
    """Friendly name of the app that will receive the paste, '' if unknown."""
    try:
        exe = inject._get_exe_name(hwnd)
        if not exe:
            return ""
        return _APP_NAMES.get(exe, exe.rsplit(".", 1)[0].capitalize())
    except Exception:
        return ""


def _border_colour(confidence: float | None) -> str:
    if confidence is None:
        return _BORDER
    if confidence >= 0.6:
        return _BLUE       # confident — ion
    if confidence >= 0.3:
        return _PAUSE      # uncertain — amber
    return _REC            # low confidence — alarm red


def _open_window(text: str, hwnd: int, empty: bool = False,
                 confidence: float | None = None,
                 auto_dismiss: float = 0.0,
                 words: list | None = None,
                 raw: str | None = None,
                 duration: float = 0.0,
                 incognito: bool = False) -> None:
    global _current_preview_win

    refresh_theme()  # catch theme flips before drawing a fresh panel
    try:
        cx, cy = win32api.GetCursorPos()
    except Exception:
        cx, cy = 200, 200

    win = tk.Toplevel(_root)
    _current_preview_win = win
    win.overrideredirect(True)
    win.configure(bg=_BG)
    win.attributes("-topmost", True)
    win.attributes("-alpha", 0.0)   # winfx fades in at end

    accent    = _BLUE
    accent_hv = _BLUE_HV

    # 1-pixel outer ring for depth
    ring = tk.Frame(win, bg=_BORDER2, bd=0)
    ring.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

    # Premium accent bar — 3px coloured strip at the very top
    tk.Frame(ring, bg=accent, height=3).pack(fill=tk.X, side=tk.TOP)

    # Auto-dismiss countdown — a thin depleting bar along the bottom edge
    # (item 53). Packed before `frame` below so it reserves its strip first;
    # `frame`'s expand=True then fills whatever space is left. Only exists
    # when there's actually a timer to visualise.
    progress_fill = None
    if auto_dismiss > 0:
        progress_track = tk.Frame(ring, bg=_BG2, height=_px(3))
        progress_track.pack(fill=tk.X, side=tk.BOTTOM)
        progress_fill = tk.Frame(progress_track, bg=accent, height=_px(3))
        progress_fill.place(relx=0.0, rely=0.0, relwidth=1.0, relheight=1.0)

    frame = tk.Frame(ring, bg=_BG, padx=20, pady=16)
    frame.pack(fill=tk.BOTH, expand=True)

    # Header row: label + status dot (left), paste destination (right).
    # Also the drag handle (item 54) — title/dot/destination are bound to
    # drag below, the pin button is excluded so its own click still toggles.
    header_row = tk.Frame(frame, bg=_BG, cursor="fleur")
    header_row.pack(fill=tk.X, pady=(0, 8))
    title_lbl = tk.Label(header_row, text="Quiett", bg=_BG, fg=_FG3,
                         font=(_FONT_FAM_DISPLAY, 9, "normal"), anchor="w", cursor="fleur")
    title_lbl.pack(side=tk.LEFT)
    # Status dot: green = ready to insert, grey = nothing usable.
    # A text glyph, not a Canvas oval — ClearType antialiases it for free.
    dot_lbl = tk.Label(header_row, text="●", bg=_BG, fg=(_FG3 if empty else _TASK),
                       font=(_FONT_FAM_TEXT, 7, "normal"), cursor="fleur")
    dot_lbl.pack(side=tk.LEFT, padx=(6, 0))
    # Incognito indicator (item 86) — muted, so it doesn't compete with the
    # destination/status but still makes the not-saved state visible.
    if incognito:
        incognito_lbl = tk.Label(header_row, text="incognito", bg=_BG, fg=_FG3,
                                 font=(_FONT_FAM_TEXT, 8, "normal"), cursor="fleur")
        incognito_lbl.pack(side=tk.LEFT, padx=(6, 0))
    # Pin — suspends the auto-dismiss countdown for long edits (item 55).
    # Only meaningful when there's a countdown running at all.
    pin_btn = None
    if auto_dismiss > 0:
        pin_btn = tk.Label(header_row, text="📌", bg=_BG, fg=_FG3,
                           font=(_FONT_FAM_TEXT, 10, "normal"), cursor="hand2")
        pin_btn.pack(side=tk.RIGHT, padx=(6, 0))

    # Paste destination — so it's obvious where Enter sends the text
    dest = _target_app_label(hwnd)
    dest_lbl = None
    if dest:
        dest_lbl = tk.Label(header_row, text=f"→  {dest}", bg=_BG, fg=_FG2,
                            font=(_FONT_FAM_TEXT, 8, "normal"), anchor="e", cursor="fleur")
        dest_lbl.pack(side=tk.RIGHT)

    # ── Text entry ─────────────────────────────────────────────────────────
    if empty:
        display = "Nothing detected — try again"
    else:
        display = text
    entry = tk.Text(
        frame, font=_FONT_BODY, wrap=tk.WORD,
        bg=_BG2, fg=_FG, insertbackground=_FG,
        relief="flat", bd=0,
        highlightthickness=1,
        highlightbackground=_BORDER,  # eases to the confidence colour once the window is up
        highlightcolor=_BLUE,
        height=4, padx=10, pady=8,
        undo=True,
    )
    entry.insert("1.0", display)
    entry.pack(fill=tk.X, pady=(0, 4))
    if not empty:
        entry.tag_add("sel", "1.0", "end-1c")
        entry.mark_set(tk.INSERT, tk.END)
    entry.focus_set()

    # Word-level confidence as underline (not foreground colour) — subtler, more pro
    if words and not empty:
        entry.tag_configure("conf_low",    underline=True, underlinefg=_REC)
        entry.tag_configure("conf_medium", underline=True, underlinefg=_PAUSE)
        search_start = "1.0"
        for w in words:
            wtext = (w.get("text") or "").strip()
            if not wtext:
                continue
            prob = w.get("prob", 1.0)
            if prob >= 0.7:
                continue
            tag = "conf_low" if prob < 0.4 else "conf_medium"
            idx = entry.search(wtext, search_start, tk.END, nocase=True)
            if idx:
                end = f"{idx}+{len(wtext)}c"
                entry.tag_add(tag, idx, end)
                search_start = end

    # ── Confidence hint ────────────────────────────────────────────────────
    if confidence is not None and not empty:
        if confidence < 0.3:
            hint_text = "Low confidence — review before inserting"
            hint_fg = _REC
        elif confidence < 0.6:
            hint_text = "Check transcription before inserting"
            hint_fg = _PAUSE
        else:
            hint_text = ""
            hint_fg = _FG2
        if hint_text:
            tk.Label(
                frame, text=hint_text, bg=_BG, fg=hint_fg,
                font=_FONT_HINT, anchor="w",
            ).pack(fill=tk.X, pady=(0, 2))

    # ── Word + char count + speaking pace ───────────────────────────────────
    # Word/char counts live-update as the user edits; WPM is fixed at open
    # time from the original transcription and recording duration (item 57)
    # — editing the text afterwards doesn't change how fast it was spoken.
    orig_word_count = len(text.split()) if not empty and text else 0
    wpm = (round(orig_word_count / (duration / 60.0))
           if duration > 0 and orig_word_count > 0 else None)

    count_var = tk.StringVar()
    tk.Label(
        frame, textvariable=count_var,
        bg=_BG, fg=_FG3, font=_FONT_META, anchor="w",
    ).pack(fill=tk.X, pady=(0, 5))

    def _update_count(*_):
        content = entry.get("1.0", "end-1c")
        wc = len(content.split()) if content.strip() else 0
        cc = len(content)
        parts = [f"{wc} word{'s' if wc != 1 else ''}", f"{cc} char{'s' if cc != 1 else ''}"]
        if wpm is not None:
            parts.append(f"{wpm} wpm")
        count_var.set("  ·  ".join(parts))
        entry.edit_modified(False)

    entry.bind("<<Modified>>", _update_count)
    _update_count()

    # ── Append checkbox ────────────────────────────────────────────────────
    append_var = tk.BooleanVar(value=False)
    tk.Checkbutton(
        frame, text="Append to selection", variable=append_var,
        bg=_BG, fg=_FG2,
        activebackground=_BG, activeforeground=_FG,
        selectcolor=_BG2,
        font=_FONT_HINT,
    ).pack(anchor=tk.W, pady=(0, 12))

    # ── Buttons ────────────────────────────────────────────────────────────
    btns = tk.Frame(frame, bg=_BG)
    btns.pack(anchor=tk.W)

    _hooks: list = []  # WH_KEYBOARD_LL hooks active while this preview is open
    _dismiss_cancel_ref = [lambda: None]  # set below when the countdown exists (item 53)

    # Raw/Cleaned toggle state (item 9) — declared here, ahead of on_insert,
    # so the correction-learning comparison below can tell which side was
    # actually showing at insert time. The toggle UI itself is only built
    # further down when raw actually differs from the cleaned text.
    _showing_raw = [False]
    _raw_originals = {"cleaned": text, "raw": raw}

    def _close() -> None:
        global _preview_open, _current_preview_win
        _dismiss_cancel_ref[0]()
        for _h in list(_hooks):
            try:
                _keyboard.remove_hotkey(_h)
            except Exception:
                pass
        _hooks.clear()
        _preview_open = False
        _current_preview_win = None
        try:
            win.destroy()
        except Exception:
            pass

    def on_insert(submit: bool = False) -> None:
        result = entry.get("1.0", "end-1c").rstrip()
        if not empty:
            side = "raw" if _showing_raw[0] else "cleaned"
            original = (_raw_originals[side] or "").rstrip()
            if original != result and _learn_from_edits_enabled():
                try:
                    promoted = profile.log_correction(original, result)
                except Exception:
                    promoted = []
                for w_out, c_out in (promoted or []):
                    show_toast(
                        f'Learned: "{w_out}" → "{c_out or "(removed)"}" — will now auto-apply',
                        kind="info", action_label="Forget",
                        action_cb=(lambda w=w_out, c=c_out: profile.delete_rule(w, c)),
                    )

        # Prime focus on target BEFORE closing preview — while we still own the foreground,
        # SetForegroundWindow is guaranteed to succeed. Closing first creates a vacuum where
        # Windows blocks the call (anti-focus-steal protection).
        inject.prime_foreground(hwnd)
        _close()
        to_paste = (" " + result) if append_var.get() else result
        target_fn = inject.inject_text_and_submit if submit else inject.inject_text
        threading.Thread(target=target_fn, args=(to_paste, hwnd), daemon=True).start()

    def on_cancel() -> None:
        _close()

    insert_btn = tk.Button(
        btns, text="Insert", command=on_insert, width=10,
        bg=accent, fg="#ffffff",
        activebackground=accent_hv, activeforeground="#ffffff",
        relief="flat", bd=0,
        font=_FONT_BTN, padx=8, pady=6, cursor="hand2",
    )
    if empty:
        insert_btn.config(state="disabled", bg=_BG3, fg=_FG3, cursor="")
    else:
        insert_btn.bind("<Enter>", lambda e: insert_btn.config(bg=accent_hv))
        insert_btn.bind("<Leave>", lambda e: insert_btn.config(bg=accent))
    insert_btn.pack(side=tk.LEFT)

    if empty:
        # "Try Again" replaces Cancel in empty state
        try_btn = tk.Button(
            btns, text="Try Again", command=on_cancel, width=10,
            bg=_BG2, fg=_FG,
            activebackground=_BORDER, activeforeground=_FG,
            relief="flat", bd=1,
            font=_FONT_BTN, padx=8, pady=6, cursor="hand2",
        )
        try_btn.pack(side=tk.LEFT, padx=(8, 0))
    else:
        cancel_wrap = tk.Frame(btns, bg=_BORDER, padx=1, pady=1)
        tk.Button(
            cancel_wrap, text="Cancel", command=on_cancel, width=10,
            bg=_BG, fg=_FG2,
            activebackground=_BG2, activeforeground=_FG,
            relief="flat", bd=0,
            font=_FONT_BTN, padx=8, pady=6, cursor="hand2",
        ).pack()
        cancel_wrap.pack(side=tk.LEFT, padx=(8, 0))

    # ── Raw / Cleaned toggle (item 9) — only when cleanup actually changed
    # the transcript. Each side keeps whatever the user has edited into it
    # while the panel is open; toggling never clobbers the other side's
    # edits, and whichever side is showing at Insert time is what gets
    # pasted (on_insert reads straight from the entry widget).
    if raw and raw.strip() != text.strip() and not empty:
        _stash = {"cleaned": text, "raw": raw}

        def _toggle_raw():
            current_side = "raw" if _showing_raw[0] else "cleaned"
            _stash[current_side] = entry.get("1.0", "end-1c")
            _showing_raw[0] = not _showing_raw[0]
            next_side = "raw" if _showing_raw[0] else "cleaned"
            entry.delete("1.0", tk.END)
            entry.insert("1.0", _stash[next_side])
            toggle_btn.config(text=("Cleaned" if _showing_raw[0] else "Raw"))

        toggle_row = tk.Frame(frame, bg=_BG)
        toggle_row.pack(fill=tk.X, pady=(4, 0))

        toggle_btn = tk.Button(
            toggle_row, text="Raw", command=_toggle_raw,
            bg=_BG2, fg=_FG2,
            activebackground=_BORDER, activeforeground=_FG,
            relief="flat", bd=1,
            font=_FONT_CHIP, padx=8, pady=3, cursor="hand2",
        )
        toggle_btn.pack(side=tk.RIGHT)

    # ── Keyboard hint chips ────────────────────────────────────────────────
    chip_row = tk.Frame(frame, bg=_BG)
    chip_row.pack(fill=tk.X, pady=(10, 0))

    def _chip(parent, key: str, label: str, accent: bool = False, key_color: str | None = None) -> None:
        outer = tk.Frame(parent, bg=_BORDER, bd=0)
        inner = tk.Frame(outer, bg=_BG3 if accent else _BG2, padx=8, pady=3)
        inner.pack(padx=1, pady=1)
        tk.Label(inner, text=key, bg=inner["bg"],
                 fg=(key_color or _BLUE) if accent else _FG,
                 font=(_FONT_FAM_TEXT, 8, "bold")).pack(side=tk.LEFT)
        tk.Label(inner, text=f" {label}", bg=inner["bg"],
                 fg=_FG2, font=_FONT_CHIP).pack(side=tk.LEFT)
        outer.pack(side=tk.LEFT, padx=(0, 6))

    _chip(chip_row, "↵",       "Insert", accent=True, key_color=accent)
    _chip(chip_row, "Ctrl+↵", "Insert & Send")
    _chip(chip_row, "Esc",     "Cancel")
    _chip(chip_row, "Ctrl+R",  "Re-record")
    _chip(chip_row, "Shift+↵", "Newline")

    # ── Keybindings ────────────────────────────────────────────────────────
    if not empty:
        def _on_return(e):
            on_insert()
            return "break"
        def _on_ctrl_return(e):
            on_insert(submit=True)
            return "break"
        entry.bind("<Return>",  _on_return)
        entry.bind("<Control-Return>", _on_ctrl_return)  # insert-and-send
        entry.bind("<Insert>",  _on_return)   # Insert key also pastes
        entry.bind("<Shift-Return>", lambda e: None)  # allow literal newline
        win.bind("<Insert>",    _on_return)   # belt-and-suspenders: fires if focus drifts off entry

    # Redo bindings (Tkinter Text only auto-binds Ctrl+Z for undo)
    entry.bind("<Control-y>",       lambda e: (entry.edit_redo(), "break")[1])
    entry.bind("<Control-Shift-z>", lambda e: (entry.edit_redo(), "break")[1])

    # Ctrl+R — close preview and start a new recording
    def _on_rerecord_key(e):
        on_cancel()
        if _on_rerecord:
            _root.after(200, _on_rerecord)
        return "break"
    entry.bind("<Control-r>", _on_rerecord_key)
    win.bind("<Control-r>",   _on_rerecord_key)

    win.bind("<Escape>", lambda _: on_cancel())
    win.protocol("WM_DELETE_WINDOW", on_cancel)

    # ── Global WH_KEYBOARD_LL hooks ─────────────────────────────────────────
    # VS Code / Electron calls LockSetForegroundWindow so the preview often
    # cannot steal OS keyboard focus even after activate_window(). Without
    # focus, Insert/Enter keypresses go to VS Code (toggling overwrite mode)
    # instead of triggering on_insert(). A low-level keyboard hook captures
    # the key BEFORE it reaches any application's message queue and, with
    # suppress=True, prevents VS Code from ever seeing it.
    # The hook thread is not the Tkinter thread — marshal via _root.after().
    if not empty:
        def _global_commit():
            # Ctrl held at commit time = insert-and-send (forward one Enter)
            submit = bool(win32api.GetAsyncKeyState(0x11) & 0x8000)
            _root.after(0, lambda s=submit: on_insert(submit=s) if _preview_open else None)
        try:
            _hooks.append(_keyboard.add_hotkey('insert', _global_commit, suppress=True))
            _hooks.append(_keyboard.add_hotkey('enter',  _global_commit, suppress=True))
        except Exception:
            pass

    def _global_cancel():
        _root.after(0, lambda: on_cancel() if _preview_open else None)
    try:
        _hooks.append(_keyboard.add_hotkey('escape', _global_cancel, suppress=True))
    except Exception:
        pass

    # These suppressing hooks intercept Enter/Insert/Esc system-wide. _close()
    # removes them, but _tick() destroys the window directly when a new recording
    # or replacement transcription arrives — every close path must unhook, or the
    # leaked hooks keep the keyboard library intercepting all input (stuck
    # modifiers, swallowed keys). <Destroy> fires on all of them.
    def _remove_hooks_on_destroy(event):
        if event.widget is not win:
            return
        for _h in list(_hooks):
            try:
                _keyboard.remove_hotkey(_h)
            except Exception:
                pass
        _hooks.clear()
    win.bind("<Destroy>", _remove_hooks_on_destroy)

    # ── Drag (header only, item 54) ─────────────────────────────────────────
    def _drag_start(event):
        win._ox = event.x_root - win.winfo_x()
        win._oy = event.y_root - win.winfo_y()

    def _drag_motion(event):
        win.geometry(f"+{event.x_root - win._ox}+{event.y_root - win._oy}")

    def _drag_release(_event):
        try:
            wx, wy = win.winfo_x(), win.winfo_y()
            ww, wh = win.winfo_width(), win.winfo_height()
            cx2, cy2 = win32api.GetCursorPos()
            mon_key, work = _monitor_key_and_workarea(cx2, cy2)
            wx, wy = _clamp_to_workarea(wx, wy, ww, wh, work)
            _save_panel_position(mon_key, wx, wy)
        except Exception:
            pass

    for widget in (header_row, title_lbl, dot_lbl):
        widget.bind("<ButtonPress-1>", _drag_start)
        widget.bind("<B1-Motion>", _drag_motion)
        widget.bind("<ButtonRelease-1>", _drag_release)
    if dest_lbl is not None:
        dest_lbl.bind("<ButtonPress-1>", _drag_start)
        dest_lbl.bind("<B1-Motion>", _drag_motion)
        dest_lbl.bind("<ButtonRelease-1>", _drag_release)

    # ── Size & position ────────────────────────────────────────────────────
    win.update_idletasks()
    w = max(420, win.winfo_reqwidth())
    h = win.winfo_reqheight()
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    # A prior drag on this monitor wins over cursor-relative / configured
    # placement (item 54) — the user's manual placement is a deliberate
    # override, honoured regardless of the preview_position setting.
    mon_key, work = _monitor_key_and_workarea(cx, cy)
    saved_pos = _load_panel_position(mon_key)
    if saved_pos is not None:
        x, y = _clamp_to_workarea(saved_pos[0], saved_pos[1], w, h, work)
    else:
        x, y = _calc_position(cx, cy, w, h, sw, sh, _preview_position)
    win.geometry(f"{w}x{h}+{x}+{y}")

    # Activate the preview so keyboard input (Enter/Insert) goes here, not to
    # VS Code / Electron. overrideredirect(True) popup windows don't auto-activate
    # on Windows — the previously-focused app keeps OS keyboard focus and the
    # user's Insert/Enter keypress goes straight to VS Code (toggling overwrite
    # mode or whatever) instead of triggering on_insert().
    # activate_window uses AttachThreadInput to steal foreground even when our
    # process is not the current foreground process.
    # NEVER steal focus while the recording hotkey is still physically held.
    # Recording stops on the FIRST modifier release; the target app received
    # the modifier key-downs, so it must also receive the key-ups. If we grab
    # focus before every key is up, the ups are delivered to this preview
    # instead: mstsc never forwards them, so the modifier stays stuck in the
    # remote session, and Electron apps latch their own modifier tracking
    # (ctrl-clicks, dead Ctrl+A, Tab acting as Alt+Tab) until the user presses
    # the key again inside that app. The old 500ms blocking wait gave up and
    # stole focus anyway; poll with after() instead, and only activate once
    # the keys are up. Cap at ~10s: past that, leave focus alone — the
    # suppressed Enter/Esc hooks below keep the panel usable without it.
    def _activate_when_released(attempt: int = 0) -> None:
        if not _preview_open or _current_preview_win is not win:
            return  # panel already closed or replaced; never activate a corpse
        held = inject.modifiers_physically_down()
        if held:
            if attempt == 0:
                log("preview", f"deferring focus steal, modifiers still held: {held}")
            if attempt < 200:
                win.after(50, _activate_when_released, attempt + 1)
            return
        if attempt:
            log("preview", f"modifiers released, stealing focus after {attempt * 50}ms deferral")
        try:
            inject.activate_window(win.winfo_id())
            win.lift()
            win.focus_force()
            entry.focus_set()
        except Exception:
            pass

    _activate_when_released()

    # ── Rounded corners + slide-up entrance ────────────────────────────────
    winfx.apply_rounded_region(win, radius=12)
    _apply_backdrop(win)
    _play_entrance(win, 1.0, dy=_px(14), duration_ms=200)
    if not empty:
        target_border = _border_colour(confidence)
        winfx.ease_color(entry, "highlightbackground", _BORDER, target_border,
                         duration_ms=260, steps=10)

    # ── Auto-dismiss countdown (items 53, 55) ───────────────────────────────
    # progress_fill / pin_btn were created earlier (header + bottom-edge
    # strip) only when auto_dismiss > 0. State machine:
    #  - typing / focus / hover restart the countdown from full (still counts
    #    as "the user is engaged, give them a fresh window")
    #  - the pin button suspends it outright, preserving the remaining time,
    #    and resumes from there on unpin — distinct from an interaction reset
    if auto_dismiss > 0:
        # Motion policy (item 47): a smooth 30fps sweep is motion, so when
        # animations are off (config or Windows reduce-motion) the fill still
        # depletes accurately, just in coarse discrete steps instead of a
        # continuous glide — read once per window, not per tick.
        _dismiss_tick_ms = 33 if _animations_enabled() else 500
        _dismiss = {"total": auto_dismiss, "remaining": auto_dismiss,
                    "deadline": None, "job": None, "tick_job": None, "pinned": False}

        def _dismiss_cancel() -> None:
            if _dismiss["job"] is not None:
                try:
                    win.after_cancel(_dismiss["job"])
                except Exception:
                    pass
                _dismiss["job"] = None
            if _dismiss["tick_job"] is not None:
                try:
                    win.after_cancel(_dismiss["tick_job"])
                except Exception:
                    pass
                _dismiss["tick_job"] = None
            _dismiss["deadline"] = None

        def _dismiss_tick() -> None:
            if _dismiss["deadline"] is None or progress_fill is None:
                return
            remaining = _dismiss["deadline"] - time.time()
            try:
                progress_fill.place(relwidth=max(0.0, min(1.0, remaining / _dismiss["total"])))
            except Exception:
                return
            if remaining <= 0:
                return
            _dismiss["tick_job"] = win.after(_dismiss_tick_ms, _dismiss_tick)

        def _dismiss_start(seconds: float | None = None) -> None:
            if _dismiss["pinned"]:
                return
            secs = _dismiss["total"] if seconds is None else seconds
            if secs <= 0:
                return
            _dismiss["deadline"] = time.time() + secs
            _dismiss["job"] = win.after(int(secs * 1000), _close)
            _dismiss_tick()

        def _dismiss_reset(*_args) -> None:
            """User interaction (typing / focus / hover) restarts the countdown."""
            if _dismiss["pinned"]:
                return
            _dismiss_cancel()
            if progress_fill is not None:
                try:
                    progress_fill.place(relwidth=1.0)
                except Exception:
                    pass
            _dismiss_start()

        def _toggle_pin() -> None:
            if pin_btn is None:
                return
            if not _dismiss["pinned"]:
                remaining = (_dismiss["deadline"] - time.time()) if _dismiss["deadline"] else _dismiss["total"]
                _dismiss["remaining"] = max(0.0, remaining)
                _dismiss_cancel()
                _dismiss["pinned"] = True
                pin_btn.config(fg=accent, bg=_BG2)
            else:
                _dismiss["pinned"] = False
                pin_btn.config(fg=_FG3, bg=_BG)
                _dismiss_start(_dismiss["remaining"])

        if pin_btn is not None:
            pin_btn.bind("<Button-1>", lambda e: _toggle_pin())
        entry.bind("<Key>", _dismiss_reset, add="+")
        entry.bind("<FocusIn>", _dismiss_reset, add="+")
        for _hover_widget in (win, ring, frame, entry):
            _hover_widget.bind("<Enter>", _dismiss_reset, add="+")

        _dismiss_cancel_ref[0] = _dismiss_cancel
        _dismiss_start()


# ---------------------------------------------------------------------------
# Speech Profile viewer
# ---------------------------------------------------------------------------

def _open_profile() -> None:
    rules = profile.get_all_rules()

    win = tk.Toplevel(_root)
    win.title("Quiett — Speech Profile")
    win.configure(bg=_BG)
    win.geometry("560x440")
    win.minsize(420, 240)

    tk.Label(
        win, text="Speech Profile",
        bg=_BG, fg=_FG, font=("Segoe UI", 13, "bold"),
    ).pack(anchor="w", padx=16, pady=(14, 2))

    tk.Label(
        win,
        text=f"Corrections are auto-applied after {profile.MIN_OCCURRENCES} occurrences.  "
             f"Pending rules are shown in grey.",
        bg=_BG, fg=_FG2, font=("Segoe UI", 8),
    ).pack(anchor="w", padx=16, pady=(0, 8))

    list_frame = tk.Frame(win, bg=_BG)
    list_frame.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 8))

    sb = tk.Scrollbar(list_frame)
    sb.pack(side=tk.RIGHT, fill=tk.Y)

    txt = tk.Text(
        list_frame, bg=_BG2, fg=_FG,
        font=("Segoe UI", 10), wrap=tk.WORD,
        relief="flat", bd=0, padx=10, pady=8,
        yscrollcommand=sb.set, cursor="ibeam",
    )
    txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    sb.config(command=txt.yview)

    txt.tag_configure("active",  foreground=_FG,       font=("Segoe UI", 10))
    txt.tag_configure("pending", foreground=_FG3,      font=("Segoe UI", 10))
    txt.tag_configure("count",   foreground=_FG3,      font=("Segoe UI", 9))
    txt.tag_configure("del",     foreground=_REC,  font=("Segoe UI", 8), underline=True)
    txt.tag_configure("sep",     foreground=_BORDER)
    txt.tag_configure("empty",   foreground=_FG2,      font=("Segoe UI", 10))

    def _populate(data: list) -> None:
        txt.config(state="normal")
        txt.delete("1.0", tk.END)
        if not data:
            txt.insert(tk.END, "No corrections learned yet.\n\n"
                               "Edit transcriptions in the preview panel and they'll\n"
                               "appear here after a few repetitions.", "empty")
        else:
            for i, rule in enumerate(data):
                style = "active" if rule["active"] else "pending"
                correct = rule["correct_out"] if rule["correct_out"] else "(deleted)"
                label = f'"{rule["whisper_out"]}"  →  "{correct}"'
                txt.insert(tk.END, label + "  ", style)
                txt.insert(tk.END, f"·  {rule['count']}x  ", "count")
                del_tag = f"del_{i}"
                txt.insert(tk.END, "[remove]\n", ("del", del_tag))
                txt.tag_bind(del_tag, "<Button-1>",
                             lambda e, r=rule: _delete_rule(r, data))
                txt.tag_bind(del_tag, "<Enter>",
                             lambda e: txt.config(cursor="hand2"))
                txt.tag_bind(del_tag, "<Leave>",
                             lambda e: txt.config(cursor="ibeam"))
                if i < len(data) - 1:
                    txt.insert(tk.END, "─" * 60 + "\n", "sep")
        txt.config(state="disabled")

    def _delete_rule(rule: dict, data: list) -> None:
        try:
            profile.delete_rule(rule["whisper_out"], rule["correct_out"])
        except Exception:
            pass
        refreshed = profile.get_all_rules()
        _populate(refreshed)

    _populate(rules)

    bar = tk.Frame(win, bg=_BG)
    bar.pack(fill=tk.X, padx=16, pady=(0, 12))

    def on_close() -> None:
        global _profile_open
        _profile_open = False
        winfx.fade_out_then_destroy(win, duration_ms=140)

    close_wrap = tk.Frame(bar, bg=_BORDER, padx=1, pady=1)
    tk.Button(
        close_wrap, text="Close", command=on_close,
        bg=_BG, fg=_FG2,
        activebackground=_BG2, activeforeground=_FG,
        relief="flat", bd=0,
        font=("Segoe UI", 10), padx=10, pady=4, cursor="hand2",
    ).pack()
    close_wrap.pack(side=tk.LEFT)

    win.protocol("WM_DELETE_WINDOW", on_close)
    win.bind("<Escape>", lambda _: on_close())

    winfx.fade_in(win, target=1.0, duration_ms=160)


# ---------------------------------------------------------------------------
# Settings UI
# ---------------------------------------------------------------------------

_POSITIONS = ["cursor", "top-right", "bottom-right", "top-left", "bottom-left", "center"]


def _open_settings() -> None:
    try:
        with open(_CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}

    win = tk.Toplevel(_root)
    win.title("Quiett — Settings")
    win.configure(bg=_BG)
    win.geometry("760x600")
    win.minsize(680, 480)
    win.resizable(True, True)

    # ── Layout: sidebar + content panel ────────────────────────────────────
    root_frame = tk.Frame(win, bg=_BG)
    root_frame.pack(fill=tk.BOTH, expand=True)

    sidebar = tk.Frame(root_frame, bg=_BG2, width=180)
    sidebar.pack(side=tk.LEFT, fill=tk.Y)
    sidebar.pack_propagate(False)

    tk.Label(sidebar, text="Settings", bg=_BG2, fg=_FG,
             font=(_FONT_FAM_DISPLAY, 14, "bold"),
             anchor="w").pack(fill=tk.X, padx=18, pady=(18, 12))

    panel_holder = tk.Frame(root_frame, bg=_BG)
    panel_holder.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    # Vertically scrollable content area inside panel
    canvas = tk.Canvas(panel_holder, bg=_BG, highlightthickness=0)
    sb = tk.Scrollbar(panel_holder, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=sb.set)
    sb.pack(side=tk.RIGHT, fill=tk.Y)
    canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    pages_holder = tk.Frame(canvas, bg=_BG)
    pages_window = canvas.create_window((0, 0), window=pages_holder, anchor="nw")

    def _on_configure(_e=None):
        canvas.configure(scrollregion=canvas.bbox("all"))
        canvas.itemconfig(pages_window, width=canvas.winfo_width())

    pages_holder.bind("<Configure>", _on_configure)
    canvas.bind("<Configure>", _on_configure)

    # Bind mousewheel scrolling on the canvas only while pointer is over it
    def _mw(e):
        canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
    canvas.bind("<Enter>", lambda _: canvas.bind_all("<MouseWheel>", _mw))
    canvas.bind("<Leave>", lambda _: canvas.unbind_all("<MouseWheel>"))

    # State containers — populated by build_*() then used by on_save
    state: dict = {}
    pages: dict[str, tk.Frame] = {}

    def _label(parent, text: str) -> tk.Label:
        return tk.Label(parent, text=text, bg=_BG, fg=_FG2, font=_FONT_HINT, anchor="w")

    def _h2(parent, text: str) -> tk.Label:
        return tk.Label(parent, text=text, bg=_BG, fg=_FG,
                        font=(_FONT_FAM_DISPLAY, 12, "bold"), anchor="w")

    def _note(parent, text: str) -> tk.Label:
        return tk.Label(parent, text=text, bg=_BG, fg=_FG3,
                        font=(_FONT_FAM_TEXT, 8), anchor="w", justify="left",
                        wraplength=480)

    def _entry(parent, var) -> tk.Entry:
        return tk.Entry(parent, textvariable=var, bg=_BG2, fg=_FG,
                        insertbackground=_FG, relief="flat", bd=0,
                        highlightthickness=1, highlightbackground=_BORDER,
                        highlightcolor=_BLUE, font=_FONT_BODY)

    def _text_area(parent, height: int) -> tk.Text:
        return tk.Text(parent, bg=_BG2, fg=_FG, insertbackground=_FG,
                       relief="flat", bd=0, highlightthickness=1,
                       highlightbackground=_BORDER, highlightcolor=_BLUE,
                       font=_FONT_BODY, height=height, undo=True, padx=8, pady=6)

    def _toggle_row(parent, label: str, var: tk.BooleanVar, sub: str = "") -> None:
        row = tk.Frame(parent, bg=_BG)
        row.pack(fill=tk.X, pady=(8, 0), padx=22)
        left = tk.Frame(row, bg=_BG)
        left.pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Label(left, text=label, bg=_BG, fg=_FG, font=_FONT_BODY,
                 anchor="w").pack(fill=tk.X)
        if sub:
            tk.Label(left, text=sub, bg=_BG, fg=_FG3, font=(_FONT_FAM_TEXT, 8),
                     anchor="w", justify="left", wraplength=420).pack(fill=tk.X)
        widgets.Toggle(row, variable=var, bg=_BG,
                       off_bg=_BORDER, on_bg=_BLUE,
                       knob_fg=_FG).pack(side=tk.RIGHT, padx=(12, 0))

    # ── Page builders ──────────────────────────────────────────────────────

    def build_audio() -> tk.Frame:
        p = tk.Frame(pages_holder, bg=_BG)
        _h2(p, "Audio").pack(fill=tk.X, padx=22, pady=(18, 6))
        _note(p, "Microphone selection and recording behaviour.").pack(
            fill=tk.X, padx=22, pady=(0, 12))

        # Mic device
        try:
            devices = audio.list_input_devices()
        except Exception:
            devices = []
        dev_labels = ["System default"] + [f"[{d['index']}] {d['name']}" for d in devices]
        current_dev = cfg.get("input_device")
        if current_dev is None:
            current_label = "System default"
        else:
            current_label = next(
                (f"[{d['index']}] {d['name']}" for d in devices if d["index"] == current_dev),
                "System default",
            )
        mic_var = tk.StringVar(value=current_label)
        state["mic_var"] = mic_var
        state["devices"] = devices

        _label(p, "Microphone").pack(fill=tk.X, padx=22, pady=(0, 2))
        om = tk.OptionMenu(p, mic_var, *dev_labels)
        om.config(bg=_BG2, fg=_FG, activebackground=_BORDER, activeforeground=_FG,
                  relief="flat", highlightthickness=0, font=_FONT_BODY)
        om.pack(fill=tk.X, padx=22)

        # Live meter (item 22)
        _label(p, "Live level").pack(fill=tk.X, padx=22, pady=(12, 2))
        meter = widgets.MicMeter(p, bg=_BG, idle=_BORDER,
                                 active_lo=_BLUE, active_hi=_REC)
        meter.pack(anchor="w", padx=22)

        def _on_mic_change(*_):
            sel = mic_var.get()
            if sel == "System default":
                meter.set_device(None)
            else:
                try:
                    meter.set_device(int(sel.split("]")[0].lstrip("[")))
                except Exception:
                    meter.set_device(None)
        mic_var.trace_add("write", _on_mic_change)
        # Don't open the mic stream the instant Settings opens — only once the
        # user actually looks at the Audio page (show_page below) or changes
        # the mic dropdown (trace above).
        state["_start_mic_meter"] = _on_mic_change
        # Tear down meter stream when window closes
        win.bind("<Destroy>", lambda e: meter.stop(), add="+")

        # Numeric recording fields
        max_var     = tk.StringVar(value=str(cfg.get("max_record_seconds", 120)))
        silence_var = tk.StringVar(value=str(cfg.get("silence_auto_stop_seconds", 3)))
        sthresh_var = tk.StringVar(value=str(cfg.get("silence_threshold", 0.01)))
        state.update(max_var=max_var, silence_var=silence_var, sthresh_var=sthresh_var)

        for lbl, var in [("Max recording time (seconds)", max_var),
                         ("Silence auto-stop (seconds, 0 = disabled)", silence_var),
                         ("Silence threshold (RMS 0.001–0.5, lower = more sensitive)", sthresh_var)]:
            _label(p, lbl).pack(fill=tk.X, padx=22, pady=(12, 2))
            _entry(p, var).pack(fill=tk.X, padx=22)

        vad_var = tk.BooleanVar(value=bool(cfg.get("vad_filter", False)))
        state["vad_var"] = vad_var
        _toggle_row(p, "VAD filter", vad_var,
                    sub="Suppress background noise during transcription.")

        vad_sil_var = tk.BooleanVar(value=bool(cfg.get("vad_silence_mode", False)))
        state["vad_sil_var"] = vad_sil_var
        _toggle_row(p, "VAD silence detection", vad_sil_var,
                    sub="Use voice-activity detection (webrtcvad) instead of RMS threshold "
                        "for silence auto-stop. Works better in noisy rooms. Restart required.")
        return p

    def build_hotkey() -> tk.Frame:
        p = tk.Frame(pages_holder, bg=_BG)
        _h2(p, "Hotkey").pack(fill=tk.X, padx=22, pady=(18, 6))
        _note(p, "Push-to-talk binding. Hot-reloads on save.").pack(
            fill=tk.X, padx=22, pady=(0, 12))

        hotkey_var = tk.StringVar(value=cfg.get("hotkey", "ctrl+alt"))
        state["hotkey_var"] = hotkey_var
        _label(p, "Hotkey combo (e.g. ctrl+alt, ctrl+shift, alt+space)").pack(
            fill=tk.X, padx=22, pady=(0, 2))
        _entry(p, hotkey_var).pack(fill=tk.X, padx=22)
        return p

    def build_transcription() -> tk.Frame:
        p = tk.Frame(pages_holder, bg=_BG)
        _h2(p, "Transcription").pack(fill=tk.X, padx=22, pady=(18, 6))
        _note(p, "Language, model, and Whisper biasing. Model changes require a restart.").pack(
            fill=tk.X, padx=22, pady=(0, 12))

        lang_var  = tk.StringVar(value=cfg.get("language", "en"))
        model_var = tk.StringVar(value=cfg.get("model", "large-v3-turbo"))
        state.update(lang_var=lang_var, model_var=model_var)

        _label(p, "Language (e.g. en, fr, es)").pack(fill=tk.X, padx=22, pady=(0, 2))
        _entry(p, lang_var).pack(fill=tk.X, padx=22)

        _label(p, "Model").pack(fill=tk.X, padx=22, pady=(12, 2))
        _entry(p, model_var).pack(fill=tk.X, padx=22)
        _note(p, "tiny / base / small / medium / large-v3 / large-v3-turbo").pack(
            fill=tk.X, padx=22, pady=(2, 0))

        _label(p, "Initial prompt (seeds Whisper with context)").pack(
            fill=tk.X, padx=22, pady=(14, 2))
        prompt_txt = _text_area(p, 3)
        prompt_txt.insert("1.0", cfg.get("initial_prompt", "") or "")
        prompt_txt.pack(fill=tk.X, padx=22)
        state["prompt_txt"] = prompt_txt

        _label(p, "Custom vocabulary — one term per line").pack(
            fill=tk.X, padx=22, pady=(14, 2))
        vocab_txt = _text_area(p, 5)
        vocab_txt.insert("1.0", "\n".join(cfg.get("custom_vocabulary", []) or []))
        vocab_txt.pack(fill=tk.X, padx=22)
        state["vocab_txt"] = vocab_txt
        return p

    def build_behaviour() -> tk.Frame:
        p = tk.Frame(pages_holder, bg=_BG)
        _h2(p, "Behaviour").pack(fill=tk.X, padx=22, pady=(18, 6))
        _note(p, "Preview window, auto-paste, filler removal, and corrections.").pack(
            fill=tk.X, padx=22, pady=(0, 12))

        pos_var = tk.StringVar(value=cfg.get("preview_position", "cursor"))
        state["pos_var"] = pos_var
        _label(p, "Preview position").pack(fill=tk.X, padx=22, pady=(0, 2))
        om = tk.OptionMenu(p, pos_var, *_POSITIONS)
        om.config(bg=_BG2, fg=_FG, activebackground=_BORDER, activeforeground=_FG,
                  relief="flat", highlightthickness=0, font=_FONT_BODY)
        om.pack(anchor="w", padx=22)

        auto_dismiss_var = tk.StringVar(value=str(cfg.get("preview_auto_dismiss_seconds", 0)))
        auto_paste_var   = tk.StringVar(value=str(cfg.get("auto_paste_threshold", 0.0)))
        state.update(auto_dismiss_var=auto_dismiss_var, auto_paste_var=auto_paste_var)

        for lbl, var in [("Auto-dismiss preview after (seconds, 0 = never)", auto_dismiss_var),
                         ("Auto-paste threshold 0–1 (skip preview when ≥ this)", auto_paste_var)]:
            _label(p, lbl).pack(fill=tk.X, padx=22, pady=(12, 2))
            _entry(p, var).pack(fill=tk.X, padx=22)

        history_paused_var = tk.BooleanVar(value=bool(cfg.get("history_paused", False)))
        state["history_paused_var"] = history_paused_var
        _toggle_row(p, "Pause history", history_paused_var,
                    sub="Don't save transcriptions to history.")

        _label(p, "Filler words (one per line)").pack(fill=tk.X, padx=22, pady=(16, 2))
        fillers_txt = _text_area(p, 4)
        fillers_txt.insert("1.0", "\n".join(cfg.get("filler_words", [])))
        fillers_txt.pack(fill=tk.X, padx=22)
        state["fillers_txt"] = fillers_txt

        _label(p, 'Custom corrections — one per line, "wrong → correct"').pack(
            fill=tk.X, padx=22, pady=(14, 2))
        corrections_txt = _text_area(p, 5)
        corr_dict = cfg.get("corrections", {})
        corrections_txt.insert("1.0", "\n".join(f"{k} → {v}" for k, v in corr_dict.items()))
        corrections_txt.pack(fill=tk.X, padx=22)
        state["corrections_txt"] = corrections_txt
        return p

    def build_appearance() -> tk.Frame:
        p = tk.Frame(pages_holder, bg=_BG)
        _h2(p, "Appearance").pack(fill=tk.X, padx=22, pady=(18, 6))
        _note(p, "Theme change takes effect after restart.").pack(
            fill=tk.X, padx=22, pady=(0, 12))

        theme_var = tk.StringVar(value=cfg.get("theme", "dark"))
        state["theme_var"] = theme_var

        _label(p, "Theme").pack(fill=tk.X, padx=22, pady=(0, 6))
        row = tk.Frame(p, bg=_BG)
        row.pack(fill=tk.X, padx=22)
        for name in ("dark", "light"):
            rb = tk.Radiobutton(
                row, text=name.capitalize(), value=name, variable=theme_var,
                bg=_BG, fg=_FG, selectcolor=_BG2,
                activebackground=_BG, activeforeground=_FG,
                font=_FONT_BODY, padx=4, pady=2,
            )
            rb.pack(side=tk.LEFT, padx=(0, 16))
        return p

    PAGE_DEFS = [
        ("Audio",          build_audio),
        ("Hotkey",         build_hotkey),
        ("Transcription",  build_transcription),
        ("Behaviour",      build_behaviour),
        ("Appearance",     build_appearance),
    ]

    # ── Build sidebar items ────────────────────────────────────────────────
    nav_buttons: dict[str, tk.Label] = {}
    current_page = tk.StringVar(value=PAGE_DEFS[0][0])

    def show_page(name: str) -> None:
        for n, lbl in nav_buttons.items():
            if n == name:
                lbl.config(bg=_BG3, fg=_FG)
            else:
                lbl.config(bg=_BG2, fg=_FG2)
        for n, page in pages.items():
            page.pack_forget()
        pages[name].pack(fill=tk.BOTH, expand=True)
        current_page.set(name)
        canvas.yview_moveto(0)
        indicator = state.get("nav_indicator")
        if indicator is not None:
            winfx.ease_place_y(indicator, indicator.winfo_y(),
                               nav_buttons[name].winfo_y(), duration_ms=150)

    def _on_nav_click(name: str) -> None:
        show_page(name)
        # Lazily open the mic meter only on an actual user click into the Audio
        # tab — not as a side effect of Settings opening (Audio happens to be
        # the default first page).
        if name == "Audio" and "_start_mic_meter" in state and not state.get("_mic_meter_started"):
            state["_mic_meter_started"] = True
            state["_start_mic_meter"]()

    for name, builder in PAGE_DEFS:
        pages[name] = builder()
        nav = tk.Label(sidebar, text=name, bg=_BG2, fg=_FG2,
                       font=_FONT_BODY, anchor="w", padx=18, pady=10,
                       cursor="hand2")
        nav.pack(fill=tk.X)
        nav.bind("<Button-1>", lambda e, n=name: _on_nav_click(n))
        nav.bind("<Enter>", lambda e, n=name:
                 winfx.ease_color(nav_buttons[n], "bg", _BG2, _BG3, duration_ms=100, steps=5)
                 if current_page.get() != n else None)
        nav.bind("<Leave>", lambda e, n=name:
                 winfx.ease_color(nav_buttons[n], "bg", _BG3, _BG2, duration_ms=100, steps=5)
                 if current_page.get() != n else None)
        nav_buttons[name] = nav

    # Sliding accent indicator that animates to the active tab.
    sidebar.update_idletasks()
    first_nav = nav_buttons[PAGE_DEFS[0][0]]
    nav_indicator = tk.Frame(sidebar, bg=_BLUE, width=3, height=first_nav.winfo_height())
    nav_indicator.place(x=0, y=first_nav.winfo_y())
    state["nav_indicator"] = nav_indicator

    show_page(PAGE_DEFS[0][0])

    # ── Footer buttons (Save / Cancel / status) ────────────────────────────
    footer = tk.Frame(win, bg=_BG2, height=58)
    footer.pack(side=tk.BOTTOM, fill=tk.X)
    footer.pack_propagate(False)

    err_var = tk.StringVar()
    err_lbl = tk.Label(footer, textvariable=err_var, bg=_BG2, fg=_FG3,
                       font=(_FONT_FAM_TEXT, 9), anchor="w")
    err_lbl.pack(side=tk.LEFT, padx=18)

    def on_save() -> None:
        try:
            max_secs     = float(state["max_var"].get())
            silence_secs = float(state["silence_var"].get())
            sthresh      = float(state["sthresh_var"].get())
            dismiss_secs = float(state["auto_dismiss_var"].get())
            paste_thresh = float(state["auto_paste_var"].get())
        except ValueError:
            err_var.set("Numeric fields must be numbers.")
            err_lbl.config(fg=_REC)
            return

        fillers = [w.strip() for w in state["fillers_txt"].get("1.0", "end-1c").splitlines()
                   if w.strip()]

        corrections: dict = {}
        for line in state["corrections_txt"].get("1.0", "end-1c").splitlines():
            if "→" in line:
                k, v = line.split("→", 1)
                k, v = k.strip(), v.strip()
                if k:
                    corrections[k] = v

        vocab = [w.strip() for w in state["vocab_txt"].get("1.0", "end-1c").splitlines()
                 if w.strip()]

        sel = state["mic_var"].get()
        if sel == "System default":
            mic_idx = None
        else:
            try:
                mic_idx = int(sel.split("]")[0].lstrip("["))
            except Exception:
                mic_idx = None

        # Re-read the on-disk config right before writing and merge into that,
        # rather than the snapshot taken when this window opened — otherwise we'd
        # clobber concurrent writes (tray toggles, hot-reload) made while Settings
        # was open.
        try:
            with open(_CONFIG_FILE, encoding="utf-8") as f:
                new_cfg = json.load(f)
        except Exception:
            new_cfg = dict(cfg)
        new_cfg.update({
            "hotkey":                      state["hotkey_var"].get().strip() or "ctrl+alt",
            "language":                    state["lang_var"].get().strip(),
            "model":                       state["model_var"].get().strip(),
            "max_record_seconds":          max(5.0, min(300.0, max_secs)),
            "silence_auto_stop_seconds":   max(0.0, silence_secs),
            "silence_threshold":           max(0.001, min(0.5, sthresh)),
            "vad_filter":                  state["vad_var"].get(),
            "vad_silence_mode":            state["vad_sil_var"].get(),
            "preview_position":            state["pos_var"].get(),
            "preview_auto_dismiss_seconds": max(0.0, dismiss_secs),
            "auto_paste_threshold":        max(0.0, min(1.0, paste_thresh)),
            "filler_words":                fillers,
            "corrections":                 corrections,
            "initial_prompt":              state["prompt_txt"].get("1.0", "end-1c").strip(),
            "custom_vocabulary":           vocab,
            "input_device":                mic_idx,
            "history_paused":              state["history_paused_var"].get(),
            "theme":                       state["theme_var"].get(),
        })

        try:
            with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(new_cfg, f, indent=2, ensure_ascii=False)
            configure_position(state["pos_var"].get())
            err_var.set("Saved.")
            err_lbl.config(fg=_BLUE)
            # Brief flash on the Save button itself — a tactile confirmation
            # beyond the footer text, since that's easy to miss.
            winfx.ease_color(
                save_btn, "bg", _BLUE, _BLUE_HV, duration_ms=150, steps=6,
                on_done=lambda: winfx.ease_color(
                    save_btn, "bg", _BLUE_HV, _BLUE, duration_ms=300, steps=8))
        except Exception as exc:
            err_var.set(f"Save failed: {exc}")
            err_lbl.config(fg=_REC)

    def on_close() -> None:
        global _settings_open
        _settings_open = False
        winfx.fade_out_then_destroy(win, duration_ms=140)

    btns = tk.Frame(footer, bg=_BG2)
    btns.pack(side=tk.RIGHT, padx=14, pady=10)

    cancel_wrap = tk.Frame(btns, bg=_BORDER, padx=1, pady=1)
    tk.Button(cancel_wrap, text="Cancel", command=on_close, width=10,
              bg=_BG2, fg=_FG2, activebackground=_BG3, activeforeground=_FG,
              relief="flat", bd=0,
              font=_FONT_BTN, padx=8, pady=6, cursor="hand2").pack()
    cancel_wrap.pack(side=tk.RIGHT, padx=(8, 0))

    save_btn = tk.Button(btns, text="Save", command=on_save, width=10,
                         bg=_BLUE, fg="#ffffff", activebackground=_BLUE_HV,
                         activeforeground="#ffffff", relief="flat", bd=0,
                         font=_FONT_BTN, padx=8, pady=6, cursor="hand2")
    save_btn.bind("<Enter>", lambda e: save_btn.config(bg=_BLUE_HV))
    save_btn.bind("<Leave>", lambda e: save_btn.config(bg=_BLUE))
    save_btn.pack(side=tk.RIGHT)

    win.protocol("WM_DELETE_WINDOW", on_close)
    win.bind("<Escape>", lambda _: on_close())

    winfx.fade_in(win, target=1.0, duration_ms=160)
