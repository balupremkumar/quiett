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
_external_recording  = False    # True when agent/rewrite mode started the recording

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
    """Prevent hold-mode stop logic while agent/rewrite recording is active."""
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
