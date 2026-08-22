"""Last-dictation stash: one item, in memory, outliving the clipboard.

The clipboard is not a safe place to park the only copy of a dictation:
anything on the machine can overwrite it between an insert failing and the
user noticing, and our own restore thread used to do exactly that. History
persists every transcript (history.py) but says nothing about whether it ever
landed anywhere, so it cannot drive a recovery affordance.

The stash holds exactly one item, the newest dictation, the target it was
meant for, when it was parked, and whether an insert has been confirmed for
it. Deliberately not persisted: a dictation you did not place before quitting
is in history, and a stale item surviving a restart would be noise.
"""
import threading
import time

from logger import log, warn

_lock = threading.Lock()
_item: dict | None = None
_change_cb = None


def put(text: str, target_hwnd: int = 0) -> None:
    """Park a dictation, replacing whatever was there. One item, always."""
    global _item
    with _lock:
        _item = {
            "text": text,
            "target_hwnd": target_hwnd,
            "at": time.monotonic(),
            "consumed": False,
        }
    log("stash", f"parked {len(text)} chars for hwnd={target_hwnd}")
    _notify()


def get() -> dict | None:
    """A copy of the parked item, or None. Copied so a caller reading it on
    the tray thread cannot mutate what the insert path is about to consume."""
    with _lock:
        return dict(_item) if _item is not None else None


def mark_consumed() -> None:
    """Called when an insert is CONFIRMED landed. An unconfirmed insert must
    leave the item armed: that is the whole point of the stash."""
    global _item
    with _lock:
        if _item is None or _item["consumed"]:
            return
        _item = {**_item, "consumed": True}
    log("stash", "marked consumed")
    _notify()


def clear() -> None:
    """Drop the item entirely (user dismissed it)."""
    global _item
    with _lock:
        if _item is None:
            return
        _item = None
    log("stash", "cleared")
    _notify()


def has_unconsumed() -> bool:
    """True while there is real text parked that no confirmed insert has taken.
    Blank text never counts: it would show as an empty tray item."""
    with _lock:
        return bool(_item) and not _item["consumed"] and bool(_item["text"].strip())


def set_change_callback(fn) -> None:
    """fn(), fired after every state change so the tray can re-render.

    Never invoked while the lock is held: the callback re-enters this module
    (has_unconsumed, get) to build the menu, which would deadlock."""
    global _change_cb
    _change_cb = fn


def _notify() -> None:
    cb = _change_cb
    if cb is None:
        return
    try:
        cb()
    except Exception as exc:
        warn("stash", f"change callback failed: {exc}")
