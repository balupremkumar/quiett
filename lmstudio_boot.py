"""
LM Studio process control — boot/stop the local LM Studio server + model so
Fitness Pal's parser only holds VRAM while the "Fitness Pal (LM Studio)" tray
toggle is enabled.

Drives the `lms` CLI (bundled with LM Studio) via subprocess. Both boot() and
shutdown() run on a daemon thread so the caller (tray toggle, app startup)
never blocks; failures are logged and toasted, never raised, matching the
taskflow.py / fitness.py contract of I/O helpers that never crash the tray.
"""

import os
import shutil
import subprocess
import threading

from logger import log, warn, error as log_error
import tray

LMS_EXE = shutil.which("lms") or os.path.expanduser("~/.lmstudio/bin/lms.exe")

_SERVER_START_TIMEOUT_S = 20.0
_MODEL_LOAD_TIMEOUT_S = 120.0
_MODEL_UNLOAD_TIMEOUT_S = 20.0
_SERVER_STOP_TIMEOUT_S = 20.0

__all__ = ["LMS_EXE", "boot", "shutdown"]


def _run(args: list, timeout: float) -> None:
    subprocess.run(
        [LMS_EXE, *args],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
        timeout=timeout,
    )


def _lms_missing() -> bool:
    return not LMS_EXE or not os.path.isfile(LMS_EXE)


def _boot_worker(model_key: str) -> None:
    tray.notify("VoiceDictate", "LM Studio starting…")
    try:
        _run(["server", "start"], _SERVER_START_TIMEOUT_S)
        _run(["load", model_key, "-y"], _MODEL_LOAD_TIMEOUT_S)
        log("lmstudio_boot", f"server started, model loaded ({model_key})")
        tray.notify("VoiceDictate", "LM Studio ready (model loaded)")
    except Exception as exc:
        log_error("lmstudio_boot", f"boot failed: {exc}")
        tray.notify("VoiceDictate", "LM Studio failed to start — check app.log")


def boot(model_key: str) -> None:
    """Start the LM Studio server and preload model_key. Non-blocking (runs on
    a daemon thread); never raises. No-op with a toast if lms.exe isn't found."""
    if _lms_missing():
        log_error("lmstudio_boot", f"lms executable not found ({LMS_EXE!r})")
        tray.notify("VoiceDictate", "LM Studio not found — is it installed?")
        return
    threading.Thread(target=_boot_worker, args=(model_key,), daemon=True).start()


def _shutdown_worker(model_key: str) -> None:
    try:
        _run(["unload", model_key], _MODEL_UNLOAD_TIMEOUT_S)
        _run(["server", "stop"], _SERVER_STOP_TIMEOUT_S)
        log("lmstudio_boot", f"model unloaded, server stopped ({model_key})")
        tray.notify("VoiceDictate", "LM Studio stopped")
    except Exception as exc:
        log_error("lmstudio_boot", f"shutdown failed: {exc}")
        tray.notify("VoiceDictate", "LM Studio failed to stop cleanly — check app.log")


def shutdown(model_key: str) -> None:
    """Unload model_key then stop the LM Studio server. Non-blocking (runs on
    a daemon thread); never raises. No-op if lms.exe isn't found."""
    if _lms_missing():
        log_error("lmstudio_boot", f"lms executable not found ({LMS_EXE!r})")
        return
    threading.Thread(target=_shutdown_worker, args=(model_key,), daemon=True).start()
