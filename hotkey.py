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
