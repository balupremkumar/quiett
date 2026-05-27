import threading
import time

import pyperclip
import win32api
import win32gui

_restore_delay_ms = 150

_VK_CONTROL      = 0x11
_VK_MENU         = 0x12   # Alt
_VK_V            = 0x56
_KEYEVENTF_KEYUP = 0x0002


def configure(restore_delay_ms: int) -> None:
    global _restore_delay_ms
    _restore_delay_ms = restore_delay_ms


def capture_foreground() -> int:
    return win32gui.GetForegroundWindow()


def inject_text(text: str, hwnd: int) -> None:
    """Copy text to clipboard then send Ctrl+V to the target window.

    Uses win32api.keybd_event (not the keyboard library) so modifier-key state
    from the Ctrl+Alt hotkey cannot contaminate the paste sequence.  Works for
    all app types: Win32, Chrome, Electron, UWP, etc.
    """
    if not hwnd or not win32gui.IsWindow(hwnd):
        return
    original = pyperclip.paste()
    pyperclip.copy(text)
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    time.sleep(0.1)  # allow focus to settle
    # Release any Ctrl/Alt still held from the recording hotkey
    win32api.keybd_event(_VK_MENU,    0, _KEYEVENTF_KEYUP, 0)
    win32api.keybd_event(_VK_CONTROL, 0, _KEYEVENTF_KEYUP, 0)
    time.sleep(0.03)
    # Ctrl+V via low-level API — bypasses keyboard-library modifier state
    win32api.keybd_event(_VK_CONTROL, 0, 0, 0)
    win32api.keybd_event(_VK_V,       0, 0, 0)
    win32api.keybd_event(_VK_V,       0, _KEYEVENTF_KEYUP, 0)
    win32api.keybd_event(_VK_CONTROL, 0, _KEYEVENTF_KEYUP, 0)

    def _restore():
        time.sleep(_restore_delay_ms / 1000)
        pyperclip.copy(original)

    threading.Thread(target=_restore, daemon=True).start()
