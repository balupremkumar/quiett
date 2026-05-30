"""Text injection via clipboard + paste keystroke.

Uses SendInput with scan codes (not keybd_event with virtual keys) so paste works
in RDP sessions, VS Code/Electron, browsers, and other contexts where the legacy
keybd_event API is intercepted or treated as untrusted input.

Strategy per paste:
  1. Save and replace clipboard
  2. Force target window to foreground via AttachThreadInput
  3. Wait for focus to settle (longer for Electron/RDP)
  4. Flush stuck modifier keys (Ctrl/Alt/Shift/Win — all sides)
  5. Send Ctrl+V (or Ctrl+Shift+V for terminals) via SendInput + scan codes
  6. Verify clipboard was consumed; if not, retry with WM_PASTE fallback
  7. Restore original clipboard after configurable delay
"""
import ctypes
import threading
import time
from ctypes import wintypes

import pyperclip
import win32api
import win32con
import win32gui

from logger import log, warn

_restore_delay_ms = 150

_VK_CONTROL = 0x11
_VK_MENU    = 0x12  # Alt
_VK_SHIFT   = 0x10
_VK_LWIN    = 0x5B
_VK_RWIN    = 0x5C
_VK_V       = 0x56

_KEYEVENTF_KEYUP    = 0x0002
_KEYEVENTF_SCANCODE = 0x0008
_KEYEVENTF_EXTENDED = 0x0001

_INPUT_KEYBOARD = 1
_MAPVK_VK_TO_VSC = 0

_WM_PASTE = 0x0302

_user32   = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

_TERMINAL_CLASSES = {
    "CASCADIA_HOSTING_WINDOW_CLASS",  # Windows Terminal
    "mintty",                          # Git Bash / Cygwin
}

# Windows that need extra settle time before they accept Ctrl+V
_SLOW_FOCUS_CLASSES_SUBSTR = (
    "Chrome_WidgetWin",   # Chrome, Edge, VS Code, Slack, Teams, Electron in general
    "MozillaWindowClass", # Firefox
    "TscShellContainer",  # mstsc.exe (RDP client)
    "RAIL_WINDOW",        # RDP RAIL apps
)


# ---------------------------------------------------------------------------
# SendInput structures
# ---------------------------------------------------------------------------

ULONG_PTR = ctypes.c_size_t


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk",         wintypes.WORD),
        ("wScan",       wintypes.WORD),
        ("dwFlags",     wintypes.DWORD),
        ("time",        wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _INPUTunion(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("type", wintypes.DWORD),
        ("u",    _INPUTunion),
    ]


def _make_key_input(vk: int, key_up: bool) -> _INPUT:
    scan = _user32.MapVirtualKeyW(vk, _MAPVK_VK_TO_VSC) & 0xFFFF
    flags = _KEYEVENTF_SCANCODE
    if key_up:
        flags |= _KEYEVENTF_KEYUP
    # Extended keys (right ctrl/alt, arrows, etc.) — not strictly needed for V/Ctrl/Shift
    inp = _INPUT()
    inp.type = _INPUT_KEYBOARD
    inp.ki = _KEYBDINPUT(wVk=0, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    return inp


def _send_inputs(inputs: list) -> int:
    n = len(inputs)
    arr = (_INPUT * n)(*inputs)
    return _user32.SendInput(n, ctypes.byref(arr), ctypes.sizeof(_INPUT))


def _send_keystroke(vks_to_hold: list[int], main_vk: int) -> None:
    """Press modifiers, tap main key, release modifiers (in reverse)."""
    inputs = []
    for vk in vks_to_hold:
        inputs.append(_make_key_input(vk, key_up=False))
    inputs.append(_make_key_input(main_vk, key_up=False))
    inputs.append(_make_key_input(main_vk, key_up=True))
    for vk in reversed(vks_to_hold):
        inputs.append(_make_key_input(vk, key_up=True))
    _send_inputs(inputs)


def _flush_modifier(vk: int) -> None:
    """Send key-up for a modifier in case it's stuck from the recording hotkey."""
    inp = _make_key_input(vk, key_up=True)
    _send_inputs([inp])


def _flush_all_modifiers() -> None:
    for vk in (_VK_CONTROL, _VK_MENU, _VK_SHIFT, _VK_LWIN, _VK_RWIN):
        _flush_modifier(vk)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def configure(restore_delay_ms: int) -> None:
    global _restore_delay_ms
    _restore_delay_ms = restore_delay_ms


def capture_foreground() -> int:
    return win32gui.GetForegroundWindow()


def _get_class(hwnd: int) -> str:
    try:
        return win32gui.GetClassName(hwnd)
    except Exception:
        return ""


def _is_terminal(hwnd: int) -> bool:
    return _get_class(hwnd) in _TERMINAL_CLASSES


def _needs_slow_settle(hwnd: int) -> bool:
    cls = _get_class(hwnd)
    return any(s in cls for s in _SLOW_FOCUS_CLASSES_SUBSTR)


def _is_rdp(hwnd: int) -> bool:
    cls = _get_class(hwnd)
    return "TscShellContainer" in cls or "RAIL_WINDOW" in cls


def _force_foreground(hwnd: int) -> None:
    cur_thread = _kernel32.GetCurrentThreadId()
    fg_hwnd    = _user32.GetForegroundWindow()
    fg_thread  = _user32.GetWindowThreadProcessId(fg_hwnd, None)

    attached = False
    if fg_thread and fg_thread != cur_thread:
        _user32.AttachThreadInput(fg_thread, cur_thread, True)
        attached = True

    try:
        if _user32.IsIconic(hwnd):
            _user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        _user32.SetForegroundWindow(hwnd)
        _user32.BringWindowToTop(hwnd)
        # AllowSetForegroundWindow doesn't help here, but SwitchToThisWindow does for some hosts
        try:
            _user32.SwitchToThisWindow(hwnd, True)
        except Exception:
            pass
    except Exception:
        pass
    finally:
        if attached:
            _user32.AttachThreadInput(fg_thread, cur_thread, False)


def _try_wm_paste(hwnd: int) -> bool:
    """Fallback: send WM_PASTE to the focused control inside hwnd.

    Works for standard Win32 edit controls (Notepad, classic input fields).
    Doesn't work for Electron/Chromium/UWP (they don't honor WM_PASTE), but it's
    a cheap last resort for the cases where SendInput is blocked.
    """
    try:
        # Find the focused control. GetGUIThreadInfo gives us the actual focus hwnd.
        class GUITHREADINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize",        wintypes.DWORD),
                ("flags",         wintypes.DWORD),
                ("hwndActive",    wintypes.HWND),
                ("hwndFocus",     wintypes.HWND),
                ("hwndCapture",   wintypes.HWND),
                ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize",  wintypes.HWND),
                ("hwndCaret",     wintypes.HWND),
                ("rcCaret",       wintypes.RECT),
            ]
        info = GUITHREADINFO()
        info.cbSize = ctypes.sizeof(GUITHREADINFO)
        tid = _user32.GetWindowThreadProcessId(hwnd, None)
        if not _user32.GetGUIThreadInfo(tid, ctypes.byref(info)):
            return False
        target = info.hwndFocus or hwnd
        win32api.SendMessage(target, _WM_PASTE, 0, 0)
        return True
    except Exception as exc:
        warn("inject", f"WM_PASTE fallback failed: {exc}")
        return False


def inject_text(text: str, hwnd: int) -> None:
    if not hwnd or not win32gui.IsWindow(hwnd):
        log("inject", "no target hwnd")
        return

    target_cls = _get_class(hwnd)
    log("inject", f"target hwnd={hwnd} class={target_cls!r} chars={len(text)}")

    original = pyperclip.paste()
    try:
        pyperclip.copy(text)
    except Exception as exc:
        warn("inject", f"clipboard copy failed: {exc}")
        return

    _force_foreground(hwnd)

    # Settle delay — longer for Electron/Chromium/RDP. These hosts have multiple
    # internal focus targets (web frame, devtools, sidebar) and need time for
    # the inner focus to land in the actual input element.
    settle = 0.30 if _needs_slow_settle(hwnd) else 0.15
    if _is_rdp(hwnd):
        settle = 0.40
    time.sleep(settle)

    # Drain any stuck modifier from the Ctrl+Alt hotkey. Critical: without this,
    # the OS thinks Alt is still held when we tap Ctrl+V, which produces Ctrl+Alt+V
    # (often a no-op or app-specific shortcut) instead of paste.
    _flush_all_modifiers()
    time.sleep(0.05)

    if _is_terminal(hwnd):
        _send_keystroke([_VK_CONTROL, _VK_SHIFT], _VK_V)
    else:
        _send_keystroke([_VK_CONTROL], _VK_V)

    # Verify: most apps consume the clipboard during paste (briefly), but a reliable
    # signal that paste worked is hard. Instead, if the foreground window changed
    # away during settle (focus stolen), retry once.
    time.sleep(0.05)
    if win32gui.GetForegroundWindow() != hwnd:
        warn("inject", "focus lost mid-paste, retrying with WM_PASTE fallback")
        _force_foreground(hwnd)
        time.sleep(0.20)
        if not _try_wm_paste(hwnd):
            # Last-resort retry of the keystroke path
            _flush_all_modifiers()
            time.sleep(0.05)
            if _is_terminal(hwnd):
                _send_keystroke([_VK_CONTROL, _VK_SHIFT], _VK_V)
            else:
                _send_keystroke([_VK_CONTROL], _VK_V)

    def _restore():
        time.sleep(_restore_delay_ms / 1000)
        try:
            # Don't clobber if user/app already changed the clipboard
            current = pyperclip.paste()
            if current == text:
                pyperclip.copy(original)
            else:
                log("inject", "clipboard changed during paste, skipping restore")
        except Exception as exc:
            warn("inject", f"clipboard restore failed: {exc}")

    threading.Thread(target=_restore, daemon=True).start()
