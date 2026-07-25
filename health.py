"""
Background health monitor for the whisper-server backend.

Polls every 30s. If the server is detected as down (after initial successful
boot), auto-restarts it and surfaces an in-app toast with a "Restart" action.
"""
from __future__ import annotations

import threading
import time

from logger import log, warn

_POLL_INTERVAL = 30.0

_thread: threading.Thread | None = None
_stop = threading.Event()
_last_whisper_ok = False
_restart_whisper_fn = None
_toast_fn = None


def configure(toast_fn, restart_whisper_fn) -> None:
    """toast_fn(message, kind, action_label, action_cb) — called when the
    backend goes down."""
    global _toast_fn, _restart_whisper_fn
    _toast_fn = toast_fn
    _restart_whisper_fn = restart_whisper_fn


def start() -> None:
    global _thread
    if _thread is not None:
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()


def _loop() -> None:
    import transcribe
    global _last_whisper_ok

    # Wait for initial boot to settle before first check
    time.sleep(15.0)
    _last_whisper_ok = transcribe.server_alive()

    while not _stop.is_set():
        if _stop.wait(_POLL_INTERVAL):
            break

        ok = transcribe.server_alive()
        if _last_whisper_ok and not ok:
            warn("health", "whisper-server appears down — auto-restarting")
            if _restart_whisper_fn:
                _restart_whisper_fn()
            if _toast_fn:
                _toast_fn("Whisper server stopped responding — restarting automatically.",
                          "warn", "Restart now", _restart_whisper_fn)
        elif not _last_whisper_ok and ok:
            log("health", "whisper-server recovered")
        _last_whisper_ok = ok
