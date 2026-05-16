"""
Tkinter preview window + history viewer, running on a dedicated worker thread.

pystray owns the main thread, so we spin up a persistent hidden Tk root here.
Two independent queues are polled every 50 ms — one for dictation previews,
one for the history viewer — so both can be open simultaneously.
"""

import queue
import threading
import tkinter as tk
from datetime import date, datetime

import pyperclip
import win32api

import history as hist
import inject

# ---------------------------------------------------------------------------
# Public API — safe to call from any thread
# ---------------------------------------------------------------------------

_preview_q: queue.Queue = queue.Queue()
_history_q: queue.Queue = queue.Queue()
_root: tk.Tk | None = None
_ready = threading.Event()
_preview_open = False   # only touched on the tkinter thread
_history_open = False   # only touched on the tkinter thread

# Shared dark-theme palette
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


def show(text: str, hwnd: int, empty: bool = False) -> None:
    """Queue a dictation preview window."""
    _preview_q.put({"text": text, "hwnd": hwnd, "empty": empty})


def show_history() -> None:
    """Request the history viewer window."""
    _history_q.put(True)


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


def _tick() -> None:
    global _preview_open, _history_open
    if not _preview_open:
        try:
            item = _preview_q.get_nowait()
            _preview_open = True
            _open_window(item["text"], item["hwnd"], item.get("empty", False))
        except queue.Empty:
            pass
    if not _history_open:
        try:
            _history_q.get_nowait()
            _history_open = True
            _open_history()
        except queue.Empty:
            pass
    _root.after(50, _tick)


def _open_window(text: str, hwnd: int, empty: bool = False) -> None:
    try:
        cx, cy = win32api.GetCursorPos()
    except Exception:
        cx, cy = 200, 200

    win = tk.Toplevel(_root)
    win.overrideredirect(True)          # no title bar chrome
    win.configure(bg=_BG)
    win.attributes("-topmost", True)

    frame = tk.Frame(win, bg=_BG, padx=16, pady=14)
    frame.pack(fill=tk.BOTH, expand=True)

    # ── Text entry (multi-line, word-wrap) ────────────────────────────────
    display = "Nothing detected — try again" if empty else text
    entry = tk.Text(
        frame, font=("Segoe UI", 11), wrap=tk.WORD,
        bg=_BG2, fg=_FG, insertbackground=_FG,
        relief="flat", bd=0,
        highlightthickness=1,
        highlightbackground="#3d3d3d",
        highlightcolor=_BLUE,
        height=4,
        undo=True,
    )
    entry.insert("1.0", display)
    entry.pack(fill=tk.X, pady=(0, 3))
    if not empty:
        entry.tag_add("sel", "1.0", tk.END)
        entry.mark_set(tk.INSERT, tk.END)
    entry.focus_set()

    # ── Character count ────────────────────────────────────────────────────
    count_var = tk.StringVar()
    tk.Label(
        frame, textvariable=count_var,
        bg=_BG, fg="#666666", font=("Segoe UI", 8), anchor="w",
    ).pack(fill=tk.X, pady=(0, 5))

    def _update_count(*_):
        n = len(entry.get("1.0", "end-1c"))
        count_var.set(f"{n} character{'s' if n != 1 else ''}")
        entry.edit_modified(False)

    entry.bind("<<Modified>>", _update_count)
    _update_count()

    # ── Append checkbox ────────────────────────────────────────────────────
    append_var = tk.BooleanVar(value=False)
    tk.Checkbutton(
        frame, text="Append", variable=append_var,
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
        result = entry.get("1.0", "end-1c")
        _close()
        inject.inject_text((" " + result) if append_var.get() else result, hwnd)

    def on_cancel() -> None:
        _close()

    # Insert — blue accent with hover
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

    # Cancel — grey border, no fill
    cancel_wrap = tk.Frame(btns, bg=_BORDER, padx=1, pady=1)
    tk.Button(
        cancel_wrap, text="Cancel", command=on_cancel, width=10,
        bg=_BG, fg=_FG2,
        activebackground=_BG2, activeforeground=_FG,
        relief="flat", bd=0,
        font=("Segoe UI", 10), padx=6, pady=4, cursor="hand2",
    ).pack()
    cancel_wrap.pack(side=tk.LEFT, padx=(8, 0))

    # ── Keybindings ────────────────────────────────────────────────────────
    if not empty:
        # "break" stops the Text widget from inserting a newline
        entry.bind("<Return>", lambda e: (on_insert(), "break"))
        win.bind("<Return>", lambda _: on_insert())
    win.bind("<Escape>", lambda _: on_cancel())
    win.protocol("WM_DELETE_WINDOW", on_cancel)

    # ── Drag (bind to background only, not interactive children) ───────────
    def _drag_start(event):
        win._ox = event.x_root - win.winfo_x()
        win._oy = event.y_root - win.winfo_y()

    def _drag_motion(event):
        win.geometry(f"+{event.x_root - win._ox}+{event.y_root - win._oy}")

    for widget in (win, frame):
        widget.bind("<ButtonPress-1>", _drag_start)
        widget.bind("<B1-Motion>", _drag_motion)

    # ── Size & position (auto height, min 400 px wide, clamped to screen) ─
    win.update_idletasks()
    w = max(400, win.winfo_reqwidth())
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

    # ── Scrollable list ────────────────────────────────────────────────────
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

                # Inline copy link
                tag = f"copy_{i}"
                txt.insert(tk.END, "· copy\n", ("copy", tag))
                txt.tag_bind(tag, "<Button-1>",
                             lambda e, t=body_text: pyperclip.copy(t))
                txt.tag_bind(tag, "<Enter>",
                             lambda e: txt.config(cursor="hand2"))
                txt.tag_bind(tag, "<Leave>",
                             lambda e: txt.config(cursor="ibeam"))

                if i < len(data) - 1:
                    txt.insert(tk.END, "─" * 55 + "\n", "sep")
        else:
            txt.insert(tk.END, "No dictation history yet.", "ts")
        txt.config(state="disabled")

    _populate(entries)

    # ── Bottom bar ─────────────────────────────────────────────────────────
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
