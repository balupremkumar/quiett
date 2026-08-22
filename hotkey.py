import ctypes
import threading
import time

import keyboard

from logger import log, warn

_on_start      = None
_on_stop       = None
_on_cancel     = None   # stops recording without transcription
_on_too_short  = None   # called when hotkey released too quickly
_on_not_ready  = None   # called when model is still loading
_is_recording  = None
_is_ready      = None
_paused              = False
_press_time: float   = 0.0
_hotkey_mode         = "hold"   # "hold" | "toggle" | "auto"
_toggle_armed        = False    # True once all mods went down together (toggle mode)
_external_recording  = False    # True when something other than the hold hotkey started the recording

_MIN_HOLD_MS = 300  # ms — ignore releases faster than this

# Each element is a set of canonical key names for one modifier in the combo.
# e.g. ctrl+alt → [{"ctrl","left ctrl","right ctrl"}, {"alt","left alt","right alt"}]
_mod_sets: list[set] = []
_held: list[bool] = []


def configure(on_start, on_stop, is_recording, is_ready, keys: str = "ctrl+alt",
              on_cancel=None, on_too_short=None, on_not_ready=None,
              hotkey_mode: str = "hold") -> None:
    global _on_start, _on_stop, _is_recording, _is_ready, _mod_sets, _held
    global _on_cancel, _on_too_short, _on_not_ready, _hotkey_mode
    _on_start     = on_start
    _on_stop      = on_stop
    _on_cancel    = on_cancel
    _on_too_short = on_too_short
    _on_not_ready = on_not_ready
    _is_recording = is_recording
    _is_ready     = is_ready
    _hotkey_mode  = hotkey_mode if hotkey_mode in ("hold", "toggle", "auto") else "hold"
    _mod_sets     = _parse_hotkey(keys)
    _held[:]      = [False] * len(_mod_sets)


def _parse_hotkey(keys: str) -> list[set]:
    _ALIASES = {
        "ctrl":  {"ctrl", "left ctrl", "right ctrl"},
        "alt":   {"alt", "left alt", "right alt"},
        "shift": {"shift", "left shift", "right shift"},
        "win":   {"win", "left win", "right win"},
    }
    result = []
    for token in keys.lower().split("+"):
        token = token.strip()
        result.append(_ALIASES.get(token, {token}))
    return result


def set_paused(val: bool) -> None:
    global _paused
    _paused = val
    # Events arriving while paused are never seen, so any remembered key-down
    # state is untrustworthy on both transitions; a stale True would let the
    # next single-modifier press look like the full combo.
    _held[:] = [False] * len(_mod_sets)


def set_external_recording(active: bool) -> None:
    """Prevent hold-mode stop logic while an externally started recording runs."""
    global _external_recording
    _external_recording = active


def rebind(keys: str) -> None:
    """Re-parse hotkey at runtime. Resets held state."""
    global _mod_sets, _held
    _mod_sets = _parse_hotkey(keys)
    _held[:]  = [False] * len(_mod_sets)
    log("hotkey", f"rebind -> {keys}")


def start() -> None:
    keyboard.hook(_on_key)


def _on_key(event) -> None:
    global _press_time, _toggle_armed
    if _paused:
        return

    name = event.name or ""

    for i, mod_set in enumerate(_mod_sets):
        if name in mod_set:
            _held[i] = event.event_type == keyboard.KEY_DOWN

    both = all(_held)

    if _hotkey_mode == "toggle":
        # Toggle: combo-down fires start or stop; combo-up is ignored.
        if both and event.event_type == keyboard.KEY_DOWN and not _toggle_armed:
            _toggle_armed = True
            if _is_recording():
                _on_stop()
            else:
                if not _is_ready():
                    if _on_not_ready:
                        _on_not_ready()
                    return
                _press_time = time.time()
                try:
                    _on_start()
                except Exception as exc:
                    warn("hotkey", f"on_start failed: {exc}")
                    _held[:] = [False] * len(_mod_sets)
        elif not both:
            _toggle_armed = False
        return

    # "hold" and "auto" behave identically (auto = hold, reserved for future smart detection)
    if both and not _is_recording():
        if not _is_ready():
            if _on_not_ready:
                _on_not_ready()
            else:
                print("Model still loading, please wait")
            return
        _press_time = time.time()
        try:
            _on_start()
        except Exception as exc:
            warn("hotkey", f"on_start failed, clearing held state: {exc}")
            _held[:] = [False] * len(_mod_sets)
    elif not both and _is_recording() and not _external_recording:
        held_ms = (time.time() - _press_time) * 1000
        if held_ms < _MIN_HOLD_MS and _on_cancel:
            # Too short — cancel silently, show feedback
            _on_cancel()
            if _on_too_short:
                _on_too_short()
        else:
            _on_stop()


# ---------------------------------------------------------------------------
# Tap hotkeys (PASTE_UX_PLAN section 4)
#
# Deliberately a separate registration path from everything above. _mod_sets
# and the raw keyboard.hook exist to time a modifiers-only HOLD, with held
# state, a minimum hold and a release that means "stop recording". A combo
# that ends in a normal key needs none of that: it fires once, on the press,
# and there is nothing to time. Bending _mod_sets to cover both would put the
# hold path's state machine on the critical path of every tap.
#
# Tap, never hold, is also the point: pressing and HOLDING a modifier is the
# exact mechanism that latches Ctrl inside an RDP session (RDP_FIX_PLAN), so
# place mode must not be a hold.
# ---------------------------------------------------------------------------

_tap_handles: dict = {}     # normalised combo -> keyboard library handle


def register_tap(keys: str, callback) -> bool:
    """Register `keys` (e.g. "ctrl+alt+v", "esc") to fire `callback` once per
    press. Returns True when the registration took.

    suppress=False throughout: swallowing the combo would hide it from the app
    the user is working in, and place mode has no business doing that.
    Re-registering the same combo replaces the previous binding.
    """
    combo = (keys or "").strip().lower()
    if not combo or callback is None:
        return False
    unregister_tap(combo)

    def _fire() -> None:
        # Runs on the keyboard library's hook thread. An exception escaping
        # here kills that thread and every hotkey in the app with it.
        try:
            callback()
        except Exception as exc:
            warn("hotkey", f"tap hotkey {combo} callback failed: {exc}")

    try:
        _tap_handles[combo] = keyboard.add_hotkey(combo, _fire, suppress=False)
    except Exception as exc:
        warn("hotkey", f"tap hotkey {combo} failed to register: {exc}")
        return False
    log("hotkey", f"tap hotkey registered: {combo}")
    return True


def unregister_tap(keys: str) -> None:
    """Remove a tap hotkey. Safe to call when it was never registered."""
    combo = (keys or "").strip().lower()
    handle = _tap_handles.pop(combo, None)
    if handle is None:
        return
    try:
        keyboard.remove_hotkey(handle)
    except Exception as exc:
        warn("hotkey", f"tap hotkey {combo} failed to unregister: {exc}")


# ---------------------------------------------------------------------------
# One-shot click capture (PASTE_UX_PLAN section 4)
#
# Place mode needs to know which window the user clicks next. The hook exists
# ONLY while armed and is torn down on the first click or on disarm, so there
# is no permanent input-hook cost.
#
# The click is never swallowed: it has to reach the target app or the field
# the user picked never gets focus, and the whole flow is pointless. The hook
# observes and returns straight to CallNextHookEx; the insert happens later,
# on a worker thread, once focus has settled.
# ---------------------------------------------------------------------------

_WH_MOUSE_LL   = 14
_WM_LBUTTONUP  = 0x0202
_WM_RBUTTONUP  = 0x0205
_WM_QUIT       = 0x0012

_user32   = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_LRESULT  = ctypes.c_ssize_t
_HOOKPROC = ctypes.WINFUNCTYPE(_LRESULT, ctypes.c_int, ctypes.c_size_t, ctypes.c_void_p)

_user32.SetWindowsHookExW.restype = ctypes.c_void_p
_user32.SetWindowsHookExW.argtypes = [ctypes.c_int, _HOOKPROC, ctypes.c_void_p, ctypes.c_uint]
_user32.CallNextHookEx.restype = _LRESULT
_user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t, ctypes.c_void_p]
_user32.UnhookWindowsHookEx.restype = ctypes.c_int
_user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]

_click_lock   = threading.Lock()
_click_cb     = None       # fn(), called once, on a worker thread
_click_hook   = None       # HHOOK
_click_tid    = 0          # thread id of the pump, for the WM_QUIT
_click_thread = None
# Module-level reference on purpose: a hook proc that gets garbage collected
# leaves Windows calling into freed memory.
_click_proc = None


def _click_hook_proc(n_code, w_param, l_param):
    """Low-level mouse hook. Incapable of raising: a hook proc that throws is
    silently torn down by Windows and the capture dies with it."""
    try:
        if n_code == 0 and w_param in (_WM_LBUTTONUP, _WM_RBUTTONUP):
            _dispatch_click()
    except Exception:
        pass
    try:
        return _user32.CallNextHookEx(None, n_code, w_param, l_param)
    except Exception:
        return 0


def _dispatch_click() -> None:
    """Hand the click to a worker and take the hook down. Never does the work
    inline: everything after this point (focus settle, probe, insert) takes
    hundreds of milliseconds, and a low-level hook that blocks that long gets
    removed by Windows, freezing the mouse for every other app meanwhile."""
    with _click_lock:
        cb = _click_cb
        if cb is None:
            return
        _set_click_callback(None)
    threading.Thread(target=_run_click_callback, args=(cb,), daemon=True).start()
    cancel_click_capture()


def _set_click_callback(cb) -> None:
    global _click_cb
    _click_cb = cb


def _run_click_callback(cb) -> None:
    try:
        cb()
    except Exception as exc:
        warn("hotkey", f"click capture callback failed: {exc}")


def _click_pump(ready: threading.Event) -> None:
    global _click_hook, _click_tid, _click_proc
    try:
        _click_tid = _kernel32.GetCurrentThreadId()
        _click_proc = _HOOKPROC(_click_hook_proc)
        _click_hook = _user32.SetWindowsHookExW(_WH_MOUSE_LL, _click_proc, None, 0)
        if not _click_hook:
            warn("hotkey", f"mouse hook failed: {ctypes.get_last_error()}")
            return
    except Exception as exc:
        warn("hotkey", f"mouse hook could not be installed: {exc}")
        return
    finally:
        ready.set()

    try:
        # A low-level hook only receives events while its owning thread pumps
        # messages, so this loop is not optional bookkeeping.
        msg = ctypes.create_string_buffer(64)
        while _user32.GetMessageW(msg, None, 0, 0) > 0:
            pass
    except Exception as exc:
        warn("hotkey", f"mouse hook pump stopped: {exc}")
    finally:
        hook = _click_hook
        _click_hook = None
        _click_tid = 0
        try:
            if hook:
                _user32.UnhookWindowsHookEx(hook)
        except Exception:
            pass


def capture_next_click(on_click) -> bool:
    """Install a one-shot mouse hook; `on_click()` fires on the next mouse
    button release, on a worker thread, after the click has been delivered to
    whatever the user clicked. Returns True when the hook is live."""
    global _click_thread
    if on_click is None:
        return False
    with _click_lock:
        _set_click_callback(on_click)
        if _click_thread is not None and _click_thread.is_alive():
            return True     # already hooked, just re-pointed at the new callback
        ready = threading.Event()
        _click_thread = threading.Thread(target=_click_pump, args=(ready,),
                                         daemon=True, name="quiett-click-hook")
        _click_thread.start()
    ready.wait(1.0)
    if not _click_hook:
        with _click_lock:
            _set_click_callback(None)
            _click_thread = None
        return False
    log("hotkey", "click capture armed")
    return True


def cancel_click_capture() -> None:
    """Remove the mouse hook. Safe to call when nothing is captured."""
    global _click_thread
    with _click_lock:
        _set_click_callback(None)
        tid, thread = _click_tid, _click_thread
        _click_thread = None
    if not tid:
        return
    try:
        _user32.PostThreadMessageW(ctypes.c_ulong(tid), _WM_QUIT, 0, 0)
    except Exception as exc:
        warn("hotkey", f"could not stop the mouse hook pump: {exc}")
        return
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=1.0)
    log("hotkey", "click capture released")


def click_capture_active() -> bool:
    return _click_cb is not None


# ---------------------------------------------------------------------------
# Reserved single key (PASTE_UX_PLAN section 4, revised 2026-08-22)
#
# Place mode's everyday entry point is ONE key with no modifiers, on a key
# nobody uses, reserved by Quiett for as long as it runs. A chord was rejected:
# this fires constantly and a chord is too much work for it.
#
# Why a hand-rolled WH_KEYBOARD_LL hook instead of the keyboard library:
#
#  1. keyboard shares ONE low-level hook across the whole library, and asking
#     any part of it to suppress switches that shared hook into blocking mode.
#     That would put the library's Python callbacks on the critical path of
#     every keystroke the machine sees. Unacceptable in a dictation app.
#  2. The numpad "." and the nav-cluster Delete share base scan code 83, so
#     matching on the scan code alone would swallow Delete everywhere. They are
#     told apart ONLY by the extended flag: Delete is E0-prefixed
#     (LLKHF_EXTENDED set), the numpad key is not. Every entry below therefore
#     carries the extended value it expects, and a mismatch is passed straight
#     through untouched.
#
# The hold-to-record hook above and the tap hotkeys are untouched by any of
# this: separate hook, separate thread, separate state.
# ---------------------------------------------------------------------------

_WH_KEYBOARD_LL = 13
_WM_KEYDOWN     = 0x0100
_WM_KEYUP       = 0x0101
_WM_SYSKEYDOWN  = 0x0104
_WM_SYSKEYUP    = 0x0105
_LLKHF_EXTENDED = 0x01

_KEY_DOWN_MESSAGES = (_WM_KEYDOWN, _WM_SYSKEYDOWN)
_KEY_MESSAGES      = (_WM_KEYDOWN, _WM_SYSKEYDOWN, _WM_KEYUP, _WM_SYSKEYUP)

# name -> (scan code, extended flag expected)
RESERVED_KEYS = {
    "numpad_decimal":  (83, False),   # the . / Del key on the numpad; extended 83 is the real Delete
    "numpad_plus":     (78, False),
    "numpad_minus":    (74, False),
    "numpad_multiply": (55, False),   # extended 55 is PrintScreen, so False matters here
    "numpad_0":        (82, False),
    "num_lock":        (69, False),
}

_RESERVED_DEBOUNCE_SECONDS = 0.30   # auto-repeat must not fire the same press twice


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode",      ctypes.c_uint),
                ("scanCode",    ctypes.c_uint),
                ("flags",       ctypes.c_uint),
                ("time",        ctypes.c_uint),
                ("dwExtraInfo", ctypes.c_size_t)]


_reserved_lock      = threading.Lock()
_reserved_cb        = None      # fn(), called on a worker thread
_reserved_spec      = None      # (scan code, extended expected) currently reserved
_reserved_name      = ""
_reserved_hook      = None      # HHOOK
_reserved_tid       = 0         # thread id of the pump, for the WM_QUIT
_reserved_thread    = None
_reserved_last_fire = 0.0
# Set by the hook proc, consumed by a worker started at registration. The proc
# must NEVER create a thread: a low-level keyboard hook blocks all keyboard
# input on the machine until it returns, and one that overruns
# LowLevelHooksTimeout (300ms by default) is silently removed by Windows.
# Thread creation under GIL contention is exactly that risk.
_reserved_signal = None
_reserved_stop   = None
_reserved_worker = None
# Whether we swallowed the key-DOWN of the press now in progress. Swallowing an
# up whose down we let through leaves the focused app believing the key is
# still held, which is the stuck-key symptom.
_reserved_down_swallowed = False
# Module-level reference on purpose: a hook proc that gets garbage collected
# leaves Windows calling into freed memory. Classic silent failure for this API.
_reserved_proc = None


def reserved_key_matches(scan_code: int, flags: int, spec) -> bool:
    """The whole Delete-vs-numpad decision, as a pure function.

    scan 83 with LLKHF_EXTENDED set is the nav-cluster Delete and must NEVER
    match: swallowing that would break Delete system-wide.
    """
    if not spec:
        return False
    want_scan, want_extended = spec
    return scan_code == want_scan and bool(flags & _LLKHF_EXTENDED) == bool(want_extended)


def _reserved_debounced(now: float) -> bool:
    """True when this press lands inside the debounce window of the last one,
    i.e. it is auto-repeat rather than a new press."""
    global _reserved_last_fire
    if now - _reserved_last_fire < _RESERVED_DEBOUNCE_SECONDS:
        return True
    _reserved_last_fire = now
    return False


def _reserved_hook_proc(n_code, w_param, l_param):
    """Low-level keyboard hook. Incapable of raising: a hook proc that throws
    is torn down by Windows and every keystroke after it stops being seen.

    This runs for EVERY keystroke on the machine and blocks all keyboard input
    until it returns, so the body is deliberately as cheap as possible and does
    no allocation on the hot path. Real work is handed to a worker that is
    already running, by setting an Event.

    Returns 1 (swallow) only on an exact scan-code AND extended-flag match. The
    key-up is swallowed only when its key-down was, so the focused app can never
    be left holding a key it never saw released.
    """
    global _reserved_down_swallowed
    try:
        spec = _reserved_spec
        if n_code == 0 and spec is not None and w_param in _KEY_MESSAGES:
            info = ctypes.cast(l_param, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
            if reserved_key_matches(info.scanCode, info.flags, spec):
                if w_param in _KEY_DOWN_MESSAGES:
                    _reserved_down_swallowed = True
                    if not _reserved_debounced(time.monotonic()):
                        sig = _reserved_signal
                        if sig is not None:
                            sig.set()
                    return 1
                if _reserved_down_swallowed:
                    _reserved_down_swallowed = False
                    return 1
                # We never swallowed the down for this press (the hook was
                # installed mid-press). Let the up through so the app is not
                # left with the key stuck down.
    except Exception:
        pass
    try:
        return _user32.CallNextHookEx(None, n_code, w_param, l_param)
    except Exception:
        return 0


def _dispatch_reserved() -> None:
    """Signal the already-running worker. Kept as a function because the hook
    proc used to call it and the tests still drive it directly."""
    if _reserved_cb is None:
        return
    if _reserved_debounced(time.monotonic()):
        return
    sig = _reserved_signal
    if sig is not None:
        sig.set()


def _reserved_worker_loop(signal, stop, cb) -> None:
    """Persistent consumer for reserved-key presses. Started once at
    registration so the hook proc never pays for thread creation."""
    while not stop.is_set():
        if not signal.wait(0.2):
            continue
        signal.clear()
        if stop.is_set():
            return
        _run_reserved_callback(cb)


def _run_reserved_callback(cb) -> None:
    try:
        cb()
    except Exception as exc:
        warn("hotkey", f"reserved key callback failed: {exc}")


def _reserved_pump(ready: threading.Event) -> None:
    global _reserved_hook, _reserved_tid, _reserved_proc
    try:
        _reserved_tid = _kernel32.GetCurrentThreadId()
        _reserved_proc = _HOOKPROC(_reserved_hook_proc)
        _reserved_hook = _user32.SetWindowsHookExW(_WH_KEYBOARD_LL, _reserved_proc, None, 0)
        if not _reserved_hook:
            warn("hotkey", f"reserved key hook failed: {ctypes.get_last_error()}")
            return
    except Exception as exc:
        warn("hotkey", f"reserved key hook could not be installed: {exc}")
        return
    finally:
        ready.set()

    try:
        # A low-level hook only receives events while its owning thread pumps
        # messages, so this loop is not optional bookkeeping.
        msg = ctypes.create_string_buffer(64)
        while _user32.GetMessageW(msg, None, 0, 0) > 0:
            pass
    except Exception as exc:
        warn("hotkey", f"reserved key hook pump stopped: {exc}")
    finally:
        hook = _reserved_hook
        _reserved_hook = None
        _reserved_tid = 0
        try:
            if hook:
                _user32.UnhookWindowsHookEx(hook)
        except Exception:
            pass


def _stop_reserved_worker() -> None:
    """Stop the press consumer. Safe to call when none is running."""
    global _reserved_signal, _reserved_stop, _reserved_worker
    stop, sig, worker = _reserved_stop, _reserved_signal, _reserved_worker
    _reserved_stop = _reserved_signal = _reserved_worker = None
    if stop is not None:
        stop.set()
    if sig is not None:
        sig.set()   # wake it so it sees the stop without waiting out the timeout
    if worker is not None and worker is not threading.current_thread():
        worker.join(timeout=1.0)


def register_reserved_key(name: str, cb) -> bool:
    """Reserve one key, by name from RESERVED_KEYS, for as long as the app
    runs. `cb()` fires once per press, on a worker thread, and the key never
    reaches the focused app. Returns True when the hook is live."""
    global _reserved_cb, _reserved_spec, _reserved_name, _reserved_thread
    global _reserved_signal, _reserved_stop, _reserved_worker
    key = (name or "").strip().lower()
    spec = RESERVED_KEYS.get(key)
    if spec is None or cb is None:
        if key:
            warn("hotkey", f"reserved key {key} is not one this build can reserve")
        return False
    unregister_reserved_key()
    ready = threading.Event()
    with _reserved_lock:
        _reserved_cb     = cb
        _reserved_spec   = spec
        _reserved_name   = key
        _reserved_signal = threading.Event()
        _reserved_stop   = threading.Event()
        _reserved_worker = threading.Thread(
            target=_reserved_worker_loop, args=(_reserved_signal, _reserved_stop, cb),
            daemon=True, name="quiett-reserved-key-worker")
        _reserved_worker.start()
        _reserved_thread = threading.Thread(target=_reserved_pump, args=(ready,),
                                            daemon=True, name="quiett-reserved-key-hook")
        _reserved_thread.start()
    ready.wait(1.0)
    if not _reserved_hook:
        _stop_reserved_worker()
        with _reserved_lock:
            _reserved_cb     = None
            _reserved_spec   = None
            _reserved_name   = ""
            _reserved_thread = None
        return False
    log("hotkey", f"reserved key: {key} (scan {spec[0]}, extended {spec[1]})")
    return True


def unregister_reserved_key() -> None:
    """Give the key back to Windows. Safe to call when nothing is reserved."""
    global _reserved_cb, _reserved_spec, _reserved_name, _reserved_thread
    global _reserved_down_swallowed
    _stop_reserved_worker()
    _reserved_down_swallowed = False
    with _reserved_lock:
        _reserved_cb   = None
        _reserved_spec = None
        name = _reserved_name
        _reserved_name = ""
        tid, thread = _reserved_tid, _reserved_thread
        _reserved_thread = None
    if not tid:
        return
    try:
        _user32.PostThreadMessageW(ctypes.c_ulong(tid), _WM_QUIT, 0, 0)
    except Exception as exc:
        warn("hotkey", f"could not stop the reserved key hook pump: {exc}")
        return
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=1.0)
    log("hotkey", f"reserved key released: {name or 'none'}")


def reserved_key_active() -> bool:
    return _reserved_cb is not None


def reserved_key_name() -> str:
    return _reserved_name


def simulate_reserved_key_press() -> bool:
    """Fire the reserved key's callback exactly as a real press does, without
    touching the keyboard, for end-to-end testing of everything behind the key.
    Returns False when no key is reserved or the debounce swallowed it."""
    if _reserved_cb is None:
        return False
    before = _reserved_last_fire
    _dispatch_reserved()
    return _reserved_last_fire != before
