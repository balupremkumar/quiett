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
from datetime import date, datetime

import math

import pyperclip
import keyboard as _keyboard
import win32api
from PIL import ImageEnhance, ImageTk

import audio
import chime
import history as hist
import inject
import profile
import taskflow
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
_history_q:      queue.Queue = queue.Queue()
_profile_q:      queue.Queue = queue.Queue()
_settings_q:     queue.Queue = queue.Queue()
_badge_q:        queue.Queue = queue.Queue()
_toast_q:        queue.Queue = queue.Queue()
_flash_q:        queue.Queue = queue.Queue()
_agent_q:        queue.Queue = queue.Queue()
_root:        tk.Tk | None = None
_ready = threading.Event()

_preview_open  = False   # only touched on the tkinter thread
_history_open  = False
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
_THEMES = {
    "dark": {
        "BG": "#1a1a1d", "BG2": "#26262a", "BG3": "#2f2f34",
        "FG": "#f3f4f6", "FG2": "#a1a1aa", "FG3": "#71717a",
        "BLUE": "#3b82f6", "BLUE_HV": "#60a5fa",
        "BORDER": "#3a3a40", "BORDER2": "#4a4a52",
        "TASK": "#22c55e", "TASK_HV": "#4ade80",
        "MATCH_BG": "#3b5274", "MATCH_FG": "#f3f4f6",
    },
    "light": {
        "BG": "#f4f4f6", "BG2": "#eaeaee", "BG3": "#dedee4",
        "FG": "#18181b", "FG2": "#52525b", "FG3": "#8e8e99",
        "BLUE": "#2563eb", "BLUE_HV": "#3b82f6",
        "BORDER": "#cbcbd2", "BORDER2": "#b9b9c2",
        "TASK": "#16a34a", "TASK_HV": "#22c55e",
        "MATCH_BG": "#bfdbfe", "MATCH_FG": "#1e3a8a",
    },
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
    global _BORDER, _BORDER2, _TASK, _TASK_HV, _MATCH_BG, _MATCH_FG
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


def refresh_theme() -> bool:
    """Re-resolve the palette from config + Windows theme. Returns True when it
    changed; open windows keep their colours, new ones use the fresh palette."""
    name = _resolve_theme()
    if name == _THEME_NAME:
        return False
    _apply_palette(name)
    return True


_apply_palette(_resolve_theme())

# Typography ladder — Segoe UI Variable with weight cascade
# (falls back automatically to Segoe UI if Variable isn't installed)
_FONT_FAM_TEXT    = "Segoe UI Variable Text"
_FONT_FAM_DISPLAY = "Segoe UI Variable Display"
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
         reformat_backend: str | None = None,
         task_mode: bool = False) -> None:
    """Queue a dictation preview window. raw= is the pre-vibe-mode Whisper text.

    reformat_backend: 'lmstudio' | 'api' | 'rules' | None — which backend
    actually produced `text` when vibe mode ran, so the preview can show
    whether it was LLM-cleaned or the regex fallback kicked in.

    task_mode: when True, this is a TaskFlow trigger-phrase capture — the
    panel relabels to "Add Task" and confirming creates a TaskFlow task
    instead of pasting.
    """
    _preview_q.put({"text": text, "hwnd": hwnd, "empty": empty,
                    "confidence": confidence, "words": words,
                    "auto_dismiss": auto_dismiss, "raw": raw,
                    "reformat_backend": reformat_backend,
                    "task_mode": task_mode})


def show_history() -> None:
    _history_q.put(True)


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


def flash_screen_edge(colour: str = "#3b82f6") -> None:
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


def show_agent_confirm(desc: str, confirm_cb) -> None:
    """Show a modal confirm gate for an agent action. Safe to call from any thread."""
    _agent_q.put({"desc": desc, "cb": confirm_cb})


_partial_q: queue.Queue = queue.Queue()


def set_partial_text(text: str) -> None:
    """Live partial transcription shown under the recording badge. Any thread."""
    _partial_q.put(text or "")


# ---------------------------------------------------------------------------
# Internal — everything below runs exclusively on the tkinter worker thread
# ---------------------------------------------------------------------------

def _tk_main() -> None:
    global _root
    _root = tk.Tk()
    try:
        # points-per-pixel ratio so point-sized fonts track the real DPI
        _root.tk.call("tk", "scaling", ctypes.windll.user32.GetDpiForSystem() / 72.0)
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
_badge_time:   tk.Label | None = None
_badge_logo_lbl: tk.Label | None = None
_badge_logo_variants: list = []           # PhotoImage list indexed by brightness level
_badge_logo_idx: int = 0
_badge_state:  str | None = None
_badge_anim_phase: float = 0.0            # drives the processing sweep
_badge_smoothed: list[float] = []         # interpolated bar heights for ease-out decay
_badge_partial_lbl: tk.Label | None = None  # live partial transcription line


def _tick() -> None:
    global _preview_open, _history_open, _profile_open, _settings_open
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
                    latest_preview.get("reformat_backend"),
                    latest_preview.get("task_mode", False),
                )
            except Exception as e:
                print(f"preview window error: {e}")
                _preview_open = False

    # Live-update waveform badge
    if _badge_state in ("recording", "recording_task", "processing") and _badge_alive():
        _draw_badge_frame()

    # Always drain these queues so items don't accumulate while a window is open
    # and immediately reopen it the moment the user closes it.
    history_requested = False
    while True:
        try:
            _history_q.get_nowait()
            history_requested = True
        except queue.Empty:
            break
    if history_requested and not _history_open:
        _history_open = True
        try:
            _open_history()
        except Exception as e:
            print(f"history window error: {e}")
            _history_open = False

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

    # Drain agent confirm queue
    while True:
        try:
            a = _agent_q.get_nowait()
            try:
                _open_agent_confirm(a["desc"], a["cb"])
            except Exception as exc:
                log_error("preview", f"agent confirm error: {exc}")
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

    accent = {"info": _BLUE, "warn": "#f59e0b", "error": "#ef4444"}.get(kind, _BLUE)

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

_BADGE_CFG = {
    "recording":      {"accent": "#e03030", "logo_bg": (210,  30,  30)},
    "recording_task": {"accent": "#22c55e", "logo_bg": ( 34, 197,  94)},  # green — Ctrl+Shift+Alt task capture
    "processing":     {"accent": "#c8a000", "logo_bg": (200, 160,   0)},
    "reformatting":   {"accent": "#7b5ea7", "logo_bg": (123,  94, 167)},
    "too_short":      {"accent": "#888888", "text": "Hold longer to record"},
    "not_ready":      {"accent": "#888888", "text": "Model loading — please wait"},
}

# Visual layout for the recording badge
_BADGE_W       = _px(320)
_BADGE_H       = _px(64)
_LOGO_SIZE     = _px(44)
_WAVE_BAR_N    = 28
_WAVE_BAR_W    = 4
_WAVE_BAR_GAP  = 2


def _badge_alive() -> bool:
    """Return True only if _badge_win is a live Tkinter window."""
    global _badge_win, _badge_label, _badge_dot, _badge_canvas
    global _badge_time, _badge_logo_lbl, _badge_logo_variants
    global _badge_partial_lbl
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


def _build_recording_badge(cfg: dict) -> None:
    """Create the large recording badge with logo + waveform canvas + elapsed timer."""
    global _badge_win, _badge_canvas, _badge_time
    global _badge_label, _badge_dot, _badge_logo_lbl
    global _badge_logo_variants, _badge_logo_idx, _badge_smoothed
    global _badge_partial_lbl

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

    # Live partial transcription line — packed lazily when text first arrives
    _badge_partial_lbl = tk.Label(
        inner, text="", bg=_BG, fg=_FG2,
        font=(_FONT_FAM_TEXT, 9), wraplength=300,
        justify="left", anchor="w",
    )

    _badge_label = None
    _badge_dot = None
    _badge_smoothed = [0.0] * _WAVE_BAR_N

    _badge_win.update_idletasks()
    w = _badge_win.winfo_reqwidth()
    h = _badge_win.winfo_reqheight()
    sw = _badge_win.winfo_screenwidth()
    sh = _badge_win.winfo_screenheight()
    _badge_win.geometry(f"{w}x{h}+{sw - w - 20}+{sh - h - 60}")

    winfx.apply_rounded_region(_badge_win, radius=14)
    winfx.fade_in(_badge_win, target=0.96, duration_ms=180)


def _build_text_badge(cfg: dict) -> None:
    """Lightweight text badge for too_short / not_ready toasts."""
    global _badge_win, _badge_label, _badge_dot
    global _badge_canvas, _badge_time, _badge_logo_lbl, _badge_logo_variants
    global _badge_partial_lbl
    _badge_partial_lbl = None

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

    _badge_win.update_idletasks()
    w = _badge_win.winfo_reqwidth()
    h = _badge_win.winfo_reqheight()
    sw = _badge_win.winfo_screenwidth()
    sh = _badge_win.winfo_screenheight()
    _badge_win.geometry(f"{w}x{h}+{sw - w - 20}+{sh - h - 60}")

    winfx.apply_rounded_region(_badge_win, radius=10)
    winfx.fade_in(_badge_win, target=0.94, duration_ms=160)


def _hex_blend(hex_a: str, hex_b: str, t: float) -> str:
    """Blend two #rrggbb colours. t=0 → a, t=1 → b."""
    a = int(hex_a[1:3], 16), int(hex_a[3:5], 16), int(hex_a[5:7], 16)
    b = int(hex_b[1:3], 16), int(hex_b[3:5], 16), int(hex_b[5:7], 16)
    r = int(a[0] + (b[0] - a[0]) * t)
    g = int(a[1] + (b[1] - a[1]) * t)
    bl = int(a[2] + (b[2] - a[2]) * t)
    return f"#{r:02x}{g:02x}{bl:02x}"


# Cache gradient swatches per accent to avoid re-blending every frame
_GRADIENT_STOPS = 6
_gradient_cache: dict[str, list[str]] = {}


def _gradient_for(accent: str) -> list[str]:
    """Return GRADIENT_STOPS colours from `accent` (center, bright) → tip (faded toward _BG)."""
    cached = _gradient_cache.get(accent)
    if cached is not None:
        return cached
    bright = _hex_blend(accent, "#ffffff", 0.18)
    stops = [_hex_blend(bright, accent, i / (_GRADIENT_STOPS - 1)) for i in range(_GRADIENT_STOPS)]
    _gradient_cache[accent] = stops
    return stops


def _draw_badge_frame() -> None:
    """Render one frame of the mirrored gradient equalizer + update the timer + pulse logo."""
    global _badge_anim_phase, _badge_logo_idx
    if _badge_canvas is None:
        return

    cfg = _BADGE_CFG.get(_badge_state or "recording", _BADGE_CFG["recording"])
    accent = cfg["accent"]

    try:
        canvas_h = _LOGO_SIZE
        mid = canvas_h / 2
        max_half = canvas_h * 0.44

        if _badge_state in ("recording", "recording_task"):
            levels = audio.get_recent_levels(_WAVE_BAR_N)
            silence = audio.get_silence_elapsed()
            timeout = audio.get_silence_timeout()
            is_silent_warn = timeout > 0 and silence > 1.5
            if is_silent_warn:
                accent = "#c8a000"

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

        # Smooth interpolation: snap up fast, ease down slow (ease-out decay)
        if len(_badge_smoothed) != _WAVE_BAR_N:
            _badge_smoothed[:] = list(targets)
        else:
            for i, t in enumerate(targets):
                cur = _badge_smoothed[i]
                if t > cur:
                    _badge_smoothed[i] = cur * 0.45 + t * 0.55     # fast attack
                else:
                    _badge_smoothed[i] = cur * 0.78 + t * 0.22     # slow release

        # Render mirrored gradient bars
        _badge_canvas.delete("all")
        stops = _gradient_for(accent)
        seg = max_half / _GRADIENT_STOPS
        for i, h in enumerate(_badge_smoothed):
            x = i * (_WAVE_BAR_W + _WAVE_BAR_GAP)
            bar_h = max(2.0, h * max_half)
            visible_segs = int(math.ceil(bar_h / seg))
            for s in range(min(visible_segs, _GRADIENT_STOPS)):
                seg_top    = s * seg
                seg_bottom = min((s + 1) * seg, bar_h)
                colour = stops[s]
                _badge_canvas.create_rectangle(
                    x, mid - seg_bottom, x + _WAVE_BAR_W, mid - seg_top,
                    fill=colour, outline="",
                )
                _badge_canvas.create_rectangle(
                    x, mid + seg_top, x + _WAVE_BAR_W, mid + seg_bottom,
                    fill=colour, outline="",
                )

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

        if _badge_time is not None and _badge_state in ("recording", "recording_task"):
            elapsed = int(audio.get_elapsed())
            new_t = f"{elapsed // 60}:{elapsed % 60:02d}"
            if _badge_time.cget("text") != new_t:
                _badge_time.config(text=new_t)
        elif _badge_time is not None and _badge_state not in ("recording", "recording_task"):
            if _badge_time.cget("text") != "":
                _badge_time.config(text="")
    except Exception:
        pass


def _update_badge_partial(text: str) -> None:
    """Show/refresh the live partial transcription line under the waveform."""
    if not _badge_alive() or _badge_partial_lbl is None:
        return
    try:
        disp = text if len(text) <= 160 else "…" + text[-160:]
        if disp:
            if not _badge_partial_lbl.winfo_ismapped():
                _badge_partial_lbl.pack(fill=tk.X, pady=(6, 0))
            _badge_partial_lbl.config(text=disp)
        else:
            _badge_partial_lbl.pack_forget()
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
        if _badge_alive():
            win = _badge_win
            _badge_win = None  # null first so reference is released
            winfx.fade_out_then_destroy(win, duration_ms=160)
        return

    cfg = _BADGE_CFG.get(cmd, _BADGE_CFG["processing"])
    needs_wave = cmd in ("recording", "recording_task", "processing", "reformatting")

    # Rebuild if widget type doesn't match the new state
    have_wave = _badge_canvas is not None
    if _badge_alive() and have_wave != needs_wave:
        try:
            _badge_win.destroy()
        except Exception:
            pass
        _badge_win = None

    if not _badge_alive():
        if needs_wave:
            _build_recording_badge(cfg)
            _draw_badge_frame()
        else:
            _build_text_badge(cfg)
        return

    # Live update existing badge
    if needs_wave:
        if prev != cmd:
            _badge_logo_variants = _build_logo_variants(cfg["logo_bg"])
            _badge_logo_idx = 0
            if _badge_logo_lbl is not None:
                try:
                    _badge_logo_lbl.config(image=_badge_logo_variants[0])
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
        return _BLUE       # confident — blue
    if confidence >= 0.3:
        return "#c8a000"   # uncertain — amber
    return "#cc4400"       # low confidence — orange-red


def _open_window(text: str, hwnd: int, empty: bool = False,
                 confidence: float | None = None,
                 auto_dismiss: float = 0.0,
                 words: list | None = None,
                 raw: str | None = None,
                 reformat_backend: str | None = None,
                 task_mode: bool = False) -> None:
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

    # Task-mode gets its own accent (green) instead of the normal blue, applied
    # consistently to the outer ring, heading, entry border, button, and chip —
    # so a task capture is unmistakable from a normal paste preview at a glance.
    accent    = _TASK if task_mode else _BLUE
    accent_hv = _TASK_HV if task_mode else _BLUE_HV

    # 1-pixel outer ring for depth (task mode: accent-coloured, thicker)
    ring = tk.Frame(win, bg=(accent if task_mode else _BORDER2), bd=0)
    ring.pack(fill=tk.BOTH, expand=True, padx=(2 if task_mode else 1), pady=(2 if task_mode else 1))

    # Premium accent bar — 3px coloured strip at the very top
    tk.Frame(ring, bg=accent, height=3).pack(fill=tk.X, side=tk.TOP)

    frame = tk.Frame(ring, bg=_BG, padx=20, pady=16)
    frame.pack(fill=tk.BOTH, expand=True)

    # Header row: label + status dot (left), paste destination (right)
    header_row = tk.Frame(frame, bg=_BG)
    header_row.pack(fill=tk.X, pady=(0, 8))
    if task_mode:
        tk.Label(header_row, text="Add to To-Do List", bg=_BG, fg=accent,
                 font=(_FONT_FAM_DISPLAY, 11, "bold"), anchor="w").pack(side=tk.LEFT)
    else:
        tk.Label(header_row, text="VoiceDictate", bg=_BG, fg=_FG3,
                 font=(_FONT_FAM_DISPLAY, 9, "normal"), anchor="w").pack(side=tk.LEFT)
    # Status dot: green = ready to insert, grey = nothing usable.
    # A text glyph, not a Canvas oval — ClearType antialiases it for free.
    tk.Label(header_row, text="●", bg=_BG, fg=(_FG3 if empty else _TASK),
             font=(_FONT_FAM_TEXT, 7, "normal")).pack(side=tk.LEFT, padx=(6, 0))
    # Paste destination — so it's obvious where Enter sends the text
    dest = "TaskFlow" if task_mode else _target_app_label(hwnd)
    if dest:
        tk.Label(header_row, text=f"→  {dest}", bg=_BG, fg=_FG2,
                 font=(_FONT_FAM_TEXT, 8, "normal"), anchor="e").pack(side=tk.RIGHT)

    # ── Text entry ─────────────────────────────────────────────────────────
    if empty:
        display = ("Heard the trigger phrase, but no task text after it — try again"
                   if task_mode else "Nothing detected — try again")
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
        entry.tag_configure("conf_low",    underline=True, underlinefg="#ef4444")
        entry.tag_configure("conf_medium", underline=True, underlinefg="#f59e0b")
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
            hint_fg = "#cc4400"
        elif confidence < 0.6:
            hint_text = "Check transcription before inserting"
            hint_fg = "#c8a000"
        else:
            hint_text = ""
            hint_fg = _FG2
        if hint_text:
            tk.Label(
                frame, text=hint_text, bg=_BG, fg=hint_fg,
                font=_FONT_HINT, anchor="w",
            ).pack(fill=tk.X, pady=(0, 2))

    # ── Word + char count ──────────────────────────────────────────────────
    count_var = tk.StringVar()
    tk.Label(
        frame, textvariable=count_var,
        bg=_BG, fg=_FG3, font=_FONT_META, anchor="w",
    ).pack(fill=tk.X, pady=(0, 5))

    def _update_count(*_):
        content = entry.get("1.0", "end-1c")
        wc = len(content.split()) if content.strip() else 0
        cc = len(content)
        count_var.set(f"{wc} word{'s' if wc != 1 else ''}  ·  {cc} char{'s' if cc != 1 else ''}")
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

    def _close() -> None:
        global _preview_open, _current_preview_win
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
        submit = submit and not task_mode
        result = entry.get("1.0", "end-1c").rstrip()
        if not empty:
            original = text.rstrip()
            if original != result:
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

        if task_mode:
            # Prime + close immediately, same as the normal Insert path below —
            # the health-check/POST round-trip then runs off the Tk thread so
            # the UI is never blocked waiting on the network.
            inject.prime_foreground(hwnd)
            _close()

            def _relaunch() -> None:
                threading.Thread(target=taskflow.ensure_running, daemon=True).start()

            def _confirm_task() -> None:
                try:
                    if taskflow.is_duplicate(result):
                        log("taskflow", f"duplicate skipped (task mode): {result!r}")
                        show_toast(f"Already added recently: {result}", kind="info")
                        return
                    if not taskflow.check_health():
                        warn("taskflow", "health check failed in task mode — aborting task creation")
                        if taskflow.record_health_check(False):
                            show_toast("TaskFlow seems to be down.", kind="warn",
                                      action_label="Relaunch", action_cb=_relaunch)
                        else:
                            show_toast("TaskFlow isn't running — pasted instead.", kind="warn")
                        inject.inject_text(result, hwnd)
                        return
                    taskflow.record_health_check(True)
                    try:
                        with open(_CONFIG_FILE, encoding="utf-8") as f:
                            default_project = json.load(f).get("taskflow_default_project") or None
                    except Exception:
                        default_project = None
                    spec = taskflow.build_task_spec(result, default_project)
                    created = taskflow.create_task_from_spec(spec)
                    if created is None:
                        warn("taskflow", f"create_task_from_spec returned None for {result!r}")
                        show_toast("Couldn't reach TaskFlow — pasted instead.", kind="warn")
                        inject.inject_text(result, hwnd)
                        return
                    title = spec.get("title", result)
                    task_id = created.get("id")
                    chime.play_task_added()
                    tray.increment_task_count()
                    try:
                        with open(_CONFIG_FILE, encoding="utf-8") as f:
                            voice_confirm = json.load(f).get("taskflow_voice_confirm", False)
                    except Exception:
                        voice_confirm = False
                    if voice_confirm:
                        chime.speak(f"Added {title} to your to-do list")
                    hist.save(title, source="taskflow")

                    def _undo() -> None:
                        if task_id and taskflow.delete_task(task_id):
                            show_toast(f"Removed: {title}", kind="info")

                    show_toast(f"Added to to-do list: {title}", kind="info",
                              action_label="Undo" if task_id else "",
                              action_cb=_undo if task_id else None)
                except Exception as exc:
                    log_error("taskflow", f"_confirm_task unhandled exception: {exc}")
                    show_toast("Error adding task — check app.log.", kind="warn")

            threading.Thread(target=_confirm_task, daemon=True).start()
            return

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
        btns, text=("Add to List" if task_mode else "Insert"), command=on_insert, width=10,
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

    # ── Raw / Prompt toggle (only when vibe mode produced a different text) ──
    if raw and raw.strip() != text.strip() and not empty:
        _showing_raw = [False]

        def _toggle_raw():
            _showing_raw[0] = not _showing_raw[0]
            new_content = raw if _showing_raw[0] else text
            new_label   = "Prompt" if _showing_raw[0] else "Raw"
            entry.config(state="normal")
            entry.delete("1.0", tk.END)
            entry.insert("1.0", new_content)
            toggle_btn.config(text=new_label)

        toggle_row = tk.Frame(frame, bg=_BG)
        toggle_row.pack(fill=tk.X, pady=(4, 0))

        # Indicator: did the LLM actually clean this up, or did it silently
        # fall back to the regex "rules" cleaner (e.g. LM Studio model unloaded)?
        if reformat_backend == "rules":
            tk.Label(toggle_row, text="rules fallback", bg=_BG, fg=_FG3,
                     font=_FONT_CHIP).pack(side=tk.LEFT)
        elif reformat_backend in ("lmstudio", "api"):
            tk.Label(toggle_row, text="LLM", bg=_BG, fg="#22c55e",
                     font=(_FONT_FAM_TEXT, 8, "bold")).pack(side=tk.LEFT)

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

    _chip(chip_row, "↵",       "Add to List" if task_mode else "Insert", accent=True, key_color=accent)
    if not task_mode:
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

    # ── Drag ───────────────────────────────────────────────────────────────
    def _drag_start(event):
        win._ox = event.x_root - win.winfo_x()
        win._oy = event.y_root - win.winfo_y()

    def _drag_motion(event):
        win.geometry(f"+{event.x_root - win._ox}+{event.y_root - win._oy}")

    for widget in (win, frame):
        widget.bind("<ButtonPress-1>", _drag_start)
        widget.bind("<B1-Motion>", _drag_motion)

    # ── Size & position ────────────────────────────────────────────────────
    win.update_idletasks()
    w = max(420, win.winfo_reqwidth())
    h = win.winfo_reqheight()
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    x, y = _calc_position(cx, cy, w, h, sw, sh, _preview_position)
    win.geometry(f"{w}x{h}+{x}+{y}")

    # Activate the preview so keyboard input (Enter/Insert) goes here, not to
    # VS Code / Electron. overrideredirect(True) popup windows don't auto-activate
    # on Windows — the previously-focused app keeps OS keyboard focus and the
    # user's Insert/Enter keypress goes straight to VS Code (toggling overwrite
    # mode or whatever) instead of triggering on_insert().
    # activate_window uses AttachThreadInput to steal foreground even when our
    # process is not the current foreground process.
    # Don't steal focus while the recording hotkey is still physically held.
    # Recording stops on the FIRST modifier release; if an RDP window has focus
    # and we grab it before the second key comes up, mstsc never forwards that
    # key-up and the modifier stays stuck in the remote session (ctrl-clicks,
    # broken right-click) until the user presses it again inside the remote.
    inject.wait_modifiers_released(timeout_ms=500)
    inject.activate_window(win.winfo_id())
    win.lift()
    win.focus_force()
    entry.focus_set()

    # ── Rounded corners + slide-up entrance ────────────────────────────────
    winfx.apply_rounded_region(win, radius=12)
    winfx.slide_in(win, dx=0, dy=8, duration_ms=200, alpha_target=1.0)
    if not empty:
        target_border = accent if task_mode else _border_colour(confidence)
        winfx.ease_color(entry, "highlightbackground", _BORDER, target_border,
                         duration_ms=260, steps=10)

    # ── Auto-dismiss ───────────────────────────────────────────────────────
    if auto_dismiss > 0:
        win.after(int(auto_dismiss * 1000), _close)


# ---------------------------------------------------------------------------
# History viewer
# ---------------------------------------------------------------------------

def _fmt_ts(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
        if dt.date() == date.today():
            return f"Today  {dt.strftime('%H:%M')}"
        elif (date.today() - dt.date()).days < 7:
            return dt.strftime("%a  %H:%M")
        else:
            return dt.strftime("%d %b %Y  %H:%M")
    except Exception:
        return iso


def _open_agent_confirm(desc: str, confirm_cb) -> None:
    """Modal confirm gate for agent actions. Runs on the tkinter thread."""
    win = tk.Toplevel(_root)
    win.title("Agent Command")
    win.configure(bg=_BG)
    win.resizable(False, False)
    win.attributes("-topmost", True)
    win.attributes("-alpha", 0.0)
    winfx.apply_rounded_region(win, radius=12)

    # Accent top bar (orange for agent)
    accent_bar = tk.Frame(win, bg="#f97316", height=3)
    accent_bar.pack(fill=tk.X, side=tk.TOP)

    body = tk.Frame(win, bg=_BG)
    body.pack(fill=tk.BOTH, expand=True, padx=24, pady=(18, 14))

    tk.Label(body, text="Run this command?", bg=_BG, fg=_FG,
             font=(_FONT_FAM_DISPLAY, 13, "bold")).pack(anchor="w")

    tk.Label(body, text=desc, bg=_BG2, fg=_FG,
             font=_FONT_BODY, wraplength=380, justify="left",
             padx=12, pady=8).pack(fill=tk.X, pady=(10, 0))

    hint = tk.Label(body, text="Enter to run  ·  Esc to cancel",
                    bg=_BG, fg=_FG3, font=_FONT_HINT)
    hint.pack(anchor="w", pady=(8, 0))

    btns = tk.Frame(body, bg=_BG)
    btns.pack(fill=tk.X, pady=(14, 0))

    def do_run():
        win.destroy()
        if confirm_cb:
            threading.Thread(target=confirm_cb, daemon=True).start()

    def do_cancel():
        win.destroy()

    tk.Button(btns, text="Cancel", command=do_cancel,
              bg=_BG2, fg=_FG2, activebackground=_BG3, activeforeground=_FG,
              relief="flat", bd=0, font=_FONT_BTN, padx=12, pady=6,
              cursor="hand2").pack(side=tk.RIGHT, padx=(6, 0))
    tk.Button(btns, text="Run", command=do_run,
              bg="#f97316", fg="#ffffff", activebackground="#ea6900",
              activeforeground="#ffffff", relief="flat", bd=0,
              font=_FONT_BTN, padx=16, pady=6, cursor="hand2").pack(side=tk.RIGHT)

    win.bind("<Return>", lambda _: do_run())
    win.bind("<Escape>", lambda _: do_cancel())

    win.update_idletasks()
    sw = win.winfo_screenwidth()
    sh = win.winfo_screenheight()
    w  = win.winfo_reqwidth()
    h  = win.winfo_reqheight()
    win.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")

    winfx.fade_in(win, target=0.97, duration_ms=150)
    win.focus_force()




def _open_history() -> None:
    entries = hist.load()

    win = tk.Toplevel(_root)
    win.title("VoiceDictate — History")
    win.configure(bg=_BG)
    win.geometry("620x500")
    win.minsize(440, 260)

    # ── Header: title + search ─────────────────────────────────────────────
    header = tk.Frame(win, bg=_BG)
    header.pack(fill=tk.X, padx=18, pady=(16, 10))

    tk.Label(
        header, text="Dictation History",
        bg=_BG, fg=_FG, font=(_FONT_FAM_DISPLAY, 14, "bold"),
    ).pack(side=tk.LEFT)

    count_var = tk.StringVar(value=f"{len(entries)} entries")
    tk.Label(header, textvariable=count_var, bg=_BG, fg=_FG3,
             font=(_FONT_FAM_TEXT, 9)).pack(side=tk.RIGHT)

    search_row = tk.Frame(win, bg=_BG)
    search_row.pack(fill=tk.X, padx=18, pady=(0, 8))
    search_var = tk.StringVar()
    search_entry = tk.Entry(
        search_row, textvariable=search_var, bg=_BG2, fg=_FG,
        insertbackground=_FG, relief="flat", bd=0,
        highlightthickness=1, highlightbackground=_BORDER,
        highlightcolor=_BLUE, font=_FONT_BODY,
    )
    search_entry.pack(fill=tk.X, ipady=4)
    # Placeholder behaviour
    PLACEHOLDER = "Search…"
    search_entry.insert(0, PLACEHOLDER)
    search_entry.config(fg=_FG3)

    def _on_search_focus(_=None):
        if search_var.get() == PLACEHOLDER:
            search_entry.delete(0, tk.END)
            search_entry.config(fg=_FG)

    def _on_search_blur(_=None):
        if not search_var.get():
            search_entry.insert(0, PLACEHOLDER)
            search_entry.config(fg=_FG3)

    search_entry.bind("<FocusIn>",  _on_search_focus)
    search_entry.bind("<FocusOut>", _on_search_blur)

    # ── Entries list (text widget with tags) ───────────────────────────────
    list_frame = tk.Frame(win, bg=_BG)
    list_frame.pack(fill=tk.BOTH, expand=True, padx=18, pady=(0, 10))

    sb = tk.Scrollbar(list_frame)
    sb.pack(side=tk.RIGHT, fill=tk.Y)

    txt = tk.Text(
        list_frame, bg=_BG2, fg=_FG,
        font=_FONT_BODY, wrap=tk.WORD,
        relief="flat", bd=0, padx=12, pady=10,
        yscrollcommand=sb.set, cursor="arrow",
    )
    txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    sb.config(command=txt.yview)

    txt.tag_configure("ts",     foreground=_FG3, font=(_FONT_FAM_TEXT, 9), spacing1=4)
    txt.tag_configure("ts_task", foreground=_TASK, font=(_FONT_FAM_TEXT, 9, "bold"), spacing1=4)
    txt.tag_configure("body",   foreground=_FG,  font=_FONT_BODY, spacing3=6)
    txt.tag_configure("sep",    foreground=_BORDER)
    txt.tag_configure("hover",  background=_BG3)
    txt.tag_configure("match",  background=_MATCH_BG, foreground=_MATCH_FG)
    txt.tag_configure("flash",  background=_BLUE)  # brief click-to-copy confirmation, eased off in _flash_row

    def _flash_row(start: str, end: str) -> None:
        """Quick background flash on a row, echoing the copy-to-clipboard click."""
        steps = 6
        txt.tag_add("flash", start, end)

        def tick(i: int) -> None:
            try:
                if not txt.winfo_exists():
                    return
            except Exception:
                return
            txt.tag_configure("flash", background=_hex_blend(_BLUE, _BG2, i / steps))
            if i >= steps:
                txt.tag_remove("flash", start, end)
                return
            txt.after(35, lambda: tick(i + 1))

        tick(0)

    def _populate(data: list, query: str = "") -> None:
        txt.config(state="normal")
        txt.delete("1.0", tk.END)

        q = query.strip().lower()
        if q == PLACEHOLDER.lower():
            q = ""

        filtered = [e for e in data
                    if not q or q in e.get("text", "").lower()]
        count_var.set(f"{len(filtered)} of {len(data)} entries" if q else f"{len(data)} entries")

        if filtered:
            for i, entry in enumerate(filtered):
                body_text = entry.get("text", "").strip()
                is_task = entry.get("source") == "taskflow"
                ts = _fmt_ts(entry.get("timestamp", ""))
                if is_task:
                    ts = "✓ " + ts + " — added to to-do list"
                start_idx = txt.index(tk.END)
                txt.insert(tk.END, ts + "\n", "ts_task" if is_task else "ts")
                body_start = txt.index(tk.END)
                txt.insert(tk.END, body_text + "\n", "body")
                body_end = txt.index(f"{tk.END}-1c")
                end_idx = txt.index(tk.END)

                # Per-entry click-to-copy tag spans entire entry
                row_tag = f"row_{i}"
                txt.tag_add(row_tag, start_idx, end_idx)
                txt.tag_bind(row_tag, "<Button-1>",
                             lambda e, t=body_text, s=start_idx, en=end_idx: (
                                 _flash_row(s, en), _copy_with_toast(t)))
                txt.tag_bind(row_tag, "<Enter>",
                             lambda e, s=start_idx, en=end_idx: (
                                 txt.tag_add("hover", s, en),
                                 txt.config(cursor="hand2"),
                             ))
                txt.tag_bind(row_tag, "<Leave>",
                             lambda e, s=start_idx, en=end_idx: (
                                 txt.tag_remove("hover", s, en),
                                 txt.config(cursor="arrow"),
                             ))

                # Highlight matches
                if q:
                    pos = body_start
                    while True:
                        idx = txt.search(q, pos, stopindex=body_end, nocase=True)
                        if not idx:
                            break
                        end = f"{idx}+{len(q)}c"
                        txt.tag_add("match", idx, end)
                        pos = end

                if i < len(filtered) - 1:
                    txt.insert(tk.END, "─" * 64 + "\n", "sep")
        else:
            msg = "No matching entries." if q else "No dictation history yet."
            txt.insert(tk.END, msg, "ts")
        txt.config(state="disabled")

    def _copy_with_toast(text: str) -> None:
        try:
            pyperclip.copy(text)
        except Exception:
            return
        widgets.Toast(win, "Copied to clipboard",
                      bg=_BG2, fg=_FG, duration_ms=1200)

    _populate(entries)

    def _on_search_change(*_):
        _populate(entries, search_var.get())
    search_var.trace_add("write", _on_search_change)

    # ── Footer: clear + close ──────────────────────────────────────────────
    bar = tk.Frame(win, bg=_BG)
    bar.pack(fill=tk.X, padx=18, pady=(0, 14))

    def on_clear() -> None:
        hist.clear()
        nonlocal entries
        entries = []
        _populate(entries, search_var.get())

    def on_close() -> None:
        global _history_open
        _history_open = False
        winfx.fade_out_then_destroy(win, duration_ms=140)

    clear_wrap = tk.Frame(bar, bg=_BORDER, padx=1, pady=1)
    tk.Button(
        clear_wrap, text="Clear History", command=on_clear,
        bg=_BG, fg=_FG2,
        activebackground=_BG2, activeforeground=_FG,
        relief="flat", bd=0,
        font=_FONT_BTN, padx=10, pady=6, cursor="hand2",
    ).pack()
    clear_wrap.pack(side=tk.LEFT)

    tk.Label(bar, text="Click any entry to copy",
             bg=_BG, fg=_FG3, font=(_FONT_FAM_TEXT, 8)).pack(side=tk.RIGHT)

    win.protocol("WM_DELETE_WINDOW", on_close)
    win.bind("<Escape>", lambda _: on_close())

    winfx.fade_in(win, target=1.0, duration_ms=160)


# ---------------------------------------------------------------------------
# Speech Profile viewer
# ---------------------------------------------------------------------------

def _open_profile() -> None:
    rules = profile.get_all_rules()

    win = tk.Toplevel(_root)
    win.title("VoiceDictate — Speech Profile")
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
    txt.tag_configure("del",     foreground="#cc4444",  font=("Segoe UI", 8), underline=True)
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
    win.title("VoiceDictate — Settings")
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
                                 active_lo="#22c55e", active_hi="#ef4444")
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

    def build_agent() -> tk.Frame:
        p = tk.Frame(pages_holder, bg=_BG)
        _h2(p, "Agent Command Mode").pack(fill=tk.X, padx=22, pady=(18, 6))
        _note(p, "Voice command execution via local LLM. Enable to launch LM Studio "
                 "and load the model. Disable to unload it and free VRAM.").pack(
            fill=tk.X, padx=22, pady=(0, 12))

        agent_cmd_var = tk.BooleanVar(value=bool(cfg.get("agent_command_mode_enabled", False)))
        state["agent_cmd_var"] = agent_cmd_var
        _toggle_row(p, "Enable Agent Command Mode", agent_cmd_var,
                    sub="Launches LM Studio on enable; unloads model on disable.")

        _note(p, "Hotkey: Ctrl+Shift+C — hold to record a command, release to classify and confirm.").pack(
            fill=tk.X, padx=22, pady=(12, 0))
        _note(p, "Supported commands: open Chrome, search for X, run git status, type Hello.").pack(
            fill=tk.X, padx=22, pady=(4, 0))

        _label(p, "Model (shared with Vibe Mode)").pack(fill=tk.X, padx=22, pady=(16, 2))
        model_var_agent = tk.StringVar(value=cfg.get("lmstudio_model", "qwen/qwen3-8b"))
        state["lmstudio_model_var"] = model_var_agent
        _entry(p, model_var_agent).pack(fill=tk.X, padx=22)
        _note(p, "Must be installed in LM Studio. Smaller models (1B–3B) work well for command classification.").pack(
            fill=tk.X, padx=22, pady=(2, 0))
        return p

    def build_todo() -> tk.Frame:
        p = tk.Frame(pages_holder, bg=_BG)
        _h2(p, "To-Do List").pack(fill=tk.X, padx=22, pady=(18, 6))
        _note(p, "Voice capture into TaskFlow. Phrases are checked at the start "
                 "(or end) of each sentence, so they work mid-conversation too.").pack(
            fill=tk.X, padx=22, pady=(0, 12))

        taskflow_var = tk.BooleanVar(value=bool(cfg.get("taskflow_enabled", True)))
        state["taskflow_var"] = taskflow_var
        _toggle_row(p, "Enable TaskFlow capture", taskflow_var,
                    sub="Also gates the read-back and mark-done voice commands below.")

        voice_confirm_var = tk.BooleanVar(value=bool(cfg.get("taskflow_voice_confirm", False)))
        state["voice_confirm_var"] = voice_confirm_var
        _toggle_row(p, "Speak confirmations", voice_confirm_var,
                    sub="Use Windows text-to-speech to confirm tasks added/completed, "
                        "for when you're not looking at the screen.")

        default_project_var = tk.StringVar(value=cfg.get("taskflow_default_project", ""))
        state["default_project_var"] = default_project_var
        _label(p, "Default project (TaskFlow project name, optional)").pack(
            fill=tk.X, padx=22, pady=(14, 2))
        _entry(p, default_project_var).pack(fill=tk.X, padx=22)
        _note(p, "Used when a task isn't routed to a project by saying "
                 "'...to my <Project> list'.").pack(fill=tk.X, padx=22, pady=(2, 0))

        _label(p, "Leading trigger phrases — one per line, checked at the start "
                  "of a sentence").pack(fill=tk.X, padx=22, pady=(14, 2))
        leading_txt = _text_area(p, 4)
        leading_txt.insert("1.0", "\n".join(cfg.get("taskflow_trigger_phrases", [])))
        leading_txt.pack(fill=tk.X, padx=22)
        state["taskflow_leading_txt"] = leading_txt

        _label(p, "Trailing trigger phrases — one per line, checked at the end "
                  "of a sentence (e.g. 'buy milk, add that to my list')").pack(
            fill=tk.X, padx=22, pady=(14, 2))
        trailing_txt = _text_area(p, 3)
        trailing_txt.insert("1.0", "\n".join(cfg.get("taskflow_trailing_trigger_phrases", [])))
        trailing_txt.pack(fill=tk.X, padx=22)
        state["taskflow_trailing_txt"] = trailing_txt

        _label(p, "Read-back phrases — one per line (e.g. \"what's on my to-do list\")").pack(
            fill=tk.X, padx=22, pady=(14, 2))
        readback_txt = _text_area(p, 3)
        readback_txt.insert("1.0", "\n".join(cfg.get("taskflow_readback_phrases", [])))
        readback_txt.pack(fill=tk.X, padx=22)
        state["taskflow_readback_txt"] = readback_txt

        _note(p, "Mark-done ('mark X as done') is always on while TaskFlow capture "
                 "is enabled — no separate phrase list needed.").pack(
            fill=tk.X, padx=22, pady=(8, 0))
        return p

    PAGE_DEFS = [
        ("Audio",          build_audio),
        ("Hotkey",         build_hotkey),
        ("Transcription",  build_transcription),
        ("Behaviour",      build_behaviour),
        ("Appearance",     build_appearance),
        ("Agent Commands", build_agent),
        ("To-Do List",     build_todo),
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
            err_lbl.config(fg="#ef4444")
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
            "agent_command_mode_enabled":  state["agent_cmd_var"].get(),
            "lmstudio_model":              state["lmstudio_model_var"].get().strip() or "qwen/qwen3-8b",
            "theme":                       state["theme_var"].get(),
            "taskflow_enabled":            state["taskflow_var"].get(),
            "taskflow_voice_confirm":      state["voice_confirm_var"].get(),
            "taskflow_default_project":    state["default_project_var"].get().strip(),
            "taskflow_trigger_phrases":    [l.strip() for l in
                                             state["taskflow_leading_txt"].get("1.0", "end-1c").splitlines()
                                             if l.strip()],
            "taskflow_trailing_trigger_phrases": [l.strip() for l in
                                                   state["taskflow_trailing_txt"].get("1.0", "end-1c").splitlines()
                                                   if l.strip()],
            "taskflow_readback_phrases":   [l.strip() for l in
                                             state["taskflow_readback_txt"].get("1.0", "end-1c").splitlines()
                                             if l.strip()],
        })

        try:
            with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(new_cfg, f, indent=2, ensure_ascii=False)
            configure_position(state["pos_var"].get())
            err_var.set("Saved.")
            err_lbl.config(fg="#22c55e")
            # Brief flash on the Save button itself — a tactile confirmation
            # beyond the footer text, since that's easy to miss.
            winfx.ease_color(
                save_btn, "bg", _BLUE, "#22c55e", duration_ms=150, steps=6,
                on_done=lambda: winfx.ease_color(
                    save_btn, "bg", "#22c55e", _BLUE, duration_ms=300, steps=8))
        except Exception as exc:
            err_var.set(f"Save failed: {exc}")
            err_lbl.config(fg="#ef4444")

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
