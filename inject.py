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

# inject_text() outcome, so callers can offer a retry instead of the text
# silently ending up only on the clipboard (or nowhere at all).
#
# INSERTED vs INSERTED_UNCONFIRMED is the distinction the app lacked: SendInput
# accepting our events only means Windows queued them, never that the target
# consumed them. With no editable element focused the characters go nowhere,
# and reporting that as success is what destroyed dictations.
INSERTED  = "inserted"
INSERTED_UNCONFIRMED = "inserted_unconfirmed"
CLIPBOARD = "clipboard"
FAILED    = "failed"
# Distinct from CLIPBOARD so the recovery panel can say WHY. A plain CLIPBOARD
# has six different causes; this one means the pre-flight probe was confident
# there was no editable field, so nothing was ever sent.
REFUSED_NOT_EDITABLE = "refused_not_editable"

# Pre-flight verdicts (see set_preflight_callback).
EDITABLE     = "EDITABLE"
NOT_EDITABLE = "NOT_EDITABLE"
UNKNOWN      = "UNKNOWN"


def landed(status: str) -> bool:
    """True when the text went out to the target. Not the same as confirmed:
    an unconfirmed insert probably landed and must not be re-sent, or the user
    gets it twice."""
    return status in (INSERTED, INSERTED_UNCONFIRMED)


_restore_delay_ms = 150
# Whether the user's previous clipboard stays dropped when we could not confirm
# the insert landed. True = never lose the dictation, at the cost of the old
# clipboard contents. See should_restore_clipboard().
_retain_on_unconfirmed = True
# RDP clipboard timing: rdpclip propagates the format list to the remote session
# asynchronously and serves the data via delayed rendering, so the local timings
# above are far too tight for an RDP target.
_rdp_clipboard_settle_ms = 250
_rdp_clipboard_restore_delay_ms = 3000
# How a target receives text. "type" is SendInput KEYEVENTF_UNICODE; the rest
# put the text on the clipboard and send that app's paste keystroke.
PASTE_METHODS = ("type", "ctrl_v", "ctrl_shift_v", "shift_insert")
# Terminals get Shift+Insert, not Ctrl+Shift+V. Legacy conhost (cmd.exe, the
# old PowerShell console) has no Ctrl+Shift+V binding at all, which is why
# right-click was the only paste that worked there. Shift+Insert is bound in
# conhost, Windows Terminal, mintty, ConEmu and Alacritty alike, so it is the
# one keystroke that covers every terminal we detect.
_TERMINAL_PASTE_METHOD = "shift_insert"
_per_app_paste: dict = {}   # exe_name_lower → one of PASTE_METHODS
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

_VK_INSERT = 0x2D
_VK_RETURN = 0x0D
_VK_TAB    = 0x09
_VK_BACK   = 0x08
_VK_Z      = 0x5A
_VK_C      = 0x43

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
    "Tauri Window",       # Tauri apps (WebView2) — Flightdeck
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


# Virtual keys that MUST carry LLKHF_EXTENDED, because their scan code is
# shared with a numpad key and the extended bit is the only thing telling them
# apart. MapVirtualKey(VK_INSERT) returns 0x52, and scan 0x52 WITHOUT the
# extended bit is numpad 0 — so a Shift+Insert paste sent without this types a
# literal "0" into the target whenever NumLock is on. Same shadowing that made
# the numpad "." collide with Delete (hotkey.py).
_EXTENDED_VKS = frozenset({_VK_INSERT})


def _make_key_input(vk: int, key_up: bool) -> _INPUT:
    hkl = _user32.GetKeyboardLayout(0)
    scan = (_user32.MapVirtualKeyExW(vk, _MAPVK_VK_TO_VSC, hkl) or
            _user32.MapVirtualKeyW(vk, _MAPVK_VK_TO_VSC)) & 0xFFFF
    flags = _KEYEVENTF_SCANCODE
    if key_up:
        flags |= _KEYEVENTF_KEYUP
    if vk in _EXTENDED_VKS:
        flags |= _KEYEVENTF_EXTENDED
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
    n = _send_inputs(inputs)
    if 0 < n < len(inputs):
        # Partial insert can land a modifier-down without its matching up,
        # leaving Ctrl/Shift logically stuck system-wide. Re-send the ups.
        warn("inject", f"SendInput inserted {n}/{len(inputs)} events; re-releasing modifiers")
        _send_inputs([_make_key_input(vk, key_up=True) for vk in reversed(vks_to_hold)])
    return n


def _flush_modifier(vk: int) -> None:
    """Send key-up for a modifier in case it's stuck from the recording hotkey."""
    inp = _make_key_input(vk, key_up=True)
    _send_inputs([inp])


# (scan code, extended flag) for both physical variants of each modifier.
# Used by the forced flush — an RDP session can hold a modifier the local
# key state knows nothing about, so we can't derive which side to release.
_MOD_RELEASE_SCANCODES = (
    (0x1D, False),  # left ctrl
    (0x1D, True),   # right ctrl
    (0x38, False),  # left alt
    (0x38, True),   # right alt
    (0x2A, False),  # left shift
    (0x36, False),  # right shift
    (0x5B, True),   # left win
    (0x5C, True),   # right win
)


def _make_scan_up(scan: int, extended: bool) -> _INPUT:
    flags = _KEYEVENTF_SCANCODE | _KEYEVENTF_KEYUP
    if extended:
        flags |= _KEYEVENTF_EXTENDED
    inp = _INPUT()
    inp.type = _INPUT_KEYBOARD
    inp.ki = _KEYBDINPUT(wVk=0, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    return inp


def _flush_all_modifiers(force: bool = False) -> None:
    if force:
        # RDP targets: mstsc forwards key events to the remote session, and it
        # drops key-ups when focus changes mid-hold (recording hotkey) or when
        # our synthetic Ctrl-up gets lost in forwarding. The remote then has
        # Ctrl/Alt logically stuck — clicks become ctrl-clicks, right-click
        # misbehaves — and the local physical key state can't see it. While the
        # RDP window has focus, send unconditional key-ups for every modifier
        # variant; unmatched key-ups are no-ops for apps that aren't stuck.
        # Key-UPS ONLY, never downs. The 2026-08-12 down+up "tap" experiment
        # made things dramatically worse: mstsc sometimes loses key-ups around
        # focus changes, so injecting a synthetic Ctrl-DOWN into that channel
        # can latch the remote session by our own hand — a dropped up after our
        # own down is strictly worse than a stuck key we failed to clear.
        # Unmatched ups are no-ops everywhere; unmatched downs are landmines.
        inputs = [_make_scan_up(s, e) for s, e in _MOD_RELEASE_SCANCODES]
        n = _send_inputs(inputs)
        msg = (f"force flush: SendInput inserted {n}/{len(inputs)} events; "
               f"local logical mods down before flush: {_logical_modifiers_down() or 'none'}")
        if n < len(inputs):
            warn("inject", msg)
        else:
            log("inject", msg)
        return
    # Only flush keys that are still physically held — don't send spurious
    # synthetic key-ups for already-released keys (confuses Electron/VS Code).
    held = _modifiers_physically_down()
    for vk in (_VK_CONTROL, _VK_MENU, _VK_SHIFT, _VK_LWIN, _VK_RWIN):
        if vk in held:
            _flush_modifier(vk)


_LOGICAL_MOD_VKS = (
    (0xA2, "lctrl"), (0xA3, "rctrl"),
    (0xA4, "lalt"),  (0xA5, "ralt"),
    (0xA0, "lshift"), (0xA1, "rshift"),
    (0x5B, "lwin"),  (0x5C, "rwin"),
)


def _logical_modifiers_down() -> list[str]:
    """Side-specific snapshot of the LOCAL logical modifier state, for the
    flush log line. Discriminates the failure layer on the next report: a key
    listed here while the remote misbehaves means the corruption is local (our
    synthetic input or the keyboard hook), an empty list means it is on the
    mstsc/remote side."""
    return [name for vk, name in _LOGICAL_MOD_VKS
            if _user32.GetAsyncKeyState(vk) & 0x8000]


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


def wait_modifiers_released(timeout_ms: int = 400) -> bool:
    """Public wrapper — preview.py waits on this before stealing focus so an
    RDP window in the foreground still receives the physical hotkey key-ups."""
    return _wait_modifiers_released(timeout_ms)


def modifiers_physically_down() -> list[int]:
    """Public wrapper. preview.py polls this to DEFER stealing focus until every
    modifier is physically up: the target app received the hotkey key-downs, so
    it must also receive the key-ups. If the preview grabs focus mid-hold, the
    ups land in the preview instead and the target (an RDP session, or an
    Electron app tracking modifiers from its own event stream) keeps the
    modifier latched: clicks act as ctrl-clicks, Tab acts as Alt+Tab."""
    return _modifiers_physically_down()


def flush_rdp_if_foreground() -> bool:
    """Force-flush modifiers right now if an RDP window owns the foreground.
    Returns True when it flushed (an RDP window was in front), else False.

    Synchronous on purpose: the caller needs to know BEFORE acting whether the
    foreground is an RDP session — the preview uses True to skip its focus
    steal entirely, because stealing focus from mstsc (SetForegroundWindow +
    AttachThreadInput against mstsc's input queue) right after key events were
    in flight is exactly the moment mstsc loses key-ups. Costs 50ms only when
    an RDP window is in front; returns immediately otherwise."""
    fg = win32gui.GetForegroundWindow()
    if not fg or not _is_rdp(fg):
        return False
    time.sleep(0.05)  # let mstsc forward the physical key-ups first
    _flush_all_modifiers(force=True)
    log("inject", "RDP foreground — force-flushed modifier key-ups")
    return True


def flush_hotkey_modifiers_async() -> None:
    """Fire-and-forget cleanup after ANY recording-hotkey interaction ends.

    mstsc forwards the physical hotkey key-downs (Ctrl/Shift/Alt) into the
    remote session but can drop the matching key-ups, leaving modifiers
    logically stuck remotely — shift-click opens new windows, ctrl-click opens
    new tabs, the number row stops working. The paste path already force-
    flushes for RDP targets, but flows that never inject (cancel, too-short)
    ended with no flush at all.

    Runs a bounded watcher: wait for the physical release, then watch the
    foreground for up to 4s and force-flush the first time an RDP window holds
    it. Watching rather than sampling once matters because the preview panel or
    a toast can own the foreground at the instant the hotkey ends, with the RDP
    window coming back moments later. Unmatched key-ups are no-ops for
    everything else."""
    threading.Thread(target=_flush_hotkey_modifiers, daemon=True).start()


def _flush_hotkey_modifiers() -> None:
    _wait_modifiers_released(timeout_ms=2000)
    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline:
        fg = win32gui.GetForegroundWindow()
        if fg and _is_rdp(fg):
            time.sleep(0.05)  # let mstsc forward the physical key-ups first
            _flush_all_modifiers(force=True)
            log("inject", "hotkey ended — force-flushed modifier key-ups with RDP foreground")
            return
        time.sleep(0.1)
    # Naming what IS in front is the whole value of this line. The old version
    # said only that RDP was not, which is why four rounds of this fix were
    # argued from no evidence at all.
    log("inject", "hotkey ended, RDP never took foreground within 4s, no flush "
                  f"needed; foreground now: {_describe_window(win32gui.GetForegroundWindow())}")


# ---------------------------------------------------------------------------
# Persistent foreground watcher (PASTE_UX_PLAN section 6, RDP round 5).
#
# The 4s window above provably never fires: every hotkey release in app.log
# logged "RDP never took foreground within 4s" while Ctrl stayed latched in the
# remote session. The client is mstsc, which _is_rdp already matches, so the
# miss is the window of observation, not the detection. A SetWinEventHook on
# EVENT_SYSTEM_FOREGROUND has no window at all: whenever an RDP session takes
# the foreground, minutes later or seconds, the key-ups go in.
#
# Key-ups only, forever. See the comment in _flush_all_modifiers: the
# 2026-08-12 synthetic-down "tap" experiment made the latch dramatically worse
# and is permanently reverted.
# ---------------------------------------------------------------------------

_EVENT_SYSTEM_FOREGROUND = 0x0003
_WINEVENT_OUTOFCONTEXT   = 0x0000
_WINEVENT_SKIPOWNPROCESS = 0x0002
_WM_QUIT                 = 0x0012

# Never flush more than this often, so alt-tabbing between two RDP windows
# cannot flood SendInput.
_FG_FLUSH_MIN_INTERVAL = 0.25
# How long mstsc gets to settle after taking the foreground before we send.
_FG_FLUSH_SETTLE = 0.05

_WINEVENTPROC = ctypes.WINFUNCTYPE(
    None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
    wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD)

_user32.SetWinEventHook.restype  = wintypes.HANDLE
_user32.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE,
                                    _WINEVENTPROC, wintypes.DWORD, wintypes.DWORD,
                                    wintypes.DWORD]
_user32.UnhookWinEvent.argtypes  = [wintypes.HANDLE]

# Module-level on purpose. The ctypes trampoline must outlive the call that
# installed it: if it is garbage collected the hook stays registered and every
# foreground change calls into freed memory. That is the classic silent failure
# for this API and it takes the watcher with it.
_fg_hook_proc = None
_fg_hook_handle = 0
_fg_hook_thread = None
_fg_hook_tid = 0
_last_fg_flush = 0.0
_fg_lock = threading.Lock()


def _describe_window(hwnd: int) -> str:
    """exe, class and title for a diagnostic log line. Cheap and never raises."""
    if not hwnd:
        return "no foreground window"
    try:
        title = str(win32gui.GetWindowText(hwnd) or "")
    except Exception:
        title = ""
    if len(title) > 60:
        title = title[:57] + "..."
    return (f"exe={_get_exe_name(hwnd) or '?'} "
            f"class={_get_class(hwnd) or '?'} title={title!r}")


def _fg_flush_allowed() -> bool:
    """Rate limiter for the foreground watcher. True at most once per 250ms."""
    global _last_fg_flush
    now = time.monotonic()
    with _fg_lock:
        if now - _last_fg_flush < _FG_FLUSH_MIN_INTERVAL:
            return False
        _last_fg_flush = now
        return True


def _settle_and_flush(hwnd: int) -> None:
    """Let mstsc finish taking focus, then send the key-ups. Runs off the hook
    thread so the callback itself stays cheap."""
    time.sleep(_FG_FLUSH_SETTLE)
    _flush_all_modifiers(force=True)
    log("inject", f"RDP took foreground, force-flushed modifier key-ups: "
                  f"{_describe_window(hwnd)}")


_last_declined: str = ""   # dedupe key for the non-RDP decline log


def _handle_foreground_window(hwnd: int) -> None:
    """One foreground change. Split out of the ctypes callback so the callback
    body is nothing but a try/except."""
    global _last_declined
    if not hwnd:
        return
    if not _is_rdp(hwnd):
        # Log the decline, but only when the window identity actually changes.
        # Every alt-tab fires this hook, and an unconditional line adds a few
        # hundred entries a day to the log we rely on for RDP diagnosis. The
        # identity is still what matters: if the RDP client ever shows up here
        # instead of in the flush branch, _is_rdp is the thing that is wrong.
        desc = _describe_window(hwnd)
        if desc != _last_declined:
            _last_declined = desc
            log("inject", f"foreground changed, not RDP, no flush: {desc}")
        return
    if not _fg_flush_allowed():
        log("inject", "RDP foreground again within 250ms, flush rate-limited")
        return
    threading.Thread(target=_settle_and_flush, args=(hwnd,), daemon=True).start()


def _on_foreground_change(hook, event, hwnd, id_object, id_child, thread_id, ts) -> None:
    """WinEvent callback. Must never let an exception escape: one that does
    tears the hook down and the watcher is lost for the rest of the session,
    silently. Everything it can do is therefore inside the try."""
    try:
        _handle_foreground_window(hwnd)
    except Exception as exc:
        try:
            warn("inject", f"foreground watcher callback failed: {exc}")
        except Exception:
            pass


def _foreground_watch_loop(ready: threading.Event) -> None:
    """Install the hook and pump messages for it. An out-of-context WinEvent
    hook is delivered through the installing thread's message queue, so it
    needs a GetMessage loop of its own; without one the callback never fires."""
    global _fg_hook_proc, _fg_hook_handle, _fg_hook_tid
    try:
        _fg_hook_tid = _kernel32.GetCurrentThreadId()
        _fg_hook_proc = _WINEVENTPROC(_on_foreground_change)
        _fg_hook_handle = _user32.SetWinEventHook(
            _EVENT_SYSTEM_FOREGROUND, _EVENT_SYSTEM_FOREGROUND, None,
            _fg_hook_proc, 0, 0,
            _WINEVENT_OUTOFCONTEXT | _WINEVENT_SKIPOWNPROCESS)
        if not _fg_hook_handle:
            warn("inject", "SetWinEventHook failed, no persistent RDP modifier flush")
            return
        log("inject", "foreground watcher installed")
    except Exception as exc:
        warn("inject", f"foreground watcher failed to start: {exc}")
        return
    finally:
        ready.set()
    try:
        msg = wintypes.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))
    finally:
        # UnhookWinEvent belongs on the thread that installed the hook.
        try:
            _user32.UnhookWinEvent(_fg_hook_handle)
        except Exception:
            pass
        _fg_hook_handle = 0
        log("inject", "foreground watcher stopped")


def start_foreground_watch() -> bool:
    """Watch for RDP windows taking the foreground and flush modifier key-ups
    into them, for the life of the process. Idempotent; returns True when the
    hook is live."""
    global _fg_hook_thread
    with _fg_lock:
        if _fg_hook_thread is not None and _fg_hook_thread.is_alive():
            return bool(_fg_hook_handle)
        ready = threading.Event()
        _fg_hook_thread = threading.Thread(
            target=_foreground_watch_loop, args=(ready,),
            name="fg-watch", daemon=True)
        thread = _fg_hook_thread
    thread.start()
    ready.wait(2.0)
    return bool(_fg_hook_handle)


def stop_foreground_watch() -> None:
    """Stop the pump; the loop unhooks itself on the way out. Safe to call when
    the watcher never started."""
    global _fg_hook_thread
    thread, tid = _fg_hook_thread, _fg_hook_tid
    if thread is None or not tid:
        return
    try:
        _user32.PostThreadMessageW(tid, _WM_QUIT, 0, 0)
    except Exception:
        pass
    thread.join(timeout=1.0)
    _fg_hook_thread = None


def unstick_modifiers() -> str:
    """Manual escape hatch: force-flush every modifier key-up into whatever
    holds the foreground right now, whatever it is.

    Every automatic path in this file first has to guess that an RDP window is
    involved. This one guesses nothing, which is the point: it still works when
    every heuristic in the app is wrong. Key-ups only, so it is a no-op when
    nothing is actually stuck. Returns a short line for the toast."""
    try:
        fg = win32gui.GetForegroundWindow()
    except Exception:
        fg = 0
    where = _describe_window(fg)
    before = _logical_modifiers_down() or ["none"]
    _flush_all_modifiers(force=True)
    after = _logical_modifiers_down() or ["none"]
    log("inject", f"unstick: flushed modifier key-ups into {where}; "
                  f"local logical mods before=[{','.join(before)}] "
                  f"after=[{','.join(after)}]")
    exe = _get_exe_name(fg) if fg else ""
    return f"Modifiers released into {exe or 'the focused window'}"


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


def copy_text(text: str) -> bool:
    """Public clipboard write for fallback paths (used when there is no paste
    target to inject into). No snapshot/restore: the point IS to hand the text
    to the user."""
    return _clipboard_set_text(text)


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
              paste_mode: str | None = None,
              rdp_clipboard_settle_ms: int | None = None,
              rdp_clipboard_restore_delay_ms: int | None = None,
              clipboard_retain_on_unconfirmed: bool = True) -> None:
    global _restore_delay_ms, _per_app_paste, _electron_paste_method, _paste_mode
    global _rdp_clipboard_settle_ms, _rdp_clipboard_restore_delay_ms
    global _retain_on_unconfirmed
    _restore_delay_ms = restore_delay_ms
    _retain_on_unconfirmed = bool(clipboard_retain_on_unconfirmed)
    if rdp_clipboard_settle_ms is not None:
        _rdp_clipboard_settle_ms = rdp_clipboard_settle_ms
    if rdp_clipboard_restore_delay_ms is not None:
        _rdp_clipboard_restore_delay_ms = rdp_clipboard_restore_delay_ms
    if per_app_paste is not None:
        _per_app_paste = {k.lower(): v for k, v in per_app_paste.items()}
    if electron_paste_method in ("type", "ctrl_v", "ctrl_shift_v"):
        _electron_paste_method = electron_paste_method
    if paste_mode in ("auto", "clipboard_only"):
        _paste_mode = paste_mode


def _is_own_window(hwnd: int) -> bool:
    """True for our own surfaces: tray icon message window, preview panel,
    badge, and the dashboard subprocess. Typing a dictation into one of those
    loses it (seen 2026-07-12: 498 chars typed into the hidden pystray
    window)."""
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value == os.getpid():
        return True
    # The dashboard runs as a separate python subprocess with this exact title.
    try:
        title = win32gui.GetWindowText(hwnd)
    except Exception:
        title = ""
    return title == "Quiett" and _get_exe_name(hwnd).startswith("python")


def capture_foreground(quiet: bool = False) -> int:
    """Foreground hwnd to paste into later, or 0 when it is one of our own
    windows. quiet=True suppresses the log line for speculative captures (the
    one taken at hotkey-down), where our own window in front is normal and the
    hotkey-up capture will usually resolve it anyway."""
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return 0
    if _is_own_window(hwnd):
        if not quiet:
            warn("inject", f"foreground is our own window (class='{_get_class(hwnd)}') — no paste target")
        return 0
    return hwnd


def is_usable_target(hwnd: int) -> bool:
    """Still a live, visible, non-ours window worth pasting into."""
    if not hwnd:
        return False
    try:
        return bool(win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd)
                    and not _is_own_window(hwnd))
    except Exception:
        return False


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
    # Check per-app override first — an exe the user pinned to a terminal paste
    # keystroke is being told it is a terminal, so treat it as one.
    exe = _get_exe_name(hwnd)
    override = _per_app_paste.get(exe)
    if override in ("ctrl_shift_v", "shift_insert"):
        return True
    return False


def _needs_slow_settle(hwnd: int) -> bool:
    cls = _get_class(hwnd)
    return any(s in cls for s in _SLOW_FOCUS_CLASSES_SUBSTR)


def _is_rdp(hwnd: int) -> bool:
    cls = _get_class(hwnd)
    if "TscShellContainer" in cls or "RAIL_WINDOW" in cls:
        return True
    # msrdc / msrdcw (new Remote Desktop, AVD, Windows App) don't use the mstsc
    # class names
    return _get_exe_name(hwnd) in ("mstsc.exe", "msrdc.exe", "msrdcw.exe")


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
    if _user32.GetForegroundWindow() == hwnd:
        # Already in front — do nothing. This matters for RDP targets: the
        # preview no longer steals focus from mstsc, so at insert time the RDP
        # window usually still owns the foreground and attaching to mstsc's
        # input queue here would risk eating in-flight key events for nothing.
        return
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


# ---------------------------------------------------------------------------
# Target probing, best effort. Never allowed to block an insert on its own
# uncertainty (PASTE_UX_PLAN section 1). targetprobe.py supplies both hooks;
# with neither set the behaviour is today's, minus the false "inserted".
# ---------------------------------------------------------------------------

_preflight_fn = None
_verify_fn = None


def set_preflight_callback(fn) -> None:
    """fn(hwnd) -> "EDITABLE" | "NOT_EDITABLE" | "UNKNOWN", called before the
    insert. Only a confident NOT_EDITABLE stops it; UNKNOWN always attempts,
    because the probe is blind on Tk, canvas apps and RDP sessions and
    refusing on uncertainty would break apps that work today."""
    global _preflight_fn
    _preflight_fn = fn


def set_verify_callback(fn) -> None:
    """fn(hwnd, before_token) -> True | False | None, called after the insert.

    True = confirmed landed, False = confirmed did NOT land, None = no signal
    available for this target. before_token carries what the insert knew
    (expected char count, method, start time) so the verifier can compare its
    own pre-insert snapshot, taken during the pre-flight, against the target
    now."""
    global _verify_fn
    _verify_fn = fn


def _preflight(hwnd: int) -> str:
    """Pre-flight verdict for hwnd. A probe that raises is no signal at all,
    never a reason to refuse an insert."""
    fn = _preflight_fn
    if fn is None:
        return UNKNOWN
    try:
        verdict = fn(hwnd)
    except Exception as exc:
        warn("inject", f"pre-flight probe failed: {exc}")
        return UNKNOWN
    return verdict if verdict in (EDITABLE, NOT_EDITABLE, UNKNOWN) else UNKNOWN


def _verify(hwnd: int, before_token: dict) -> bool | None:
    """Post-insert verification signal for hwnd, or None when there is none."""
    fn = _verify_fn
    if fn is None:
        return None
    try:
        result = fn(hwnd, before_token)
    except Exception as exc:
        warn("inject", f"insert verification failed: {exc}")
        return None
    return result if result is None else bool(result)


def _post_insert_status(hwnd: int, before_token: dict, text: str,
                        on_clipboard: bool) -> str:
    """Grade an insert that SendInput accepted.

    Accepted is not landed, so this returns INSERTED only on a confident yes
    and INSERTED_UNCONFIRMED otherwise.

    A negative verdict no longer downgrades the status or puts anything on
    screen. It cannot be trusted: UI Automation reads a length of 0 for most
    real targets whichever way the insert went, and the "after" read races the
    target's own processing of the input we just sent. Measured 2026-08-23,
    every one of Notepad, conhost, a Chrome textarea and Flightdeck graded
    "unconfirmed" on inserts that a screenshot showed landing perfectly, and
    app.log holds 14 confident "NOT landed" verdicts. The verifier is now
    advisory: it can promote a status to INSERTED, never demote one. The
    clipboard, not the verdict, is what makes a missed insert recoverable.
    """
    verdict = _verify(hwnd, before_token)
    if verdict is True:
        log("inject", "insert confirmed landed in the target")
        return INSERTED
    if verdict is None:
        log("inject", "insert unconfirmed: no verification signal for this target")
    else:
        log("inject", "verifier says not landed; treating as unconfirmed "
                      "(the text is on the clipboard either way)")
    return INSERTED_UNCONFIRMED


def should_restore_clipboard(status: str, retain_on_unconfirmed: bool | None = None) -> bool:
    """Never. The last dictation always stays on the clipboard.

    Kept as a function so callers and settings keep working, but the answer is
    now fixed. Restoring the previous clipboard is what destroyed dictations:
    the characters did not reach the target and 150-450ms later the old
    clipboard came back over the top of the only remaining copy. Making the
    restore conditional on a verdict only narrowed that window, and the verdict
    turned out to be unreliable. Balu's ruling, 2026-08-23: the last thing he
    spoke sits in the clipboard, full stop. Older transcripts are in the
    history page and the tray's recent-dictations submenu.
    """
    return False


def _decide_method(hwnd: int) -> str:
    """Pick the paste method for this target. Returns one of PASTE_METHODS.

    Ctrl+V is the default because it is the universal paste gesture and it
    never corrupts the text. Typing is the exception, for the two target
    families measured to have no working Ctrl+V.

    Measured end to end 2026-08-23 against live windows, reading back what
    actually arrived from a screenshot rather than trusting a status string:

        target                      ctrl_v   type            shift_insert
        Notepad (Win11 WinUI)       exact    MANGLED         -
        conhost (cmd.exe)           exact    exact           nothing
        Chrome <textarea>           exact    exact           -
        Flightdeck xterm.js/Tauri   NOTHING  exact           nothing

    Two findings drive this table:

    Windows 11 Notepad SILENTLY CORRUPTS typed text. "FIXED notepad ok"
    arrived as "FIXED kkkkkkkkkk" and "AAtype fox" as "AAtype xxx" — a run of
    characters all replaced by the last one in the burst. Its WinUI/TSF input
    stack resolves each VK_PACKET against the CURRENT async key state instead
    of the queued event, so a fast burst collapses. Every batch size and pace
    tried (32/8/4/1 chars per SendInput, 0-5ms apart) mangled it; only a pace
    far too slow for a 465-char dictation survives. Ctrl+V is byte-exact
    there, so Ctrl+V is what a normal editable target gets.

    Flightdeck's terminal is xterm.js inside a WebView2, and it has no Ctrl+V
    paste binding at all — scan-code events, virtual-key events, both
    together, and keybd_event all pasted nothing, which is why Balu was down
    to right-clicking. Typing lands there every time. Routing Tauri to Ctrl+V
    on 2026-08-23 (a3339a5) is the regression he reported; it was chosen off
    six "failed" inserts that were really the verifier's false negative, fixed
    separately in 1b4db7c.

    RDP keeps Ctrl+V for its own reason: KEYEVENTF_UNICODE events frequently
    do not propagate into a remote session at all.
    """
    exe = _get_exe_name(hwnd)
    override = _per_app_paste.get(exe)
    if override in PASTE_METHODS:
        return override
    if _is_rdp(hwnd):
        return "ctrl_v"
    if _is_terminal(hwnd) or "Tauri Window" in _get_class(hwnd):
        # Terminals and terminal-hosting webviews. conhost has no working
        # Shift+Insert and turns Ctrl+Shift+V into a literal "^V"; Flightdeck
        # answers no paste keystroke at all. Typing lands in both.
        return "type"
    return "ctrl_v"


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
    _wait_modifiers_released(timeout_ms=1000)
    _flush_all_modifiers(force=_is_rdp(hwnd))
    time.sleep(0.03)
    _send_keystroke([_VK_CONTROL], _VK_Z)
    if _is_rdp(hwnd):
        time.sleep(0.05)
        _flush_all_modifiers(force=True)
    log("inject", f"undo_last: sent Ctrl+Z to hwnd={hwnd}")
    _last_insert = None
    return True


def get_selected_text(timeout_ms: int = 600) -> str:
    """Copy the current selection via synthetic Ctrl+C and return it, restoring
    the user's clipboard afterwards. GetClipboardSequenceNumber tells us when
    OUR copy has landed — polling for content would race other clipboard
    writers and can't distinguish a stale value from a fresh one."""
    old = _clipboard_get_text()
    seq0 = _user32.GetClipboardSequenceNumber()
    is_rdp_fg = _is_rdp(win32gui.GetForegroundWindow())
    if not _wait_modifiers_released(timeout_ms=2000):
        # Never synthesise Ctrl+C while the user still physically holds part of
        # the hotkey: a held Shift makes Windows see Ctrl+Shift+C and a held
        # Alt turns it into Ctrl+Alt+C for the target app.
        warn("inject", "get_selected_text: modifiers still held after 2s "
                       f"({_modifiers_physically_down()}), aborting the copy")
        return ""
    _flush_all_modifiers(force=is_rdp_fg)
    time.sleep(0.03)
    _send_keystroke([_VK_CONTROL], _VK_C)
    if is_rdp_fg:
        # mstsc can drop the synthetic Ctrl-up in forwarding (same failure the
        # paste path guards against) — flush while the RDP window has focus.
        time.sleep(0.05)
        _flush_all_modifiers(force=True)
    text = ""
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        time.sleep(0.03)
        if _user32.GetClipboardSequenceNumber() != seq0:
            time.sleep(0.02)  # give the source app time to finish writing
            text = _clipboard_get_text()
            break
    if old:
        _clipboard_set_text(old)
    return text


def inject_text_and_submit(text: str, hwnd: int) -> str:
    """Insert text, then forward a single Enter to the target (insert-and-send).

    Returns the same status string as inject_text(); the Enter forward never
    downgrades an insert that already landed. An unconfirmed insert still gets
    the Enter: the input almost certainly went in, and withholding it would
    make insert-and-send silently stop working on every target the probe
    cannot read."""
    status = inject_text(text, hwnd)
    if not landed(status):
        return status
    if _paste_mode == "clipboard_only":
        return status
    time.sleep(0.15)  # let the target app process the paste before Enter lands
    if not _wait_modifiers_released(timeout_ms=3000):
        # Enter under a still-held Ctrl would land as another Ctrl+Enter.
        warn("inject", "inject_text_and_submit: modifiers still held, skipping the Enter forward")
        return status
    _flush_all_modifiers(force=_is_rdp(hwnd))
    time.sleep(0.03)
    _send_inputs([_make_vk_input(_VK_RETURN, key_up=False),
                  _make_vk_input(_VK_RETURN, key_up=True)])
    log("inject", "inject_text_and_submit: forwarded Enter")
    return status


def inject_text(text: str, hwnd: int) -> str:
    """Insert text into hwnd. Returns one of:

    INSERTED  - the text is confirmed to have gone into the target window
                (or there was nothing to do)
    INSERTED_UNCONFIRMED - the input went out and probably landed, but
                nothing could confirm it; the text stays on the clipboard
    CLIPBOARD — the insert did not land, but the text is on the clipboard
    FAILED    — the insert did not land and the clipboard copy failed too
    """
    global _last_insert
    if not text:
        return INSERTED

    # Clipboard-only mode: don't inject anywhere — just copy and tell the user to paste.
    # A reliable manual fallback for apps that fight synthetic input.
    if _paste_mode == "clipboard_only":
        if _clipboard_set_text(text):
            _notify_info(f"Copied to clipboard ({len(text)} chars) — press Ctrl+V to paste")
            return CLIPBOARD
        _notify_failure("Could not place text on clipboard")
        return FAILED

    if not hwnd or not win32gui.IsWindow(hwnd):
        # No usable target — never drop the text; leave it on the clipboard.
        warn("inject", f"no paste target (hwnd={hwnd}) — {len(text)} chars left on the clipboard")
        if _clipboard_set_text(text):
            _notify_info(f"No target window — copied to clipboard ({len(text)} chars), press Ctrl+V to paste")
            return CLIPBOARD
        _notify_failure("No target window and clipboard copy failed")
        return FAILED

    target_cls   = _get_class(hwnd)
    target_exe   = _get_exe_name(hwnd)
    target_title = win32gui.GetWindowText(hwnd) if hwnd else ""
    method = _decide_method(hwnd)
    log("inject", f"target hwnd={hwnd} class={target_cls!r} exe={target_exe!r} "
                  f"title={target_title[:60]!r} method={method} chars={len(text)}")

    # UAC guard: SendInput cannot reach a higher-integrity process.
    if _is_higher_integrity_target(hwnd):
        # Copy first: without this the text was simply lost on elevated targets.
        copied = _clipboard_set_text(text)
        _notify_failure(
            f"Cannot insert into elevated window ({target_exe or 'unknown'}). "
            + ("Text copied, press Ctrl+V to paste. " if copied else "")
            + "Run Quiett as administrator to enable input into UAC-elevated apps."
        )
        return CLIPBOARD if copied else FAILED

    # Bring target to foreground (preview already called prime_foreground while
    # we still owned the foreground, so this should succeed).
    _force_foreground(hwnd)

    settle = 0.15
    if _needs_slow_settle(hwnd):
        settle = 0.40   # Electron internal focus is slow but we don't need clipboard polling now
    if _is_rdp(hwnd):
        settle = 0.40
    time.sleep(settle)

    # Chromium hosts (Electron, WebView2, browsers) get the longer settle above
    # and nothing else. We used to AttachThreadInput to the focused
    # Chrome_RenderWidgetHostHWND and force Win32 focus onto it; that step
    # DESTROYED the DOM focus inside a WebView2 host, so every keystroke after
    # it went nowhere. Measured 2026-08-23 against Flightdeck: typing with the
    # focus step inserted nothing, the identical run with the step removed
    # inserted the text. Chromium restores its own renderer focus when the
    # top-level window comes forward; taking it by hand only breaks that.
    is_electron = any(s in target_cls for s in _SLOW_FOCUS_CLASSES_SUBSTR) and not _is_rdp(hwnd)

    # Wait for the user to physically release the recording hotkey before
    # injecting input. Ctrl/Alt still held corrupts either the typed chars or
    # the Ctrl+V keystroke, and synthetic modifier-ups sent mid-hold get
    # re-asserted by hardware auto-repeat, so injecting anyway can latch a
    # modifier in the target. If the keys are still down after 3s, do not
    # inject at all: leave the text on the clipboard for a manual paste.
    # keys are still down after the wait, flush them and inject anyway rather
    # than refusing: the text is on the clipboard either way, and refusing was
    # firing the recovery panel on a condition the user could not see. A
    # latched Ctrl/Alt that no one is holding (Windows loses a key-up around a
    # focus change often enough) used to cost the whole insert.
    if not _wait_modifiers_released(timeout_ms=1500):
        warn("inject", "modifiers still held after 1.5s "
                       f"({_modifiers_physically_down()}), flushing and injecting anyway")
        _flush_all_modifiers(force=True)
        time.sleep(0.05)
    # Force-flush for RDP: clears modifiers stuck in the remote session (mstsc
    # has focus here, so the key-ups get forwarded) before we send Ctrl+V.
    _flush_all_modifiers(force=_is_rdp(hwnd))
    time.sleep(0.03)

    # Verify foreground is still our target before injecting input.
    fg = win32gui.GetForegroundWindow()
    if fg != hwnd and not _is_descendant(fg, hwnd):
        warn("inject", f"foreground drifted (got {fg}, want {hwnd}), re-priming")
        _force_foreground(hwnd)
        time.sleep(0.15)

    # Pre-flight: only a CONFIDENT "there is nowhere to type here" stops the
    # insert. UNKNOWN and EDITABLE both proceed, and that asymmetry is the rule
    # that keeps the probe from making working apps worse.
    #
    # This runs HERE, not before the foreground steal, and the ordering is the
    # whole point: the probe reads the focused UI element, so asking before
    # _force_foreground describes whatever happened to be in front (our own
    # preview panel, or the app the user was in), not the target we are about
    # to type into. Found by the E2E harness, 2026-08-22.
    if _preflight(hwnd) == NOT_EDITABLE:
        warn("inject", f"pre-flight says no editable field in hwnd={hwnd} "
                       f"({target_exe or 'unknown'}), not injecting, "
                       f"{len(text)} chars left on the clipboard")
        copied = _clipboard_set_text(text)
        _notify_failure(
            "No text field is focused. "
            + ("The text is on your clipboard. Click into the field you want, then insert again."
               if copied else "The clipboard copy failed too.")
        )
        return REFUSED_NOT_EDITABLE if copied else FAILED

    # Opaque handle for the verifier: what we are about to send, and when.
    before_token = {"chars": len(text), "method": method, "started": time.monotonic()}

    # The dictation goes on the clipboard BEFORE anything is sent, every time,
    # whatever the method and whatever happens next. That is the whole safety
    # net now: if the insert does not land, right-click paste or Ctrl+V always
    # works, and there is nothing to recover, restore or reason about. Ruled
    # 2026-08-23: "whatever the last thing I spoke about should sit in the
    # clipboard."
    on_clipboard = _clipboard_set_text(text)
    if not on_clipboard:
        warn("inject", "clipboard copy failed; the insert is the only chance this text gets")

    if method == "type":
        # Type characters via SendInput KEYEVENTF_UNICODE.
        sent = _send_unicode_text(text)
        log("inject", f"typed {sent} chars via KEYEVENTF_UNICODE")
        if sent > 0:
            _last_insert = {"hwnd": hwnd, "chars": sent}
            return _post_insert_status(hwnd, before_token, text, on_clipboard=on_clipboard)
        # SendInput inserted nothing (throttled / blocked / secure desktop). Safe to
        # fall back to clipboard paste because nothing landed — no duplication risk.
        warn("inject", "type path inserted 0 chars; falling back to clipboard Ctrl+V")
        method = "ctrl_v"

    # Clipboard paste path — RDP (Ctrl+V), per-app overrides, and the
    # type-path fallback above.
    if not on_clipboard:
        _notify_failure("Could not place text on clipboard")
        return FAILED

    # Brief pause so VS Code's clipboard-change listener (WM_CLIPBOARDUPDATE) finishes
    # processing before we send Ctrl+V. Without this, Electron can have BlockInput active
    # for a few ms while handling the clipboard notification.
    if is_electron:
        time.sleep(0.08)
    if _is_rdp(hwnd):
        # rdpclip announces the new format list to the remote session
        # asynchronously and serves the data by delayed rendering. Ctrl+V sent
        # before that lands pastes the previous clipboard content, or no-ops.
        time.sleep(_rdp_clipboard_settle_ms / 1000)

    if method == "ctrl_shift_v":
        n = _send_keystroke([_VK_CONTROL, _VK_SHIFT], _VK_V)
    elif method == "shift_insert":
        n = _send_keystroke([_VK_SHIFT], _VK_INSERT)
    else:
        n = _send_keystroke([_VK_CONTROL], _VK_V)

    paste_blocked = False
    if n == 0:
        # SendInput inserted nothing at all — it's fully blocked for this target
        # (not just ignored). Last resort: WM_PASTE goes through SendMessage, not
        # SendInput, so it can still reach a plain Win32 edit control.
        warn("inject", f"{method} keystroke blocked by SendInput; trying WM_PASTE fallback")
        if not _try_wm_paste(hwnd):
            paste_blocked = True
            _notify_failure("Paste blocked — text is in your clipboard, press Ctrl+V to paste")

    if paste_blocked:
        # Leave text on clipboard so the manual Ctrl+V in the toast actually works.
        return CLIPBOARD

    _last_insert = {"hwnd": hwnd, "chars": len(text)}

    if _is_rdp(hwnd):
        # mstsc can drop the synthetic Ctrl-up in forwarding, leaving Ctrl held
        # in the remote session. Flush again while the RDP window still has focus.
        time.sleep(0.05)
        _flush_all_modifiers(force=True)

    # No clipboard restore. The dictation stays on the clipboard until the next
    # dictation replaces it, so a failed insert is always a right-click or
    # Ctrl+V away. The save/restore dance is what used to wipe the only
    # remaining copy of a transcript 150-450ms after an insert that never
    # landed; dropping the previous clipboard contents is the accepted trade.
    return _post_insert_status(hwnd, before_token, text, on_clipboard=True)


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
