import threading
import time

import pyperclip
import win32con
import win32gui

_restore_delay_ms = 150


def configure(restore_delay_ms: int) -> None:
    global _restore_delay_ms
    _restore_delay_ms = restore_delay_ms


def capture_foreground() -> int:
    """Return the HWND of the currently focused window."""
    return win32gui.GetForegroundWindow()


def inject_text(text: str, hwnd: int) -> None:
    """Copy text to clipboard, refocus target window, send WM_PASTE,
    then restore the original clipboard contents after the configured delay.

    WM_PASTE bypasses the keyboard library entirely — no modifier-key state
    contamination from the Ctrl+Alt hotkey that triggered the recording.
    """
    if not hwnd or not win32gui.IsWindow(hwnd):
        return
    original = pyperclip.paste()
    pyperclip.copy(text)
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    time.sleep(0.08)  # allow focus switch before message arrives
    win32gui.PostMessage(hwnd, win32con.WM_PASTE, 0, 0)

    def _restore():
        time.sleep(_restore_delay_ms / 1000)
        pyperclip.copy(original)

    threading.Thread(target=_restore, daemon=True).start()
