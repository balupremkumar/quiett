import ctypes
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

_user32   = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32


def configure(restore_delay_ms: int) -> None:
    global _restore_delay_ms
    _restore_delay_ms = restore_delay_ms


def capture_foreground() -> int:
    return win32gui.GetForegroundWindow()


def _force_foreground(hwnd: int) -> None:
    """Bring hwnd to foreground reliably via AttachThreadInput.

    Plain SetForegroundWindow silently fails when the calling process is not
    the current foreground process (Windows focus-stealing restriction).
    Attaching to the foreground thread's input queue bypasses that restriction.
    """
    cur_thread = _kernel32.GetCurrentThreadId()
    fg_hwnd    = _user32.GetForegroundWindow()
    fg_thread  = _user32.GetWindowThreadProcessId(fg_hwnd, None)

    attached = False
    if fg_thread and fg_thread != cur_thread:
        _user32.AttachThreadInput(fg_thread, cur_thread, True)
        attached = True

    try:
        _user32.ShowWindow(hwnd, 9)      # SW_RESTORE — unminimise if needed
        _user32.SetForegroundWindow(hwnd)
        _user32.BringWindowToTop(hwnd)
    except Exception:
        pass
    finally:
        if attached:
            _user32.AttachThreadInput(fg_thread, cur_thread, False)


def inject_text(text: str, hwnd: int) -> None:
    """Paste text into hwnd via clipboard + Ctrl+V.

    Uses win32api.keybd_event (not the keyboard library) so modifier-key state
    from the Ctrl+Alt hotkey cannot contaminate the paste.  Works for Win32,
    Chrome, Electron (VS Code), UWP, and all other app types.
    """
    if not hwnd or not win32gui.IsWindow(hwnd):
        return
    original = pyperclip.paste()
    pyperclip.copy(text)

    _force_foreground(hwnd)
    time.sleep(0.15)  # let Electron/Chrome internal focus settle

    # Release any Ctrl/Alt still held from the recording hotkey
    win32api.keybd_event(_VK_MENU,    0, _KEYEVENTF_KEYUP, 0)
    win32api.keybd_event(_VK_CONTROL, 0, _KEYEVENTF_KEYUP, 0)
    time.sleep(0.05)

    # Ctrl+V via low-level API
    win32api.keybd_event(_VK_CONTROL, 0, 0, 0)
    win32api.keybd_event(_VK_V,       0, 0, 0)
    win32api.keybd_event(_VK_V,       0, _KEYEVENTF_KEYUP, 0)
    win32api.keybd_event(_VK_CONTROL, 0, _KEYEVENTF_KEYUP, 0)

    def _restore():
        time.sleep(_restore_delay_ms / 1000)
        pyperclip.copy(original)

    threading.Thread(target=_restore, daemon=True).start()
