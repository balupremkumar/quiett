import threading
import time

import keyboard
import pyperclip
import win32gui

_restore_delay_ms = 150


def configure(restore_delay_ms: int) -> None:
    global _restore_delay_ms
    _restore_delay_ms = restore_delay_ms


def capture_foreground() -> int:
    """Return the HWND of the currently focused window."""
    return win32gui.GetForegroundWindow()


def inject_text(text: str, hwnd: int) -> None:
    """Swap clipboard to text, restore target window focus, send Ctrl+V,
    then restore the original clipboard contents after the configured delay."""
    if not hwnd or not win32gui.IsWindow(hwnd):
        return
    original = pyperclip.paste()
    pyperclip.copy(text)
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    time.sleep(0.08)  # allow focus switch before keystrokes arrive
    keyboard.send("ctrl+v")

    def _restore():
        time.sleep(_restore_delay_ms / 1000)
        pyperclip.copy(original)

    threading.Thread(target=_restore, daemon=True).start()
