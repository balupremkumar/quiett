"""
Modern pywebview dashboard for Quiett.

Replaces the Tkinter Settings + History popup windows with a single
Edge-rendered window (Windows 11 native WebView2). The floating preview
panel (preview.py) is unchanged — it still appears near the cursor.
"""
from __future__ import annotations

import time

# Taken before the heavy imports below (PIL, webview) so the boot-timing lines
# cover the real cold cost of this subprocess, not just the window build.
_T0 = time.monotonic()

import base64
import ctypes
import io
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import wave
import winsound
from datetime import date, datetime, timedelta

from PIL import Image, ImageDraw

import theme

# webview and history are only needed when running as __main__ (subprocess),
# but importing them at module level is harmless and keeps DashboardAPI clean.
try:
    import webview
    import history as hist
except ImportError:
    webview = None  # type: ignore
    hist = None     # type: ignore

_CONFIG_FILE = "config.json"

# Bumped by hand alongside CHANGELOG entries below (no CHANGELOG.md yet —
# app is pre-productisation per PRODUCTION_PLAN.md, hardcode until it exists).
VERSION = "0.1.0"
_CHANGELOG = [
    {"version": "0.1.0", "date": "2026-07-11", "notes": [
        "History search, day-grouping, and window-position memory added to dashboard.",
        "About page added.",
    ]},
]

# ── Subprocess launcher ────────────────────────────────────────────────────────
# pywebview requires the main thread, but pystray already owns it in main.py.
# Solution: launch the dashboard as a separate pythonw.exe subprocess so
# webview gets a clean main thread. Config/history are shared via the JSON files.

_proc: "subprocess.Popen | None" = None
_proc_lock = threading.Lock()
_AUTOSTART_TASK = "Quiett"
_THIS_FILE = os.path.abspath(__file__)
_PROJECT_DIR = os.path.dirname(_THIS_FILE)

# Control channel. The running dashboard binds this loopback port; the bind
# doubling as the single-instance lock is the point — an in-process _proc
# handle can't see a window left over from a previous main.py run, which is
# how duplicate dashboards used to appear. Anyone who fails to bind is a
# second instance and simply hands its request to the first.
_CONTROL_PORT_DEFAULT = 8093


def _control_port() -> int:
    try:
        return int(_read_cfg().get("dashboard_control_port", _CONTROL_PORT_DEFAULT))
    except (TypeError, ValueError):
        return _CONTROL_PORT_DEFAULT


def _send_control(payload: dict, timeout: float = 1.5) -> bool:
    """Hand a command to the running dashboard. False = nobody is listening."""
    try:
        with socket.create_connection(("127.0.0.1", _control_port()), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            return sock.recv(32).startswith(b"ok")
    except OSError:
        return False


def _perf(msg: str) -> None:
    """One-line boot/show timing into app.log. Imported lazily so the parent
    process pays nothing for it."""
    try:
        import logger
        logger.log("dashboard", msg)
    except Exception:
        pass


def open_window(page: str = "home") -> None:
    """Show the dashboard on `page`: raise and refresh the existing window if
    one is up, otherwise launch the subprocess.

    Must not be named `open`: a module-level `open` shadows the builtin for
    every function here and silently broke all config reads/writes."""
    global _proc
    with _proc_lock:
        if _send_control({"cmd": "show", "page": page}):
            return
        # Nothing listening. A live _proc here means one is still booting (its
        # control port isn't bound yet) — don't stack a second window on top.
        if _proc is not None and _proc.poll() is None:
            return
        _proc = subprocess.Popen(
            [sys.executable, _THIS_FILE, page],
            cwd=_PROJECT_DIR,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )


def prewarm() -> None:
    """Boot the dashboard hidden so the first tray click hits the warm control
    path. No-op if one is already up (the control port is the lock)."""
    global _proc
    with _proc_lock:
        if _send_control({"cmd": "ping"}, timeout=1.0):
            return
        if _proc is not None and _proc.poll() is None:
            return
        _proc = subprocess.Popen(
            [sys.executable, _THIS_FILE, "home", "--hidden"],
            cwd=_PROJECT_DIR,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )


def shutdown() -> None:
    """End the resident dashboard on app exit. Closing its window only hides
    it now, so without this a hidden WebView2 tree would outlive the app."""
    global _proc
    _send_control({"cmd": "quit"}, timeout=1.5)
    proc = _proc
    if proc is not None and proc.poll() is None:
        try:
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    _proc = None


def notify_change(what: str = "history") -> None:
    """Tell an open dashboard its data changed, so the page it is showing
    refreshes in place. Fire-and-forget: never blocks the caller (this runs on
    the transcription path) and never raises if no dashboard is running."""
    threading.Thread(
        target=lambda: _send_control({"cmd": "refresh", "what": what}, timeout=0.8),
        daemon=True,
    ).start()


# ── Config helpers ─────────────────────────────────────────────────────────────

def _read_cfg() -> dict:
    try:
        with open(_CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _merge_cfg(updates: dict) -> bool:
    try:
        current = _read_cfg()
        current.update(updates)
        tmp = _CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, _CONFIG_FILE)
        return True
    except Exception:
        return False


# Known config keys, used only to sanity-check imported settings files
# (item 76); an import is rejected if it contains none of these. Mirrors main.py's
# _CONFIG_DEFAULTS keys, plus dashboard-only keys that have no entry there
# (theme, animations, sound_volume, history_max_entries,
# recording_retention_days, dashboard_scale).
_KNOWN_CONFIG_KEYS = frozenset({
    "hotkey", "model", "language", "min_record_seconds", "max_record_seconds",
    "filler_words", "clipboard_restore_delay_ms", "vad_filter", "corrections",
    "rdp_clipboard_settle_ms", "rdp_clipboard_restore_delay_ms",
    "silence_auto_stop_seconds", "preview_position", "preview_auto_dismiss_seconds",
    "auto_paste_threshold", "initial_prompt", "custom_vocabulary", "input_device",
    "history_paused", "silence_threshold", "per_app_paste", "per_app_context",
    "electron_paste_method", "paste_mode", "hotkey_mode",
    "api_server_enabled", "api_server_port",
    "live_preview_enabled", "retain_audio",
    "retain_audio_max_files", "retain_audio_min_seconds", "voice_profile_max_samples",
    "badge_animation", "incognito", "redact_patterns",
    "theme", "animations", "sound_volume", "history_max_entries",
    "recording_retention_days", "dashboard_scale", "dashboard_control_port",
    "dashboard_prewarm",
    "tts_enabled", "tts_speed", "tts_max_chunk_chars", "tts_reference",
    "study_mode", "study_speed", "study_pause_scale", "learn_from_edits",
})

_DIAG_API_BASE = "http://127.0.0.1:8090"


def _diag_api_get(path: str, timeout: float = 2.0) -> "dict | None":
    """GET against the main app's own HTTP API (api_server.py), same loopback
    transport health.py uses for its own backend checks. Returns None if the
    main app isn't running or didn't answer in time."""
    try:
        with urllib.request.urlopen(_DIAG_API_BASE + path, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _diag_api_post_json(path: str, payload: dict, timeout: float = 3.0) -> dict:
    """POST a JSON body against api_server.py (the /speak endpoint for the
    Voice page's Play sample button). Same never-raises contract as
    _diag_api_post: always a dict with 'ok' plus either 'body' or 'error'."""
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(_DIAG_API_BASE + path, method="POST", data=data,
                                      headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return {"ok": True, "body": json.loads(raw) if raw else {}}
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except Exception:
            body = {}
        return {"ok": False, "http_error": True, "status": exc.code, "body": body}
    except Exception as exc:
        return {"ok": False, "http_error": False, "error": str(exc)}


def _diag_api_post(path: str, timeout: float = 2.0) -> dict:
    """POST against api_server.py. Always returns a dict with 'ok' and either
    'body' (parsed JSON) or 'error' (network failure, not an HTTP error)."""
    try:
        req = urllib.request.Request(_DIAG_API_BASE + path, method="POST", data=b"")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"ok": True, "body": json.loads(resp.read().decode("utf-8"))}
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except Exception:
            body = {}
        return {"ok": False, "http_error": True, "body": body}
    except Exception as exc:
        return {"ok": False, "http_error": False, "error": str(exc)}


def _format_history_export(entries: list) -> str:
    lines = ["# Quiett history export", ""]
    for e in entries:
        src = e.get("source") or "dictation"
        lines.append(f"## {e.get('timestamp', '')} - {src}")
        lines.append("")
        lines.append(e.get("text", ""))
        lines.append("")
    return "\n".join(lines)


def _format_history_export_txt(entries: list) -> str:
    """Plain text export (item 39) — no Markdown syntax, just timestamp/source
    header lines followed by the transcript."""
    lines = []
    for e in entries:
        src = e.get("source") or "dictation"
        lines.append(f"{e.get('timestamp', '')} ({src})")
        lines.append(e.get("text", "").strip())
        lines.append("")
    return "\n".join(lines)


# Fake per-entry cue length for the SRT/VTT exports below — dictations in
# history have no per-word timing data, so this is a sequential placeholder
# timeline, not real speech timing. Called out in both file headers.
_FAKE_CUE_SECONDS = 2.0
_TIMING_DISCLAIMER = (
    "Cue timings are sequential placeholders (2 seconds per entry), not real "
    "per-word timing - dictations stored in history have no per-word timing data."
)


def _fmt_srt_ts(total_seconds: float) -> str:
    ms = int(round(total_seconds * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _fmt_vtt_ts(total_seconds: float) -> str:
    ms = int(round(total_seconds * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def _format_history_export_srt(entries: list) -> str:
    """SRT export (item 39). SRT has no official comment syntax, so the
    timing disclaimer is written as plain text ahead of the first numbered
    cue — most players simply ignore text that doesn't match a cue block."""
    lines = [_TIMING_DISCLAIMER, ""]
    for i, e in enumerate(entries):
        start = i * _FAKE_CUE_SECONDS
        end = start + _FAKE_CUE_SECONDS
        lines.append(str(i + 1))
        lines.append(f"{_fmt_srt_ts(start)} --> {_fmt_srt_ts(end)}")
        lines.append(e.get("text", "").strip())
        lines.append("")
    return "\n".join(lines)


def _format_history_export_vtt(entries: list) -> str:
    """VTT export (item 39). WebVTT has a real NOTE block, so the disclaimer
    lives there instead of as loose text."""
    lines = ["WEBVTT", "", "NOTE", _TIMING_DISCLAIMER, ""]
    for i, e in enumerate(entries):
        start = i * _FAKE_CUE_SECONDS
        end = start + _FAKE_CUE_SECONDS
        lines.append(f"{_fmt_vtt_ts(start)} --> {_fmt_vtt_ts(end)}")
        lines.append(e.get("text", "").strip())
        lines.append("")
    return "\n".join(lines)


# DOCX was considered for item 39 and dropped: no dependency is allowed for
# this build (python-docx isn't installed), and the four formats above cover
# plain text, formatted notes, and subtitle interchange without adding one.
_EXPORT_FORMATS = {
    "md":  {"ext": "md",  "label": "Markdown (*.md)", "fn": _format_history_export},
    "txt": {"ext": "txt", "label": "Text file (*.txt)", "fn": _format_history_export_txt},
    "srt": {"ext": "srt", "label": "SubRip subtitles (*.srt)", "fn": _format_history_export_srt},
    "vtt": {"ext": "vtt", "label": "WebVTT subtitles (*.vtt)", "fn": _format_history_export_vtt},
}


def _render_waveform_png(path: str, buckets: int = 60,
                          width: int = 120, height: int = 40) -> "str | None":
    """Downsample a 16-bit PCM WAV's peak amplitude into ~60 buckets and
    render a small bar-style waveform PNG (item 88), returned as a data URI.
    None on anything unreadable/unsupported — caller treats that as "no
    thumbnail", never a crash."""
    try:
        with wave.open(path, "rb") as wf:
            n_frames = wf.getnframes()
            n_channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            raw = wf.readframes(n_frames)
        if sampwidth != 2 or n_frames == 0:
            return None  # recordings/ is always 16-bit PCM; bail rather than mis-decode
        total_samples = len(raw) // 2
        samples = struct.unpack(f"<{total_samples}h", raw)
        if n_channels > 1:
            samples = samples[::n_channels]  # first channel only
        n = len(samples)
        if n == 0:
            return None
        chunk = max(1, n // buckets)
        peaks = []
        for i in range(0, n, chunk):
            block = samples[i:i + chunk]
            if block:
                peaks.append(max(abs(s) for s in block))
        if not peaks:
            return None
        peak_max = max(peaks) or 1

        img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        bar_w = width / len(peaks)
        for i, p in enumerate(peaks):
            bar_h = max(1, int((p / peak_max) * (height - 2)))
            x0 = int(i * bar_w)
            x1 = max(x0 + 1, int((i + 1) * bar_w) - 1)
            y0 = (height - bar_h) // 2
            y1 = y0 + bar_h
            draw.rounded_rectangle([x0, y0, x1, y1], radius=1, fill=(140, 140, 150, 220))

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None


_waveform_cache: dict = {}  # filename -> data URI, in-memory only (item 88)
_audio_duration_cache: dict = {}  # filename -> seconds, in-memory only


def _entry_audio_seconds(entry: dict) -> "float | None":
    """Real speaking duration for one history entry, read from its own wav.

    History entries carry no stored wpm - the Home page's average pace is
    derived honestly from actual recordings rather than invented, so an
    entry with no audio (retention purged it, or incognito skipped it)
    simply doesn't count toward the average."""
    audio = entry.get("audio")
    if not audio:
        return None
    cached = _audio_duration_cache.get(audio)
    if cached is not None:
        return cached
    path = os.path.join("recordings", audio)
    try:
        with wave.open(path, "rb") as wf:
            rate = wf.getframerate()
            dur = wf.getnframes() / float(rate) if rate else None
    except Exception:
        dur = None
    if dur:
        _audio_duration_cache[audio] = dur
    return dur


# Fixed passage the Voice page's pause-plan visual and Play sample button use.
# Same for every user, every session - this makes the plan reproducible against
# the real narration.py segmenter, and "hear it" ambiguity-free.
_STUDY_SAMPLE = (
    "Study Mode reads the way a teacher speaks. Short sentences land first, then a "
    "pause. Lists breathe: one, two, three. Headings get the longest beat of all."
)


def _day_group_label(d: "date", today: "date") -> str:
    """Today / Yesterday / weekday name (this calendar week) / '3 July 2026'."""
    diff = (today - d).days
    if diff == 0:
        return "Today"
    if diff == 1:
        return "Yesterday"
    if diff > 1 and d.isocalendar()[:2] == today.isocalendar()[:2]:
        return d.strftime("%A")
    return d.strftime("%d %B %Y").lstrip("0")


# ── Window geometry persistence ──────────────────────────────────────────────

_MIN_W, _MIN_H = 700, 500


def _load_window_geom() -> "dict | None":
    geom = _read_cfg().get("dashboard_window")
    if not isinstance(geom, dict):
        return None
    try:
        x, y = int(geom["x"]), int(geom["y"])
        w, h = max(_MIN_W, int(geom["w"])), max(_MIN_H, int(geom["h"]))
    except Exception:
        return None
    # Windows reports minimized windows at -32000,-32000; a config poisoned
    # by such a save must fall back to the default placement, not be clamped
    # onto a monitor edge.
    if x <= -30000 or y <= -30000:
        return None
    try:
        screens = webview.screens
        if screens:
            min_x = min(s.x for s in screens)
            min_y = min(s.y for s in screens)
            max_x = max(s.x + s.width for s in screens)
            max_y = max(s.y + s.height for s in screens)
            # Clamp so the title bar always stays reachable even if the
            # monitor that used to hold the window is now unplugged.
            x = min(max(x, min_x), max_x - 120)
            y = min(max(y, min_y), max_y - 80)
    except Exception:
        pass
    return {"x": x, "y": y, "w": w, "h": h}


def _save_window_geom(win) -> None:
    try:
        # Minimizing fires a moved event with the -32000,-32000 shell
        # placeholder position — persisting that loses the real geometry.
        if win.x <= -30000 or win.y <= -30000:
            return
        _merge_cfg({"dashboard_window": {
            "x": win.x, "y": win.y, "w": win.width, "h": win.height,
        }})
    except Exception:
        pass


def _own_main_hwnd() -> int:
    """Top-level window of this process titled Quiett — the webview host.
    pywebview doesn't expose the native handle on every backend, and we need a
    real hwnd to steal foreground properly."""
    import win32gui
    import win32process

    found = []

    def _enum(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            return True
        if pid == os.getpid() and win32gui.GetWindowText(hwnd) == "Quiett":
            found.append(hwnd)
            return False
        return True

    try:
        win32gui.EnumWindows(_enum, None)
    except Exception:
        pass
    return found[0] if found else 0


def _raise_self() -> None:
    """Restore + foreground our own window. SetForegroundWindow alone is
    refused when another process owns the foreground, so attach to that
    thread's input queue first (same dance inject.py does for paste targets)."""
    hwnd = _own_main_hwnd()
    if not hwnd:
        return
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    cur_thread = kernel32.GetCurrentThreadId()
    fg_thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
    attached = bool(fg_thread) and fg_thread != cur_thread
    if attached:
        user32.AttachThreadInput(fg_thread, cur_thread, True)
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
        try:
            user32.SwitchToThisWindow(hwnd, True)
        except Exception:
            pass
    finally:
        if attached:
            user32.AttachThreadInput(fg_thread, cur_thread, False)


_VALID_PAGES = ("home", "dictation", "voice", "history", "dictionary",
                "settings", "diagnostics", "about")


def _purge_history_quietly() -> None:
    """Retention pruning, off the boot path. The short wait keeps the file lock
    clear of the first history read the page does."""
    time.sleep(2.0)
    try:
        hist.purge()
    except Exception:
        pass


def _destroy_window(window) -> None:
    """Tear the window down for real (the quit command), with a hard exit as a
    backstop so a wedged WebView2 can't keep the process resident."""
    try:
        window.destroy()
    except Exception:
        pass
    time.sleep(3)
    os._exit(0)


def _serve_control(srv: "socket.socket", window, ready: threading.Event,
                   quitting: "threading.Event | None" = None) -> None:
    """Handle show/refresh/ping/quit from other instances and from the main app.

    Runs on a daemon thread for the life of the window. Every command waits for
    the page to be interactive first, otherwise evaluate_js lands before the
    script block exists and is silently lost."""
    while True:
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        with conn:
            try:
                conn.settimeout(2.0)
                raw = conn.recv(4096).decode("utf-8").strip()
                msg = json.loads(raw) if raw else {}
                cmd = msg.get("cmd", "")
                if cmd == "show":
                    t0 = time.monotonic()
                    page = msg.get("page", "home")
                    if page not in _VALID_PAGES:
                        page = "home"
                    # The window may be hidden (closed by the user, or prewarmed
                    # and never shown yet) — unhide before raising, since
                    # _raise_self only finds visible windows.
                    try:
                        window.show()
                    except Exception:
                        pass
                    if ready.wait(timeout=10):
                        window.evaluate_js(f"navigateTo('{page}')")
                    _raise_self()
                    _perf(f"control show page={page} in "
                          f"{(time.monotonic() - t0) * 1000:.0f}ms")
                elif cmd == "quit":
                    # App shutdown. Let the close through this time, then leave
                    # the accept loop: the port goes with the process.
                    if quitting is not None:
                        quitting.set()
                    conn.sendall(b"ok\n")
                    threading.Thread(target=_destroy_window, args=(window,),
                                     daemon=True).start()
                    return
                elif cmd == "refresh":
                    what = str(msg.get("what", "history"))[:32].replace("'", "")
                    if ready.wait(timeout=10):
                        window.evaluate_js(f"onExternalRefresh('{what}')")
                elif cmd != "ping":
                    conn.sendall(b"err\n")
                    continue
                conn.sendall(b"ok\n")
            except Exception:
                try:
                    conn.sendall(b"err\n")
                except OSError:
                    pass


def _apply_titlebar_theme(dark: bool) -> None:
    """Match the Win11 titlebar to the app theme (DWMWA_USE_IMMERSIVE_DARK_MODE);
    otherwise a light app under a dark Windows theme keeps a dark titlebar."""
    try:
        import ctypes
        from ctypes import wintypes

        user32, dwmapi = ctypes.windll.user32, ctypes.windll.dwmapi
        pid = os.getpid()
        val = ctypes.c_int(1 if dark else 0)

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _enum(hwnd, _):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid and user32.IsWindowVisible(hwnd):
                dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(val),
                                             ctypes.sizeof(val))
            return True

        user32.EnumWindows(_enum, 0)
    except Exception:
        pass


# ── Python → JS API ───────────────────────────────────────────────────────────

class DashboardAPI:
    """All methods callable from JS as window.pywebview.api.method(args)."""

    def get_stats(self) -> dict:
        entries = hist.load()
        today_str = date.today().isoformat()
        today_words = total_words = today_recs = 0
        for e in entries:
            wc = len(e.get("text", "").split())
            total_words += wc
            if e.get("timestamp", "").startswith(today_str):
                today_words += wc
                today_recs += 1
        cfg = _read_cfg()
        return {
            "today_words": today_words,
            "total_words": total_words,
            "today_recordings": today_recs,
            "total_recordings": len(entries),
            "correction_count": len(cfg.get("corrections", {})),
            "vocab_count": len(cfg.get("custom_vocabulary", [])),
        }

    def get_home_stats(self) -> dict:
        """Home page (P3): words/day for the last 14 days, this-week total +
        delta vs the prior week, average pace, time reclaimed, dictation
        count, and the 3 most recent entries. All derived from history.json;
        wpm has no other source of truth (see _entry_audio_seconds)."""
        entries = hist.load()
        today = date.today()

        day_words = {(today - timedelta(days=13 - i)).isoformat(): 0 for i in range(14)}
        for e in entries:
            try:
                d = datetime.fromisoformat(e.get("timestamp", "")).date()
            except ValueError:
                continue
            key = d.isoformat()
            if key in day_words:
                day_words[key] += len(e.get("text", "").split())
        ordered_days = sorted(day_words)
        per_day = [day_words[k] for k in ordered_days]
        day_labels = [date.fromisoformat(k).strftime("%a")[0] for k in ordered_days]

        def _words_between(start_ago: int, end_ago: int) -> int:
            lo = today - timedelta(days=end_ago)
            hi = today - timedelta(days=start_ago)
            total = 0
            for e in entries:
                try:
                    d = datetime.fromisoformat(e.get("timestamp", "")).date()
                except ValueError:
                    continue
                if lo <= d <= hi:
                    total += len(e.get("text", "").split())
            return total

        words_this_week = _words_between(0, 6)
        words_prior_week = _words_between(7, 13)
        if words_prior_week > 0:
            delta_pct = round((words_this_week - words_prior_week) / words_prior_week * 100)
        else:
            delta_pct = 100 if words_this_week > 0 else 0

        wpms = []
        reclaimed_minutes = 0.0
        week_start = today - timedelta(days=6)
        for e in entries:
            try:
                d = datetime.fromisoformat(e.get("timestamp", "")).date()
            except ValueError:
                continue
            if d < week_start:
                continue
            words = len(e.get("text", "").split())
            if not words:
                continue
            dur = _entry_audio_seconds(e)
            if not dur:
                continue
            wpm = words / (dur / 60.0)
            if wpm <= 0:
                continue
            wpms.append(wpm)
            reclaimed_minutes += max(0.0, words / 40.0 - words / wpm)

        avg_wpm = round(sum(wpms) / len(wpms)) if wpms else None
        faster_than_typing = round(avg_wpm / 40.0, 1) if avg_wpm else None

        recent = []
        for e in entries[:3]:
            text = e.get("text", "")
            ts = e.get("timestamp", "")
            try:
                dt = datetime.fromisoformat(ts)
                label = dt.strftime("%I:%M %p").lstrip("0")
            except ValueError:
                label = ts
            recent.append({
                "text": text, "label": label,
                "words": len(text.split()),
                "source": e.get("source") or "",
            })

        return {
            "has_history": bool(entries),
            "words_this_week": words_this_week,
            "delta_pct": delta_pct,
            "avg_wpm": avg_wpm,
            "faster_than_typing": faster_than_typing,
            "reclaimed_minutes": round(reclaimed_minutes),
            "dictation_count": len(entries),
            "per_day": per_day,
            "day_labels": day_labels,
            "recent": recent,
        }

    def get_pause_plan(self) -> dict:
        """Voice page (P6): the pause-plan visual is real narration.py output
        on a fixed sample passage, not a faked shape - segment widths are the
        actual segment character counts and pause bars are the actual planned
        pause durations, at the user's own study speed/pause settings."""
        try:
            import narration
            cfg = _read_cfg()
            budget = int(cfg.get("tts_max_chunk_chars", 120) or 120)
            base_speed = float(cfg.get("study_speed", 0.95))
            pause_scale = float(cfg.get("study_pause_scale", 1.0))
            segments = narration.plan(_STUDY_SAMPLE, budget=budget, base_speed=base_speed,
                                       study=True, pause_scale=pause_scale)
            return {
                "ok": True,
                "sample": _STUDY_SAMPLE,
                "segments": [
                    {"text": text, "chars": len(text), "pause": round(pause, 2),
                     "speed": round(speed, 2)}
                    for text, pause, speed in segments
                ],
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def play_sample(self) -> dict:
        """Voice page Play sample button. Speaks _STUDY_SAMPLE through the
        main app's /speak endpoint (api_server.py, Agent B). Degrades to a
        toast rather than a crash: connection failure means the app isn't
        running, 403/404 means the endpoint exists but read-aloud is off."""
        result = _diag_api_post_json("/speak", {"text": _STUDY_SAMPLE})
        if result["ok"]:
            return {"ok": True}
        if result.get("http_error") and result.get("status") in (403, 404):
            return {"ok": False, "reason": "off"}
        return {"ok": False, "reason": "unreachable"}

    def get_network_probe(self) -> dict:
        """Diagnostics 'Network isolation' probe + the header seal's data
        source (Agent B's /diag/sockets on api_server.py). {"reachable":
        False} degrades to an honest unlit seal rather than a guess."""
        data = _diag_api_get("/diag/sockets")
        if data is None:
            return {"reachable": False}
        data["reachable"] = True
        return data

    def get_promotions(self) -> list:
        """Dictionary 'Learned this week' feed (Agent B's profile.recent_promotions,
        contract: [{"raw","fixed","count","promoted_at"}, ...]). Empty list,
        never a crash, if that function isn't there yet or the DB is empty."""
        try:
            import profile
            return profile.recent_promotions(days=7)
        except Exception:
            return []

    def get_backend_status(self) -> dict:
        """Live health for the Home status strip (BACKLOG item 48b). Both
        probes are plain HTTP GETs to localhost, safe to call from this
        subprocess even though whisper-server/LM Studio live in main.py's
        process — same checks health.py uses there."""
        try:
            import transcribe
            whisper_ok = transcribe.server_alive()
        except Exception:
            whisper_ok = False
        return {"whisper_ok": whisper_ok}

    def get_history(self, limit: int = 200) -> list:
        entries = hist.load()
        today = date.today()
        today_str = today.isoformat()
        result = []
        for i, e in enumerate(entries[:limit]):
            text = e.get("text", "")
            ts = e.get("timestamp", "")
            source = e.get("source", "")
            try:
                dt = datetime.fromisoformat(ts)
                ts_date = dt.date().isoformat()
                fmt = "%I:%M %p" if ts_date == today_str else "%d %b, %I:%M %p"
                label = dt.strftime(fmt).lstrip("0")
                day_label = _day_group_label(dt.date(), today)
            except Exception:
                label = ts
                ts_date = ""
                day_label = ""
            result.append({
                "index": i, "text": text, "ts": ts,
                "words": len(text.split()),
                "label": label, "ts_date": ts_date, "source": source,
                "day_label": day_label, "pinned": bool(e.get("pinned")),
                "has_audio": bool(e.get("audio")),
            })
        return result

    def get_history_stamp(self) -> str:
        """Cheap change token for history.json (mtime + size). The page polls
        this as the backstop for the push refresh, so a dictation still shows
        up if the notify never lands (main app restarted, port taken)."""
        try:
            st = os.stat(hist.HISTORY_FILE)
            return f"{st.st_mtime_ns}:{st.st_size}"
        except OSError:
            return "0:0"

    def delete_history_entry(self, idx: int, stamp: str = "") -> bool:
        try:
            with hist.transaction():
                entries = hist._load()
                real = hist.resolve_index(entries, int(idx), stamp)
                if real < 0:
                    return False
                entries.pop(real)
                hist._write(entries)
                return True
        except Exception:
            return False

    def delete_history_entries(self, indices: list, stamps: list | None = None) -> int:
        try:
            with hist.transaction():
                entries = hist._load()
                drop = set()
                for pos, i in enumerate(indices):
                    stamp = stamps[pos] if stamps and pos < len(stamps) else ""
                    real = hist.resolve_index(entries, int(i), stamp)
                    if real >= 0:
                        drop.add(real)
                keep = [e for i, e in enumerate(entries) if i not in drop]
                removed = len(entries) - len(keep)
                hist._write(keep)
                return removed
        except Exception:
            return 0

    def export_history_entries(self, indices: list, fmt: str = "md",
                               stamps: list | None = None) -> dict:
        """Item 39 — format is one of "md" / "txt" / "srt" / "vtt". Unknown
        values fall back to Markdown rather than erroring."""
        try:
            spec = _EXPORT_FORMATS.get(fmt, _EXPORT_FORMATS["md"])
            entries = hist.load()
            chosen = []
            for pos, i in enumerate(indices):
                stamp = stamps[pos] if stamps and pos < len(stamps) else ""
                real = hist.resolve_index(entries, int(i), stamp)
                if real >= 0:
                    chosen.append(entries[real])
            if not chosen:
                return {"ok": False, "error": "No entries to export."}
            chosen.sort(key=lambda e: e.get("timestamp", ""))
            content = spec["fn"](chosen)
            default_name = (f"quiett-history-"
                             f"{datetime.now().strftime('%Y%m%d-%H%M%S')}.{spec['ext']}")

            dialog_ok = True
            result = None
            try:
                win = webview.windows[0] if webview.windows else None
                if win is None:
                    raise RuntimeError("no window")
                result = win.create_file_dialog(
                    webview.FileDialog.SAVE, save_filename=default_name,
                    file_types=(spec["label"], "All files (*.*)"))
            except Exception:
                dialog_ok = False

            if not dialog_ok:
                downloads = os.path.join(os.path.expanduser("~"), "Downloads")
                os.makedirs(downloads, exist_ok=True)
                path = os.path.join(downloads, default_name)
            elif not result:
                return {"ok": False, "cancelled": True}
            else:
                path = result if isinstance(result, str) else result[0]

            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return {"ok": True, "path": path, "fallback": not dialog_ok}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def set_pinned(self, index: int, pinned: bool, stamp: str = "") -> bool:
        try:
            return hist.set_pinned(int(index), bool(pinned), stamp)
        except Exception:
            return False

    def clear_history(self, confirm: bool = False) -> bool:
        # Destructive: refuses without the explicit confirm flag so that no
        # argument-less call (tooling, introspection, a stray bridge call)
        # can ever wipe the history.
        if confirm is not True:
            return False
        try:
            hist.clear()
            return True
        except Exception:
            return False

    def _audio_path_for_index(self, index: int, stamp: str = "") -> "str | None":
        entries = hist.load()
        index = hist.resolve_index(entries, index, stamp)
        if index < 0:
            return None
        audio_name = entries[index].get("audio")
        if not audio_name:
            return None
        return os.path.join("recordings", audio_name)

    def play_history_audio(self, index: int, stamp: str = "") -> dict:
        """Item 38. winsound.PlaySound with SND_ASYNC replaces whatever it
        was already playing, so this naturally enforces "only one plays"."""
        try:
            path = self._audio_path_for_index(int(index), stamp)
            if path is None:
                return {"ok": False, "error": "No recording for this entry."}
            if not os.path.exists(path):
                return {"ok": False, "error": "Recording no longer on disk."}
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def stop_audio(self) -> dict:
        try:
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass
        return {"ok": True}

    def get_waveform(self, index: int, stamp: str = "") -> dict:
        """Item 88 — cached in-memory by filename so re-opening a row (or
        re-filtering History) doesn't re-decode the WAV every time."""
        try:
            path = self._audio_path_for_index(int(index), stamp)
            if path is None:
                return {"ok": False}
            if not os.path.exists(path):
                return {"ok": False, "error": "Recording no longer on disk."}
            key = os.path.basename(path)
            cached = _waveform_cache.get(key)
            if cached is not None:
                return {"ok": True, "data": cached}
            data_uri = _render_waveform_png(path)
            if data_uri is None:
                return {"ok": False}
            _waveform_cache[key] = data_uri
            return {"ok": True, "data": data_uri}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def get_dictionary(self) -> dict:
        cfg = _read_cfg()
        return {
            "corrections": cfg.get("corrections", {}),
            "vocabulary": cfg.get("custom_vocabulary", []),
            "learn_from_edits": cfg.get("learn_from_edits", True),
        }

    def set_correction(self, original: str, replacement: str) -> bool:
        original = original.strip()
        replacement = replacement.strip()
        if not original or not replacement:
            return False
        cfg = _read_cfg()
        corr = dict(cfg.get("corrections", {}))
        corr[original] = replacement
        return _merge_cfg({"corrections": corr})

    def remove_correction(self, original: str) -> bool:
        cfg = _read_cfg()
        corr = dict(cfg.get("corrections", {}))
        if original in corr:
            del corr[original]
            return _merge_cfg({"corrections": corr})
        return False

    def add_vocabulary(self, word: str) -> bool:
        word = word.strip()
        if not word:
            return False
        cfg = _read_cfg()
        vocab = list(cfg.get("custom_vocabulary", []))
        if word not in vocab:
            vocab.append(word)
            return _merge_cfg({"custom_vocabulary": vocab})
        return True

    def remove_vocabulary(self, word: str) -> bool:
        cfg = _read_cfg()
        vocab = list(cfg.get("custom_vocabulary", []))
        if word in vocab:
            vocab.remove(word)
            return _merge_cfg({"custom_vocabulary": vocab})
        return False

    def get_config(self) -> dict:
        return _read_cfg()

    def set_titlebar_dark(self, dark: bool) -> bool:
        _apply_titlebar_theme(bool(dark))
        return True

    def get_autostart(self) -> bool:
        try:
            r = subprocess.run(
                ["schtasks", "/Query", "/TN", _AUTOSTART_TASK],
                capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            return r.returncode == 0
        except Exception:
            return False

    def set_autostart(self, enabled: bool) -> bool:
        try:
            if not enabled:
                r = subprocess.run(
                    ["schtasks", "/Delete", "/F", "/TN", _AUTOSTART_TASK],
                    capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                return True
            vbs = os.path.join(_PROJECT_DIR, "launch.vbs")
            base = ["schtasks", "/Create", "/F", "/TN", _AUTOSTART_TASK,
                    "/SC", "ONLOGON", "/TR", f'wscript.exe "{vbs}"']
            # /RL HIGHEST keeps global hotkeys working (app runs elevated);
            # fall back to a normal task when we aren't elevated ourselves
            r = subprocess.run(base + ["/RL", "HIGHEST"], capture_output=True,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            if r.returncode != 0:
                r = subprocess.run(base, capture_output=True,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
            return r.returncode == 0
        except Exception:
            return False

    def save_settings(self, data: dict) -> bool:
        ok = _merge_cfg(data)
        if ok and ("history_max_entries" in data or "recording_retention_days" in data):
            try:
                hist.purge()
            except Exception:
                pass
        return ok

    def export_settings(self) -> dict:
        """Item 76: writes config.json (corrections/vocabulary already live in
        it) to a single JSON file, excluding dashboard_window since that's
        machine-specific window geometry, not a portable setting."""
        try:
            cfg = _read_cfg()
            cfg.pop("dashboard_window", None)
            content = json.dumps(cfg, indent=2, ensure_ascii=False)
            default_name = f"quiett-settings-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"

            dialog_ok = True
            result = None
            try:
                win = webview.windows[0] if webview.windows else None
                if win is None:
                    raise RuntimeError("no window")
                result = win.create_file_dialog(
                    webview.FileDialog.SAVE, save_filename=default_name,
                    file_types=("JSON file (*.json)",))
            except Exception:
                dialog_ok = False

            if not dialog_ok:
                downloads = os.path.join(os.path.expanduser("~"), "Downloads")
                os.makedirs(downloads, exist_ok=True)
                path = os.path.join(downloads, default_name)
            elif not result:
                return {"ok": False, "cancelled": True}
            else:
                path = result if isinstance(result, str) else result[0]

            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return {"ok": True, "path": path, "fallback": not dialog_ok}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def import_settings(self) -> dict:
        """Item 76: file dialog -> validate it's a JSON object with at least
        one known config key -> shallow-merge over current config. Never
        crashes on a malformed file; always returns a designed error."""
        try:
            win = webview.windows[0] if webview.windows else None
            if win is None:
                return {"ok": False, "error": "Dashboard window isn't ready yet."}
            result = win.create_file_dialog(
                webview.FileDialog.OPEN,
                file_types=("JSON file (*.json)", "All files (*.*)"))
            if not result:
                return {"ok": False, "cancelled": True}
            path = result if isinstance(result, str) else result[0]
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError:
            return {"ok": False, "error": "That file isn't valid JSON."}
        except Exception as exc:
            return {"ok": False, "error": f"Couldn't read that file: {exc}"}

        if not isinstance(data, dict):
            return {"ok": False, "error": "That file isn't a settings JSON object."}
        data.pop("dashboard_window", None)
        matched = [k for k in data if k in _KNOWN_CONFIG_KEYS]
        if not matched:
            return {"ok": False,
                    "error": "That file doesn't look like a Quiett settings export."}
        if not _merge_cfg(data):
            return {"ok": False, "error": "Couldn't save the imported settings."}
        return {"ok": True, "imported_keys": len(matched)}

    def get_audio_devices(self) -> list:
        try:
            import audio
            return [{"index": d["index"], "name": d["name"]}
                    for d in audio.list_input_devices()]
        except Exception:
            return []

    def get_about(self) -> dict:
        return {"version": VERSION, "changelog": _CHANGELOG}

    def get_diagnostics(self) -> dict:
        """Diagnostics page data. Routed through api_server.py in the main
        app's process, since audio/hotkey state lives there, not in this
        subprocess. {"reachable": False} if the main app isn't running."""
        data = _diag_api_get("/diagnostics")
        if data is None:
            return {"reachable": False}
        data["reachable"] = True
        return data

    def start_mic_probe(self) -> dict:
        result = _diag_api_post("/diagnostics/mic-probe")
        if not result["ok"]:
            if not result.get("http_error"):
                return {"reachable": False}
            return {"reachable": True, "error": result["body"].get("error")
                     or "Couldn't start the mic test."}
        body = result["body"]
        body["reachable"] = True
        return body

    def get_mic_level(self) -> dict:
        data = _diag_api_get("/diagnostics/mic-level")
        if data is None:
            return {"reachable": False}
        data["reachable"] = True
        return data




# ── Embedded HTML template ─────────────────────────────────────────────────────

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Quiett</title>
<style>
/* ── Quiett — ion glass identity. Tokens come from theme.py (single source
   of truth shared with the Tk panel/badge and the tray); this file only
   consumes them via css_vars(). ── */
:root {
__ROOT_VARS_DARK__
  --r:14px;
}
[data-theme=light] {
__ROOT_VARS_LIGHT__
}

*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100vh;overflow:hidden}
/* Motion policy (BACKLOG item 47) — WebView2/Edge maps Windows' own "Show
   animations" accessibility setting straight to prefers-reduced-motion, so
   this one rule honours it for every transition/keyframe in this file. */
@media (prefers-reduced-motion: reduce) {
  *{transition:none!important;animation:none!important}
}
body{
  font-family:var(--font);color:var(--text);font-size:13px;line-height:1.5;
  background:__AURORA_DARK__, var(--void);
  -webkit-font-smoothing:antialiased;
}
[data-theme=light] body{ background:__AURORA_LIGHT__, var(--void); }
/* film grain */
body::after{
  content:"";position:fixed;inset:0;pointer-events:none;opacity:.3;mix-blend-mode:overlay;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2'/%3E%3CfeColorMatrix values='0 0 0 0 1 0 0 0 0 1 0 0 0 0 1 0 0 0 0.05 0'/%3E%3C/filter%3E%3Crect width='160' height='160' filter='url(%23n)'/%3E%3C/svg%3E");
}
button{font:inherit;color:inherit;background:none;border:none;cursor:pointer}
input,select,textarea{font:inherit;color:inherit}
::selection{background:rgba(125,232,255,.28)}
:focus{outline:none}
:focus-visible{outline:1px solid var(--ion);outline-offset:2px;border-radius:4px}
::-webkit-scrollbar{width:8px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--hair2);border-radius:99px}

.app{display:flex;flex-direction:column;height:100vh}

/* ── Titlebar / brand row (native window chrome stays — this is just the
   brand strip inside the page) ── */
.titlebar{display:flex;align-items:center;gap:12px;height:46px;padding:0 18px;flex:none;position:relative}
.titlebar::after{content:"";position:absolute;left:18px;right:18px;bottom:0;height:1px;background:linear-gradient(90deg,transparent,var(--hair2) 20%,var(--hair2) 80%,transparent)}
.brand{display:flex;align-items:center;gap:9px}
.brand b{font-weight:500;font-size:13px;letter-spacing:.14em;text-transform:uppercase}
.brand .dim2{color:var(--dim);font-weight:400}
.ver{font-family:var(--mono);font-size:9px;letter-spacing:.12em;color:var(--dim);padding-left:10px}
.tb-sp{flex:1}
.seal{
  display:inline-flex;align-items:center;gap:8px;font-family:var(--mono);font-size:9px;letter-spacing:.16em;color:var(--mid);
  border:1px solid var(--hair);border-radius:99px;padding:5px 13px;
  background:linear-gradient(180deg,rgba(255,255,255,.03),transparent);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.05);
}
.seal i{width:5px;height:5px;border-radius:50%;background:var(--dim);display:inline-block}
.seal.lit i{background:var(--ion);box-shadow:0 0 8px var(--ion)}

.body{display:flex;flex:1;min-height:0}

/* ── Rail — no box, just light ── */
.rail{width:204px;flex:none;padding:16px 10px 14px 12px;display:flex;flex-direction:column;gap:1px;overflow-y:auto;position:relative}
.rail::after{content:"";position:absolute;top:20px;bottom:20px;right:0;width:1px;background:linear-gradient(180deg,transparent,var(--hair) 25%,var(--hair) 75%,transparent)}
.rail-h{font-size:9px;font-weight:600;letter-spacing:.2em;color:var(--dim);padding:14px 10px 6px}
.rail-h:first-child{padding-top:0}
.nav{display:flex;align-items:center;gap:10px;width:100%;padding:8px 10px;border-radius:9px;color:var(--mid);font-weight:400;font-size:12.5px;text-align:left;position:relative;transition:color .15s}
.nav svg{flex:none;opacity:.7}
.nav:hover{color:var(--text);background:var(--glass)}
.nav.active{color:var(--text);font-weight:500}
.nav.active svg{opacity:1;color:var(--ion);filter:drop-shadow(0 0 6px rgba(125,232,255,.6))}
.nav.active::before{content:"";position:absolute;left:-2px;top:50%;transform:translateY(-50%);width:2px;height:16px;border-radius:2px;background:var(--ion);box-shadow:0 0 10px var(--ion)}
.rail-foot{margin-top:auto;padding:12px 10px 0}
.rail-foot p{font-size:10.5px;color:var(--dim);line-height:1.8}
.rail-foot .about-link{margin-top:8px;display:block;font-family:var(--mono);font-size:9px;letter-spacing:.1em;color:var(--dim)}
.rail-foot .about-link:hover{color:var(--mid)}
.key{display:inline-block;font-family:var(--mono);font-size:10px;font-weight:500;color:var(--text);background:linear-gradient(180deg,rgba(255,255,255,.09),rgba(255,255,255,.03));border:1px solid var(--hair2);border-radius:5px;padding:1px 7px;vertical-align:1px;box-shadow:0 2px 0 rgba(0,0,0,.35)}
[data-theme=light] .key{box-shadow:0 2px 0 rgba(0,0,0,.08)}
.plus{color:var(--dim)}

/* ── Main ── */
.main{flex:1;min-width:0;display:flex;flex-direction:column}
.page{flex:1;min-height:0;overflow-y:auto;padding:26px 32px 32px;display:none}
.page.active{display:block;animation:fadeIn .15s ease}
@keyframes fadeIn{from{opacity:0;transform:translateY(3px)}to{opacity:1;transform:translateY(0)}}
.ph{margin-bottom:22px;display:flex;align-items:flex-start;justify-content:space-between;gap:14px}
.eyebrow{font-family:var(--mono);font-size:9.5px;letter-spacing:.22em;color:var(--ion);margin-bottom:9px;display:flex;align-items:center;gap:10px}
.eyebrow.neutral{color:var(--dim)}
.ph h1{font-family:var(--font-d);font-size:24px;font-weight:300;letter-spacing:-.01em}
.ph p{color:var(--mid);font-size:12.5px;margin-top:5px;max-width:560px}
.ph-actions{display:flex;gap:8px;align-items:center;flex:none}

/* ── The voice line — status strip along the bottom ── */
.voiceline{flex:none;display:flex;align-items:center;gap:14px;height:34px;padding:0 20px;position:relative;font-family:var(--mono);font-size:9px;letter-spacing:.14em;color:var(--dim)}
.voiceline::before{content:"";position:absolute;left:18px;right:18px;top:0;height:1px;background:linear-gradient(90deg,transparent,var(--hair2) 20%,var(--hair2) 80%,transparent)}
.vl-wave{width:60px;height:14px;flex:none}
.vl-wave polyline{fill:none;stroke:var(--ion);stroke-width:1.4;stroke-linecap:round;filter:drop-shadow(0 0 4px rgba(125,232,255,.7))}
.vl-sp{flex:1}
.vl-keys{letter-spacing:.05em}

/* ── Planes replace cards: light from above, no full border ── */
.plane{
  background:linear-gradient(180deg,rgba(255,255,255,.035),rgba(255,255,255,.012));
  border-radius:var(--r-card);padding:20px 22px;position:relative;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.08),0 20px 40px -24px rgba(0,0,0,.5);
}
[data-theme=light] .plane{box-shadow:inset 0 1px 0 rgba(255,255,255,.6),0 12px 30px -20px rgba(20,40,70,.15)}
.plane h3{font-size:13px;font-weight:500;letter-spacing:.01em}
.sub{font-size:11.5px;color:var(--mid)}
.row{display:flex;align-items:center;gap:12px}
.sp{flex:1}
.grid{display:grid;gap:16px}

/* ── Buttons ── */
.btn{
  display:inline-flex;align-items:center;gap:8px;font-weight:500;font-size:12px;letter-spacing:.02em;
  padding:8px 16px;border-radius:99px;border:1px solid var(--hair2);color:var(--text);
  background:linear-gradient(180deg,rgba(255,255,255,.06),rgba(255,255,255,.02));
  transition:all .16s ease;
}
[data-theme=light] .btn{background:linear-gradient(180deg,rgba(255,255,255,.9),rgba(255,255,255,.6))}
.btn:hover{border-color:rgba(125,232,255,.4);box-shadow:0 0 26px -8px rgba(125,232,255,.5)}
.btn:active{transform:scale(.98)}
.btn.primary{border-color:rgba(125,232,255,.45);background:linear-gradient(180deg,rgba(125,232,255,.14),rgba(63,140,255,.06));box-shadow:0 0 30px -10px rgba(125,232,255,.5),inset 0 1px 0 rgba(255,255,255,.14)}
.btn.ghost{border-color:transparent;background:transparent;color:var(--mid);padding:7px 11px}
.btn.ghost:hover{color:var(--text);box-shadow:none;background:var(--glass)}
.btn.danger{border-color:rgba(255,107,94,.3);color:var(--rec)}
.btn.danger:hover{box-shadow:0 0 26px -8px rgba(255,107,94,.5);border-color:rgba(255,107,94,.5)}
.btn:disabled{opacity:.45;cursor:not-allowed;pointer-events:none}
.tri{color:var(--ion);font-size:9px}

/* ── Toggle — hairline capsule ── */
.tog{position:relative;width:38px;height:21px;border-radius:99px;background:rgba(255,255,255,.05);border:1px solid var(--hair2);flex:none;transition:all .18s ease;cursor:pointer}
[data-theme=light] .tog{background:rgba(0,0,0,.05)}
.tog::after{content:"";position:absolute;top:2px;left:2px;width:15px;height:15px;border-radius:50%;background:var(--dim);transition:all .18s ease}
.tog.on{border-color:rgba(125,232,255,.5);background:rgba(125,232,255,.10);box-shadow:0 0 16px -4px rgba(125,232,255,.5)}
.tog.on::after{left:19px;background:var(--ion);box-shadow:0 0 8px var(--ion)}

/* ── Segmented ── */
.seg{display:inline-flex;border:1px solid var(--hair);border-radius:99px;padding:3px;gap:2px;background:rgba(0,0,0,.2)}
[data-theme=light] .seg{background:rgba(0,0,0,.04)}
.seg button{font-size:11px;font-weight:500;color:var(--dim);padding:5px 12px;border-radius:99px;letter-spacing:.02em}
.seg button.on{background:linear-gradient(180deg,rgba(125,232,255,.16),rgba(63,140,255,.07));color:var(--text);box-shadow:inset 0 1px 0 rgba(255,255,255,.1)}

/* ── Slider ── */
.slider{-webkit-appearance:none;appearance:none;width:100%;height:2px;border-radius:99px;background:rgba(255,255,255,.1);outline-offset:8px}
[data-theme=light] .slider{background:rgba(0,0,0,.1)}
.slider::-webkit-slider-thumb{-webkit-appearance:none;width:14px;height:14px;border-radius:50%;background:var(--ion);box-shadow:0 0 10px rgba(125,232,255,.8);cursor:grab;border:none}

/* ── Inputs ── */
.input,select.input,textarea.input{background:rgba(0,0,0,.25);border:1px solid var(--hair);border-radius:10px;padding:8px 13px;font-size:12.5px;color:var(--text);width:100%}
[data-theme=light] .input,[data-theme=light] select.input,[data-theme=light] textarea.input{background:rgba(0,0,0,.03)}
.input::placeholder{color:var(--dim)}
.input:focus{outline:none;border-color:rgba(125,232,255,.45);box-shadow:0 0 0 3px rgba(125,232,255,.08)}
select.input option{background:#0B0F17;color:var(--text)}
textarea.input{resize:vertical;min-height:56px;font-family:var(--font)}
.n-in{width:80px;text-align:right}
.t-in{width:220px}
.t-in.wide{width:100%}

/* ── Setting rows ── */
.srow{display:flex;align-items:center;gap:16px;padding:14px 0}
.srow.col{flex-direction:column;align-items:flex-start;gap:8px}
.srow + .srow{border-top:1px solid var(--hair)}
.srow .lbl{font-size:12.5px;font-weight:500}
.srow .desc{font-size:11px;color:var(--dim);margin-top:2px;max-width:440px}
.range-row{display:flex;align-items:center;gap:10px;width:260px}
.range-val{font-size:11px;color:var(--mid);width:46px;text-align:right;flex:none;font-family:var(--mono)}
.s-sec-ttl{display:flex;align-items:center;justify-content:space-between;font-family:var(--mono);font-size:9px;font-weight:600;letter-spacing:.2em;color:var(--dim);padding-bottom:8px}
.sec-reset-btn{font-family:var(--font);font-size:10.5px;font-weight:500;letter-spacing:normal;text-transform:none;color:var(--ion);background:none;border:none;cursor:pointer;padding:2px 6px;border-radius:4px}
.sec-reset-btn:hover{background:var(--ion-soft)}
.save-ok{font-size:11.5px;color:var(--ion);opacity:0;transition:opacity .3s}
.save-ok.show{opacity:1}

/* ── Chips / mono / tables / notices ── */
.chip{display:inline-flex;align-items:center;gap:7px;font-size:11.5px;border:1px solid var(--hair);border-radius:99px;padding:5px 12px;color:var(--mid)}
.mono{font-family:var(--mono);font-size:11px}
.arrow{color:var(--ion);opacity:.7}
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;font-family:var(--mono);font-size:8.5px;font-weight:500;letter-spacing:.18em;color:var(--dim);padding:0 8px 9px}
td{padding:9px 8px;border-top:1px solid var(--hair);color:var(--mid)}
td:first-child{color:var(--text)}
.notice{display:flex;gap:11px;align-items:flex-start;border:1px solid var(--hair);border-radius:12px;padding:12px 15px;font-size:11.5px;color:var(--mid);background:rgba(0,0,0,.15)}
[data-theme=light] .notice{background:rgba(0,0,0,.03)}
.notice b{color:var(--text);font-weight:500}
.notice .tick{color:var(--ion)}
.keys{display:flex;align-items:center;gap:8px}

/* ── Home ── */
.hero{display:flex;align-items:flex-end;gap:32px;margin:4px 0 20px}
.hero .big{font-family:var(--font-d);font-size:58px;font-weight:200;letter-spacing:-.03em;line-height:.95;background:linear-gradient(180deg,var(--text) 30%,rgba(140,180,230,.6));-webkit-background-clip:text;background-clip:text;color:transparent}
[data-theme=light] .hero .big{background:linear-gradient(180deg,var(--text) 30%,rgba(20,60,110,.55));-webkit-background-clip:text;background-clip:text}
.hero .cap{font-family:var(--mono);font-size:9px;letter-spacing:.24em;color:var(--dim);margin-bottom:9px}
.hero .delta{font-size:11.5px;color:var(--ion);margin-top:7px;letter-spacing:.04em}
.hero .delta.flat{color:var(--dim)}
.metric-row{display:flex;gap:0;margin-bottom:24px;flex-wrap:wrap}
.metric{padding:0 28px;border-left:1px solid var(--hair)}
.metric:first-child{padding-left:0;border-left:none}
.metric .v{font-family:var(--font-d);font-size:21px;font-weight:300;letter-spacing:-.01em}
.metric .v small{font-size:11px;color:var(--mid);font-weight:400;margin-left:2px}
.metric .k{font-family:var(--mono);font-size:8.5px;letter-spacing:.2em;color:var(--dim);margin-top:3px}
.rhythm{display:flex;align-items:flex-end;height:48px;margin:0 0 7px}
.rhythm .rcell{flex:1;display:flex;align-items:flex-end;justify-content:center;height:100%}
.rhythm i{width:4px;border-radius:99px;background:linear-gradient(180deg,rgba(125,232,255,.9),rgba(63,140,255,.28));min-height:5px;opacity:.5;display:block}
.rhythm i.hi{opacity:1;width:5px;box-shadow:0 0 12px rgba(125,232,255,.6)}
.rhythm-lbl{display:flex;font-family:var(--mono);font-size:8px;letter-spacing:.1em;color:var(--dim);margin-bottom:24px}
.rhythm-lbl span{flex:1;text-align:center}
.h-cols{grid-template-columns:1.55fr 1fr}
.h-cols.single{grid-template-columns:1fr;max-width:560px}
.recent .item{display:flex;gap:13px;align-items:flex-start;padding:12px 0}
.recent .item + .item{border-top:1px solid var(--hair)}
.glyph{flex:none;width:28px;height:28px;border-radius:9px;display:grid;place-items:center;font-family:var(--mono);font-size:8.5px;color:var(--mid);border:1px solid var(--hair);background:linear-gradient(180deg,rgba(255,255,255,.04),transparent)}
.recent .txt{font-size:12px;color:var(--text);opacity:.92;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.recent .meta{font-family:var(--mono);font-size:9px;letter-spacing:.06em;color:var(--dim);margin-top:4px}
.edu{display:flex;flex-direction:column;gap:9px}
.edu.hero-edu h3{font-family:var(--font-d);font-size:21px;font-weight:300;letter-spacing:-.01em}
.edu .keys{margin:7px 0 2px}
.edu .key{font-size:12px;padding:5px 12px}

/* ── Voice ── */
.voice-hero{display:flex;gap:18px;align-items:center;flex-wrap:wrap}
.orb{flex:none;width:56px;height:56px;border-radius:50%;position:relative;
  background:radial-gradient(circle at 34% 30%,rgba(125,232,255,.5),rgba(63,140,255,.16) 55%,transparent 75%);
  box-shadow:0 0 34px -6px rgba(125,232,255,.4),inset 0 0 18px rgba(125,232,255,.18);
  border:1px solid rgba(125,232,255,.3)}
.orb::after{content:"";position:absolute;inset:11px;border-radius:50%;border:1px solid rgba(125,232,255,.25);animation:orb 3.2s ease-in-out infinite}
@keyframes orb{0%,100%{transform:scale(.92);opacity:.5}50%{transform:scale(1.05);opacity:1}}
.pauseplan{display:flex;align-items:center;gap:0;height:48px;margin-top:14px;overflow-x:auto}
.pp-seg{height:3px;border-radius:99px;background:linear-gradient(90deg,rgba(125,232,255,.9),rgba(63,140,255,.7));position:relative;transition:all .3s;min-width:8px}
.pp-seg.played{box-shadow:0 0 14px rgba(125,232,255,.8);height:5px}
.pp-gap{flex:none;display:flex;align-items:center;justify-content:center}
.pp-gap i{width:3px;border-radius:99px;background:var(--pause);opacity:.85;box-shadow:0 0 8px rgba(255,184,107,.45);display:block}
.legend{display:flex;gap:20px;font-family:var(--mono);font-size:8.5px;letter-spacing:.12em;color:var(--dim);margin-top:12px}
.legend i{display:inline-block;width:14px;height:3px;border-radius:99px;margin-right:6px;vertical-align:2px}

/* ── History ── */
.search-wrap{position:relative}
.search-icon{position:absolute;left:13px;top:50%;transform:translateY(-50%);width:13px;height:13px;color:var(--dim);pointer-events:none}
.search-in{padding-left:34px!important}
.bulk-bar{display:flex;align-items:center;gap:14px;padding:9px 0;font-size:11.5px;color:var(--mid)}
.bulk-all{display:flex;align-items:center;gap:6px;cursor:pointer}
.bulk-all input{accent-color:var(--ion);cursor:pointer}
.bulk-acts{margin-left:auto;display:flex;gap:8px;align-items:center}
.dayh{font-family:var(--mono);font-size:9px;font-weight:500;letter-spacing:.22em;color:var(--dim);padding:18px 4px 7px}
.dayh:first-child{padding-top:2px}
.hrow{display:flex;align-items:center;gap:12px;padding:10px 12px;border-radius:11px}
.hrow:hover,.hrow:focus-within{background:var(--glass)}
.hrow.pinned{background:var(--ion-soft)}
.hrow .txt{flex:1;min-width:0;font-size:12px;color:var(--text);opacity:.9;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hrow .t{font-family:var(--mono);font-size:9px;letter-spacing:.05em;color:var(--dim);flex:none}
.hrow .act{display:flex;gap:2px;opacity:0;transition:opacity .12s;flex:none}
.hrow:hover .act,.hrow:focus-within .act{opacity:1}
.wave-thumb{width:50px;height:18px;background-color:rgba(255,255,255,.04);background-repeat:no-repeat;background-position:center;background-size:contain;border-radius:3px;flex:none}
[data-theme=light] .wave-thumb{background-color:rgba(0,0,0,.05)}
.iby{width:26px;height:26px;display:grid;place-items:center;border-radius:7px;color:var(--mid);font-size:11px;flex:none}
.iby:hover{background:var(--glass2);color:var(--text)}
.iby.pin.on{color:var(--pause);opacity:1}
.iby.on{color:var(--ion)}
.iby svg{width:12px;height:12px}
#historyList .li-check{display:none;flex:none;accent-color:var(--ion);cursor:pointer}
#historyList.select-mode .li-check{display:block}
#historyList.select-mode .act{display:none!important}
mark{background:var(--ion-soft);color:inherit;border-radius:2px;padding:0 1px}
.empty{text-align:center;padding:40px 0;color:var(--dim);font-size:12px}
.empty-state{display:flex;flex-direction:column;align-items:center;gap:11px;padding:56px 0;color:var(--mid);text-align:center}
.empty-state .es-icon{opacity:.4;display:flex;justify-content:center}
.empty-state .es-icon svg{width:30px;height:30px}
.empty-state .es-title{font-size:13px;color:var(--text);font-weight:500}
.empty-state .es-sub{font-size:11.5px;color:var(--dim);max-width:300px}

/* ── Dictionary ── */
.vchip{display:inline-flex;align-items:center;gap:6px;font-size:11px;border:1px solid var(--hair);border-radius:99px;padding:4px 6px 4px 11px;color:var(--text)}
.vchip .x{color:var(--dim);cursor:pointer}
.vchip .x:hover{color:var(--rec)}
.corr-row{display:flex;align-items:center;gap:8px;padding:8px 0}
.corr-row + .corr-row{border-top:1px solid var(--hair)}

/* ── Diagnostics ── */
.probe{display:flex;align-items:center;gap:13px;padding:11px 0;font-size:12px}
.probe + .probe{border-top:1px solid var(--hair)}
.probe .st{flex:none;width:14px;text-align:center;color:var(--ion);font-size:11px;text-shadow:0 0 8px rgba(125,232,255,.7)}
.probe .st.bad{color:var(--rec);text-shadow:none}
.probe .st.warm{color:var(--pause);text-shadow:none}
.probe .val{margin-left:auto;font-family:var(--mono);font-size:10px;letter-spacing:.03em;color:var(--mid);text-align:right}
.sock-well{font-family:var(--mono);font-size:10.5px;color:var(--mid);background:rgba(0,0,0,.25);border:1px solid var(--hair);border-radius:10px;padding:11px 13px;max-height:140px;overflow-y:auto}
[data-theme=light] .sock-well{background:rgba(0,0,0,.03)}
.sock-well div{padding:2px 0}
.sock-well .sock-app{color:var(--text)}
.mic-meter{position:relative;height:8px;border-radius:5px;background:rgba(255,255,255,.06);border:1px solid var(--hair);overflow:visible;margin:10px 0 12px}
[data-theme=light] .mic-meter{background:rgba(0,0,0,.05)}
.mic-meter-fill{position:absolute;left:0;top:0;bottom:0;width:0%;border-radius:5px;background:var(--ion);box-shadow:0 0 8px rgba(125,232,255,.6);transition:width .1s linear}
.mic-meter-peak{position:absolute;top:-3px;bottom:-3px;width:2px;background:var(--pause);left:0%;display:none}

/* ── Toast ── */
.dash-toast{position:fixed;bottom:16px;left:50%;transform:translateX(-50%) translateY(8px);background:rgba(8,11,18,.92);border:1px solid var(--hair2);border-radius:99px;padding:9px 20px;font-size:12px;color:var(--text);box-shadow:0 20px 50px -12px rgba(0,0,0,.8);opacity:0;pointer-events:none;transition:all .22s ease;z-index:80}
[data-theme=light] .dash-toast{background:rgba(255,255,255,.96)}
.dash-toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
</style>
</head>
<body>
<div class="app">

  <!-- ── Titlebar / brand row (native window chrome, no fake buttons) ── -->
  <div class="titlebar">
    <span class="brand">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
        <defs><linearGradient id="lg" x1="4" y1="2" x2="20" y2="22"><stop stop-color="#7DE8FF"/><stop offset="1" stop-color="#3F8CFF"/></linearGradient></defs>
        <rect x="8.4" y="2.6" width="7.2" height="11.4" rx="3.6" fill="url(#lg)"/>
        <path d="M5.6 10.4a6.4 6.4 0 0 0 12.8 0" stroke="url(#lg)" stroke-width="1.7" stroke-linecap="round" fill="none"/>
        <path d="M12 17v4.4M9.6 21.4h4.8" stroke="url(#lg)" stroke-width="1.7" stroke-linecap="round"/>
      </svg>
      <b>QUIE<span class="dim2">TT</span></b>
      <span class="ver" id="verTag">0.1.0</span>
    </span>
    <span class="tb-sp"></span>
    <span class="seal" id="seal" title="Checking network isolation…"><i></i>LOCAL ONLY</span>
  </div>

  <div class="body">
    <!-- ── Rail ── -->
    <nav class="rail">
      <div class="rail-h">DICTATE</div>
      <button class="nav active" data-page="home" onclick="navigateTo('home')">
        <svg width="15" height="15" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M3 10.2 10 4l7 6.2V17h-4.6v-4.4H7.6V17H3v-6.8Z"/></svg>Home</button>
      <button class="nav" data-page="dictation" onclick="navigateTo('dictation')">
        <svg width="15" height="15" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.4"><rect x="7.2" y="2.5" width="5.6" height="9" rx="2.8"/><path d="M4.5 9.5a5.5 5.5 0 0 0 11 0M10 15v2.5"/></svg>Dictation</button>
      <button class="nav" data-page="voice" onclick="navigateTo('voice')">
        <svg width="15" height="15" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M3.5 8v4M6.75 5.5v9M10 3.5v13M13.25 6.5v7M16.5 8.5v3"/></svg>Voice</button>
      <button class="nav" data-page="dictionary" onclick="navigateTo('dictionary')">
        <svg width="15" height="15" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M4 3.5h9.5A2.5 2.5 0 0 1 16 6v10.5H6.5A2.5 2.5 0 0 1 4 14V3.5Z"/><path d="M4 13.5A2.5 2.5 0 0 1 6.5 11H16"/></svg>Dictionary</button>
      <button class="nav" data-page="history" onclick="navigateTo('history')">
        <svg width="15" height="15" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.4"><circle cx="10" cy="10" r="7"/><path d="M10 6v4.2l2.8 1.7"/></svg>History</button>
      <div class="rail-h">SYSTEM</div>
      <button class="nav" data-page="diagnostics" onclick="navigateTo('diagnostics')">
        <svg width="15" height="15" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M2.5 10h3l2-5 3.5 10 2-5h4.5"/></svg>Diagnostics</button>
      <button class="nav" data-page="settings" onclick="navigateTo('settings')">
        <svg width="15" height="15" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.4"><circle cx="10" cy="10" r="2.6"/><path d="M10 3.5v2M10 14.5v2M3.5 10h2M14.5 10h2M5.4 5.4l1.5 1.5M13.1 13.1l1.5 1.5M5.4 14.6l1.5-1.5M13.1 6.9l1.5-1.5"/></svg>Settings</button>
      <div class="rail-foot">
        <p>Hold <span class="key">Ctrl</span> <span class="plus">+</span> <span class="key">Alt</span> anywhere.<br>Release, and the words land where your cursor is.</p>
        <a class="about-link" href="#" onclick="navigateTo('about');return false" role="button">About Quiett</a>
      </div>
    </nav>

    <!-- ── Main ── -->
    <div class="main">

      <!-- Home -->
      <section class="page active" id="page-home">
        <div class="eyebrow" id="homeEyebrow">THIS WEEK</div>
        <div id="homeHero"></div>
        <div class="metric-row" id="homeMetrics"></div>
        <div class="rhythm" id="rhythm" aria-label="Words per day, last 14 days"></div>
        <div class="rhythm-lbl" id="rhythmLbl"></div>
        <div class="grid h-cols" id="homeGrid">
          <div class="plane recent">
            <div class="row" style="margin-bottom:2px"><h3>Recent</h3><span class="sp"></span><button class="btn ghost" onclick="navigateTo('history')">View all</button></div>
            <div id="homeRecent"></div>
          </div>
          <div class="plane edu" id="homeEdu">
            <h3>Dictate anywhere</h3>
            <p class="sub">No app to open. No window to switch to.</p>
            <div class="keys"><span class="key">Ctrl</span><span class="plus">+</span><span class="key">Alt</span><span style="color:var(--dim);font-size:11px;letter-spacing:.04em">&nbsp;hold · speak · release</span></div>
            <p class="sub">Hear anything back in your own cloned voice with <span class="key">Ctrl</span><span class="plus">+</span><span class="key">Shift</span><span class="plus">+</span><span class="key">S</span></p>
            <div style="margin-top:auto;padding-top:12px"><button class="btn primary" onclick="navigateTo('history')">Open History</button></div>
          </div>
        </div>
      </section>

      <!-- Dictation -->
      <section class="page" id="page-dictation">
        <div class="ph"><div><div class="eyebrow neutral">CAPTURE</div><h1>Dictation</h1><p>The hotkey, the microphone, and what happens on release.</p></div></div>
        <div class="plane" style="margin-bottom:16px">
          <div class="srow" style="padding-top:2px">
            <div><div class="lbl">Push-to-talk hotkey</div><div class="desc">Hold to record, release to transcribe.</div></div>
            <span class="sp"></span>
            <span class="keys"><span class="key">Ctrl</span><span class="plus">+</span><span class="key">Alt</span></span>
          </div>
          <div class="srow">
            <div><div class="lbl">Microphone</div><div class="desc">Which input device Quiett records from.</div></div>
            <span class="sp"></span>
            <select class="input" style="max-width:260px" data-key="input_device" id="micSelect">
              <option value="">System default</option>
            </select>
          </div>
          <div class="srow">
            <div><div class="lbl">Silence auto-stop</div><div class="desc">Stops recording after this much quiet. 0 = never auto-stop.</div></div>
            <span class="sp"></span>
            <div class="range-row"><input type="range" class="slider" id="silenceAutoStopRange" data-key="silence_auto_stop_seconds" min="0" max="10" step="0.5" oninput="document.getElementById('silenceAutoStopLbl').textContent=silenceAutoStopLabel(this.value)"><span class="range-val" id="silenceAutoStopLbl">3.0s</span></div>
          </div>
        </div>
        <div class="plane">
          <div class="srow" style="padding-top:2px">
            <div><div class="lbl">Clipboard-only mode</div><div class="desc">Copy to clipboard instead of auto-pasting.</div></div>
            <span class="sp"></span>
            <div class="tog" role="switch" aria-checked="false" data-key="_paste_clipboard_only" aria-label="Clipboard-only mode" tabindex="0" onclick="togClick(this)" onkeydown="togKeydown(event,this)"></div>
          </div>
          <div class="srow">
            <div><div class="lbl">Incognito mode</div><div class="desc">No history, no audio kept, nothing written to disk.</div></div>
            <span class="sp"></span>
            <div class="tog" role="switch" aria-checked="false" data-key="incognito" aria-label="Incognito mode" tabindex="0" onclick="togClick(this)" onkeydown="togKeydown(event,this)"></div>
          </div>
          <div class="srow col">
            <div><div class="lbl">Redaction patterns</div><div class="desc">One regular expression per line. Matches are replaced with ▊▊▊ before an entry is stored, this only affects saved history, never the pasted text.</div></div>
            <textarea class="input" data-key="redact_patterns" data-type="lines" rows="3" placeholder="\\d{3}-\\d{2}-\\d{4}"></textarea>
          </div>
        </div>
      </section>

      <!-- Voice -->
      <section class="page" id="page-voice">
        <div class="ph"><div><div class="eyebrow neutral">READ ALOUD</div><h1>Voice</h1><p>Select text anywhere, press <span class="key">Ctrl</span> <span class="key">Shift</span> <span class="key">S</span>, and hear it in your own voice.</p></div></div>
        <div class="plane" style="margin-bottom:16px">
          <div class="voice-hero" style="margin-bottom:14px">
            <div class="orb" aria-hidden="true"></div>
            <div style="flex:1;min-width:220px">
              <h3>Your voice, cloned locally</h3>
              <p class="sub" style="margin-top:4px">Synthesised on your own GPU. The model never leaves this machine.</p>
            </div>
            <button class="btn" id="playSampleBtn" onclick="playSample()"><span class="tri">▶</span>Play sample</button>
          </div>
          <div class="srow" style="border-top:1px solid var(--hair);padding-top:14px">
            <div><div class="lbl">Enable read-aloud</div><div class="desc">Ctrl+Shift+S speaks the highlighted text. The voice server starts on first use and unloads when idle.</div></div>
            <span class="sp"></span>
            <div class="tog" role="switch" aria-checked="false" data-key="tts_enabled" aria-label="Enable read-aloud" tabindex="0" onclick="togClick(this)" onkeydown="togKeydown(event,this)"></div>
          </div>
        </div>
        <div class="grid" style="grid-template-columns:1fr 1fr;margin-bottom:16px">
          <div class="plane">
            <div class="lbl" style="font-size:12.5px;font-weight:500">Reading speed</div>
            <div class="desc" style="font-size:11px;color:var(--dim);margin:2px 0 13px">Pitch stays yours at every speed.</div>
            <div class="range-row" style="width:100%"><input type="range" class="slider" id="ttsSpeedRange" data-key="tts_speed" min="0.5" max="2" step="0.05" oninput="document.getElementById('ttsSpeedLbl').textContent=Number(this.value).toFixed(2)+'x'"><span class="range-val" id="ttsSpeedLbl">1.00x</span></div>
          </div>
          <div class="plane">
            <div class="row">
              <div><div class="lbl" style="font-size:12.5px;font-weight:500">Study Mode</div><div class="desc" style="font-size:11px;color:var(--dim);margin-top:2px">Slower, teacher-style delivery that breathes at sentences, lists and headings.</div></div>
              <span class="sp"></span>
              <div class="tog" role="switch" aria-checked="false" data-key="study_mode" aria-label="Study mode" tabindex="0" onclick="togClick(this)" onkeydown="togKeydown(event,this)"></div>
            </div>
          </div>
        </div>
        <div class="grid" style="grid-template-columns:1fr 1fr;margin-bottom:16px">
          <div class="plane">
            <div class="lbl" style="font-size:12.5px;font-weight:500">Study speed</div>
            <div class="desc" style="font-size:11px;color:var(--dim);margin:2px 0 13px">Kept separate so it never overwrites the speed above.</div>
            <div class="range-row" style="width:100%"><input type="range" class="slider" id="studySpeedRange" data-key="study_speed" min="0.5" max="2" step="0.05" oninput="document.getElementById('studySpeedLbl').textContent=Number(this.value).toFixed(2)+'x'"><span class="range-val" id="studySpeedLbl">0.95x</span></div>
          </div>
          <div class="plane">
            <div class="lbl" style="font-size:12.5px;font-weight:500">Study pause length</div>
            <div class="desc" style="font-size:11px;color:var(--dim);margin:2px 0 13px">Scales every study-mode pause.</div>
            <div class="range-row" style="width:100%"><input type="range" class="slider" id="studyPauseRange" data-key="study_pause_scale" min="0" max="3" step="0.1" oninput="document.getElementById('studyPauseLbl').textContent=Number(this.value).toFixed(1)+'x'"><span class="range-val" id="studyPauseLbl">1.0x</span></div>
          </div>
        </div>
        <div class="plane">
          <div class="row"><h3>How Study Mode reads a passage</h3><span class="sp"></span><button class="btn" id="playStudyBtn" onclick="playStudyPlan()"><span class="tri">▶</span>Play the plan</button></div>
          <p class="sub" id="pausePlanSub" style="margin-top:4px">Loading the real plan from narration.py…</p>
          <div class="pauseplan" id="pauseplan" aria-label="Study mode segment plan"></div>
          <div class="legend"><span><i style="background:var(--ion)"></i>SPOKEN</span><span><i style="background:var(--pause)"></i>PAUSE, SCALED TO STRUCTURE</span></div>
        </div>
      </section>

      <!-- Dictionary -->
      <section class="page" id="page-dictionary">
        <div class="ph"><div><div class="eyebrow neutral">ACCURACY</div><h1>Dictionary</h1><p>The words that are yours: names, clients, jargon. Whisper learns to spell them your way.</p></div></div>
        <div class="plane" style="margin-bottom:16px">
          <div class="srow" style="padding-top:2px">
            <div><div class="lbl">Learn from my edits</div><div class="desc">Fix the same word three times and it becomes a correction on its own.</div></div>
            <span class="sp"></span>
            <div class="tog" role="switch" aria-checked="true" data-key="learn_from_edits" aria-label="Learn from edits" tabindex="0" onclick="togClick(this)" onkeydown="togKeydown(event,this)"></div>
          </div>
          <div style="padding-top:16px">
            <div class="row" style="margin-bottom:10px"><h3 id="vocabCount">Vocabulary</h3><span class="sp"></span></div>
            <div class="row" style="flex-wrap:wrap;gap:8px" id="vocabList"></div>
            <div class="row" style="gap:8px;margin-top:12px">
              <input class="input" id="vocabWord" placeholder="New word or phrase…" style="max-width:280px" onkeydown="if(event.key==='Enter')addVocab()">
              <button class="btn" style="flex:none" onclick="addVocab()">+ Add term</button>
            </div>
          </div>
        </div>
        <div class="grid" style="grid-template-columns:1.4fr 1fr">
          <div class="plane">
            <h3 style="margin-bottom:10px">Corrections</h3>
            <div id="corrList"></div>
            <div class="row" style="gap:8px;margin-top:12px">
              <input class="input" id="corrFrom" placeholder="Heard (e.g. Selvin)">
              <input class="input" id="corrTo" placeholder="Replace with (e.g. Selwyn)">
              <button class="btn" style="flex:none" onclick="addCorrection()">Add</button>
            </div>
          </div>
          <div class="plane">
            <h3>Learned this week</h3>
            <p class="sub" style="margin:2px 0 10px">Promoted automatically from your edits.</p>
            <div id="learnedList"></div>
          </div>
        </div>
      </section>

      <!-- History -->
      <section class="page" id="page-history">
        <div class="ph">
          <div><div class="eyebrow neutral">ARCHIVE</div><h1>History</h1><p>Every dictation, searchable. Stored here, and nowhere else.</p></div>
          <div class="ph-actions">
            <button class="btn ghost" id="selectModeBtn" onclick="toggleSelectMode()">Select</button>
            <button class="btn danger" onclick="confirmClear()">Clear all</button>
          </div>
        </div>
        <div class="row" style="margin-bottom:12px;gap:10px">
          <div class="search-wrap" style="flex:1;max-width:340px">
            <svg class="search-icon" viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M8 4a4 4 0 100 8 4 4 0 000-8zM2 8a6 6 0 1110.89 3.476l4.817 4.817a1 1 0 01-1.414 1.414l-4.816-4.816A6 6 0 012 8z" clip-rule="evenodd"/></svg>
            <input class="input search-in" id="histSearch" placeholder="Search text or target app…" oninput="onHistSearchInput(this.value)" aria-label="Search history">
          </div>
        </div>
        <div class="bulk-bar" id="bulkBar" style="display:none">
          <label class="bulk-all"><input type="checkbox" id="selectAllChk" onchange="selectAllToggle(this.checked)"> Select all</label>
          <span id="bulkCount">0 selected</span>
          <div class="bulk-acts">
            <select class="input" id="exportFormatSel" style="width:auto" aria-label="Export format">
              <option value="md">Markdown (.md)</option>
              <option value="txt">Plain text (.txt)</option>
              <option value="srt">SRT subtitles (.srt)</option>
              <option value="vtt">WebVTT (.vtt)</option>
            </select>
            <button class="btn ghost" onclick="exportSelected()">Export</button>
            <button class="btn danger" onclick="deleteSelected()">Delete</button>
          </div>
        </div>
        <div id="historyList"><div class="empty">Loading…</div></div>
      </section>

      <!-- Diagnostics -->
      <section class="page" id="page-diagnostics">
        <div class="ph">
          <div><div class="eyebrow neutral">SYSTEM</div><h1>Diagnostics</h1><p>Every moving part, checked in one click.</p></div>
          <div class="ph-actions"><button class="btn" onclick="loadDiagnostics()">Refresh</button></div>
        </div>
        <div class="plane" style="margin-bottom:16px">
          <div class="probe"><span class="st" id="diagWhisperSt">·</span><span id="diagWhisperTxt">Checking…</span><span class="val" id="diagWhisperVal"></span></div>
          <div class="probe"><span class="st" id="diagHotkeySt">·</span><span id="diagHotkeyTxt">Checking…</span><span class="val" id="diagHotkeyVal"></span></div>
          <div class="probe"><span class="st" id="diagMicSt">·</span><span id="diagMicTxt">Checking…</span><span class="val" id="diagMicVal"></span></div>
          <div class="probe"><span class="st" id="diagNetSt">·</span>Network isolation<span class="val" id="diagNetVal">checking…</span></div>
        </div>
        <div class="plane" style="margin-bottom:16px">
          <div class="sock-well" id="sockWell">Checking…</div>
        </div>
        <div class="notice" style="margin-bottom:16px"><span class="tick">●</span><span><b>Network isolation</b> is a real check, not a promise: the app lists its own open sockets. Dictation works with the cable out.</span></div>
        <div class="plane">
          <div class="row" style="margin-bottom:8px"><h3>Mic level test</h3></div>
          <p class="sub">Runs a 5 second test recording to show your live input level. Nothing is saved.</p>
          <div class="mic-meter"><div class="mic-meter-fill" id="micMeterFill"></div><div class="mic-meter-peak" id="micMeterPeak"></div></div>
          <button class="btn" id="micProbeBtn" onclick="startMicProbe()">Test mic</button>
        </div>
      </section>

      <!-- Settings -->
      <section class="page" id="page-settings">
        <div class="ph"><div><div class="eyebrow neutral">SYSTEM</div><h1>Settings</h1><p>Instant apply. There is no save button.</p></div></div>
        <div id="settingsForm">

        <div class="plane" style="margin-bottom:16px">
          <div class="s-sec-ttl">APPEARANCE<button class="sec-reset-btn" onclick="resetSection('appearance')">Reset section</button></div>
          <div class="srow" style="padding-top:2px">
            <div><div class="lbl">Theme</div><div class="desc">Dark or light interface.</div></div>
            <span class="sp"></span>
            <select class="input" style="max-width:180px" data-key="theme" onchange="applyThemeFromSelect(this.value)">
              <option value="dark">Dark</option><option value="light">Light</option><option value="system">Follow Windows</option>
            </select>
          </div>
          <div class="srow">
            <div><div class="lbl">Dashboard scale</div><div class="desc">Resizes this window's interface. The floating preview panel is unaffected.</div></div>
            <span class="sp"></span>
            <select class="input" style="max-width:120px" data-key="dashboard_scale" onchange="applyScaleFromSelect(this.value)">
              <option value="90">90%</option><option value="100" selected>100%</option><option value="110">110%</option><option value="125">125%</option>
            </select>
          </div>
          <div class="srow">
            <div><div class="lbl">Recording animation</div><div class="desc">Style of the pill's motion while you speak.</div></div>
            <span class="sp"></span>
            <select class="input" style="max-width:160px" data-key="badge_animation">
              <option value="waveform">Waveform</option><option value="pulse">Pulse</option><option value="bars">Bars</option>
            </select>
          </div>
          <div class="srow">
            <div><div class="lbl">Preview position</div><div class="desc">Where the transcription panel appears.</div></div>
            <span class="sp"></span>
            <select class="input" style="max-width:180px" data-key="preview_position">
              <option value="cursor">Near cursor</option><option value="top-right">Top right</option><option value="bottom-right">Bottom right</option><option value="top-left">Top left</option><option value="bottom-left">Bottom left</option><option value="center">Centre</option>
            </select>
          </div>
          <div class="srow">
            <div><div class="lbl">Auto-dismiss (seconds)</div><div class="desc">0 = never auto-dismiss.</div></div>
            <span class="sp"></span>
            <input class="input n-in" type="number" data-key="preview_auto_dismiss_seconds" min="0" max="30" step="0.5">
          </div>
          <div class="srow">
            <div><div class="lbl">Animations</div><div class="desc">Popup slide/fade motion. Also off automatically when Windows' own "Show animations" setting is off.</div></div>
            <span class="sp"></span>
            <div class="tog" data-key="animations" role="switch" aria-checked="false" aria-label="Animations" tabindex="0" onclick="togClick(this)" onkeydown="togKeydown(event,this)"></div>
          </div>
          <div class="srow">
            <div><div class="lbl">Sound volume</div><div class="desc">Record/stop/success/error chimes. 0 = mute.</div></div>
            <span class="sp"></span>
            <div class="range-row"><input class="slider" type="range" id="soundVolumeRange" data-key="sound_volume" data-type="int" min="0" max="100" step="5" oninput="document.getElementById('soundVolumeLbl').textContent=this.value+'%'"><span class="range-val" id="soundVolumeLbl">100%</span></div>
          </div>
        </div>

        <div class="plane" style="margin-bottom:16px">
          <div class="s-sec-ttl">RECORDING<button class="sec-reset-btn" onclick="resetSection('recording')">Reset section</button></div>
          <div class="srow" style="padding-top:2px">
            <div><div class="lbl">Min duration (s)</div><div class="desc">Ignore recordings shorter than this.</div></div>
            <span class="sp"></span><input class="input n-in" type="number" data-key="min_record_seconds" min="0.1" max="5" step="0.1">
          </div>
          <div class="srow">
            <div><div class="lbl">Max duration (s)</div><div class="desc">Auto-stop after this many seconds.</div></div>
            <span class="sp"></span><input class="input n-in" type="number" data-key="max_record_seconds" min="5" max="300" step="5">
          </div>
          <div class="srow">
            <div><div class="lbl">VAD filter</div><div class="desc">Strip silence via voice activity detection.</div></div>
            <span class="sp"></span>
            <div class="tog" data-key="vad_filter" role="switch" aria-checked="false" aria-label="VAD filter" tabindex="0" onclick="togClick(this)" onkeydown="togKeydown(event,this)"></div>
          </div>
        </div>

        <div class="plane" style="margin-bottom:16px">
          <div class="s-sec-ttl">TRANSCRIPTION<button class="sec-reset-btn" onclick="resetSection('transcription')">Reset section</button></div>
          <div class="srow" style="padding-top:2px">
            <div><div class="lbl">Language</div><div class="desc">ISO code, e.g. en, fr, de.</div></div>
            <span class="sp"></span><input class="input t-in" type="text" data-key="language" maxlength="10">
          </div>
          <div class="srow col">
            <div><div class="lbl">Filler words</div><div class="desc">Comma-separated words to remove (e.g. um, uh, like).</div></div>
            <input class="input t-in wide" type="text" data-key="filler_words" data-type="array">
          </div>
          <div class="srow col">
            <div><div class="lbl">Initial prompt</div><div class="desc">Primes Whisper with context (vocabulary, style).</div></div>
            <textarea class="input" data-key="initial_prompt" rows="3"></textarea>
          </div>
        </div>

        <div class="plane" style="margin-bottom:16px">
          <div class="s-sec-ttl">PASTE<button class="sec-reset-btn" onclick="resetSection('paste')">Reset section</button></div>
          <div class="srow" style="padding-top:2px">
            <div><div class="lbl">Clipboard restore delay (ms)</div><div class="desc">How long before restoring your previous clipboard.</div></div>
            <span class="sp"></span><input class="input n-in" type="number" data-key="clipboard_restore_delay_ms" min="50" max="1000" step="50" data-type="int">
          </div>
          <div class="srow">
            <div><div class="lbl">Electron / VS Code paste</div><div class="desc">Method for Electron apps.</div></div>
            <span class="sp"></span>
            <select class="input" style="max-width:220px" data-key="electron_paste_method">
              <option value="ctrl_v">Clipboard + Ctrl+V</option><option value="type">Unicode typing</option>
            </select>
          </div>
        </div>

        <div class="plane" style="margin-bottom:16px">
          <div class="s-sec-ttl">HISTORY<button class="sec-reset-btn" onclick="resetSection('history')">Reset section</button></div>
          <div class="srow" style="padding-top:2px">
            <div><div class="lbl">Keep history</div><div class="desc">Older entries are trimmed past this count. Pinned entries are never trimmed.</div></div>
            <span class="sp"></span>
            <select class="input" style="max-width:160px" data-key="history_max_entries">
              <option value="50">50 entries</option><option value="100" selected>100 entries</option><option value="250">250 entries</option><option value="500">500 entries</option>
            </select>
          </div>
          <div class="srow">
            <div><div class="lbl">Auto-delete recordings</div><div class="desc">Deletes saved audio older than this. Transcripts stay either way; pinned entries are exempt.</div></div>
            <span class="sp"></span>
            <select class="input" style="max-width:160px" data-key="recording_retention_days">
              <option value="0" selected>Never</option><option value="7">After 7 days</option><option value="30">After 30 days</option><option value="90">After 90 days</option>
            </select>
          </div>
        </div>

        <div class="plane" style="margin-bottom:16px">
          <div class="s-sec-ttl">SYSTEM</div>
          <div class="srow" style="padding-top:2px">
            <div><div class="lbl">Start with Windows</div><div class="desc">Launch hidden at logon via Task Scheduler.</div></div>
            <span class="sp"></span>
            <div class="tog" id="autostartTog" role="switch" aria-checked="false" aria-label="Start with Windows" tabindex="0" onclick="autostartClick(this)" onkeydown="togKeydown(event,this)"></div>
          </div>
          <div class="srow">
            <div><div class="lbl">Export settings</div><div class="desc">Save your settings, corrections, and vocabulary to a JSON file.</div></div>
            <span class="sp"></span><button class="btn" onclick="exportSettings()">Export</button>
          </div>
          <div class="srow">
            <div><div class="lbl">Import settings</div><div class="desc">Load settings from a previously exported JSON file.</div></div>
            <span class="sp"></span><button class="btn ghost" onclick="importSettings()">Import</button>
          </div>
        </div>

        </div>
        <div class="row" style="padding:6px 0 0;justify-content:flex-end;gap:12px">
          <span class="save-ok" id="saveOk">✓ Saved</span>
          <button class="btn ghost" onclick="reloadSettings()">Reload</button>
        </div>
      </section>

      <!-- About -->
      <section class="page" id="page-about">
        <div class="ph"><div><div class="eyebrow neutral">SYSTEM</div><h1>About</h1><p>Version, licence, and credits.</p></div></div>
        <div class="plane" style="margin-bottom:16px">
          <h3>Quiett</h3>
          <p class="sub" id="aboutVersion" style="margin-top:6px">Version —</p>
          <p class="sub" style="margin-top:8px;max-width:560px">Offline, hold-to-talk voice dictation for Windows. Records while you hold a hotkey, transcribes locally, and lets you review before it lands in whatever app has focus.</p>
          <p class="sub" style="margin-top:8px">Licence: private build, not for redistribution.</p>
        </div>
        <div class="plane" style="margin-bottom:16px">
          <h3 style="margin-bottom:8px">Credits</h3>
          <p class="sub" style="line-height:1.7">Speech recognition by <b style="color:var(--text);font-weight:500">whisper.cpp</b> (large-v3-turbo, Vulkan build), MIT licence.<br>Cloned-voice read-aloud by <b style="color:var(--text);font-weight:500">qwentts.cpp</b> (Qwen3-TTS, Vulkan build), MIT licence, Apache 2.0 weights.</p>
        </div>
        <div class="plane">
          <h3 style="margin-bottom:8px">Changelog</h3>
          <div id="changelogList" class="sub">Loading…</div>
        </div>
      </section>

    </div>
  </div>

  <!-- the voice line -->
  <div class="voiceline">
    <svg class="vl-wave" viewBox="0 0 74 16" aria-hidden="true"><polyline id="vlPoly" points=""/></svg>
    <span id="vlStatus">QUIETT NOT RUNNING</span>
    <span class="vl-sp"></span>
    <span class="vl-keys">HOLD <span class="key">Ctrl</span>+<span class="key">Alt</span> TO DICTATE</span>
  </div>
</div>

<script>
// ── State ──────────────────────────────────────────────────────────────────
let _histAll = [];
let _histTexts = [];  // parallel array — safe index-based access avoids JSON-in-onclick quoting bugs
let _currentPage = 'home';
let _theme = 'dark';
let _INIT_PAGE = 'home';
let _histSourceFilter = 'all';
let _selectMode = false;
let _selectedIdx = new Set();
let _pausePlanSegments = [];
let _networkSealed = false;
let _lastNetworkProbe = null;

// ── Helpers ────────────────────────────────────────────────────────────────
function fmt(n) {
  if (n == null) return '—';
  if (n >= 1000000) return (n/1e6).toFixed(1).replace('.0','')+'M';
  if (n >= 10000) return (n/1000).toFixed(1).replace('.0','')+'K';
  return n.toLocaleString();
}

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function copyIcon() {
  return '<svg viewBox="0 0 20 20" fill="currentColor"><path d="M8 2a1 1 0 000 2h2a1 1 0 100-2H8z"/><path d="M3 5a2 2 0 012-2 3 3 0 003 3h2a3 3 0 003-3 2 2 0 012 2v6h-4.586l1.293-1.293a1 1 0 00-1.414-1.414l-3 3a1 1 0 000 1.414l3 3a1 1 0 001.414-1.414L10.414 13H15v3a2 2 0 01-2 2H5a2 2 0 01-2-2V5z"/></svg>';
}

function trashIcon() {
  return '<svg viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M9 2a1 1 0 00-.894.553L7.382 4H4a1 1 0 000 2v10a2 2 0 002 2h8a2 2 0 002-2V6a1 1 0 100-2h-3.382l-.724-1.447A1 1 0 0011 2H9zM7 8a1 1 0 012 0v6a1 1 0 11-2 0V8zm5-1a1 1 0 00-1 1v6a1 1 0 102 0V8a1 1 0 00-1-1z" clip-rule="evenodd"/></svg>';
}

function xIcon() {
  return '<svg viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clip-rule="evenodd"/></svg>';
}

function starIcon(filled) {
  return filled
    ? '<svg viewBox="0 0 20 20" fill="currentColor"><path d="M9.049 2.927c.3-.921 1.603-.921 1.902 0l1.286 3.958a1 1 0 00.95.69h4.162c.969 0 1.371 1.24.588 1.81l-3.368 2.446a1 1 0 00-.363 1.118l1.287 3.957c.3.922-.755 1.688-1.539 1.118l-3.367-2.446a1 1 0 00-1.176 0l-3.367 2.446c-.784.57-1.838-.196-1.539-1.118l1.286-3.957a1 1 0 00-.363-1.118L2.062 9.385c-.783-.57-.38-1.81.588-1.81h4.163a1 1 0 00.95-.69l1.286-3.958z"/></svg>'
    : '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"><path d="M9.049 2.927c.3-.921 1.603-.921 1.902 0l1.286 3.958a1 1 0 00.95.69h4.162c.969 0 1.371 1.24.588 1.81l-3.368 2.446a1 1 0 00-.363 1.118l1.287 3.957c.3.922-.755 1.688-1.539 1.118l-3.367-2.446a1 1 0 00-1.176 0l-3.367 2.446c-.784.57-1.838-.196-1.539-1.118l1.286-3.957a1 1 0 00-.363-1.118L2.062 9.385c-.783-.57-.38-1.81.588-1.81h4.163a1 1 0 00.95-.69l1.286-3.958z"/></svg>';
}

function playIcon() {
  return '<svg viewBox="0 0 20 20" fill="currentColor"><path d="M6.5 4.27a1 1 0 011.5-.868l8 4.73a1 1 0 010 1.736l-8 4.73A1 1 0 016.5 13.73V4.27z"/></svg>';
}

function stopIcon() {
  return '<svg viewBox="0 0 20 20" fill="currentColor"><rect x="5" y="5" width="10" height="10" rx="1.5"/></svg>';
}

function highlightText(text, q) {
  if (!q) return esc(text);
  const lower = text.toLowerCase();
  const needle = q.toLowerCase();
  let out = '';
  let i = 0;
  while (true) {
    const pos = lower.indexOf(needle, i);
    if (pos === -1) { out += esc(text.slice(i)); break; }
    out += esc(text.slice(i, pos));
    out += '<mark>' + esc(text.slice(pos, pos + needle.length)) + '</mark>';
    i = pos + needle.length;
  }
  return out;
}

function showToast(msg) {
  let t = document.getElementById('dashToast');
  if (!t) {
    t = document.createElement('div');
    t.id = 'dashToast';
    t.className = 'dash-toast';
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(window._toastT);
  window._toastT = setTimeout(() => t.classList.remove('show'), 3200);
}

// ── Navigation ─────────────────────────────────────────────────────────────
function navigateTo(page) {
  if (_currentPage === page && document.getElementById('page-'+page).classList.contains('active')) {
    if (page === 'history') loadHistory();
    return;
  }
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav').forEach(n => n.classList.remove('active'));
  document.getElementById('page-'+page).classList.add('active');
  const navBtn = document.querySelector('.nav[data-page="'+page+'"]');
  if (navBtn) navBtn.classList.add('active');  // 'about' has no rail entry
  _currentPage = page;
  if (page === 'home') loadHome();
  else if (page === 'dictation') loadSettings();
  else if (page === 'voice') { loadSettings(); loadVoice(); }
  else if (page === 'dictionary') loadDictionary();
  else if (page === 'history') loadHistory();
  else if (page === 'settings') loadSettings();
  else if (page === 'diagnostics') loadDiagnostics();
  else if (page === 'about') loadAbout();
}

// ── Live refresh ───────────────────────────────────────────────────────────
// Called by the main app over the control channel the moment a dictation is
// saved, and by the poll below as a backstop. Refreshes whatever page is on
// screen in place, keeping scroll position, search and selection intact.
let _histStamp = '';
let _refreshPending = false;

async function onExternalRefresh(what) {
  if (what !== 'history' && what !== 'all') return;
  if (_selectMode) { _refreshPending = true; return; }  // don't move rows out from under a bulk selection
  try {
    if (_currentPage === 'history') await loadHistory(true);
    else if (_currentPage === 'home') await loadHome();
    _histStamp = await window.pywebview.api.get_history_stamp();
  } catch (e) { console.error('refresh error:', e); }
}

async function pollHistory() {
  if (_currentPage !== 'history' && _currentPage !== 'home') return;
  try {
    const stamp = await window.pywebview.api.get_history_stamp();
    if (_histStamp && stamp !== _histStamp) await onExternalRefresh('history');
    _histStamp = stamp;
  } catch (e) {}
}

// ── Theme ──────────────────────────────────────────────────────────────────
function resolveTheme(t) {
  return t === 'system'
    ? (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
    : t;
}

let _themePref = 'dark';

function applyTheme(t) {
  _themePref = t;
  t = resolveTheme(t);
  _theme = t;
  document.documentElement.setAttribute('data-theme', t);
  try { window.pywebview.api.set_titlebar_dark(t !== 'light'); } catch (e) {}
  const icon = document.getElementById('themeIcon');
  if (!icon) return;  // theme icon left the titlebar in the ion-glass restyle
  if (t === 'light') {
    icon.innerHTML = '<path fill-rule="evenodd" d="M10 2a1 1 0 011 1v1a1 1 0 11-2 0V3a1 1 0 011-1zm4 8a4 4 0 11-8 0 4 4 0 018 0zm-.464 4.95l.707.707a1 1 0 001.414-1.414l-.707-.707a1 1 0 00-1.414 1.414zm2.12-10.607a1 1 0 010 1.414l-.706.707a1 1 0 11-1.414-1.414l.707-.707a1 1 0 011.414 0zM17 11a1 1 0 100-2h-1a1 1 0 100 2h1zm-7 4a1 1 0 011 1v1a1 1 0 11-2 0v-1a1 1 0 011-1zM5.05 6.464A1 1 0 106.465 5.05l-.708-.707a1 1 0 00-1.414 1.414l.707.707zm1.414 8.486l-.707.707a1 1 0 01-1.414-1.414l.707-.707a1 1 0 011.414 1.414zM4 11a1 1 0 100-2H3a1 1 0 000 2h1z" clip-rule="evenodd"/>';
  } else {
    icon.innerHTML = '<path d="M17.293 13.293A8 8 0 016.707 2.707a8.001 8.001 0 1010.586 10.586z"/>';
  }
}

function applyThemeFromSelect(val) { applyTheme(val); }

function toggleTheme() {
  const next = _theme === 'dark' ? 'light' : 'dark';
  applyTheme(next);
  const sel = document.querySelector('select[data-key="theme"]');
  if (sel) sel.value = next;
  window.pywebview.api.save_settings({theme: next});
}

// ── Home page — real data, no simulate button (P3) ──────────────────────────
async function loadHome() {
  const s = await window.pywebview.api.get_home_stats();
  const eyebrow = document.getElementById('homeEyebrow');
  const hero = document.getElementById('homeHero');
  const metrics = document.getElementById('homeMetrics');
  const grid = document.getElementById('homeGrid');
  const edu = document.getElementById('homeEdu');
  const recentPlane = document.getElementById('homeRecent').closest('.plane');

  if (!s.has_history) {
    eyebrow.textContent = 'GET STARTED';
    hero.innerHTML = '';
    metrics.innerHTML = '';
    document.getElementById('rhythm').innerHTML = '';
    document.getElementById('rhythmLbl').innerHTML = '';
    grid.classList.add('single');
    recentPlane.style.display = 'none';
    edu.classList.add('hero-edu');
    edu.innerHTML = `
      <h3>Hold Ctrl+Alt anywhere.</h3>
      <p class="sub">Your first dictation lands here.</p>
      <div class="keys" style="margin-top:8px"><span class="key">Ctrl</span><span class="plus">+</span><span class="key">Alt</span><span style="color:var(--dim);font-size:11px;letter-spacing:.04em">&nbsp;hold · speak · release</span></div>
      <p class="sub">Hear anything back in your own cloned voice with <span class="key">Ctrl</span><span class="plus">+</span><span class="key">Shift</span><span class="plus">+</span><span class="key">S</span></p>`;
    return;
  }

  eyebrow.textContent = 'THIS WEEK';
  grid.classList.remove('single');
  recentPlane.style.display = '';
  edu.classList.remove('hero-edu');
  edu.innerHTML = `
    <h3>Dictate anywhere</h3>
    <p class="sub">No app to open. No window to switch to.</p>
    <div class="keys"><span class="key">Ctrl</span><span class="plus">+</span><span class="key">Alt</span><span style="color:var(--dim);font-size:11px;letter-spacing:.04em">&nbsp;hold · speak · release</span></div>
    <p class="sub">Hear anything back in your own cloned voice with <span class="key">Ctrl</span><span class="plus">+</span><span class="key">Shift</span><span class="plus">+</span><span class="key">S</span></p>
    <div style="margin-top:auto;padding-top:12px"><button class="btn primary" onclick="navigateTo('history')">Open History</button></div>`;

  const deltaArrow = s.delta_pct > 0 ? '▲' : (s.delta_pct < 0 ? '▼' : '·');
  const deltaCls = s.delta_pct === 0 ? ' flat' : '';
  hero.innerHTML = `<div class="hero">
      <div><div class="big">${fmt(s.words_this_week)}</div>
      <div class="delta${deltaCls}">${deltaArrow} ${Math.abs(s.delta_pct)}% on last week</div></div>
      <div class="cap">WORDS SPOKEN<br>INTO PLACE</div>
    </div>`;

  const wpmVal = s.avg_wpm != null ? `${s.avg_wpm}<small>wpm</small>` : '—';
  const fasterVal = s.faster_than_typing != null ? `${s.faster_than_typing}×` : '—';
  const hrs = Math.floor(s.reclaimed_minutes / 60), mins = s.reclaimed_minutes % 60;
  const reclaimedVal = s.reclaimed_minutes > 0 ? (hrs > 0 ? `${hrs}h ${mins}m` : `${mins}m`) : '—';
  metrics.innerHTML = `
    <div class="metric"><div class="v">${wpmVal}</div><div class="k">AVERAGE PACE</div></div>
    <div class="metric"><div class="v">${fasterVal}</div><div class="k">FASTER THAN TYPING</div></div>
    <div class="metric"><div class="v">${reclaimedVal}</div><div class="k">RECLAIMED</div></div>
    <div class="metric"><div class="v">${fmt(s.dictation_count)}</div><div class="k">DICTATIONS</div></div>`;

  const rEl = document.getElementById('rhythm'), lEl = document.getElementById('rhythmLbl');
  const max = Math.max(1, ...s.per_day);
  rEl.innerHTML = s.per_day.map((n, i) => {
    const hiCls = i === s.per_day.length - 1 ? ' hi' : '';
    return `<span class="rcell"><i class="${hiCls.trim()}" style="height:${Math.max(9, (n / max) * 100)}%"></i></span>`;
  }).join('');
  lEl.innerHTML = s.day_labels.map(d => `<span>${esc(d)}</span>`).join('');

  const recEl = document.getElementById('homeRecent');
  if (!s.recent.length) {
    recEl.innerHTML = '<p class="sub">Nothing yet.</p>';
  } else {
    recEl.innerHTML = s.recent.map(e => `
      <div class="item"><span class="glyph">${e.source ? esc(e.source.slice(0, 2).toUpperCase()) : '··'}</span>
      <div><div class="txt">${esc(e.text)}</div><div class="meta">${esc(e.label)} · ${e.words} WORDS${e.source ? ' · ' + esc(e.source.toUpperCase()) : ''}</div></div></div>`).join('');
  }
}

// ── History page ──────────────────────────────────────────────────────────
let _histQuery = '';

async function loadHistory(quiet) {
  const el = document.getElementById('historyList');
  // quiet = live refresh of an already-populated list: no Loading flash, and
  // the reader keeps their place in the list.
  const scroll = quiet ? el.scrollTop : 0;
  if (!quiet) el.innerHTML = '<div class="empty">Loading…</div>';
  _histAll = await window.pywebview.api.get_history(500);
  renderHistory(filterEntries(_histAll, _histQuery, _histSourceFilter));
  if (quiet && scroll) el.scrollTop = scroll;
}

function filterEntries(items, q, srcFilter) {
  let out = items;
  if (srcFilter && srcFilter !== 'all') {
    out = out.filter(e => (e.source || '') === srcFilter);
  }
  if (q) {
    const needle = q.toLowerCase();
    out = out.filter(e =>
      e.text.toLowerCase().includes(needle) ||
      (e.source || '').toLowerCase().includes(needle));
  }
  return out;
}

function renderHistItem(e, i) {
  const hasAudio = !!e.has_audio;
  return `
    <div class="hrow${e.pinned ? ' pinned' : ''}" id="hi-${e.index}">
      <input type="checkbox" class="li-check" ${_selectedIdx.has(e.index) ? 'checked' : ''}
        onclick="toggleSelect(${e.index}, this.checked)">
      <span class="txt">${highlightText(e.text, _histQuery)}</span>
      ${hasAudio ? `<span class="wave-thumb" data-idx="${e.index}" title="Waveform"></span>` : ''}
      <span class="t">${esc(e.label)} · ${e.words}w${e.source ? ' · ' + esc(e.source) : ''}</span>
      <span class="act">
        ${hasAudio ? `<button class="iby play" title="Play" aria-label="Play recording" onclick="toggleAudioPlay(${e.index})">${playIcon()}</button>` : ''}
        <button class="iby pin${e.pinned ? ' on' : ''}" title="${e.pinned ? 'Unpin' : 'Pin'}" aria-label="${e.pinned ? 'Unpin' : 'Pin'}"
          onclick="togglePin(${e.index}, ${e.pinned ? 'false' : 'true'})">${starIcon(e.pinned)}</button>
        <button class="iby" title="Copy" aria-label="Copy" onclick="copyText(_histTexts[${i}])">${copyIcon()}</button>
        <button class="iby" title="Delete" aria-label="Delete" onclick="deleteHistory(${e.index})">${trashIcon()}</button>
      </span>
    </div>`;
}

// Designed empty state (BACKLOG item 49): icon + what belongs here + how to
// get the first one. `emptyState` is reused by History and Dictionary;
// search/filter "no results" stays the plain, distinct .empty message above
// it — that's not a first-run state, so it doesn't get the onboarding treatment.
function emptyState(icon, title, sub) {
  return `<div class="empty-state">
    <div class="es-icon">${icon}</div>
    <div class="es-title">${esc(title)}</div>
    <div class="es-sub">${esc(sub)}</div>
  </div>`;
}
const _ES_MIC_ICON = '<svg viewBox="0 0 20 20" fill="currentColor"><path d="M10 2a3 3 0 00-3 3v5a3 3 0 006 0V5a3 3 0 00-3-3z"/><path d="M5.5 9.643a.75.75 0 00-1.5 0V10c0 3.06 2.29 5.585 5.25 5.954V17.5h-1.5a.75.75 0 000 1.5h4.5a.75.75 0 000-1.5h-1.5v-1.546A6.001 6.001 0 0016 10v-.357a.75.75 0 00-1.5 0V10a4.5 4.5 0 01-9 0v-.357z"/></svg>';
const _ES_PENCIL_ICON = '<svg viewBox="0 0 20 20" fill="currentColor"><path d="M13.586 3.586a2 2 0 112.828 2.828l-.793.793-2.828-2.828.793-.793zM11.379 5.793L3 14.172V17h2.828l8.379-8.379-2.828-2.828z"/></svg>';
const _ES_TAG_ICON = '<svg viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M5.5 3A2.5 2.5 0 003 5.5v2.879a2.5 2.5 0 00.732 1.767l6.5 6.5a2.5 2.5 0 003.536 0l2.878-2.878a2.5 2.5 0 000-3.536l-6.5-6.5A2.5 2.5 0 008.379 3H5.5zM6 7a1 1 0 100-2 1 1 0 000 2z" clip-rule="evenodd"/></svg>';

function renderHistory(items) {
  const el = document.getElementById('historyList');
  el.classList.toggle('select-mode', _selectMode);
  if (!items.length) {
    _histTexts = [];
    el.innerHTML = (_histQuery || (_histSourceFilter && _histSourceFilter !== 'all'))
      ? '<div class="empty">No dictations match your search.</div>'
      : emptyState(_ES_MIC_ICON, 'No dictations yet',
                   'Hold Ctrl+Alt and speak. Your dictations land here.');
    updateBulkCount();
    return;
  }
  const pinned = items.filter(e => e.pinned);
  const rest = items.filter(e => !e.pinned);
  const ordered = pinned.concat(rest);
  _histTexts = ordered.map(e => e.text);
  let html = '';
  if (pinned.length) html += '<div class="dayh">PINNED</div>';
  let lastDay = null;
  ordered.forEach((e, i) => {
    if (!e.pinned && e.day_label && e.day_label !== lastDay) {
      lastDay = e.day_label;
      html += `<div class="dayh">${esc(e.day_label.toUpperCase())}</div>`;
    }
    html += renderHistItem(e, i);
  });
  el.innerHTML = html;
  updateBulkCount();
  observeWaveThumbs();
}

// ── Audio playback (item 38) ────────────────────────────────────────────────
let _playingIdx = null;

async function toggleAudioPlay(idx) {
  const wasPlaying = _playingIdx === idx;
  if (_playingIdx !== null) {
    // Only one plays at a time — stop whatever was playing before starting
    // (or finishing) this click.
    try { await window.pywebview.api.stop_audio(); } catch (e) {}
    setPlayButtonState(_playingIdx, false);
    _playingIdx = null;
  }
  if (wasPlaying) return; // this click was the stop toggle
  try {
    const res = await window.pywebview.api.play_history_audio(idx, stampFor(idx));
    if (res && res.ok) {
      _playingIdx = idx;
      setPlayButtonState(idx, true);
    } else {
      showToast((res && res.error) || 'Could not play recording.');
      if (res && res.error === 'Recording no longer on disk.') hideAudioControls(idx);
    }
  } catch (e) {
    showToast('Could not play recording.');
  }
}

function setPlayButtonState(idx, playing) {
  const btn = document.querySelector(`#hi-${idx} .iby.play`);
  if (!btn) return;
  btn.classList.toggle('on', playing);
  btn.innerHTML = playing ? stopIcon() : playIcon();
  btn.title = playing ? 'Stop' : 'Play';
  btn.setAttribute('aria-label', playing ? 'Stop playback' : 'Play recording');
}

function hideAudioControls(idx) {
  const row = document.getElementById(`hi-${idx}`);
  if (!row) return;
  const btn = row.querySelector('.iby.play');
  if (btn) btn.style.display = 'none';
  const wave = row.querySelector('.wave-thumb');
  if (wave) wave.style.display = 'none';
}

// ── Waveform thumbnails (item 88) — lazy: only fetched once a row's thumb
// placeholder actually scrolls into view, so opening History with hundreds
// of entries doesn't render hundreds of PNGs up front. ─────────────────────
let _waveObserver = null;

function observeWaveThumbs() {
  if (!_waveObserver) {
    _waveObserver = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        _waveObserver.unobserve(entry.target);
        loadWaveThumb(entry.target);
      });
    }, { root: document.getElementById('historyList'), rootMargin: '150px' });
  }
  document.querySelectorAll('.wave-thumb[data-idx]').forEach(el => _waveObserver.observe(el));
}

async function loadWaveThumb(el) {
  const idx = parseInt(el.dataset.idx, 10);
  try {
    const res = await window.pywebview.api.get_waveform(idx, stampFor(idx));
    if (res && res.ok && res.data) {
      el.style.backgroundImage = `url(${res.data})`;
    } else {
      el.style.display = 'none';
    }
  } catch (e) {
    el.style.display = 'none';
  }
}

let _histSearchT = null;
function onHistSearchInput(q) {
  clearTimeout(_histSearchT);
  _histSearchT = setTimeout(() => {
    _histQuery = q;
    renderHistory(filterEntries(_histAll, _histQuery, _histSourceFilter));
  }, 150);
}

function setSourceFilter(src) {
  _histSourceFilter = src;
  document.querySelectorAll('#sourceChips .chip').forEach(c => {
    c.classList.toggle('active', c.dataset.src === src);
  });
  renderHistory(filterEntries(_histAll, _histQuery, _histSourceFilter));
}

// Row identity. A dictation landing while History is open shifts every index
// down by one, so the index alone would delete or pin the neighbouring entry;
// the timestamp says which row was actually clicked.
function stampFor(idx) {
  const e = _histAll.find(x => x.index === idx);
  return (e && e.ts) || '';
}

async function togglePin(idx, newVal) {
  const ok = await window.pywebview.api.set_pinned(idx, newVal, stampFor(idx));
  if (!ok) return;
  const e = _histAll.find(x => x.index === idx);
  if (e) e.pinned = newVal;
  renderHistory(filterEntries(_histAll, _histQuery, _histSourceFilter));
}

async function deleteHistory(idx) {
  await window.pywebview.api.delete_history_entry(idx, stampFor(idx));
  _histAll = _histAll.filter(e => e.index !== idx);
  _selectedIdx.delete(idx);
  renderHistory(filterEntries(_histAll, _histQuery, _histSourceFilter));
}

async function confirmClear() {
  if (!confirm('Clear all history? This cannot be undone.')) return;
  await window.pywebview.api.clear_history(true);
  _histAll = [];
  _selectedIdx.clear();
  renderHistory([]);
}

// ── Bulk select ──────────────────────────────────────────────────────────
function toggleSelectMode() {
  _selectMode = !_selectMode;
  document.getElementById('selectModeBtn').textContent = _selectMode ? 'Cancel' : 'Select';
  document.getElementById('bulkBar').style.display = _selectMode ? 'flex' : 'none';
  if (!_selectMode) _selectedIdx.clear();
  renderHistory(filterEntries(_histAll, _histQuery, _histSourceFilter));
  // Dictations that landed while a selection was open apply now that the
  // indices are no longer being pointed at.
  if (!_selectMode && _refreshPending) { _refreshPending = false; onExternalRefresh('history'); }
}

function toggleSelect(idx, checked) {
  if (checked) _selectedIdx.add(idx); else _selectedIdx.delete(idx);
  updateBulkCount();
}

function updateBulkCount() {
  const el = document.getElementById('bulkCount');
  if (el) el.textContent = _selectedIdx.size + ' selected';
}

function selectAllToggle(checked) {
  const visible = filterEntries(_histAll, _histQuery, _histSourceFilter);
  visible.forEach(e => { if (checked) _selectedIdx.add(e.index); else _selectedIdx.delete(e.index); });
  renderHistory(visible);
}

async function deleteSelected() {
  if (!_selectedIdx.size) return;
  const items = _histAll.filter(e => _selectedIdx.has(e.index));
  const pinnedCount = items.filter(e => e.pinned).length;
  let msg = `Delete ${_selectedIdx.size} selected ${_selectedIdx.size === 1 ? 'entry' : 'entries'}? This cannot be undone.`;
  if (pinnedCount) msg = `${pinnedCount} of the selected entries are pinned. ` + msg;
  if (!confirm(msg)) return;
  const idxList = Array.from(_selectedIdx);
  await window.pywebview.api.delete_history_entries(idxList, idxList.map(stampFor));
  _selectedIdx.clear();
  await loadHistory();
}

async function exportSelected() {
  if (!_selectedIdx.size) return;
  const idxList = Array.from(_selectedIdx);
  const fmt = document.getElementById('exportFormatSel').value;
  try {
    const res = await window.pywebview.api.export_history_entries(idxList, fmt, idxList.map(stampFor));
    if (res && res.ok) {
      showToast(res.fallback ? 'Exported to ' + res.path : 'Exported to ' + res.path);
    } else if (res && res.cancelled) {
      // user cancelled the save dialog — no toast
    } else {
      showToast('Export failed' + (res && res.error ? ': ' + res.error : ''));
    }
  } catch (e) {
    showToast('Export failed: ' + e);
  }
}

function copyText(text) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.cssText = 'position:fixed;opacity:0;top:0;left:0';
  document.body.appendChild(ta);
  ta.select();
  document.execCommand('copy');
  document.body.removeChild(ta);
}

// ── Dictionary page ────────────────────────────────────────────────────────
async function loadDictionary() {
  const dict = await window.pywebview.api.get_dictionary();
  renderCorrections(dict.corrections);
  renderVocab(dict.vocabulary);
  const tog = document.querySelector('.tog[data-key="learn_from_edits"]');
  if (tog) setTogState(tog, dict.learn_from_edits !== false);
  loadPromotions();
}

function renderCorrections(corr) {
  const el = document.getElementById('corrList');
  const entries = Object.entries(corr || {});
  if (!entries.length) {
    el.innerHTML = emptyState(_ES_PENCIL_ICON, 'No corrections yet',
                              'Add a "heard, replace" pair below to fix words Whisper keeps mishearing.');
    return;
  }
  el.innerHTML = entries.map(([from, to]) => `
    <div class="corr-row">
      <span class="mono" style="flex:1;color:var(--mid);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(from)}</span>
      <span class="arrow">→</span>
      <span class="mono" style="flex:1;color:var(--text)">${esc(to)}</span>
      <button class="iby" title="Remove" aria-label="Remove correction for ${esc(from)}" onclick="removeCorrection(${JSON.stringify(from)})">${xIcon()}</button>
    </div>`).join('');
}

function renderVocab(vocab) {
  const el = document.getElementById('vocabList');
  const countEl = document.getElementById('vocabCount');
  if (countEl) countEl.textContent = 'Vocabulary' + (vocab && vocab.length ? ' · ' + vocab.length + ' terms' : '');
  if (!(vocab && vocab.length)) {
    el.innerHTML = emptyState(_ES_TAG_ICON, 'No custom vocabulary yet',
                              'Add names, jargon, or acronyms below so Whisper recognises them.');
    return;
  }
  el.innerHTML = vocab.map(w => `
    <span class="vchip">${esc(w)}
      <button class="x" title="Remove" aria-label="Remove ${esc(w)} from vocabulary" onclick="removeVocab(${JSON.stringify(w)})">${xIcon()}</button>
    </span>`).join('');
}

// ── Learned this week (P6 auto-learn dictionary, Agent B's promotions feed) ─
async function loadPromotions() {
  const el = document.getElementById('learnedList');
  if (!el) return;
  try {
    const items = await window.pywebview.api.get_promotions();
    if (!items || !items.length) {
      el.innerHTML = '<p class="sub">Nothing promoted yet. Fix the same word three times and it lands here.</p>';
      return;
    }
    el.innerHTML = items.map(p => `
      <div class="item"><div><div class="mono" style="font-size:11px;color:var(--text)">"${esc(p.raw)}" <span class="arrow">→</span> "${esc(p.fixed)}"</div>
      <div class="meta">${p.count} FIXES · PROMOTED ${esc(fmtPromotedDate(p.promoted_at))}</div></div></div>`).join('');
  } catch (e) {
    el.innerHTML = '<p class="sub">Nothing promoted yet. Fix the same word three times and it lands here.</p>';
  }
}

function fmtPromotedDate(iso) {
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    return d.toLocaleDateString(undefined, { weekday: 'long' }).toUpperCase();
  } catch (e) { return ''; }
}

async function addCorrection() {
  const from = document.getElementById('corrFrom').value.trim();
  const to = document.getElementById('corrTo').value.trim();
  if (!from || !to) return;
  await window.pywebview.api.set_correction(from, to);
  document.getElementById('corrFrom').value = '';
  document.getElementById('corrTo').value = '';
  loadDictionary();
}

async function removeCorrection(from) {
  await window.pywebview.api.remove_correction(from);
  loadDictionary();
}

async function addVocab() {
  const word = document.getElementById('vocabWord').value.trim();
  if (!word) return;
  await window.pywebview.api.add_vocabulary(word);
  document.getElementById('vocabWord').value = '';
  loadDictionary();
}

async function removeVocab(word) {
  await window.pywebview.api.remove_vocabulary(word);
  loadDictionary();
}

// ── Settings page ──────────────────────────────────────────────────────────
async function loadSettings() {
  const [cfg, devices] = await Promise.all([
    window.pywebview.api.get_config(),
    window.pywebview.api.get_audio_devices(),
  ]);

  // Populate mic dropdown
  const micSel = document.getElementById('micSelect');
  micSel.innerHTML = '<option value="">System default</option>';
  devices.forEach(d => {
    const opt = document.createElement('option');
    opt.value = d.index;
    opt.textContent = d.name;
    if (cfg.input_device === d.index) opt.selected = true;
    micSel.appendChild(opt);
  });

  // Text / number inputs
  document.querySelectorAll('[data-key]').forEach(el => {
    const key = el.dataset.key;
    if (key === '_paste_clipboard_only') return; // handled below
    if (el.classList.contains('tog')) return;    // handled below
    const val = cfg[key];
    if (val == null) return;
    if (el.dataset.type === 'array') {
      el.value = Array.isArray(val) ? val.join(', ') : val;
    } else if (el.dataset.type === 'lines') {
      el.value = Array.isArray(val) ? val.join('\\n') : val;
    } else {
      el.value = val;
    }
  });

  // Toggles. learn_from_edits defaults true (Agent B's config default may not
  // exist in an older config.json yet) — every other toggle here is fine
  // falling back to false/undefined.
  document.querySelectorAll('.tog[data-key]').forEach(tog => {
    const key = tog.dataset.key;
    let val = key === '_paste_clipboard_only' ? (cfg.paste_mode === 'clipboard_only')
      : key === 'learn_from_edits' ? (cfg.learn_from_edits !== false)
      : cfg[key];
    setTogState(tog, !!val);
  });

  // Theme select
  const themeSel = document.querySelector('select[data-key="theme"]');
  if (themeSel) themeSel.value = cfg.theme || 'dark';

  // Dashboard scale (item 79) — apply live, this subprocess window only
  applyScale(cfg.dashboard_scale || 100);

  // Sound volume live label
  const volRange = document.getElementById('soundVolumeRange');
  if (volRange) document.getElementById('soundVolumeLbl').textContent = volRange.value + '%';

  // Silence auto-stop live label (item 19)
  const silRange = document.getElementById('silenceAutoStopRange');
  if (silRange) document.getElementById('silenceAutoStopLbl').textContent = silenceAutoStopLabel(silRange.value);

  // Read-aloud rate labels — the sliders carry the value, these show it
  [['ttsSpeedRange', 'ttsSpeedLbl', 2], ['studySpeedRange', 'studySpeedLbl', 2],
   ['studyPauseRange', 'studyPauseLbl', 1]].forEach(([rangeId, lblId, dp]) => {
    const r = document.getElementById(rangeId);
    if (r) document.getElementById(lblId).textContent = Number(r.value).toFixed(dp) + 'x';
  });

  // Autostart state lives in Task Scheduler, not config
  try {
    const on = await window.pywebview.api.get_autostart();
    setTogState(document.getElementById('autostartTog'), !!on);
  } catch (e) {}
}

async function reloadSettings() { await loadSettings(); }

// ── Section reset-to-defaults (item 30) ────────────────────────────────────
// Mirrors main.py's _CONFIG_DEFAULTS values for the keys this dashboard
// exposes, section by section. Keys with no entry in _CONFIG_DEFAULTS
// (theme, animations, sound_volume, history_max_entries,
// recording_retention_days, dashboard_scale) are dashboard-only settings —
// their "factory" value is defined here instead.
// 'audio' and 'readaloud' sections moved to the Dictation and Voice pages
// (P2/P6) and dropped no reset button with them - the showcase design has
// no reset control on those pages either, mirroring that here.
const _SECTION_DEFAULTS = {
  appearance: {
    theme: 'dark', dashboard_scale: 100, badge_animation: 'waveform',
    preview_position: 'cursor', preview_auto_dismiss_seconds: 0,
    animations: true, sound_volume: 100,
  },
  recording: {
    min_record_seconds: 0.5, max_record_seconds: 120, vad_filter: false,
  },
  transcription: { language: 'en', filler_words: [], initial_prompt: '' },
  paste: {
    paste_mode: 'auto', clipboard_restore_delay_ms: 150,
    electron_paste_method: 'ctrl_v',
  },
  history: {
    history_max_entries: '100', recording_retention_days: '0',
  },
};
const _SECTION_LABELS = {
  appearance: 'Appearance', recording: 'Recording',
  transcription: 'Transcription', paste: 'Paste', history: 'History',
};

function applyDefaultsToForm(defaults) {
  Object.entries(defaults).forEach(([key, val]) => {
    if (key === 'paste_mode') {
      const tog = document.querySelector('.tog[data-key="_paste_clipboard_only"]');
      if (tog) setTogState(tog, val === 'clipboard_only');
      return;
    }
    if (key === 'input_device') {
      const sel = document.getElementById('micSelect');
      if (sel) sel.value = val == null ? '' : String(val);
      return;
    }
    document.querySelectorAll(`[data-key="${key}"]`).forEach(el => {
      if (el.classList.contains('tog')) { setTogState(el, !!val); return; }
      if (el.tagName === 'SELECT') { el.value = String(val); return; }
      if (el.dataset.type === 'array') { el.value = Array.isArray(val) ? val.join(', ') : val; return; }
      if (el.dataset.type === 'lines') { el.value = Array.isArray(val) ? val.join('\\n') : val; return; }
      el.value = val;
    });
    if (key === 'theme') applyTheme(val);
    if (key === 'dashboard_scale') applyScale(val);
    if (key === 'animations') setVoicelineMotion(val);
    if (key === 'sound_volume') {
      const lbl = document.getElementById('soundVolumeLbl');
      if (lbl) lbl.textContent = val + '%';
    }
    if (key === 'silence_auto_stop_seconds') {
      const lbl = document.getElementById('silenceAutoStopLbl');
      if (lbl) lbl.textContent = silenceAutoStopLabel(val);
    }
    const rateLbls = {
      tts_speed: ['ttsSpeedLbl', 2], study_speed: ['studySpeedLbl', 2],
      study_pause_scale: ['studyPauseLbl', 1],
    };
    if (rateLbls[key]) {
      const [lblId, dp] = rateLbls[key];
      const lbl = document.getElementById(lblId);
      if (lbl) lbl.textContent = Number(val).toFixed(dp) + 'x';
    }
  });
}

async function resetSection(id) {
  const defaults = _SECTION_DEFAULTS[id];
  if (!defaults) return;
  if (!confirm(`Reset ${_SECTION_LABELS[id]} settings to their defaults?`)) return;
  applyDefaultsToForm(defaults);
  const ok = await window.pywebview.api.save_settings(collectSettings());
  if (ok) {
    flashSaved();
    showToast(`${_SECTION_LABELS[id]} reset to defaults.`);
  } else {
    showToast('Could not save the reset. Try again.');
  }
}

// ── Import / export settings (item 76) ─────────────────────────────────────
async function exportSettings() {
  try {
    const res = await window.pywebview.api.export_settings();
    if (res && res.ok) {
      showToast('Exported to ' + res.path);
    } else if (res && res.cancelled) {
      // user cancelled the save dialog — no toast
    } else {
      showToast('Export failed' + (res && res.error ? ': ' + res.error : ''));
    }
  } catch (e) {
    showToast('Export failed: ' + e);
  }
}

async function importSettings() {
  try {
    const res = await window.pywebview.api.import_settings();
    if (res && res.ok) {
      await loadSettings();
      const cfg = await window.pywebview.api.get_config();
      applyTheme(cfg.theme || 'dark');
      showToast(`Imported ${res.imported_keys} setting${res.imported_keys === 1 ? '' : 's'}.`);
    } else if (res && res.cancelled) {
      // user cancelled the open dialog — no toast
    } else {
      showToast((res && res.error) || 'Import failed.');
    }
  } catch (e) {
    showToast('Import failed: ' + e);
  }
}

// ── About page ─────────────────────────────────────────────────────────────
async function loadAbout() {
  const about = await window.pywebview.api.get_about();
  document.getElementById('aboutVersion').textContent = 'Version ' + about.version;
  const el = document.getElementById('changelogList');
  if (!about.changelog || !about.changelog.length) {
    el.innerHTML = '<div class="empty" style="padding:8px 0">No changelog entries yet.</div>';
    return;
  }
  el.innerHTML = about.changelog.map(c => `
    <div style="margin-bottom:10px">
      <div style="color:var(--txt);font-weight:600">v${esc(c.version)} <span style="color:var(--txt3);font-weight:400">— ${esc(c.date)}</span></div>
      <ul style="margin:4px 0 0 16px;padding:0">
        ${c.notes.map(n => `<li style="margin-bottom:2px">${esc(n)}</li>`).join('')}
      </ul>
    </div>`).join('');
}

// ── Diagnostics page ───────────────────────────────────────────────────────
// Routed through window.pywebview.api, same as every other dashboard
// feature. The Python side (DashboardAPI.get_diagnostics etc.) makes the
// actual loopback call to api_server.py in the main app's process, since
// live audio/hotkey state lives there, not in this subprocess.
let _micProbeTimer = null;
let _micProbePeak = 0;

function _setProbe(stId, ok, warm) {
  const st = document.getElementById(stId);
  if (!st) return;
  st.textContent = ok ? '✓' : (warm ? '·' : '✕');
  st.className = 'st' + (ok ? '' : (warm ? ' warm' : ' bad'));
}

async function loadDiagnostics() {
  ['diagWhisperTxt', 'diagHotkeyTxt', 'diagMicTxt'].forEach(id => {
    document.getElementById(id).textContent = 'Checking…';
  });
  ['diagWhisperSt', 'diagHotkeySt', 'diagMicSt', 'diagNetSt'].forEach(id => _setProbe(id, false, true));
  ['diagWhisperVal', 'diagHotkeyVal', 'diagMicVal'].forEach(id => { document.getElementById(id).textContent = ''; });
  document.getElementById('diagNetVal').textContent = 'checking…';
  document.getElementById('sockWell').textContent = 'Checking…';

  try {
    const d = await window.pywebview.api.get_diagnostics();
    if (!d || d.reachable === false) renderDiagnosticsUnreachable();
    else renderDiagnostics(d);
  } catch (e) {
    renderDiagnosticsUnreachable();
  }
  refreshNetworkProbe();
}

function renderDiagnostics(d) {
  const w = d.whisper || {};
  _setProbe('diagWhisperSt', !!w.up);
  document.getElementById('diagWhisperTxt').textContent = w.up ? 'Whisper server' : 'Whisper server not responding';
  document.getElementById('diagWhisperVal').textContent = w.up
    ? `${w.model || 'unknown'} · ${w.device || 'unknown'}${w.latency_ms != null ? ' · ' + w.latency_ms + ' ms' : ''}`
    : '';

  const h = d.hotkey || {};
  _setProbe('diagHotkeySt', true);
  document.getElementById('diagHotkeyTxt').textContent = 'Hotkey hook';
  document.getElementById('diagHotkeyVal').textContent = `${h.combo || 'ctrl+alt'}${h.note ? ' · ' + h.note : ''}`;

  const m = d.mic || {};
  _setProbe('diagMicSt', !!m.device_exists);
  document.getElementById('diagMicTxt').textContent = 'Microphone';
  document.getElementById('diagMicVal').textContent = m.device_exists
    ? (m.device_name || 'Unknown device')
    : (m.device_name ? `${m.device_name} not found` : 'No microphone found');

  const btn = document.getElementById('micProbeBtn');
  if (btn && !_micProbeTimer) { btn.disabled = false; btn.title = ''; }
}

function renderDiagnosticsUnreachable() {
  const msg = "Quiett isn't running";
  ['diagWhisperSt', 'diagHotkeySt', 'diagMicSt'].forEach(id => _setProbe(id, false, true));
  document.getElementById('diagWhisperTxt').textContent = 'Whisper server';
  document.getElementById('diagWhisperVal').textContent = msg;
  document.getElementById('diagHotkeyTxt').textContent = 'Hotkey hook';
  document.getElementById('diagHotkeyVal').textContent = msg;
  document.getElementById('diagMicTxt').textContent = 'Microphone';
  document.getElementById('diagMicVal').textContent = msg;
  const btn = document.getElementById('micProbeBtn');
  if (btn) { btn.disabled = true; btn.title = 'Start Quiett first.'; }
}

// ── Network isolation probe (P7) — also feeds the header seal, one fetch
// serves both (Agent B's GET /diag/sockets on api_server.py). ──────────────
async function refreshNetworkProbe() {
  let d;
  try { d = await window.pywebview.api.get_network_probe(); } catch (e) { d = { reachable: false }; }
  _lastNetworkProbe = d;
  renderNetworkProbe(d);
  updateSeal(d);
  return d;
}

function renderNetworkProbe(d) {
  const well = document.getElementById('sockWell');
  const val = document.getElementById('diagNetVal');
  if (!well || !val) return;
  if (!d || d.reachable === false) {
    _setProbe('diagNetSt', false, true);
    val.textContent = "can't verify";
    well.textContent = "Quiett isn't running - start it to see the socket list.";
    return;
  }
  const sockets = d.sockets || [];
  const external = d.external || 0;
  _setProbe('diagNetSt', external === 0);
  val.textContent = external === 0 ? 'external connections: 0' : `${external} external connection${external === 1 ? '' : 's'}`;
  if (!sockets.length) {
    well.textContent = `external connections: ${external} - dictation works with the cable out`;
  } else {
    well.innerHTML = sockets.map(s => `<div><span class="sock-app">${esc(s.proc || '?')}</span> · ${esc(s.laddr || '')} · ${esc(s.state || '')}</div>`).join('')
      + `<div style="margin-top:6px;color:var(--dim)">external connections: ${external} - dictation works with the cable out</div>`;
  }
}

function updateSeal(d) {
  const seal = document.getElementById('seal');
  if (!seal) return;
  if (d && d.reachable !== false && (d.external || 0) === 0) {
    _networkSealed = true;
    seal.classList.add('lit');
    seal.title = 'Nothing leaves this machine';
  } else {
    _networkSealed = false;
    seal.classList.remove('lit');
    seal.title = (d && d.reachable === false) ? "Can't verify: Quiett isn't running" : 'Network isolation check unavailable';
  }
}

// ── The voice line — bottom status strip, backend status + seal state ──────
async function refreshVoiceline() {
  const el = document.getElementById('vlStatus');
  if (!el) return;
  try {
    const d = await window.pywebview.api.get_diagnostics();
    if (!d || d.reachable === false) { el.textContent = 'QUIETT NOT RUNNING'; return; }
    const w = d.whisper || {};
    const bits = [(w.model || 'WHISPER').toUpperCase(), (w.device || 'GPU').toUpperCase()];
    if (_networkSealed) bits.push('SEALED');
    el.textContent = bits.join(' · ');
  } catch (e) {
    el.textContent = 'QUIETT NOT RUNNING';
  }
}

// ── Voice page (P6) — orb identity, speed/study controls (moved from
// Settings), and the pause-plan visual rendered from REAL narration.py
// output on a fixed sample passage. ─────────────────────────────────────────
async function loadVoice() {
  const sub = document.getElementById('pausePlanSub');
  const el = document.getElementById('pauseplan');
  if (!sub || !el) return;
  sub.textContent = 'Loading the real plan from narration.py…';
  el.innerHTML = '';
  _pausePlanSegments = [];
  try {
    const res = await window.pywebview.api.get_pause_plan();
    if (!res || !res.ok) { sub.textContent = "Couldn't load the plan right now."; return; }
    _pausePlanSegments = res.segments;
    sub.textContent = `${res.segments.length} segment${res.segments.length === 1 ? '' : 's'}. Warm marks are deliberate pauses, planned from this passage's own structure.`;
    res.segments.forEach(seg => {
      const s = document.createElement('div');
      s.className = 'pp-seg';
      s.style.flex = Math.max(1, seg.chars);
      s.title = seg.text;
      el.appendChild(s);
      if (seg.pause > 0) {
        const g = document.createElement('div');
        g.className = 'pp-gap';
        g.style.width = (6 + seg.pause * 22) + 'px';
        const i = document.createElement('i');
        i.style.height = (8 + seg.pause * 22) + 'px';
        g.appendChild(i);
        el.appendChild(g);
      }
    });
  } catch (e) {
    sub.textContent = "Couldn't load the plan right now.";
  }
}

function playStudyPlan() {
  const segs = document.querySelectorAll('#pauseplan .pp-seg');
  if (!segs.length) return;
  segs.forEach(s => s.classList.remove('played'));
  let t = 100;
  _pausePlanSegments.forEach((seg, i) => {
    const el = segs[i];
    setTimeout(() => { if (el) el.classList.add('played'); }, t);
    t += 260 + seg.chars * 16 + seg.pause * 1000;
  });
}

async function playSample() {
  const btn = document.getElementById('playSampleBtn');
  if (btn) btn.disabled = true;
  try {
    const res = await window.pywebview.api.play_sample();
    if (res && res.ok) showToast('Playing sample…');
    else if (res && res.reason === 'off') showToast('Read-aloud is off.');
    else showToast('App not running.');
  } catch (e) {
    showToast('App not running.');
  } finally {
    if (btn) setTimeout(() => { btn.disabled = false; }, 800);
  }
}

function _resetMicMeter() {
  _micProbePeak = 0;
  const fill = document.getElementById('micMeterFill');
  const peak = document.getElementById('micMeterPeak');
  if (fill) fill.style.width = '0%';
  if (peak) peak.style.display = 'none';
}

async function startMicProbe() {
  const btn = document.getElementById('micProbeBtn');
  if (!btn || btn.disabled) return;
  btn.disabled = true;
  btn.textContent = 'Testing…';
  _resetMicMeter();

  let data;
  try {
    data = await window.pywebview.api.start_mic_probe();
  } catch (e) {
    data = null;
  }
  if (!data || data.reachable === false) {
    showToast("Couldn't reach Quiett to start the mic test.");
    btn.disabled = false;
    btn.textContent = 'Test mic';
    return;
  }
  if (data.error) {
    showToast(data.error);
    btn.disabled = false;
    btn.textContent = 'Test mic';
    return;
  }

  const endAt = Date.now() + ((data.duration_s || 5) * 1000);
  clearInterval(_micProbeTimer);
  _micProbeTimer = setInterval(async () => {
    if (Date.now() >= endAt) {
      clearInterval(_micProbeTimer);
      _micProbeTimer = null;
      btn.disabled = false;
      btn.textContent = 'Test mic';
      return;
    }
    try {
      const lvl = await window.pywebview.api.get_mic_level();
      if (!lvl || lvl.reachable === false) return; // transient, keep last-known level on screen
      const pct = Math.max(0, Math.min(100, Math.round((lvl.current_rms || 0) * 400)));
      document.getElementById('micMeterFill').style.width = pct + '%';
      const peakPct = Math.max(0, Math.min(100, Math.round((lvl.peak || 0) * 400)));
      if (peakPct >= _micProbePeak) {
        _micProbePeak = peakPct;
        const pk = document.getElementById('micMeterPeak');
        pk.style.left = peakPct + '%';
        pk.style.display = 'block';
      }
    } catch (e) { /* transient, keep last-known level on screen */ }
  }, 150);
}

// Keeps the on/off class and the aria-checked state of a switch-role toggle
// in sync — every place that flips a .tog goes through this (item 80).
function setTogState(el, on) {
  el.classList.toggle('on', !!on);
  el.setAttribute('aria-checked', String(!!on));
}

function togClick(el) {
  setTogState(el, !el.classList.contains('on'));
  if (el.dataset.key === 'animations') setVoicelineMotion(el.classList.contains('on'));
  autoSave();
}

// Enter/Space activates a .tog the same way a click does (item 80) — the
// toggle is a div with role="switch", not a native control, so it needs its
// own key handling. Reuses whichever onclick handler is already on the
// element (togClick, autostartClick, ...).
function togKeydown(e, el) {
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault();
    el.click();
  }
}

// ── Dashboard scale (item 79) — this window only, not the Tk preview panel ─
function applyScale(pct) {
  const n = parseInt(pct, 10) || 100;
  document.documentElement.style.zoom = (n / 100);
}
function applyScaleFromSelect(val) { applyScale(val); }

// ── Silence auto-stop slider label (item 19) ────────────────────────────────
function silenceAutoStopLabel(v) {
  const n = parseFloat(v);
  return n === 0 ? 'Off' : n.toFixed(1) + 's';
}

// ── Voice line idle waveform — rAF, respects the animations setting AND OS
// reduce-motion (P2); a flat static line otherwise. ─────────────────────────
let _voicelineAnimEnabled = true;
let _voicelineRafOn = false;

function _voicelineFrame(poly, t0, now) {
  if (!_voicelineRafOn) return;
  const t = (now - t0) * 0.0025;
  const pts = [];
  for (let x = 0; x <= 74; x += 2) {
    const y = 8 + Math.sin(t + x * 0.24) * Math.sin(t * 0.7 + x * 0.05) * 3.2;
    pts.push(x + ',' + y.toFixed(1));
  }
  poly.setAttribute('points', pts.join(' '));
  requestAnimationFrame((n) => _voicelineFrame(poly, t0, n));
}

function setVoicelineMotion(enabled) {
  const poly = document.getElementById('vlPoly');
  if (!poly) return;
  const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;
  _voicelineAnimEnabled = !!enabled && !reduceMotion;
  if (_voicelineAnimEnabled) {
    if (!_voicelineRafOn) {
      _voicelineRafOn = true;
      requestAnimationFrame((n) => _voicelineFrame(poly, n, n));
    }
  } else {
    _voicelineRafOn = false;
    poly.setAttribute('points', '0,8 74,8');
  }
}

// ── Instant apply — settings persist on change, no Save button ─────────────
let _autoSaveT = null;
function autoSave() {
  clearTimeout(_autoSaveT);
  _autoSaveT = setTimeout(async () => {
    try {
      await window.pywebview.api.save_settings(collectSettings());
      flashSaved();
    } catch (e) { console.error('autosave failed:', e); }
  }, 450);
}

function flashSaved() {
  const ok = document.getElementById('saveOk');
  ok.classList.add('show');
  clearTimeout(window._okT);
  window._okT = setTimeout(() => ok.classList.remove('show'), 1600);
}

async function autostartClick(el) {
  const next = !el.classList.contains('on');
  setTogState(el, next);
  try {
    await window.pywebview.api.set_autostart(next);
    flashSaved();
  } catch (e) {
    setTogState(el, !next);  // revert on failure
  }
}

function collectSettings() {
  const data = {};
  document.querySelectorAll('[data-key]').forEach(el => {
    const key = el.dataset.key;
    if (el.classList.contains('tog')) {
      const isOn = el.classList.contains('on');
      if (key === '_paste_clipboard_only') {
        data['paste_mode'] = isOn ? 'clipboard_only' : 'auto';
      } else {
        data[key] = isOn;
      }
      return;
    }
    if (el.tagName === 'SELECT') {
      if (key === 'input_device') {
        data[key] = el.value === '' ? null : parseInt(el.value);
      } else {
        data[key] = el.value;
      }
      return;
    }
    if (el.dataset.type === 'array') {
      data[key] = el.value.split(',').map(s => s.trim()).filter(Boolean);
      return;
    }
    if (el.dataset.type === 'lines') {
      data[key] = el.value.split('\\n').map(s => s.trim()).filter(Boolean);
      return;
    }
    if (el.dataset.type === 'int') { data[key] = parseInt(el.value) || 0; return; }
    if (el.type === 'number' || el.type === 'range') { data[key] = parseFloat(el.value) || 0; return; }
    data[key] = el.value;
  });
  return data;
}


// ── Init ───────────────────────────────────────────────────────────────────
window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
  if (_themePref === 'system') applyTheme('system');
});

// Escape exits History's bulk select mode (item 80) — wherever focus is on
// the page, not just when a specific control is focused.
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && _currentPage === 'history' && _selectMode) {
    toggleSelectMode();
  }
});

// Boot must survive both orders: script-then-bridge (pywebviewready fires
// later) and bridge-then-script (the event already fired and will never
// come again). A bare listener misses the second case - the whole init
// silently never runs. Guard + listener + slow poll fallback, run once.
let _booted = false;
async function _boot() {
  if (_booted) return;
  _booted = true;
  try {
    const cfg = await window.pywebview.api.get_config();
    applyTheme(cfg.theme || 'dark');
    applyScale(cfg.dashboard_scale || 100);
    setVoicelineMotion(cfg.animations !== false);

    // The page the user asked for goes first, and everything else runs behind
    // it: awaiting settings/about/history-stamp before the first paint is what
    // made opening the dashboard feel slow.
    if (_INIT_PAGE === 'home') loadHome();
    else navigateTo(_INIT_PAGE);

    // Settings/Dictation/Voice controls all share the same data-key config
    // system now, spread across three pages instead of one - hydrate every
    // data-key element document-wide up front, regardless of which page is
    // showing, then autosave on change wherever it fires. Not awaited any more,
    // but the autosave listeners still go on only once hydration is done: an
    // edit before that would save every other control's empty value.
    loadSettings().then(() => {
      document.addEventListener('input', (e) => {
        if (e.target.closest && e.target.closest('[data-key]')) autoSave();
      });
      document.addEventListener('change', (e) => {
        if (e.target.closest && e.target.closest('[data-key]')) autoSave();
      });
    }).catch(() => {});

    window.pywebview.api.get_about().then((about) => {
      const ver = document.getElementById('verTag');
      if (ver) ver.textContent = 'v' + about.version;
    }).catch(() => {});

    refreshVoiceline();
    setInterval(refreshVoiceline, 15000);
    // The socket probe is a 2s HTTP call against the main app. Off the boot
    // path entirely, and after that only while Diagnostics is on screen - the
    // first run still seeds the header seal.
    setTimeout(refreshNetworkProbe, 1500);
    setInterval(() => { if (_currentPage === 'diagnostics') refreshNetworkProbe(); }, 20000);
    window.pywebview.api.get_history_stamp().then((s) => { _histStamp = s; }).catch(() => {});
    setInterval(pollHistory, 4000);
  } catch (e) {
    console.error('init error:', e);
    var vl = document.getElementById('vlStatus');
    if (vl) vl.textContent = 'INIT ERR: ' + String(e && e.message || e).slice(0, 160);
  }
}
window.onerror = function (msg, src, line) {
  var vl = document.getElementById('vlStatus');
  if (vl) vl.textContent = 'JS ERR L' + line + ': ' + String(msg).slice(0, 150);
};
if (window.pywebview && window.pywebview.api) { _boot(); }
window.addEventListener('pywebviewready', _boot);
(function () {
  let tries = 0;
  const iv = setInterval(function () {
    if (window.pywebview && window.pywebview.api) { clearInterval(iv); _boot(); }
    else if (++tries > 100) { clearInterval(iv); }
  }, 100);
})();
</script>
</body>
</html>"""

# Tokens come from theme.py (single source of truth shared with the Tk panel
# and tray) - substituted once at import time, not per page-build, since the
# palette only changes when theme.py itself changes.
_HTML = (_HTML
         .replace("__ROOT_VARS_DARK__", theme.css_vars(theme.DARK))
         .replace("__ROOT_VARS_LIGHT__", theme.css_vars(theme.LIGHT))
         .replace("__AURORA_DARK__", theme.DARK["aurora"])
         .replace("__AURORA_LIGHT__", theme.LIGHT["aurora"]))


# ── Subprocess entrypoint ──────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        import ctypes as _ct
        _ct.windll.user32.SetProcessDpiAwarenessContext(_ct.c_ssize_t(-4))
    except Exception:
        pass
    _args = sys.argv[1:]
    _hidden = "--hidden" in _args              # prewarm: boot, then wait unseen
    _positional = [a for a in _args if not a.startswith("-")]
    _page = _positional[0] if _positional else "home"

    # Single instance. Binding the control port is the lock: if it's taken,
    # a dashboard is already up — hand it the page and quit rather than
    # opening a second window over the top of it.
    _srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _srv.bind(("127.0.0.1", _control_port()))
        _srv.listen(8)
    except OSError:
        _srv.close()
        if not _hidden:
            _send_control({"cmd": "show", "page": _page})
        sys.exit(0)
    _perf(f"spawn->bind {(time.monotonic() - _T0) * 1000:.0f}ms hidden={_hidden}")

    _theme_pref = _read_cfg().get("theme", "dark")
    if _theme_pref == "system":
        try:
            import winreg
            with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as _k:
                _theme = "light" if winreg.QueryValueEx(_k, "AppsUseLightTheme")[0] else "dark"
        except Exception:
            _theme = "dark"
    else:
        _theme = _theme_pref
    # Inject the initial page before pywebviewready fires — avoids the race
    # between window.shown (too early) and the JS api being available.
    _html = _HTML.replace("let _INIT_PAGE = 'home';", f"let _INIT_PAGE = '{_page}';")
    # Same trick for the theme — without it light users get a dark first paint
    # and a dark window background behind every resize.
    _html = _html.replace("let _theme = 'dark';", f"let _theme = '{_theme}';")
    _html = _html.replace('<html lang="en">', f'<html lang="en" data-theme="{_theme}">')
    _geom = _load_window_geom()
    _win_kwargs = dict(
        title="Quiett",
        html=_html,
        js_api=DashboardAPI(),
        min_size=(_MIN_W, _MIN_H),
        background_color="#ececef" if _theme == "light" else "#202020",
    )
    if _geom:
        _win_kwargs.update(x=_geom["x"], y=_geom["y"], width=_geom["w"], height=_geom["h"])
    else:
        _win_kwargs.update(width=980, height=660)
    if _hidden:
        _win_kwargs.update(hidden=True)
    _w = webview.create_window(**_win_kwargs)

    def _on_shown() -> None:
        _apply_titlebar_theme(_theme != "light")
        _perf(f"bind->shown {(time.monotonic() - _T0) * 1000:.0f}ms total")

    _w.events.shown += _on_shown

    # Hide on close instead of exiting. The process (and with it the control
    # port and the loaded WebView2) stays resident, so every later open is the
    # warm sub-second path. Only the quit command really tears it down.
    _quitting = threading.Event()

    def _on_closing() -> bool:
        if _quitting.is_set():
            return True
        threading.Timer(0, _w.hide).start()
        return False

    _w.events.closing += _on_closing

    # Page-interactive gate for the control channel. `loaded` fires when the
    # document is ready; the JS api itself is up a beat later, so the handler
    # tolerates a miss rather than assuming.
    _js_ready = threading.Event()
    _w.events.loaded += lambda: _js_ready.set()
    threading.Thread(target=_serve_control, args=(_srv, _w, _js_ready, _quitting),
                     daemon=True).start()
    # History pruning is a file-locked write that used to block the window
    # build; nothing on screen needs it to have finished.
    threading.Thread(target=_purge_history_quietly, daemon=True).start()
    # Debounced geometry save on move/resize — avoids hammering config.json
    # while the user is mid-drag.
    _geom_timer: "threading.Timer | None" = None
    _geom_timer_lock = threading.Lock()

    def _schedule_geom_save(*_args):
        global _geom_timer
        with _geom_timer_lock:
            if _geom_timer is not None:
                _geom_timer.cancel()
            _geom_timer = threading.Timer(0.6, _save_window_geom, args=(_w,))
            _geom_timer.daemon = True
            _geom_timer.start()

    _w.events.moved += _schedule_geom_save
    _w.events.resized += _schedule_geom_save
    webview.start(debug=False, gui="edgechromium")
