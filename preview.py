"""
Tkinter preview window + history viewer + speech profile viewer,
running on a dedicated worker thread.

pystray owns the main thread, so we spin up a persistent hidden Tk root here.
Queues are polled every 50 ms so all windows can coexist simultaneously.
"""

import json
import queue
import threading
import time
import tkinter as tk
from datetime import date, datetime

import pyperclip
import win32api

import audio
import history as hist
import inject
import profile

_CONFIG_FILE = "config.json"

# ---------------------------------------------------------------------------
# Public API — safe to call from any thread
# ---------------------------------------------------------------------------

_preview_q:   queue.Queue = queue.Queue()
_history_q:   queue.Queue = queue.Queue()
_profile_q:   queue.Queue = queue.Queue()
_settings_q:  queue.Queue = queue.Queue()
_badge_q:     queue.Queue = queue.Queue()
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

# Dark-theme palette
_BG      = "#1e1e1e"
_BG2     = "#2d2d2d"
_FG      = "#ffffff"
_FG2     = "#aaaaaa"
_BLUE    = "#0078d4"
_BLUE_HV = "#1a8ae0"
_BORDER  = "#444444"


def start() -> None:
    """Spawn the tkinter worker thread and block until its root is live."""
    t = threading.Thread(target=_tk_main, daemon=True)
    t.start()
    _ready.wait()


def show(text: str, hwnd: int, empty: bool = False,
         confidence: float | None = None,
         words: list | None = None,
         auto_dismiss: float = 0.0,
         raw: str | None = None) -> None:
    """Queue a dictation preview window. raw= is the pre-vibe-mode Whisper text."""
    _preview_q.put({"text": text, "hwnd": hwnd, "empty": empty,
                    "confidence": confidence, "words": words,
                    "auto_dismiss": auto_dismiss, "raw": raw})


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


# ---------------------------------------------------------------------------
# Internal — everything below runs exclusively on the tkinter worker thread
# ---------------------------------------------------------------------------

def _tk_main() -> None:
    global _root
    _root = tk.Tk()
    _root.withdraw()
    _ready.set()
    _root.after(50, _tick)
    _root.mainloop()


_badge_win:   tk.Toplevel | None = None
_badge_label: tk.Label | None = None
_badge_dot:   tk.Label | None = None
_badge_state: str | None = None


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
                )
            except Exception as e:
                print(f"preview window error: {e}")
                _preview_open = False

    # Live-update badge text when recording
    if _badge_state == "recording" and _badge_alive():
        try:
            elapsed  = int(audio.get_elapsed())
            silence  = audio.get_silence_elapsed()
            timeout  = audio.get_silence_timeout()
            if timeout > 0 and silence > 1.5:
                remaining = max(0.0, timeout - silence)
                new_text = f"Silence... {remaining:.0f}s"
                new_dot  = "#c8a000"
            else:
                new_text = f"Recording... {elapsed}s"
                new_dot  = "#e03030"
            if _badge_label and _badge_label.cget("text") != new_text:
                _badge_label.config(text=new_text)
            if _badge_dot and _badge_dot.cget("fg") != new_dot:
                _badge_dot.config(fg=new_dot)
        except Exception:
            pass

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
        _open_history()

    profile_requested = False
    while True:
        try:
            _profile_q.get_nowait()
            profile_requested = True
        except queue.Empty:
            break
    if profile_requested and not _profile_open:
        _profile_open = True
        _open_profile()

    settings_requested = False
    while True:
        try:
            _settings_q.get_nowait()
            settings_requested = True
        except queue.Empty:
            break
    if settings_requested and not _settings_open:
        _settings_open = True
        _open_settings()

    # Drain the entire badge queue each tick so a late "processing" item
    # cannot reappear after a None already cleared the badge.
    while True:
        try:
            badge_cmd = _badge_q.get_nowait()
            _handle_badge(badge_cmd)
        except queue.Empty:
            break

    _root.after(50, _tick)


# ---------------------------------------------------------------------------
# Status badge (recording / processing / feedback)
# ---------------------------------------------------------------------------

_BADGE_CFG = {
    "recording":    {"dot": "#e03030", "text": "Recording..."},
    "processing":   {"dot": "#c8a000", "text": "Transcribing..."},
    "reformatting": {"dot": "#7b5ea7", "text": "Structuring prompt..."},
    "too_short":    {"dot": "#888888", "text": "Hold longer to record"},
    "not_ready":    {"dot": "#888888", "text": "Model loading — please wait"},
}


def _badge_alive() -> bool:
    """Return True only if _badge_win is a live Tkinter window."""
    global _badge_win, _badge_label, _badge_dot
    if _badge_win is None:
        return False
    try:
        _badge_win.winfo_exists()  # raises TclError if already destroyed
        return True
    except Exception:
        _badge_win = None
        _badge_label = None
        _badge_dot = None
        return False


def _handle_badge(cmd: str | None) -> None:
    global _badge_win, _badge_label, _badge_dot, _badge_state
    _badge_state = cmd

    if cmd is None:
        if _badge_alive():
            try:
                _badge_win.destroy()
            except Exception:
                pass
            _badge_win = None
            _badge_label = None
            _badge_dot = None
        return

    cfg = _BADGE_CFG.get(cmd, _BADGE_CFG["processing"])

    if not _badge_alive():
        _badge_win = tk.Toplevel(_root)
        _badge_win.overrideredirect(True)
        _badge_win.attributes("-topmost", True)
        _badge_win.configure(bg=_BG2)
        _badge_win.attributes("-alpha", 0.92)

        inner = tk.Frame(_badge_win, bg=_BG2, padx=10, pady=6)
        inner.pack()

        _badge_dot = tk.Label(inner, text="●", bg=_BG2,
                              font=("Segoe UI", 9), fg=cfg["dot"])
        _badge_dot.pack(side=tk.LEFT, padx=(0, 5))

        _badge_label = tk.Label(inner, text=cfg["text"], bg=_BG2,
                                fg=_FG, font=("Segoe UI", 9))
        _badge_label.pack(side=tk.LEFT)

        _badge_win.update_idletasks()
        w = _badge_win.winfo_reqwidth()
        h = _badge_win.winfo_reqheight()
        sw = _badge_win.winfo_screenwidth()
        sh = _badge_win.winfo_screenheight()
        _badge_win.geometry(f"{w}x{h}+{sw - w - 16}+{sh - h - 56}")
    else:
        try:
            if _badge_dot:
                _badge_dot.config(fg=cfg["dot"])
            if _badge_label:
                _badge_label.config(text=cfg["text"])
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


def _border_colour(confidence: float | None) -> str:
    if confidence is None:
        return "#3d3d3d"
    if confidence >= 0.6:
        return _BLUE       # confident — blue
    if confidence >= 0.3:
        return "#c8a000"   # uncertain — amber
    return "#cc4400"       # low confidence — orange-red


def _open_window(text: str, hwnd: int, empty: bool = False,
                 confidence: float | None = None,
                 auto_dismiss: float = 0.0,
                 words: list | None = None,
                 raw: str | None = None) -> None:
    global _current_preview_win

    try:
        cx, cy = win32api.GetCursorPos()
    except Exception:
        cx, cy = 200, 200

    win = tk.Toplevel(_root)
    _current_preview_win = win
    win.overrideredirect(True)
    win.configure(bg=_BG)
    win.attributes("-topmost", True)

    frame = tk.Frame(win, bg=_BG, padx=16, pady=14)
    frame.pack(fill=tk.BOTH, expand=True)

    # ── Text entry ─────────────────────────────────────────────────────────
    display = "Nothing detected — try again" if empty else text
    entry = tk.Text(
        frame, font=("Segoe UI", 11), wrap=tk.WORD,
        bg=_BG2, fg=_FG, insertbackground=_FG,
        relief="flat", bd=0,
        highlightthickness=1,
        highlightbackground=_border_colour(confidence),
        highlightcolor=_BLUE,
        height=4,
        undo=True,
    )
    entry.insert("1.0", display)
    entry.pack(fill=tk.X, pady=(0, 3))
    if not empty:
        entry.tag_add("sel", "1.0", "end-1c")
        entry.mark_set(tk.INSERT, tk.END)
    entry.focus_set()

    # Word-level confidence highlighting (low-prob words tinted red/amber)
    if words and not empty:
        entry.tag_configure("conf_low",    foreground="#ff6b6b")
        entry.tag_configure("conf_medium", foreground="#e8c547")
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
                font=("Segoe UI", 8), anchor="w",
            ).pack(fill=tk.X, pady=(0, 2))

    # ── Word + char count ──────────────────────────────────────────────────
    count_var = tk.StringVar()
    tk.Label(
        frame, textvariable=count_var,
        bg=_BG, fg="#666666", font=("Segoe UI", 8), anchor="w",
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
        font=("Segoe UI", 9),
    ).pack(anchor=tk.W, pady=(0, 10))

    # ── Buttons ────────────────────────────────────────────────────────────
    btns = tk.Frame(frame, bg=_BG)
    btns.pack(anchor=tk.W)

    def _close() -> None:
        global _preview_open, _current_preview_win
        _preview_open = False
        _current_preview_win = None
        try:
            win.destroy()
        except Exception:
            pass

    def on_insert() -> None:
        result = entry.get("1.0", "end-1c").rstrip()
        if not empty:
            original = text.rstrip()
            if original != result:
                try:
                    profile.log_correction(original, result)
                except Exception:
                    pass
        # Prime focus on target BEFORE closing preview — while we still own the foreground,
        # SetForegroundWindow is guaranteed to succeed. Closing first creates a vacuum where
        # Windows blocks the call (anti-focus-steal protection).
        inject.prime_foreground(hwnd)
        _close()
        to_paste = (" " + result) if append_var.get() else result
        threading.Thread(target=inject.inject_text, args=(to_paste, hwnd), daemon=True).start()

    def on_cancel() -> None:
        _close()

    insert_btn = tk.Button(
        btns, text="Insert", command=on_insert, width=10,
        bg=_BLUE, fg=_FG,
        activebackground=_BLUE_HV, activeforeground=_FG,
        relief="flat", bd=0,
        font=("Segoe UI", 10), padx=6, pady=4, cursor="hand2",
    )
    if empty:
        insert_btn.config(state="disabled", bg="#404040", fg="#666666", cursor="")
    else:
        insert_btn.bind("<Enter>", lambda e: insert_btn.config(bg=_BLUE_HV))
        insert_btn.bind("<Leave>", lambda e: insert_btn.config(bg=_BLUE))
    insert_btn.pack(side=tk.LEFT)

    if empty:
        # "Try Again" replaces Cancel in empty state
        try_btn = tk.Button(
            btns, text="Try Again", command=on_cancel, width=10,
            bg=_BG2, fg=_FG,
            activebackground=_BORDER, activeforeground=_FG,
            relief="flat", bd=1,
            font=("Segoe UI", 10), padx=6, pady=4, cursor="hand2",
        )
        try_btn.pack(side=tk.LEFT, padx=(8, 0))
    else:
        cancel_wrap = tk.Frame(btns, bg=_BORDER, padx=1, pady=1)
        tk.Button(
            cancel_wrap, text="Cancel", command=on_cancel, width=10,
            bg=_BG, fg=_FG2,
            activebackground=_BG2, activeforeground=_FG,
            relief="flat", bd=0,
            font=("Segoe UI", 10), padx=6, pady=4, cursor="hand2",
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

        toggle_btn = tk.Button(
            frame, text="Raw", command=_toggle_raw,
            bg=_BG2, fg=_FG2,
            activebackground=_BORDER, activeforeground=_FG,
            relief="flat", bd=1,
            font=("Segoe UI", 8), padx=6, pady=2, cursor="hand2",
        )
        toggle_btn.pack(anchor=tk.E, pady=(4, 0))

    # ── Keyboard hint strip ────────────────────────────────────────────────
    hint = "↵ / Ins Insert  ·  Esc Cancel  ·  Ctrl+R Re-record  ·  Ctrl+Z Undo  ·  Shift+↵ Newline"
    tk.Label(
        frame, text=hint,
        bg=_BG, fg="#555555", font=("Segoe UI", 8), anchor="w",
    ).pack(fill=tk.X, pady=(8, 0))

    # ── Keybindings ────────────────────────────────────────────────────────
    if not empty:
        def _on_return(e):
            on_insert()
            return "break"
        entry.bind("<Return>",  _on_return)
        entry.bind("<Insert>",  _on_return)   # Insert key also pastes
        entry.bind("<Shift-Return>", lambda e: None)  # allow literal newline

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


def _open_history() -> None:
    entries = hist.load()

    win = tk.Toplevel(_root)
    win.title("VoiceDictate — History")
    win.configure(bg=_BG)
    win.geometry("540x420")
    win.minsize(400, 200)

    tk.Label(
        win, text="Dictation History",
        bg=_BG, fg=_FG, font=("Segoe UI", 13, "bold"),
    ).pack(anchor="w", padx=16, pady=(14, 8))

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

    txt.tag_configure("ts",   foreground="#888888", font=("Segoe UI", 9))
    txt.tag_configure("body", foreground=_FG,       font=("Segoe UI", 10))
    txt.tag_configure("sep",  foreground="#383838")
    txt.tag_configure("copy", foreground=_FG2,      font=("Segoe UI", 8), underline=True)

    def _populate(data: list) -> None:
        txt.config(state="normal")
        txt.delete("1.0", tk.END)
        if data:
            for i, entry in enumerate(data):
                body_text = entry.get("text", "").strip()
                txt.insert(tk.END, _fmt_ts(entry.get("timestamp", "")) + "\n", "ts")
                txt.insert(tk.END, body_text + "\n", "body")
                tag = f"copy_{i}"
                txt.insert(tk.END, "· copy\n", ("copy", tag))
                txt.tag_bind(tag, "<Button-1>",
                             lambda e, t=body_text: pyperclip.copy(t))
                txt.tag_bind(tag, "<Enter>", lambda e: txt.config(cursor="hand2"))
                txt.tag_bind(tag, "<Leave>", lambda e: txt.config(cursor="ibeam"))
                if i < len(data) - 1:
                    txt.insert(tk.END, "─" * 55 + "\n", "sep")
        else:
            txt.insert(tk.END, "No dictation history yet.", "ts")
        txt.config(state="disabled")

    _populate(entries)

    bar = tk.Frame(win, bg=_BG)
    bar.pack(fill=tk.X, padx=16, pady=(0, 12))

    def on_clear() -> None:
        hist.clear()
        _populate([])

    def on_close() -> None:
        global _history_open
        _history_open = False
        win.destroy()

    clear_wrap = tk.Frame(bar, bg=_BORDER, padx=1, pady=1)
    tk.Button(
        clear_wrap, text="Clear History", command=on_clear,
        bg=_BG, fg=_FG2,
        activebackground=_BG2, activeforeground=_FG,
        relief="flat", bd=0,
        font=("Segoe UI", 10), padx=10, pady=4, cursor="hand2",
    ).pack()
    clear_wrap.pack(side=tk.LEFT)

    win.protocol("WM_DELETE_WINDOW", on_close)
    win.bind("<Escape>", lambda _: on_close())


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
        bg=_BG, fg="#888888", font=("Segoe UI", 8),
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
    txt.tag_configure("pending", foreground="#666666",  font=("Segoe UI", 10))
    txt.tag_configure("count",   foreground="#888888",  font=("Segoe UI", 9))
    txt.tag_configure("del",     foreground="#cc4444",  font=("Segoe UI", 8), underline=True)
    txt.tag_configure("sep",     foreground="#383838")
    txt.tag_configure("empty",   foreground="#888888",  font=("Segoe UI", 10))

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
        win.destroy()

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
    win.geometry("520x780")
    win.minsize(440, 520)
    win.resizable(True, True)

    canvas = tk.Canvas(win, bg=_BG, highlightthickness=0)
    sb = tk.Scrollbar(win, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=sb.set)
    sb.pack(side=tk.RIGHT, fill=tk.Y)
    canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    content = tk.Frame(canvas, bg=_BG)
    content_window = canvas.create_window((0, 0), window=content, anchor="nw")

    def _on_configure(e):
        canvas.configure(scrollregion=canvas.bbox("all"))
        canvas.itemconfig(content_window, width=canvas.winfo_width())

    content.bind("<Configure>", _on_configure)
    canvas.bind("<Configure>", _on_configure)

    def _section(label: str) -> None:
        tk.Label(content, text=label, bg=_BG, fg=_FG,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=16, pady=(12, 0))

    def _note(text: str) -> None:
        tk.Label(content, text=text, bg=_BG, fg="#888888",
                 font=("Segoe UI", 8)).pack(anchor="w", padx=16, pady=(0, 4))

    def _field(label: str, var) -> None:
        tk.Label(content, text=label, bg=_BG, fg=_FG2,
                 font=("Segoe UI", 9)).pack(anchor="w", padx=16, pady=(6, 0))
        tk.Entry(content, textvariable=var, bg=_BG2, fg=_FG,
                 insertbackground=_FG, relief="flat", bd=0,
                 highlightthickness=1, highlightbackground="#3d3d3d",
                 font=("Segoe UI", 10)).pack(fill=tk.X, padx=16, pady=(2, 0))

    tk.Label(content, text="Settings", bg=_BG, fg=_FG,
             font=("Segoe UI", 13, "bold")).pack(anchor="w", padx=16, pady=(14, 0))
    _note("Model and Hotkey changes require a restart.")

    _section("Hotkey")
    hotkey_var = tk.StringVar(value=cfg.get("hotkey", "ctrl+alt"))
    _field("Hotkey combo (e.g. ctrl+alt, ctrl+shift, alt+space)", hotkey_var)
    _note("Hot-reloads on save. Use lowercase modifier names joined with '+'.")

    _section("Transcription")
    lang_var = tk.StringVar(value=cfg.get("language", "en"))
    _field("Language (e.g. en, fr, es)", lang_var)
    model_var = tk.StringVar(value=cfg.get("model", "small"))
    _field("Model (tiny / base / small / medium / large)  — restart required", model_var)

    _section("Whisper biasing")
    _note("initial_prompt seeds Whisper with context — improves accuracy for domain terms.")
    prompt_txt = tk.Text(content, bg=_BG2, fg=_FG, insertbackground=_FG,
                         relief="flat", bd=0, highlightthickness=1,
                         highlightbackground="#3d3d3d",
                         font=("Segoe UI", 10), height=3, undo=True)
    prompt_txt.insert("1.0", cfg.get("initial_prompt", "") or "")
    prompt_txt.pack(fill=tk.X, padx=16, pady=(2, 0))

    _note("Custom vocabulary — one term per line. Fed into initial_prompt as a hint.")
    vocab_txt = tk.Text(content, bg=_BG2, fg=_FG, insertbackground=_FG,
                        relief="flat", bd=0, highlightthickness=1,
                        highlightbackground="#3d3d3d",
                        font=("Segoe UI", 10), height=4, undo=True)
    vocab_txt.insert("1.0", "\n".join(cfg.get("custom_vocabulary", []) or []))
    vocab_txt.pack(fill=tk.X, padx=16, pady=(2, 0))

    _section("Recording")
    max_var = tk.StringVar(value=str(cfg.get("max_record_seconds", 120)))
    _field("Max recording time (seconds)", max_var)
    silence_var = tk.StringVar(value=str(cfg.get("silence_auto_stop_seconds", 3)))
    _field("Silence auto-stop (seconds, 0 = disabled)", silence_var)
    sthresh_var = tk.StringVar(value=str(cfg.get("silence_threshold", 0.01)))
    _field("Silence threshold (RMS, 0.001-0.5, lower = more sensitive)", sthresh_var)
    vad_var = tk.BooleanVar(value=bool(cfg.get("vad_filter", False)))
    tk.Checkbutton(content, text="VAD filter (suppress background noise)",
                   variable=vad_var, bg=_BG, fg=_FG2,
                   activebackground=_BG, activeforeground=_FG,
                   selectcolor=_BG2, font=("Segoe UI", 9)
                   ).pack(anchor="w", padx=16, pady=(10, 0))

    # ── Microphone dropdown ────────────────────────────────────────────────
    _section("Microphone")
    try:
        devices = audio.list_input_devices()
    except Exception:
        devices = []
    dev_labels = ["System default"] + [f"[{d['index']}] {d['name']}" for d in devices]
    current_dev = cfg.get("input_device")
    if current_dev is None:
        current_label = "System default"
    else:
        match = next((f"[{d['index']}] {d['name']}" for d in devices
                      if d["index"] == current_dev), "System default")
        current_label = match
    mic_var = tk.StringVar(value=current_label)
    mic_om = tk.OptionMenu(content, mic_var, *dev_labels)
    mic_om.config(bg=_BG2, fg=_FG, activebackground=_BORDER,
                  relief="flat", highlightthickness=0, font=("Segoe UI", 10))
    mic_om.pack(anchor="w", padx=16, pady=(2, 0), fill=tk.X)

    # ── Privacy ────────────────────────────────────────────────────────────
    _section("Privacy")
    history_paused_var = tk.BooleanVar(value=bool(cfg.get("history_paused", False)))
    tk.Checkbutton(content, text="Pause history — don't save transcriptions",
                   variable=history_paused_var, bg=_BG, fg=_FG2,
                   activebackground=_BG, activeforeground=_FG,
                   selectcolor=_BG2, font=("Segoe UI", 9)
                   ).pack(anchor="w", padx=16, pady=(10, 0))

    _section("Preview window")
    pos_var = tk.StringVar(value=cfg.get("preview_position", "cursor"))
    tk.Label(content, text="Position", bg=_BG, fg=_FG2,
             font=("Segoe UI", 9)).pack(anchor="w", padx=16, pady=(6, 0))
    om = tk.OptionMenu(content, pos_var, *_POSITIONS)
    om.config(bg=_BG2, fg=_FG, activebackground=_BORDER,
              relief="flat", highlightthickness=0, font=("Segoe UI", 10))
    om.pack(anchor="w", padx=16, pady=(2, 0))

    auto_dismiss_var = tk.StringVar(value=str(cfg.get("preview_auto_dismiss_seconds", 0)))
    _field("Auto-dismiss after (seconds, 0 = never)", auto_dismiss_var)

    auto_paste_var = tk.StringVar(value=str(cfg.get("auto_paste_threshold", 0.0)))
    _field("Auto-paste threshold 0–1 (skip preview when confidence ≥ this, 0 = always show)", auto_paste_var)

    _section("Filler words")
    _note("One per line — removed from every transcription.")
    fillers_txt = tk.Text(content, bg=_BG2, fg=_FG, insertbackground=_FG,
                          relief="flat", bd=0, highlightthickness=1,
                          highlightbackground="#3d3d3d",
                          font=("Segoe UI", 10), height=4, undo=True)
    fillers_txt.insert("1.0", "\n".join(cfg.get("filler_words", [])))
    fillers_txt.pack(fill=tk.X, padx=16, pady=(2, 0))

    _section("Vibe Coding")
    _note("Restructures dictation into a coding prompt via LM Studio (local) or Claude API.")
    vibe_var = tk.BooleanVar(value=bool(cfg.get("vibe_mode", False)))
    tk.Checkbutton(content, text="Enable vibe mode (adds ~300-500ms via API)",
                   variable=vibe_var, bg=_BG, fg=_FG2,
                   activebackground=_BG, activeforeground=_FG,
                   selectcolor=_BG2, font=("Segoe UI", 9)
                   ).pack(anchor="w", padx=16, pady=(6, 0))
    backend_var = tk.StringVar(value=cfg.get("vibe_mode_backend", "api"))
    tk.Label(content, text="Backend", bg=_BG, fg=_FG2,
             font=("Segoe UI", 9)).pack(anchor="w", padx=16, pady=(6, 0))
    be_menu = tk.OptionMenu(content, backend_var, "lmstudio", "api", "rules")
    be_menu.config(bg=_BG2, fg=_FG, activebackground=_BORDER,
                   relief="flat", highlightthickness=0, font=("Segoe UI", 10))
    be_menu.pack(anchor="w", padx=16, pady=(2, 0))

    _section("Custom corrections")
    _note('One per line: "wrong → correct"  (applied immediately, no training needed)')
    corrections_txt = tk.Text(content, bg=_BG2, fg=_FG, insertbackground=_FG,
                               relief="flat", bd=0, highlightthickness=1,
                               highlightbackground="#3d3d3d",
                               font=("Segoe UI", 10), height=5, undo=True)
    corr_dict = cfg.get("corrections", {})
    corr_lines = "\n".join(f"{k} → {v}" for k, v in corr_dict.items())
    corrections_txt.insert("1.0", corr_lines)
    corrections_txt.pack(fill=tk.X, padx=16, pady=(2, 0))

    # ── Buttons ─────────────────────────────────────────────────────────────
    btns = tk.Frame(content, bg=_BG)
    btns.pack(anchor="w", padx=16, pady=(16, 4))

    err_var = tk.StringVar()
    tk.Label(content, textvariable=err_var, bg=_BG, fg="#cc4444",
             font=("Segoe UI", 8)).pack(anchor="w", padx=16, pady=(0, 12))

    def on_save() -> None:
        try:
            max_secs      = float(max_var.get())
            silence_secs  = float(silence_var.get())
            sthresh       = float(sthresh_var.get())
            dismiss_secs  = float(auto_dismiss_var.get())
            paste_thresh  = float(auto_paste_var.get())
        except ValueError:
            err_var.set("Numeric fields must be numbers.")
            return

        fillers = [w.strip() for w in fillers_txt.get("1.0", "end-1c").splitlines()
                   if w.strip()]

        corrections: dict = {}
        for line in corrections_txt.get("1.0", "end-1c").splitlines():
            if "→" in line:
                parts = line.split("→", 1)
                k, v = parts[0].strip(), parts[1].strip()
                if k:
                    corrections[k] = v

        vocab = [w.strip() for w in vocab_txt.get("1.0", "end-1c").splitlines()
                 if w.strip()]

        # Parse mic selection back to device index (or None for default)
        sel = mic_var.get()
        if sel == "System default":
            mic_idx = None
        else:
            try:
                mic_idx = int(sel.split("]")[0].lstrip("["))
            except Exception:
                mic_idx = None

        new_cfg = dict(cfg)
        new_cfg.update({
            "hotkey":                      hotkey_var.get().strip() or "ctrl+alt",
            "language":                    lang_var.get().strip(),
            "model":                       model_var.get().strip(),
            "max_record_seconds":          max(5.0, min(300.0, max_secs)),
            "silence_auto_stop_seconds":   max(0.0, silence_secs),
            "silence_threshold":           max(0.001, min(0.5, sthresh)),
            "vad_filter":                  vad_var.get(),
            "preview_position":            pos_var.get(),
            "preview_auto_dismiss_seconds": max(0.0, dismiss_secs),
            "auto_paste_threshold":        max(0.0, min(1.0, paste_thresh)),
            "filler_words":                fillers,
            "corrections":                 corrections,
            "initial_prompt":              prompt_txt.get("1.0", "end-1c").strip(),
            "custom_vocabulary":           vocab,
            "input_device":                mic_idx,
            "history_paused":              history_paused_var.get(),
            "vibe_mode":                   vibe_var.get(),
            "vibe_mode_backend":           backend_var.get(),
        })

        try:
            with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(new_cfg, f, indent=2, ensure_ascii=False)
            configure_position(pos_var.get())
            err_var.set("Saved.")
        except Exception as exc:
            err_var.set(f"Save failed: {exc}")

    def on_close() -> None:
        global _settings_open
        _settings_open = False
        win.destroy()

    save_btn = tk.Button(btns, text="Save", command=on_save, width=10,
                         bg=_BLUE, fg=_FG, activebackground=_BLUE_HV,
                         activeforeground=_FG, relief="flat", bd=0,
                         font=("Segoe UI", 10), padx=6, pady=4, cursor="hand2")
    save_btn.bind("<Enter>", lambda e: save_btn.config(bg=_BLUE_HV))
    save_btn.bind("<Leave>", lambda e: save_btn.config(bg=_BLUE))
    save_btn.pack(side=tk.LEFT)

    cancel_wrap = tk.Frame(btns, bg=_BORDER, padx=1, pady=1)
    tk.Button(cancel_wrap, text="Cancel", command=on_close, width=10,
              bg=_BG, fg=_FG2, activebackground=_BG2, activeforeground=_FG,
              relief="flat", bd=0,
              font=("Segoe UI", 10), padx=6, pady=4, cursor="hand2").pack()
    cancel_wrap.pack(side=tk.LEFT, padx=(8, 0))

    win.protocol("WM_DELETE_WINDOW", on_close)
    win.bind("<Escape>", lambda _: on_close())
