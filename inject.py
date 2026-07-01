"""Text injection — types characters directly via SendInput KEYEVENTF_UNICODE.

Background: synthetic Ctrl+V is fundamentally unreliable in modern Electron apps
(VS Code, Cursor) because Chromium treats injected key events with extra scrutiny
and the official VS Code paste regression (electron #238609) means even working
Ctrl+V may be dropped. RDP further mangles synthetic clipboard paste.

Strategy: instead of asking the app to paste-from-clipboard, we *type* each
character via SendInput with KEYEVENTF_UNICODE. The receiving app sees normal
keyboard input — same path as the user typing manually. This bypasses:
  - Clipboard ownership / lazy-read races (Chromium polls clipboard via IPC)
  - Synthetic-Ctrl+V interception in newer Electron
  - Custom keybinding interpretation of Ctrl+V in specific app contexts
  - Modifier-state corruption from the recording hotkey
  - UAC integrity restrictions on clipboard paste

Cost: ~5 ms per character. For typical dictation (<200 chars) this is under 1 s.

Per-target routing in inject_text:
  - Electron (Chrome_WidgetWin, MozillaWindowClass), RDP, default: Unicode typing
  - Terminals (Cascadia, mintty): keep Ctrl+Shift+V clipboard paste (works well,
    faster for long output, terminals don't have the Chromium paste regression)
  - Per-app overrides via per_app_paste: "type" | "ctrl_v" | "ctrl_shift_v"
"""
import ctypes
import os
import threading
import time
from ctypes import wintypes

import win32api
import win32clipboard
import win32con
import win32gui

from logger import log, warn

_restore_delay_ms = 150
_per_app_paste: dict = {}   # exe_name_lower → "ctrl_v" | "ctrl_shift_v"
_electron_paste_method = "ctrl_v"  # how to paste into Electron/Chromium (VS Code, Cursor, browsers)
_paste_mode = "auto"               # "auto" = inject into target; "clipboard_only" = copy + notify

_VK_CONTROL = 0x11
_VK_MENU    = 0x12  # Alt
_VK_SHIFT   = 0x10
_VK_LWIN    = 0x5B
_VK_RWIN    = 0x5C
_VK_V       = 0x56

_KEYEVENTF_KEYUP    = 0x0002
_KEYEVENTF_UNICODE  = 0x0004
_KEYEVENTF_SCANCODE = 0x0008
_KEYEVENTF_EXTENDED = 0x0001

_VK_RETURN = 0x0D
_VK_TAB    = 0x09
_VK_BACK   = 0x08
_VK_Z      = 0x5A

_INPUT_KEYBOARD = 1
_MAPVK_VK_TO_VSC = 0

_WM_PASTE = 0x0302

_user32   = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

# 64-bit correctness: ctypes defaults return values to 32-bit int, truncating
# HANDLEs and pointers. A GlobalLock pointer above 4 GB then crashes
# wstring_at/memmove with an access violation — which silently broke the
# clipboard snapshot/restore path (restore raced the pending Ctrl+V and could
# paste stale clipboard content into the target).
_user32.GetClipboardData.restype = wintypes.HANDLE
_kernel32.GlobalLock.restype     = ctypes.c_void_p
_kernel32.GlobalLock.argtypes    = [wintypes.HANDLE]
_kernel32.GlobalUnlock.argtypes  = [wintypes.HANDLE]
_kernel32.GlobalSize.restype     = ctypes.c_size_t
_kernel32.GlobalSize.argtypes    = [wintypes.HANDLE]

_TERMINAL_CLASSES = {
    "CASCADIA_HOSTING_WINDOW_CLASS",  # Windows Terminal
    "mintty",                          # Git Bash / Cygwin
    "ConsoleWindowClass",              # legacy conhost (PowerShell, cmd.exe)
    "VirtualConsoleClass",             # ConEmu, cmder
    "Alacritty",                       # Alacritty
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
    _fields_ = [
        ("ki",   _KEYBDINPUT),
        ("_pad", ctypes.c_byte * 32),  # pad to MOUSEINPUT size so sizeof(INPUT)==40
    ]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("type", wintypes.DWORD),
        ("u",    _INPUTunion),
    ]


def _make_key_input(vk: int, key_up: bool) -> _INPUT:
    hkl = _user32.GetKeyboardLayout(0)
    scan = (_user32.MapVirtualKeyExW(vk, _MAPVK_VK_TO_VSC, hkl) or
            _user32.MapVirtualKeyW(vk, _MAPVK_VK_TO_VSC)) & 0xFFFF
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
    result = _user32.SendInput(n, ctypes.byref(arr), ctypes.sizeof(_INPUT))
    if result == 0:
        err = _kernel32.GetLastError()
        warn("inject", f"SendInput returned 0 for {n} events, GetLastError={err}")
    return result


def _make_unicode_input(code: int, key_up: bool) -> _INPUT:
    """Build a KEYEVENTF_UNICODE input event for a single UTF-16 code unit."""
    flags = _KEYEVENTF_UNICODE
    if key_up:
        flags |= _KEYEVENTF_KEYUP
    inp = _INPUT()
    inp.type = _INPUT_KEYBOARD
    inp.ki = _KEYBDINPUT(wVk=0, wScan=code, dwFlags=flags, time=0, dwExtraInfo=0)
    return inp


def _make_vk_input(vk: int, key_up: bool) -> _INPUT:
    """Build a virtual-key input event (for Enter / Tab / Backspace etc.)."""
    flags = _KEYEVENTF_KEYUP if key_up else 0
    inp = _INPUT()
    inp.type = _INPUT_KEYBOARD
    inp.ki = _KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=0)
    return inp


def _send_unicode_text(text: str, batch: int = 32, batch_delay_ms: int = 2) -> int:
    """Type `text` via SendInput KEYEVENTF_UNICODE. Returns chars sent.

    Newlines and tabs are sent as VK_RETURN / VK_TAB virtual keys so the target
    app handles them naturally (auto-indent, etc.) rather than inserting a raw
    line-separator codepoint that some apps render incorrectly.

    Batching: large bursts can be dropped by SendInput's internal queue when
    a foreground app is doing heavy work. We send in small batches with a
    micro-sleep between them. This is the same pattern AutoHotkey uses for
    its SendInput command at large input sizes.
    """
    if not text:
        return 0
    sent = 0
    buf: list = []

    def _flush():
        nonlocal sent
        if not buf:
            return
        n = _send_inputs(buf)       # events actually inserted into the input stream
        sent += n // 2              # 2 inputs per char (down + up)
        buf.clear()
        if batch_delay_ms > 0:
            time.sleep(batch_delay_ms / 1000.0)

    for ch in text:
        if ch == "\r":
            continue  # Normalise CRLF -> LF; the LF below handles the line break
        if ch == "\n":
            _flush()
            buf.append(_make_vk_input(_VK_RETURN, key_up=False))
            buf.append(_make_vk_input(_VK_RETURN, key_up=True))
            _flush()
            continue
        if ch == "\t":
            _flush()
            buf.append(_make_vk_input(_VK_TAB, key_up=False))
            buf.append(_make_vk_input(_VK_TAB, key_up=True))
            _flush()
            continue

        code = ord(ch)
        if code > 0xFFFF:
            # Supplementary plane → encode as UTF-16 surrogate pair
            code -= 0x10000
            hi = 0xD800 | (code >> 10)
            lo = 0xDC00 | (code & 0x3FF)
            for c in (hi, lo):
                buf.append(_make_unicode_input(c, key_up=False))
                buf.append(_make_unicode_input(c, key_up=True))
        else:
            buf.append(_make_unicode_input(code, key_up=False))
            buf.append(_make_unicode_input(code, key_up=True))

        if len(buf) >= batch * 2:
            _flush()

    _flush()
    return sent


def _send_keystroke(vks_to_hold: list[int], main_vk: int) -> int:
    """Press modifiers, tap main key, release modifiers (in reverse).

    Returns the number of input events SendInput actually inserted — 0 means
    SendInput is fully blocked for this target (e.g. secure desktop), distinct
    from the keystroke being delivered but ignored by the app.
    """
    inputs = []
    for vk in vks_to_hold:
        inputs.append(_make_key_input(vk, key_up=False))
    inputs.append(_make_key_input(main_vk, key_up=False))
    inputs.append(_make_key_input(main_vk, key_up=True))
    for vk in reversed(vks_to_hold):
        inputs.append(_make_key_input(vk, key_up=True))
    return _send_inputs(inputs)


def _flush_modifier(vk: int) -> None:
    """Send key-up for a modifier in case it's stuck from the recording hotkey."""
    inp = _make_key_input(vk, key_up=True)
    _send_inputs([inp])


def _flush_all_modifiers() -> None:
    # Only flush keys that are still physically held — don't send spurious
    # synthetic key-ups for already-released keys (confuses Electron/VS Code).
    held = _modifiers_physically_down()
    for vk in (_VK_CONTROL, _VK_MENU, _VK_SHIFT, _VK_LWIN, _VK_RWIN):
        if vk in held:
            _flush_modifier(vk)


def _modifiers_physically_down() -> list[int]:
    """Return list of VKs that are currently physically pressed (per GetAsyncKeyState).

    Synthetic key-ups from _flush_all_modifiers don't override hardware state — if
    the user is still physically holding the recording hotkey, Windows queues new
    keydowns immediately. We have to wait for the physical release.
    """
    down = []
    for vk in (_VK_CONTROL, _VK_MENU, _VK_SHIFT, _VK_LWIN, _VK_RWIN):
        # high bit set = currently pressed
        if _user32.GetAsyncKeyState(vk) & 0x8000:
            down.append(vk)
    return down


def _wait_modifiers_released(timeout_ms: int = 400) -> bool:
    """Block until Ctrl/Alt/Shift/Win are all physically released, or timeout.

    Returns True if released within timeout, False otherwise.
    """
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        if not _modifiers_physically_down():
            return True
        time.sleep(0.01)
    return False


# ---------------------------------------------------------------------------
# Raw Win32 clipboard — replaces pyperclip which uses a hidden Tk window that
# Chromium-based apps (VS Code, Cursor, Chrome) sometimes time out reading from.
# ---------------------------------------------------------------------------

_CF_UNICODETEXT = 13
_GMEM_MOVEABLE  = 0x0002

# Formats that are GDI handles, not HGLOBALs — skip during snapshot
_NON_HGLOBAL_FORMATS = {
    2,   # CF_BITMAP
    3,   # CF_METAFILEPICT (actually HGLOBAL but structure, skip for safety)
    9,   # CF_PALETTE
    14,  # CF_ENHMETAFILE
    0x0080,  # CF_DSPBITMAP
    0x0082,  # CF_DSPENHMETAFILE
    0x0085,  # CF_OWNERDISPLAY
}


def _clipboard_open(timeout_ms: int = 500) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        if _user32.OpenClipboard(0):
            return True
        time.sleep(0.01)
    return False


def _clipboard_get_text() -> str:
    if not _clipboard_open():
        return ""
    try:
        h = _user32.GetClipboardData(_CF_UNICODETEXT)
        if not h:
            return ""
        ptr = _kernel32.GlobalLock(h)
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr)
        finally:
            _kernel32.GlobalUnlock(h)
    finally:
        _user32.CloseClipboard()


_SNAPSHOT_FORMATS = frozenset([13])  # CF_UNICODETEXT only
# CF_TEXT (1) is auto-synthesised by Windows from CF_UNICODETEXT; reading it
# triggers synthesis which modifies the clipboard, fires WM_CLIPBOARDUPDATE,
# and causes VS Code's clipboard monitor to grab the clipboard, blocking our
# subsequent _clipboard_set_text call.


def _clipboard_snapshot() -> list:
    """Capture text/file clipboard formats as [(format_id, bytes), ...].

    Only reads the small set of formats we can safely round-trip. Deliberately
    skips custom registered formats (ID > 0xBFFF) owned by Electron/VS Code —
    reading those triggers delayed-rendering WM_RENDERFORMAT messages, VS Code
    then reclaims the clipboard, and our subsequent SetClipboardData fails.
    """
    result: list = []
    if not _clipboard_open():
        return result
    try:
        fmt = _user32.EnumClipboardFormats(0)
        while fmt:
            if fmt in _SNAPSHOT_FORMATS:
                h = _user32.GetClipboardData(fmt)
                if h:
                    size = _kernel32.GlobalSize(h)
                    if size and size < 8 * 1024 * 1024:  # skip blobs >8 MB
                        ptr = _kernel32.GlobalLock(h)
                        if ptr:
                            try:
                                buf = (ctypes.c_char * size)()
                                ctypes.memmove(buf, ptr, size)
                                result.append((fmt, bytes(buf)))
                            except Exception:
                                pass
                            finally:
                                _kernel32.GlobalUnlock(h)
            fmt = _user32.EnumClipboardFormats(fmt)
    except Exception:
        pass
    finally:
        _user32.CloseClipboard()
    return result


def _clipboard_restore_snapshot(snapshot: list) -> bool:
    """Restore CF_UNICODETEXT clipboard data captured by _clipboard_snapshot()."""
    if not snapshot:
        return True
    try:
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            for fmt, data in snapshot:
                if fmt == _CF_UNICODETEXT:
                    text = data.decode("utf-16le").rstrip("\x00")
                    win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
            return True
        finally:
            win32clipboard.CloseClipboard()
    except Exception as exc:
        warn("inject", f"clipboard restore failed: {exc}")
        return False


def _clipboard_set_text(text: str) -> bool:
    """Write text to clipboard using win32clipboard (handles 64-bit HGLOBAL correctly)."""
    try:
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
            return True
        finally:
            win32clipboard.CloseClipboard()
    except Exception as exc:
        warn("inject", f"clipboard set_text failed: {exc}")
        return False


# ---------------------------------------------------------------------------
# UAC integrity check — SendInput silently fails when sending to a higher-
# integrity-level process (e.g. an elevated Notepad / Regedit / Task Manager).
# Detect this so we can surface a clear error instead of a silent paste failure.
# ---------------------------------------------------------------------------

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TOKEN_QUERY = 0x0008


def _is_higher_integrity_target(hwnd: int) -> bool:
    """Heuristic: target runs at higher integrity than us (e.g. elevated UAC).

    If OpenProcessToken on the target's PID fails with ACCESS_DENIED while
    OpenProcessToken on our own PID succeeds, the target is at higher integrity
    and SendInput will silently fail. We can't fix this from a non-elevated
    process — best we can do is detect it and surface a clear error.
    """
    try:
        advapi32 = ctypes.windll.advapi32
        target_pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(target_pid))
        h_proc = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, target_pid.value)
        if not h_proc:
            return False  # can't open — assume same integrity rather than scare the user
        try:
            h_token = wintypes.HANDLE()
            ok = bool(advapi32.OpenProcessToken(h_proc, _TOKEN_QUERY, ctypes.byref(h_token)))
            if ok:
                _kernel32.CloseHandle(h_token)
                return False
            err = ctypes.get_last_error()
            # ERROR_ACCESS_DENIED = 5 strongly suggests higher integrity
            return err == 5
        finally:
            _kernel32.CloseHandle(h_proc)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def configure(restore_delay_ms: int, per_app_paste: dict | None = None,
              electron_paste_method: str | None = None,
              paste_mode: str | None = None) -> None:
    global _restore_delay_ms, _per_app_paste, _electron_paste_method, _paste_mode
    _restore_delay_ms = restore_delay_ms
    if per_app_paste is not None:
        _per_app_paste = {k.lower(): v for k, v in per_app_paste.items()}
    if electron_paste_method in ("type", "ctrl_v", "ctrl_shift_v"):
        _electron_paste_method = electron_paste_method
    if paste_mode in ("auto", "clipboard_only"):
        _paste_mode = paste_mode


def capture_foreground() -> int:
    return win32gui.GetForegroundWindow()


def _get_class(hwnd: int) -> str:
    try:
        return win32gui.GetClassName(hwnd)
    except Exception:
        return ""


def _get_exe_name(hwnd: int) -> str:
    """Return the lowercase exe filename (e.g. 'code.exe') for the process owning hwnd."""
    try:
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = _kernel32.OpenProcess(0x0410, False, pid.value)  # QUERY_INFORMATION | VM_READ
        if not h:
            return ""
        buf = ctypes.create_unicode_buffer(512)
        ctypes.windll.psapi.GetModuleFileNameExW(h, None, buf, 512)
        _kernel32.CloseHandle(h)
        return os.path.basename(buf.value).lower()
    except Exception:
        return ""


def _is_terminal(hwnd: int) -> bool:
    if _get_class(hwnd) in _TERMINAL_CLASSES:
        return True
    # Check per-app override first — if user pinned this exe to ctrl_shift_v it acts as terminal
    exe = _get_exe_name(hwnd)
    override = _per_app_paste.get(exe)
    if override == "ctrl_shift_v":
        return True
    return False


def _needs_slow_settle(hwnd: int) -> bool:
    cls = _get_class(hwnd)
    return any(s in cls for s in _SLOW_FOCUS_CLASSES_SUBSTR)


def _is_rdp(hwnd: int) -> bool:
    cls = _get_class(hwnd)
    return "TscShellContainer" in cls or "RAIL_WINDOW" in cls


def prime_foreground(hwnd: int) -> None:
    """Call this BEFORE closing the preview window so we still own the foreground.

    Windows blocks SetForegroundWindow from processes that don't own the foreground.
    By calling this while the Tkinter preview is still alive (and thus the foreground),
    the focus transfer to hwnd succeeds reliably.
    """
    if hwnd and win32gui.IsWindow(hwnd):
        _force_foreground(hwnd)


def activate_window(hwnd: int) -> None:
    """Bring hwnd to the foreground and give it keyboard focus.

    Uses AttachThreadInput so the call works even when our process is NOT the
    current foreground (e.g. VS Code has focus when the preview panel opens).
    overrideredirect(True) Tkinter popups don't auto-activate on Windows, so we
    must call this explicitly to steal OS-level keyboard focus from the target app.
    """
    _force_foreground(hwnd)


class _GUITHREADINFO(ctypes.Structure):
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


def _get_focused_child(hwnd: int) -> int:
    """Find the focused Chrome_RenderWidgetHostHWND in an Electron window.

    VS Code is multi-process: the renderer widgets live in separate processes with
    their own thread IDs. GetGUIThreadInfo on the browser-process thread won't see
    them, so we enumerate child windows by class name instead.
    """
    candidates = []

    def _enum(child, _):
        try:
            if win32gui.GetClassName(child) == "Chrome_RenderWidgetHostHWND":
                candidates.append(child)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumChildWindows(hwnd, _enum, None)
    except Exception:
        pass

    if not candidates:
        return hwnd

    # Check each renderer's own thread for which one reports itself as focused
    for child in candidates:
        try:
            tid = _user32.GetWindowThreadProcessId(child, None)
            info = _GUITHREADINFO()
            info.cbSize = ctypes.sizeof(_GUITHREADINFO)
            if _user32.GetGUIThreadInfo(tid, ctypes.byref(info)):
                if info.hwndActive == child or info.hwndFocus == child:
                    return child
        except Exception:
            pass

    # None self-reported as focused — use first visible candidate
    for child in candidates:
        try:
            if win32gui.IsWindowVisible(child):
                return child
        except Exception:
            pass

    return candidates[0]


def _wait_focus_settled(target_hwnd: int, timeout_ms: int = 250) -> bool:
    """Poll until GetGUIThreadInfo reports target_hwnd as focused, or timeout."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        try:
            tid = _user32.GetWindowThreadProcessId(target_hwnd, None)
            info = _GUITHREADINFO()
            info.cbSize = ctypes.sizeof(_GUITHREADINFO)
            if _user32.GetGUIThreadInfo(tid, ctypes.byref(info)):
                if info.hwndFocus == target_hwnd or info.hwndActive == target_hwnd:
                    return True
        except Exception:
            pass
        time.sleep(0.01)
    return False


def _set_focus_on_child(child_hwnd: int) -> None:
    """Attach to the child widget's own thread and call SetFocus + SetActiveWindow."""
    cur = _kernel32.GetCurrentThreadId()
    tid = _user32.GetWindowThreadProcessId(child_hwnd, None)
    attached = False
    if tid and tid != cur:
        _user32.AttachThreadInput(tid, cur, True)
        attached = True
    try:
        _user32.SetFocus(child_hwnd)
        _user32.SetActiveWindow(child_hwnd)
    except Exception:
        pass
    finally:
        if attached:
            _user32.AttachThreadInput(tid, cur, False)


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
    Electron/Chromium/UWP and RDP hosts silently ignore WM_PASTE — claiming
    success for them suppresses the failure toast AND lets the restore thread
    wipe our text off the clipboard, so nothing pastes and the text vanishes.
    Refuse the fallback for those targets so the caller surfaces the toast and
    leaves the text on the clipboard for a manual Ctrl+V.
    """
    cls = _get_class(hwnd)
    if any(s in cls for s in _SLOW_FOCUS_CLASSES_SUBSTR):
        return False
    try:
        info = _GUITHREADINFO()
        info.cbSize = ctypes.sizeof(_GUITHREADINFO)
        tid = _user32.GetWindowThreadProcessId(hwnd, None)
        if not _user32.GetGUIThreadInfo(tid, ctypes.byref(info)):
            return False
        target = info.hwndFocus or hwnd
        win32api.SendMessage(target, _WM_PASTE, 0, 0)
        return True
    except Exception as exc:
        warn("inject", f"WM_PASTE fallback failed: {exc}")
        return False


_paste_failure_callback = None


def set_paste_failure_callback(fn) -> None:
    """Register a callback invoked with a human-readable reason when paste fails."""
    global _paste_failure_callback
    _paste_failure_callback = fn


def _notify_failure(reason: str) -> None:
    warn("inject", f"paste failed: {reason}")
    cb = _paste_failure_callback
    if cb:
        try:
            cb(reason)
        except Exception:
            pass


_paste_info_callback = None


def set_paste_info_callback(fn) -> None:
    """Register a callback for non-error notifications (e.g. clipboard-only mode)."""
    global _paste_info_callback
    _paste_info_callback = fn


def _notify_info(msg: str) -> None:
    log("inject", msg)
    cb = _paste_info_callback
    if cb:
        try:
            cb(msg)
        except Exception:
            pass


def _decide_method(hwnd: int) -> str:
    """Pick the paste method for this target. Returns 'type' | 'ctrl_v' | 'ctrl_shift_v'."""
    exe = _get_exe_name(hwnd)
    override = _per_app_paste.get(exe)
    if override in ("type", "ctrl_v", "ctrl_shift_v"):
        return override
    if _is_terminal(hwnd):
        # Terminals work well with Ctrl+Shift+V clipboard paste and benefit from
        # the speed when output is long. Don't slow them down by typing.
        return "ctrl_shift_v"
    if _is_rdp(hwnd):
        # Synthetic Unicode typing is unreliable over RDP — the KEYEVENTF_UNICODE
        # events frequently don't propagate into the remote session. Clipboard
        # Ctrl+V matches what works when pasting into a remote desktop by hand.
        return "ctrl_v"
    cls = _get_class(hwnd)
    if any(s in cls for s in ("Chrome_WidgetWin", "MozillaWindowClass")):
        # VS Code / Cursor / Electron / browsers: clipboard Ctrl+V is the reliable
        # default (typing gets swallowed under focus races). Configurable via
        # electron_paste_method for users who prefer typing.
        return _electron_paste_method
    # Default: Unicode typing for plain Win32 controls / dialogs.
    return "type"


_last_insert: dict | None = None   # {"hwnd": int, "chars": int} of the newest insert


def undo_last() -> bool:
    """Undo the most recent successful insert by sending Ctrl+Z to its target.

    One Ctrl+Z undoes a clipboard paste atomically in almost every app; typed
    (KEYEVENTF_UNICODE) inserts may need the app's own undo grouping, which
    modern editors also treat as one chunk.
    """
    global _last_insert
    li = _last_insert
    if not li:
        return False
    hwnd = li["hwnd"]
    if not win32gui.IsWindow(hwnd):
        return False
    _force_foreground(hwnd)
    time.sleep(0.15)
    _wait_modifiers_released(timeout_ms=400)
    _flush_all_modifiers()
    time.sleep(0.03)
    _send_keystroke([_VK_CONTROL], _VK_Z)
    log("inject", f"undo_last: sent Ctrl+Z to hwnd={hwnd}")
    _last_insert = None
    return True


def inject_text_and_submit(text: str, hwnd: int) -> None:
    """Insert text, then forward a single Enter to the target (insert-and-send)."""
    inject_text(text, hwnd)
    if _paste_mode == "clipboard_only":
        return
    time.sleep(0.15)  # let the target app process the paste before Enter lands
    _wait_modifiers_released(timeout_ms=600)
    _flush_all_modifiers()
    time.sleep(0.03)
    _send_inputs([_make_vk_input(_VK_RETURN, key_up=False),
                  _make_vk_input(_VK_RETURN, key_up=True)])
    log("inject", "inject_text_and_submit: forwarded Enter")


def inject_text(text: str, hwnd: int) -> None:
    global _last_insert
    if not text:
        return

    # Clipboard-only mode: don't inject anywhere — just copy and tell the user to paste.
    # A reliable manual fallback for apps that fight synthetic input.
    if _paste_mode == "clipboard_only":
        if _clipboard_set_text(text):
            _notify_info(f"Copied to clipboard ({len(text)} chars) — press Ctrl+V to paste")
        else:
            _notify_failure("Could not place text on clipboard")
        return

    if not hwnd or not win32gui.IsWindow(hwnd):
        log("inject", "no target hwnd")
        return

    target_cls   = _get_class(hwnd)
    target_exe   = _get_exe_name(hwnd)
    target_title = win32gui.GetWindowText(hwnd) if hwnd else ""
    method = _decide_method(hwnd)
    log("inject", f"target hwnd={hwnd} class={target_cls!r} exe={target_exe!r} "
                  f"title={target_title[:60]!r} method={method} chars={len(text)}")

    # UAC guard: SendInput cannot reach a higher-integrity process.
    if _is_higher_integrity_target(hwnd):
        _notify_failure(
            f"Cannot insert into elevated window ({target_exe or 'unknown'}). "
            "Run VoiceDictate as administrator to enable input into UAC-elevated apps."
        )
        return

    # Bring target to foreground (preview already called prime_foreground while
    # we still owned the foreground, so this should succeed).
    _force_foreground(hwnd)

    settle = 0.15
    if _needs_slow_settle(hwnd):
        settle = 0.40   # Electron internal focus is slow but we don't need clipboard polling now
    if _is_rdp(hwnd):
        settle = 0.40
    time.sleep(settle)

    # For Electron: target the actual focused renderer widget directly.
    is_electron = any(s in target_cls for s in _SLOW_FOCUS_CLASSES_SUBSTR) and not _is_rdp(hwnd)
    if is_electron:
        child = _get_focused_child(hwnd)
        _set_focus_on_child(child)
        log("inject", f"electron child: hwnd={child} class={_get_class(child)!r}")
        _wait_focus_settled(child, timeout_ms=250)

    # Wait for the user to physically release the recording hotkey before
    # injecting input. Ctrl/Alt still held will corrupt either typed chars or
    # the Ctrl+V keystroke.
    if not _wait_modifiers_released(timeout_ms=400):
        log("inject", f"modifiers still held after 400ms: {_modifiers_physically_down()}")
    _flush_all_modifiers()
    time.sleep(0.03)

    # Verify foreground is still our target before injecting input.
    fg = win32gui.GetForegroundWindow()
    if fg != hwnd and not _is_descendant(fg, hwnd):
        warn("inject", f"foreground drifted (got {fg}, want {hwnd}), re-priming")
        _force_foreground(hwnd)
        time.sleep(0.15)

    if method == "type":
        # Type characters via SendInput KEYEVENTF_UNICODE (no clipboard involvement).
        sent = _send_unicode_text(text)
        log("inject", f"typed {sent} chars via KEYEVENTF_UNICODE")
        if sent > 0:
            _last_insert = {"hwnd": hwnd, "chars": sent}
            return
        # SendInput inserted nothing (throttled / blocked / secure desktop). Safe to
        # fall back to clipboard paste because nothing landed — no duplication risk.
        warn("inject", "type path inserted 0 chars; falling back to clipboard Ctrl+V")
        method = "ctrl_v"

    # Clipboard paste path — terminals (Ctrl+Shift+V), RDP/Electron (Ctrl+V), and the
    # type-path fallback above.
    snapshot = _clipboard_snapshot()  # save ALL formats (CF_HDROP, CF_DIB, HTML, etc.)
    if not _clipboard_set_text(text):
        _notify_failure("Could not place text on clipboard")
        return

    # Brief pause so VS Code's clipboard-change listener (WM_CLIPBOARDUPDATE) finishes
    # processing before we send Ctrl+V. Without this, Electron can have BlockInput active
    # for a few ms while handling the clipboard notification.
    if is_electron:
        time.sleep(0.08)

    if method == "ctrl_shift_v":
        n = _send_keystroke([_VK_CONTROL, _VK_SHIFT], _VK_V)
    else:
        n = _send_keystroke([_VK_CONTROL], _VK_V)

    paste_blocked = False
    if n == 0:
        # SendInput inserted nothing at all — it's fully blocked for this target
        # (not just ignored). Last resort: WM_PASTE goes through SendMessage, not
        # SendInput, so it can still reach a plain Win32 edit control.
        warn("inject", "Ctrl+V keystroke blocked by SendInput; trying WM_PASTE fallback")
        if not _try_wm_paste(hwnd):
            paste_blocked = True
            _notify_failure("Paste blocked — text is in your clipboard, press Ctrl+V to paste")

    if paste_blocked:
        # Leave text on clipboard so the manual Ctrl+V in the toast actually works.
        return

    _last_insert = {"hwnd": hwnd, "chars": len(text)}

    def _restore():
        # Poll until clipboard no longer holds our injected text (paste consumed),
        # or until the configured deadline — whichever comes first.
        deadline = time.monotonic() + _restore_delay_ms / 1000 * 3
        step = 0.015
        while time.monotonic() < deadline:
            time.sleep(step)
            try:
                current = _clipboard_get_text()
                if current != text:
                    log("inject", "clipboard consumed early, restoring now")
                    break
            except Exception:
                break
        try:
            _clipboard_restore_snapshot(snapshot)
        except Exception as exc:
            warn("inject", f"clipboard restore failed: {exc}")

    threading.Thread(target=_restore, daemon=True).start()


def _is_descendant(child_hwnd: int, ancestor_hwnd: int) -> bool:
    """Return True if child_hwnd is ancestor_hwnd or one of its descendants.

    Used to accept the case where Electron's focused renderer child window
    becomes the foreground rather than the top-level VS Code window.
    """
    if child_hwnd == ancestor_hwnd:
        return True
    cur = child_hwnd
    for _ in range(20):  # safety bound against pathological loops
        try:
            parent = win32gui.GetParent(cur)
        except Exception:
            return False
        if not parent:
            return False
        if parent == ancestor_hwnd:
            return True
        cur = parent
    return False
