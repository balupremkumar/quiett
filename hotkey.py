import keyboard

_on_start = None
_on_stop = None
_is_recording = None
_is_ready = None
_paused = False

# Each element is a set of canonical key names for one modifier in the combo.
# e.g. ctrl+alt → [{"ctrl","left ctrl","right ctrl"}, {"alt","left alt","right alt"}]
_mod_sets: list[set] = []
_held: list[bool] = []


def configure(on_start, on_stop, is_recording, is_ready, keys: str = "ctrl+alt") -> None:
    global _on_start, _on_stop, _is_recording, _is_ready, _mod_sets, _held
    _on_start = on_start
    _on_stop = on_stop
    _is_recording = is_recording
    _is_ready = is_ready
    _mod_sets = _parse_hotkey(keys)
    _held[:] = [False] * len(_mod_sets)


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


def start() -> None:
    keyboard.hook(_on_key)


def _on_key(event) -> None:
    if _paused:
        return

    name = event.name or ""

    for i, mod_set in enumerate(_mod_sets):
        if name in mod_set:
            _held[i] = event.event_type == keyboard.KEY_DOWN

    both = all(_held)
    if both and not _is_recording():
        if not _is_ready():
            print("Model still loading, please wait")
            return
        _on_start()
    elif not both and _is_recording():
        _on_stop()
