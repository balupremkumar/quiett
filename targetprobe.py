"""Target probe - can the focused element actually take dictated text?

The app has always injected blind: it fires SendInput at whatever holds focus
and reports success as soon as Windows accepts the events. If no editable
element had focus the characters go nowhere and the dictation is lost.

This module answers three questions, best effort, never authoritatively:
  1. Is there an editable target?      -> Target.verdict
  2. Where is it on screen?            -> Target.rect
  3. What is it called?                -> Target.label

Verdicts are deliberately three-valued. EDITABLE and NOT_EDITABLE are only
returned when the evidence is strong; everything else is UNKNOWN, and the
caller must still attempt the insert on UNKNOWN. That rule is what stops this
module from breaking dictation into apps it cannot see into.

Layers, first confident answer wins:
  1. GetGUIThreadInfo caret   - a real Win32 caret is proof of a text field
  2. UI Automation            - control type plus text/value patterns
  3. Terminal window classes  - always take typed text
  4. RDP client window        - honest UNKNOWN, the field is on another machine
  5. Anything else            - UNKNOWN

Cost measured on this machine: GetGUIThreadInfo 0.04ms, UIA one-off init 463ms
(done once by warm_up on a background thread), GetFocusedElement plus all
properties 5-32ms warm. The UIA leg runs on a worker thread against a hard
deadline because UIA calls can block for seconds on a hung app, and a hung
probe must never hang the insert path.

Usage:
    import targetprobe
    targetprobe.warm_up()                  # once, at startup
    t = targetprobe.probe(hwnd)            # before recording / before insert
    tok = targetprobe.verify_token(hwnd)   # before insert
    ...insert...
    ok = targetprobe.verify_landed(hwnd, tok)   # True / False / None
"""
import ctypes
import os
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass

from logger import log, warn

_TAG = "targetprobe"

# Verdicts
EDITABLE     = "EDITABLE"
NOT_EDITABLE = "NOT_EDITABLE"
UNKNOWN      = "UNKNOWN"


@dataclass
class Target:
    """What we believe about the currently focused element."""
    verdict: str = UNKNOWN
    rect: tuple | None = None   # (left, top, right, bottom) in screen coords
    label: str = ""             # human readable, e.g. "Claude - message box"
    kind: str = "none"          # "caret" | "uia" | "terminal" | "rdp" | "none"
    source: str = ""            # which layer decided, for the log


@dataclass
class TargetSignal:
    """Cheap before/after signal used by verify_token / verify_landed."""
    kind: str = ""              # "value" | "text"
    value: int = 0              # value length, or caret offset in the document
    runtime_id: tuple = ()      # UIA element identity, so we notice focus moves
    hwnd: int = 0


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# UIA control type ids (UIA_*ControlTypeId)
_CT_BUTTON      = 50000
_CT_CHECKBOX    = 50002
_CT_COMBOBOX    = 50003
_CT_EDIT        = 50004
_CT_HYPERLINK   = 50005
_CT_IMAGE       = 50006
_CT_LISTITEM    = 50007
_CT_MENUITEM    = 50011
_CT_RADIOBUTTON = 50013
_CT_SLIDER      = 50015
_CT_TABITEM     = 50019
_CT_TEXT        = 50020
_CT_TREEITEM    = 50024
_CT_DATAITEM    = 50029
_CT_DOCUMENT    = 50030
_CT_SPLITBUTTON = 50031
_CT_WINDOW      = 50032
_CT_PANE        = 50033

# Types that can plausibly hold a text caret
_TEXT_TYPES = (_CT_EDIT, _CT_DOCUMENT, _CT_COMBOBOX)

# Types that never take dictated text. Only trusted providers (below) are
# believed when they report one of these, because an app with no accessibility
# layer reports whatever it feels like.
_NON_TEXT_TYPES = (
    _CT_BUTTON, _CT_CHECKBOX, _CT_RADIOBUTTON, _CT_HYPERLINK, _CT_IMAGE,
    _CT_LISTITEM, _CT_MENUITEM, _CT_SLIDER, _CT_TABITEM, _CT_TREEITEM,
    _CT_DATAITEM, _CT_SPLITBUTTON,
)

_CT_NAMES = {
    _CT_BUTTON: "button", _CT_CHECKBOX: "checkbox", _CT_COMBOBOX: "combo box",
    _CT_EDIT: "text box", _CT_HYPERLINK: "link", _CT_IMAGE: "image",
    _CT_LISTITEM: "list item", _CT_MENUITEM: "menu item",
    _CT_RADIOBUTTON: "radio button", _CT_SLIDER: "slider",
    _CT_TABITEM: "tab", _CT_TEXT: "text", _CT_TREEITEM: "tree item",
    _CT_DATAITEM: "row", _CT_DOCUMENT: "document",
    _CT_SPLITBUTTON: "button", _CT_WINDOW: "window", _CT_PANE: "pane",
}

# UIA property ids read via GetCurrentPropertyValue
_PROP_IS_TEXT_PATTERN_AVAILABLE  = 30040
_PROP_IS_VALUE_PATTERN_AVAILABLE = 30043
_PROP_VALUE_IS_READONLY          = 30046

_PATTERN_VALUE = 10002   # UIA_ValuePatternId
_PATTERN_TEXT  = 10014   # UIA_TextPatternId

# WHY THIS LIST EXISTS - read before extending it.
# A Chrome pane with nothing focused reports: control type 50033, not
# keyboard-focusable, no text pattern, no value pattern, read-only. That is a
# genuine "text cannot go here".
# A Tkinter Text widget, which IS editable, reports the EXACT same shape,
# because Tk has no accessibility layer at all. Same for most games, custom
# GDI apps, Java AWT, and anything drawing its own controls.
# So a confident NOT_EDITABLE is only ever returned when the owning process is
# known to answer UI Automation honestly. Everything else gets UNKNOWN and the
# insert is still attempted. Add an exe here only after checking it reports a
# real control type for a real field.
_UIA_TRUSTED_EXES = {
    "chrome.exe",
    "msedge.exe",
    "code.exe",
    "cursor.exe",
    "claude.exe",
    "explorer.exe",
    "notepad.exe",
    "winword.exe",
    "outlook.exe",
    "teams.exe",
    "ms-teams.exe",     # the Store build of new Teams
    "slack.exe",
}
# Chromium/Electron hosts and the WinUI/XAML stacks all provide UIA properly,
# whatever exe they happen to be running under.
_UIA_TRUSTED_CLASS_SUBSTR = ("Chrome_WidgetWin",)
_UIA_TRUSTED_CLASS_PREFIX = ("Windows.UI.", "Microsoft.UI.")

# Source of truth is inject.py:_TERMINAL_CLASSES. Duplicated rather than
# imported because inject.py imports this module, and importing back would be
# circular. Keep the two in step.
_TERMINAL_CLASSES = {
    "CASCADIA_HOSTING_WINDOW_CLASS",   # Windows Terminal
    "mintty",                          # Git Bash / Cygwin
    "ConsoleWindowClass",              # legacy conhost (PowerShell, cmd.exe)
    "VirtualConsoleClass",             # ConEmu, cmder
    "Alacritty",                       # Alacritty
}

# Pretty names for the label. Anything not here falls back to the exe stem.
_APP_NAMES = {
    "chrome.exe": "Chrome", "msedge.exe": "Edge", "firefox.exe": "Firefox",
    "code.exe": "VS Code", "cursor.exe": "Cursor", "claude.exe": "Claude",
    "explorer.exe": "File Explorer", "notepad.exe": "Notepad",
    "winword.exe": "Word", "excel.exe": "Excel", "outlook.exe": "Outlook",
    "teams.exe": "Teams", "ms-teams.exe": "Teams", "slack.exe": "Slack",
    "windowsterminal.exe": "Terminal", "powershell.exe": "PowerShell",
    "pwsh.exe": "PowerShell", "cmd.exe": "Command Prompt",
    "mstsc.exe": "Remote Desktop", "msrdc.exe": "Remote Desktop",
    "msrdcw.exe": "Remote Desktop", "python.exe": "Quiett",
    "pythonw.exe": "Quiett",
}

_RDP_LABEL = "Remote session - Quiett cannot see the field"

# Below this there is not enough of the budget left to be worth a COM call.
_UIA_MIN_BUDGET_MS = 25
# A stuck UIA worker never returns. Cap how many we are willing to leak before
# giving up on the layer entirely, so a permanently hung app cannot spawn a
# thread per probe.
_MAX_INFLIGHT = 4
# Never pull a whole document to build a verify signal: a 50k-line file would
# cost more than the insert itself. Only used when the caret-offset route
# fails, and a read that hits the cap is treated as "no signal" because it
# cannot show a delta.
_TEXT_READ_CAP = 4096

_VERIFY_TOKEN_BUDGET_MS = 200


# ---------------------------------------------------------------------------
# Win32 plumbing
# ---------------------------------------------------------------------------
# Private WinDLL handles, not ctypes.windll, so setting argtypes/restypes here
# cannot disturb inject.py which shares the process-wide windll cache.
_user32   = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_psapi    = ctypes.WinDLL("psapi", use_last_error=True)


class _GUITHREADINFO(ctypes.Structure):
    # Same declaration as inject.py:_GUITHREADINFO. Keep them identical.
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


_GUI_CARETBLINKING = 0x00000001

_user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(_GUITHREADINFO)]
_user32.GetGUIThreadInfo.restype  = wintypes.BOOL
_user32.ClientToScreen.argtypes   = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
_user32.ClientToScreen.restype    = wintypes.BOOL
_user32.GetWindowRect.argtypes    = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.GetWindowRect.restype     = wintypes.BOOL
_user32.IsWindow.argtypes         = [wintypes.HWND]
_user32.IsWindow.restype          = wintypes.BOOL
_user32.GetForegroundWindow.restype = wintypes.HWND
# 64-bit correctness: the default int restype truncates a HANDLE above 4 GB.
_kernel32.OpenProcess.restype     = wintypes.HANDLE
_kernel32.CloseHandle.argtypes    = [wintypes.HANDLE]

_PROCESS_QUERY_INFORMATION_VM_READ = 0x0410


def _is_window(hwnd: int) -> bool:
    try:
        return bool(hwnd) and bool(_user32.IsWindow(hwnd))
    except Exception:
        return False


def foreground_window() -> int:
    """The current foreground hwnd, or 0. Convenience for callers and the CLI."""
    try:
        return int(_user32.GetForegroundWindow() or 0)
    except Exception:
        return 0


def _class_name(hwnd: int) -> str:
    try:
        buf = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(hwnd, buf, 256)
        return buf.value or ""
    except Exception:
        return ""


def _window_title(hwnd: int) -> str:
    try:
        buf = ctypes.create_unicode_buffer(512)
        _user32.GetWindowTextW(hwnd, buf, 512)
        return buf.value or ""
    except Exception:
        return ""


def _exe_for_pid(pid: int) -> str:
    if not pid:
        return ""
    h = None
    try:
        h = _kernel32.OpenProcess(_PROCESS_QUERY_INFORMATION_VM_READ, False, pid)
        if not h:
            return ""
        buf = ctypes.create_unicode_buffer(512)
        _psapi.GetModuleFileNameExW(h, None, buf, 512)
        return os.path.basename(buf.value).lower()
    except Exception:
        return ""
    finally:
        if h:
            try:
                _kernel32.CloseHandle(h)
            except Exception:
                pass


def _exe_for_hwnd(hwnd: int) -> str:
    try:
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return _exe_for_pid(pid.value)
    except Exception:
        return ""


def _window_rect(hwnd: int) -> tuple | None:
    try:
        r = wintypes.RECT()
        if not _user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return None
        return (r.left, r.top, r.right, r.bottom)
    except Exception:
        return None


def _is_rdp(hwnd: int) -> bool:
    """Mirrors inject.py:_is_rdp. Keep the two in step."""
    cls = _class_name(hwnd)
    if "TscShellContainer" in cls or "RAIL_WINDOW" in cls:
        return True
    return _exe_for_hwnd(hwnd) in ("mstsc.exe", "msrdc.exe", "msrdcw.exe")


def _is_terminal(hwnd: int) -> bool:
    """Class check only. inject.py additionally honours per-app paste overrides;
    those change how text is pasted, not whether the window accepts text."""
    return _class_name(hwnd) in _TERMINAL_CLASSES


def _app_name(hwnd: int, exe: str = "") -> str:
    exe = (exe or _exe_for_hwnd(hwnd)).lower()
    if exe in _APP_NAMES:
        return _APP_NAMES[exe]
    if exe.endswith(".exe"):
        stem = exe[:-4]
        return stem[:1].upper() + stem[1:] if stem else "Window"
    title = _window_title(hwnd)
    return (title[:32] or "Window")


def _label(app: str, element_name: str = "", control_type: int = 0) -> str:
    """Human readable target name, e.g. "Claude - message box"."""
    app = (app or "Window").strip()
    part = (element_name or "").strip().replace("\r", " ").replace("\n", " ")
    if part.lower() == app.lower():
        part = ""   # "MortalShell2 - MortalShell2" helps nobody
    if len(part) > 40:
        part = part[:37].rstrip() + "..."
    if not part:
        part = _CT_NAMES.get(control_type, "")
    return f"{app} - {part}" if part else app


# ---------------------------------------------------------------------------
# Layer 1: the Win32 caret
# ---------------------------------------------------------------------------

def _caret_target(hwnd: int) -> Target | None:
    """A live caret in the foreground thread is proof of an editable field.

    Costs 0.04ms and needs no COM, so it runs before anything else. Chromium,
    WinUI and Electron do not create a real caret, which is what layer 2 is for.
    """
    try:
        tid = _user32.GetWindowThreadProcessId(hwnd, None)
        if not tid:
            return None
        info = _GUITHREADINFO()
        info.cbSize = ctypes.sizeof(_GUITHREADINFO)
        if not _user32.GetGUIThreadInfo(tid, ctypes.byref(info)):
            return None
        caret = info.hwndCaret
        if not caret:
            return None
        rc = info.rcCaret
        if rc.right <= rc.left or rc.bottom <= rc.top:
            return None   # zero-size caret rect: the app left a stale handle
        rect = None
        tl = wintypes.POINT(rc.left, rc.top)
        br = wintypes.POINT(rc.right, rc.bottom)
        if _user32.ClientToScreen(caret, ctypes.byref(tl)) and \
           _user32.ClientToScreen(caret, ctypes.byref(br)):
            rect = (tl.x, tl.y, br.x, br.y)
        blinking = bool(info.flags & _GUI_CARETBLINKING)
        return Target(
            verdict=EDITABLE,
            rect=rect,
            label=_label(_app_name(hwnd), "", _CT_EDIT),
            kind="caret",
            source="caret:blinking" if blinking else "caret",
        )
    except Exception as e:
        warn(_TAG, f"caret layer failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Layer 2: UI Automation
# ---------------------------------------------------------------------------

_uia_mod = None
_uia_state = "cold"          # "cold" | "ready" | "failed"
_uia_lock = threading.Lock()
_warm_lock = threading.Lock()
_warming = False
_inflight = 0
_inflight_lock = threading.Lock()


def _com_begin():
    """Initialise COM on this thread. Returns a token for _com_end, or None.

    Multi-threaded apartment on purpose: the probe runs on short-lived worker
    threads with no message pump, and an STA without a pump can deadlock a
    cross-apartment call. Falls back to STA if the thread mode is already set.
    """
    try:
        import comtypes
    except Exception as e:
        warn(_TAG, f"comtypes unavailable: {e}")
        return None
    try:
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        return comtypes
    except Exception:
        pass
    try:
        comtypes.CoInitialize()
        return comtypes
    except Exception as e:
        warn(_TAG, f"CoInitialize failed: {e}")
        return None


def _com_end(token) -> None:
    if token is None:
        return
    try:
        token.CoUninitialize()
    except Exception:
        pass


def _ensure_uia_module():
    """Load (and on first ever run, generate) the UIAutomationCore wrapper.

    This is the 463ms one-off. warm_up() pays it on a background thread at
    startup so it never lands in an insert.
    """
    global _uia_mod, _uia_state
    if _uia_mod is not None:
        return _uia_mod
    with _uia_lock:
        if _uia_mod is not None:
            return _uia_mod
        if _uia_state == "failed":
            return None   # do not pay the cost again every probe
        try:
            import comtypes.client
            t0 = time.perf_counter()
            mod = comtypes.client.GetModule("UIAutomationCore.dll")
            _uia_mod = mod
            _uia_state = "ready"
            log(_TAG, f"UIA module ready in {(time.perf_counter() - t0) * 1000:.0f}ms")
        except Exception as e:
            _uia_state = "failed"
            warn(_TAG, f"UIA unavailable, probe falls back to caret only: {e}")
            return None
    return _uia_mod


def _new_automation():
    """A fresh IUIAutomation for this thread. ~7ms once the module is loaded.

    Deliberately not cached across threads: the object belongs to the apartment
    that created it, and every probe runs on its own short-lived thread.
    """
    mod = _ensure_uia_module()
    if mod is None:
        return None, None
    import comtypes.client
    return comtypes.client.CreateObject(mod.CUIAutomation, interface=mod.IUIAutomation), mod


def _bool_prop(element, prop_id: int, default: bool) -> bool:
    try:
        v = element.GetCurrentPropertyValue(prop_id)
    except Exception:
        return default
    if v is None:
        return default
    try:
        return bool(v)
    except Exception:
        return default


def _uia_props(hwnd: int) -> dict | None:
    """Read the focused element's properties. Runs on a COM worker thread."""
    uia, _mod = _new_automation()
    if uia is None:
        return None
    element = uia.GetFocusedElement()
    if element is None:
        return None

    def _safe(fn, default=None):
        try:
            return fn()
        except Exception:
            return default

    rect = None
    r = _safe(lambda: element.CurrentBoundingRectangle)
    if r is not None:
        try:
            if r.right > r.left and r.bottom > r.top:
                rect = (int(r.left), int(r.top), int(r.right), int(r.bottom))
        except Exception:
            rect = None

    pid = int(_safe(lambda: element.CurrentProcessId, 0) or 0)
    native = int(_safe(lambda: element.CurrentNativeWindowHandle, 0) or 0)
    exe = _exe_for_pid(pid) or _exe_for_hwnd(hwnd)
    classes = [_class_name(hwnd), _safe(lambda: element.CurrentClassName, "") or ""]
    if native and native != hwnd:
        classes.append(_class_name(native))

    return {
        "control_type": int(_safe(lambda: element.CurrentControlType, 0) or 0),
        "name":         _safe(lambda: element.CurrentName, "") or "",
        "focusable":    bool(_safe(lambda: element.CurrentIsKeyboardFocusable, 0)),
        "enabled":      bool(_safe(lambda: element.CurrentIsEnabled, 1)),
        "is_password":  bool(_safe(lambda: element.CurrentIsPassword, 0)),
        "text_pattern":  _bool_prop(element, _PROP_IS_TEXT_PATTERN_AVAILABLE, False),
        "value_pattern": _bool_prop(element, _PROP_IS_VALUE_PATTERN_AVAILABLE, False),
        "readonly":      _bool_prop(element, _PROP_VALUE_IS_READONLY, True),
        "rect":         rect,
        "pid":          pid,
        "exe":          exe,
        "classes":      [c for c in classes if c],
        "app":          _app_name(hwnd, exe),
        "trusted":      _is_trusted_provider(exe, classes),
    }


def _is_trusted_provider(exe: str, classes) -> bool:
    """Does this process answer UI Automation honestly?

    Only trusted providers are allowed to produce a confident NOT_EDITABLE.
    See the comment on _UIA_TRUSTED_EXES for why this gate exists.
    """
    try:
        if (exe or "").lower() in _UIA_TRUSTED_EXES:
            return True
        for cls in (classes or ()):
            if not cls:
                continue
            if any(s in cls for s in _UIA_TRUSTED_CLASS_SUBSTR):
                return True
            if any(cls.startswith(p) for p in _UIA_TRUSTED_CLASS_PREFIX):
                return True
    except Exception:
        pass
    return False


def _classify_uia(props: dict) -> Target:
    """Pure verdict logic over a property dict. Unit tested with fake dicts.

    Returns UNKNOWN (with rect and label still filled in where known) whenever
    the evidence is not strong enough, so the caller keeps a usable ring and
    badge but still attempts the insert.
    """
    ct       = int(props.get("control_type") or 0)
    name     = props.get("name") or ""
    app      = props.get("app") or ""
    rect     = props.get("rect")
    trusted  = bool(props.get("trusted"))
    has_text = bool(props.get("text_pattern"))
    has_val  = bool(props.get("value_pattern"))
    readonly = bool(props.get("readonly", True))
    enabled  = bool(props.get("enabled", True))
    focusable = bool(props.get("focusable"))
    label = _label(app, name, ct)

    # Never dictate into a password box, trusted provider or not.
    if props.get("is_password"):
        return Target(NOT_EDITABLE, rect, _label(app, "password field"), "uia", "uia:password")

    # ValueIsReadOnly is a ValuePattern property. Without that pattern it comes
    # back True by default, so it may only veto when the pattern really exists.
    value_says_readonly = has_val and readonly
    if ct in _TEXT_TYPES and enabled and (has_text or has_val) and not value_says_readonly:
        return Target(EDITABLE, rect, label, "uia", "uia:pattern")

    if not trusted:
        # Same shape as a Tk Text widget, which is editable. No opinion.
        return Target(UNKNOWN, rect, label, "uia", "uia:untrusted-provider")

    # From here on the provider is known good, so a negative can be trusted.
    if ct in _TEXT_TYPES and not enabled:
        return Target(NOT_EDITABLE, rect, _label(app, "disabled field"), "uia", "uia:disabled")
    if ct in _TEXT_TYPES and value_says_readonly and not has_text:
        return Target(NOT_EDITABLE, rect, _label(app, "read-only field"), "uia", "uia:readonly")
    if ct in _NON_TEXT_TYPES and not has_text and not has_val:
        return Target(NOT_EDITABLE, rect, label, "uia", "uia:non-text-control")
    if not focusable and not has_text and not has_val and readonly:
        # The Chrome-pane-with-no-field shape: the acceptance case for all of this.
        return Target(NOT_EDITABLE, rect, label, "uia", "uia:blind-shape")

    return Target(UNKNOWN, rect, label, "uia", "uia:inconclusive")


def _run_with_deadline(fn, timeout_s: float):
    """Run fn on a COM-initialised worker thread. None if it misses the deadline.

    A UIA call against a hung app never returns. The worker is a daemon and is
    simply abandoned; _MAX_INFLIGHT caps how many we will leak before the layer
    switches itself off.
    """
    global _inflight
    with _inflight_lock:
        if _inflight >= _MAX_INFLIGHT:
            warn(_TAG, f"skipping UIA, {_inflight} workers still stuck")
            return None
        _inflight += 1

    box = {}

    def _worker():
        global _inflight
        token = None
        try:
            token = _com_begin()
            if token is None:
                return
            box["v"] = fn()
        except Exception as e:
            warn(_TAG, f"UIA worker failed: {e}")
        finally:
            _com_end(token)
            with _inflight_lock:
                _inflight -= 1

    th = threading.Thread(target=_worker, name="targetprobe-uia", daemon=True)
    th.start()
    th.join(max(0.0, timeout_s))
    if th.is_alive():
        warn(_TAG, f"UIA exceeded {timeout_s * 1000:.0f}ms budget, verdict UNKNOWN")
        return None
    return box.get("v")


def _uia_target(hwnd: int, timeout_s: float) -> Target | None:
    props = _run_with_deadline(lambda: _uia_props(hwnd), timeout_s)
    if not props:
        return None
    return _classify_uia(props)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def warm_up() -> None:
    """Pay the 463ms UIA init once, on a background thread. Safe to call twice."""
    global _warming
    try:
        with _warm_lock:
            if _warming or _uia_state == "ready":
                return
            _warming = True

        def _run():
            global _warming
            token = None
            t0 = time.perf_counter()
            try:
                token = _com_begin()
                if token is None:
                    return
                uia, _mod = _new_automation()
                if uia is not None:
                    log(_TAG, f"warm-up complete in {(time.perf_counter() - t0) * 1000:.0f}ms")
            except Exception as e:
                warn(_TAG, f"warm-up failed: {e}")
            finally:
                _com_end(token)
                _warming = False

        threading.Thread(target=_run, name="targetprobe-warmup", daemon=True).start()
    except Exception as e:
        warn(_TAG, f"warm-up could not start: {e}")


def available() -> bool:
    """True once the UIA layer is loaded and usable. Caret layer works regardless."""
    return _uia_state == "ready"


def probe(hwnd: int, budget_ms: int = 250) -> Target:
    """Best-effort answer to "will text land in this window, and where".

    Never raises, never blocks past budget_ms in any measurable way. UNKNOWN
    means "no opinion", and the caller must still attempt the insert.
    """
    t0 = time.perf_counter()
    try:
        if not _is_window(hwnd):
            return Target(UNKNOWN, None, "", "none", "no-window")
        deadline = t0 + max(0, budget_ms) / 1000.0

        # 1. A real Win32 caret. Cheapest and strongest.
        caret = _caret_target(hwnd)
        if caret is not None:
            return caret

        # 2. UI Automation. Kept even when inconclusive, since its rect and
        #    label still drive the ring and the badge.
        fallback = None
        remaining_ms = (deadline - time.perf_counter()) * 1000.0
        if remaining_ms >= _UIA_MIN_BUDGET_MS:
            t = _uia_target(hwnd, remaining_ms / 1000.0)
            if t is not None:
                if t.verdict != UNKNOWN:
                    return t
                fallback = t

        # 3. Terminals always take typed text.
        if _is_terminal(hwnd):
            app = _app_name(hwnd)
            return Target(EDITABLE, _window_rect(hwnd), _label(app, "terminal"),
                          "terminal", "class:terminal")

        # 4. RDP. The field is on another machine and we cannot see it. Being
        #    honest here matters more than being clever.
        if _is_rdp(hwnd):
            return Target(UNKNOWN, _window_rect(hwnd), _RDP_LABEL, "rdp", "class:rdp")

        # 5. No opinion.
        if fallback is not None:
            return fallback
        app = _app_name(hwnd)
        return Target(UNKNOWN, _window_rect(hwnd), app, "none", "no-signal")
    except Exception as e:
        warn(_TAG, f"probe failed: {e}")
        return Target(UNKNOWN, None, "", "none", "error")


def _read_signal(hwnd: int) -> TargetSignal | None:
    """Cheap before/after measurement of the focused element. COM thread only.

    Value pattern: the length of the current value.
    Text pattern: the caret offset from the start of the document, via
    CompareEndpoints, so cost does not grow with document size. GetText is only
    a fallback and is capped at _TEXT_READ_CAP; a read that hits the cap cannot
    show a delta, so it counts as no signal at all.
    """
    uia, mod = _new_automation()
    if uia is None:
        return None
    element = uia.GetFocusedElement()
    if element is None:
        return None
    try:
        rid = tuple(element.GetRuntimeId() or ())
    except Exception:
        rid = ()

    if _bool_prop(element, _PROP_IS_VALUE_PATTERN_AVAILABLE, False):
        try:
            vp = element.GetCurrentPattern(_PATTERN_VALUE).QueryInterface(
                mod.IUIAutomationValuePattern)
            val = vp.CurrentValue or ""
            return TargetSignal("value", len(val), rid, hwnd)
        except Exception:
            pass

    if _bool_prop(element, _PROP_IS_TEXT_PATTERN_AVAILABLE, False):
        try:
            tp = element.GetCurrentPattern(_PATTERN_TEXT).QueryInterface(
                mod.IUIAutomationTextPattern)
            sel = tp.GetSelection()
            if sel is not None and sel.Length > 0:
                caret = sel.GetElement(0)
                doc = tp.DocumentRange
                # TextPatternRangeEndpoint_Start = 0 for both endpoints.
                offset = int(caret.CompareEndpoints(0, doc, 0))
                return TargetSignal("text", offset, rid, hwnd)
        except Exception:
            pass
        try:
            tp = element.GetCurrentPattern(_PATTERN_TEXT).QueryInterface(
                mod.IUIAutomationTextPattern)
            text = tp.DocumentRange.GetText(_TEXT_READ_CAP) or ""
            if len(text) >= _TEXT_READ_CAP:
                return None   # capped read cannot prove a delta
            return TargetSignal("text", len(text), rid, hwnd)
        except Exception:
            pass
    return None


def verify_token(hwnd: int) -> object | None:
    """Capture a pre-insert signal, or None when the target offers none."""
    try:
        if not _is_window(hwnd):
            return None
        return _run_with_deadline(lambda: _read_signal(hwnd),
                                  _VERIFY_TOKEN_BUDGET_MS / 1000.0)
    except Exception as e:
        warn(_TAG, f"verify_token failed: {e}")
        return None


def verify_landed(hwnd: int, token: object, budget_ms: int = 300) -> bool | None:
    """Did the text land? True, False, or None for "no opinion".

    None is returned whenever there is no signal, the focus moved to a
    different element, or the signal moved in a direction that proves nothing.
    Callers must treat None as unconfirmed, not as failure.
    """
    try:
        if not isinstance(token, TargetSignal) or not _is_window(hwnd):
            return None
        after = _run_with_deadline(lambda: _read_signal(hwnd),
                                   max(0, budget_ms) / 1000.0)
        if not isinstance(after, TargetSignal):
            return None
        if after.kind != token.kind:
            return None
        if token.runtime_id and after.runtime_id and token.runtime_id != after.runtime_id:
            return None   # focus moved elsewhere, the comparison is meaningless
        if after.value > token.value:
            return True
        if after.value == token.value:
            return False
        # Shorter than before. Something changed, but not in a way that proves
        # our text landed, so claim nothing.
        return None
    except Exception as e:
        warn(_TAG, f"verify_landed failed: {e}")
        return None
