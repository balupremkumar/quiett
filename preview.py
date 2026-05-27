"""
Tkinter preview window + history viewer + speech profile viewer,
running on a dedicated worker thread.

pystray owns the main thread, so we spin up a persistent hidden Tk root here.
Queues are polled every 50 ms so all windows can coexist simultaneously.
"""

import json
import queue
import threading
import tkinter as tk
from datetime import date, datetime

import pyperclip
import win32api

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

# Preview position: "cursor" | "top-right" | "bottom-right" | "top-left" | "bottom-left"
_preview_position = "cursor"

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
         confidence: float | None = None) -> None:
    """Queue a dictation preview window."""
    _preview_q.put({"text": text, "hwnd": hwnd, "empty": empty,
                    "confidence": confidence})


def show_history() -> None:
    _history_q.put(True)


def show_profile() -> None:
    _profile_q.put(True)


def show_badge(state: str) -> None:
    """Show the recording/processing status badge. state: 'recording'|'processing'"""
    _badge_q.put(state)


def hide_badge() -> None:
    """Remove the status badge."""
    _badge_q.put(None)


def show_settings() -> None:
    _settings_q.put(True)


def configure_position(position: str) -> None:
    """Update preview window placement. Safe to call from any thread."""
    global _preview_position
    _preview_position = position


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


def _tick() -> None:
    global _preview_open, _history_open, _profile_open, _settings_open

    if not _preview_open:
        try:
            item = _preview_q.get_nowait()
            _preview_open = True
            try:
                _open_window(
                    item["text"], item["hwnd"],
                    item.get("empty", False), item.get("confidence"),
                )
            except Exception as e:
                print(f"preview window error: {e}")
                _preview_open = False
        except queue.Empty:
            pass

    if not _history_open:
        try:
            _history_q.get_nowait()
            _history_open = True
            _open_history()
        except queue.Empty:
            pass

    if not _profile_open:
        try:
            _profile_q.get_nowait()
            _profile_open = True
            _open_profile()
        except queue.Empty:
            pass

    if not _settings_open:
        try:
            _settings_q.get_nowait()
            _settings_open = True
            _open_settings()
        except queue.Empty:
            pass

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
# Status badge (recording / processing)
# ---------------------------------------------------------------------------

_BADGE_CFG = {
    "recording":  {"dot": "#e03030", "text": "Recording..."},
    "processing": {"dot": "#c8a000", "text": "Transcribing..."},
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
    global _badge_win, _badge_label, _badge_dot
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
                 confidence: float | None = None) -> None:
    try:
        cx, cy = win32api.GetCursorPos()
    except Exception:
        cx, cy = 200, 200

    win = tk.Toplevel(_root)
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
        global _preview_open
        _preview_open = False
        win.destroy()

    def on_insert() -> None:
        result = entry.get("1.0", "end-1c").rstrip()
        # Log correction if user edited the transcription
        if not empty:
            original = text.rstrip()
            if original != result:
                try:
                    profile.log_correction(original, result)
                except Exception:
                    pass
        _close()
        inject.inject_text((" " + result) if append_var.get() else result, hwnd)

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

    # ── Keyboard hint strip ────────────────────────────────────────────────
    tk.Label(
        frame, text="↵ Insert  ·  Esc Cancel  ·  Ctrl+Z Undo  ·  Shift+↵ Newline",
        bg=_BG, fg="#555555", font=("Segoe UI", 8), anchor="w",
    ).pack(fill=tk.X, pady=(8, 0))

    # ── Keybindings ────────────────────────────────────────────────────────
    if not empty:
        def _on_return(e):
            on_insert()
            return "break"
        entry.bind("<Return>", _on_return)
        entry.bind("<Shift-Return>", lambda e: None)  # allow literal newline via default

    # Redo bindings (Tkinter Text only auto-binds Ctrl+Z for undo)
    entry.bind("<Control-y>",       lambda e: (entry.edit_redo(), "break")[1])
    entry.bind("<Control-Shift-z>", lambda e: (entry.edit_redo(), "break")[1])

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
    win.geometry("500x560")
    win.minsize(420, 460)
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

    _section("Transcription")
    lang_var = tk.StringVar(value=cfg.get("language", "en"))
    _field("Language (e.g. en, fr, es)", lang_var)
    model_var = tk.StringVar(value=cfg.get("model", "small"))
    _field("Model (small / medium / large)  — restart required", model_var)

    _section("Recording")
    max_var = tk.StringVar(value=str(cfg.get("max_record_seconds", 120)))
    _field("Max recording time (seconds)", max_var)
    silence_var = tk.StringVar(value=str(cfg.get("silence_auto_stop_seconds", 3)))
    _field("Silence auto-stop (seconds, 0 = disabled)", silence_var)
    vad_var = tk.BooleanVar(value=bool(cfg.get("vad_filter", False)))
    tk.Checkbutton(content, text="VAD filter (suppress background noise)",
                   variable=vad_var, bg=_BG, fg=_FG2,
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

    _section("Filler words")
    _note("One per line — removed from every transcription.")
    fillers_txt = tk.Text(content, bg=_BG2, fg=_FG, insertbackground=_FG,
                          relief="flat", bd=0, highlightthickness=1,
                          highlightbackground="#3d3d3d",
                          font=("Segoe UI", 10), height=4, undo=True)
    fillers_txt.insert("1.0", "\n".join(cfg.get("filler_words", [])))
    fillers_txt.pack(fill=tk.X, padx=16, pady=(2, 0))

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
            max_secs     = float(max_var.get())
            silence_secs = float(silence_var.get())
        except ValueError:
            err_var.set("Max recording and silence fields must be numbers.")
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

        new_cfg = dict(cfg)
        new_cfg.update({
            "language":                  lang_var.get().strip(),
            "model":                     model_var.get().strip(),
            "max_record_seconds":        max(5.0, min(300.0, max_secs)),
            "silence_auto_stop_seconds": max(0.0, silence_secs),
            "vad_filter":                vad_var.get(),
            "preview_position":          pos_var.get(),
            "filler_words":              fillers,
            "corrections":               corrections,
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
