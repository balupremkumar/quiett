"""
Tkinter preview window + history viewer + speech profile viewer,
running on a dedicated worker thread.

pystray owns the main thread, so we spin up a persistent hidden Tk root here.
Queues are polled every 50 ms so all windows can coexist simultaneously.
"""

import queue
import threading
import tkinter as tk
from datetime import date, datetime

import pyperclip
import win32api

import history as hist
import inject
import profile

# ---------------------------------------------------------------------------
# Public API — safe to call from any thread
# ---------------------------------------------------------------------------

_preview_q:  queue.Queue = queue.Queue()
_history_q:  queue.Queue = queue.Queue()
_profile_q:  queue.Queue = queue.Queue()
_badge_q:    queue.Queue = queue.Queue()
_root:       tk.Tk | None = None
_ready = threading.Event()

_preview_open = False   # only touched on the tkinter thread
_history_open = False
_profile_open = False

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
    global _preview_open, _history_open, _profile_open

    if not _preview_open:
        try:
            item = _preview_q.get_nowait()
            _preview_open = True
            _open_window(
                item["text"], item["hwnd"],
                item.get("empty", False), item.get("confidence"),
            )
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

    try:
        badge_cmd = _badge_q.get_nowait()
        _handle_badge(badge_cmd)
    except queue.Empty:
        pass

    _root.after(50, _tick)


# ---------------------------------------------------------------------------
# Status badge (recording / processing)
# ---------------------------------------------------------------------------

_BADGE_CFG = {
    "recording":  {"dot": "#e03030", "text": "Recording..."},
    "processing": {"dot": "#c8a000", "text": "Transcribing..."},
}


def _handle_badge(cmd: str | None) -> None:
    global _badge_win, _badge_label, _badge_dot
    if cmd is None:
        if _badge_win is not None:
            _badge_win.destroy()
            _badge_win = None
            _badge_label = None
            _badge_dot = None
        return

    cfg = _BADGE_CFG.get(cmd, _BADGE_CFG["processing"])

    if _badge_win is None:
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
        if _badge_dot:
            _badge_dot.config(fg=cfg["dot"])
        if _badge_label:
            _badge_label.config(text=cfg["text"])


# ---------------------------------------------------------------------------
# Dictation preview window
# ---------------------------------------------------------------------------

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
    entry.bind("<Control-shift-z>", lambda e: (entry.edit_redo(), "break")[1])

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
    x = max(0, min(cx + 12, sw - w))
    y = max(0, min(cy + 12, sh - h))
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
