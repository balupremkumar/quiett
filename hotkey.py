import keyboard

_ctrl_held = False
_alt_held = False
_on_start = None
_on_stop = None
_is_recording = None
_is_ready = None


def configure(on_start, on_stop, is_recording, is_ready) -> None:
    global _on_start, _on_stop, _is_recording, _is_ready
    _on_start = on_start
    _on_stop = on_stop
    _is_recording = is_recording
    _is_ready = is_ready


def start() -> None:
    keyboard.hook(_on_key)


def _on_key(event) -> None:
    global _ctrl_held, _alt_held

    name = event.name or ""
    is_ctrl = name in ("ctrl", "left ctrl", "right ctrl")
    is_alt = name in ("alt", "left alt", "right alt")

    if event.event_type == keyboard.KEY_DOWN:
        if is_ctrl:
            _ctrl_held = True
        if is_alt:
            _alt_held = True
    else:
        if is_ctrl:
            _ctrl_held = False
        if is_alt:
            _alt_held = False

    both = _ctrl_held and _alt_held
    if both and not _is_recording():
        if not _is_ready():
            print("Model still loading, please wait")
            return
        _on_start()
    elif not both and _is_recording():
        _on_stop()
