"""LM Studio lifecycle manager for agent command mode.

Handles launching the LM Studio server, loading and unloading the model,
and reporting readiness. Called by main.py when agent_command_mode_enabled
is toggled on or off.
"""

import json
import os
import subprocess
import threading
import time
import urllib.request

from logger import log, warn

_ready = threading.Event()
_model = ""

_LMSTUDIO_MODELS_URL = "http://localhost:1234/v1/models"
_LMS_EXE = os.path.expandvars(r"%USERPROFILE%\.lmstudio\bin\lms.exe")


def _probe_lmstudio() -> bool:
    try:
        urllib.request.urlopen(_LMSTUDIO_MODELS_URL, timeout=2)
        return True
    except Exception:
        return False


def _query_loaded_models() -> list[str]:
    try:
        with urllib.request.urlopen(_LMSTUDIO_MODELS_URL, timeout=2) as resp:
            body = json.loads(resp.read())
        return [m.get("id", "") for m in body.get("data", [])]
    except Exception:
        return []


def _model_loaded(model: str) -> bool:
    return model in _query_loaded_models()


def _wait_for_model_loaded(model: str, timeout_s: int = 30) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if _model_loaded(model):
            return True
        time.sleep(1)
    return False


def _load_lmstudio_model(model: str) -> None:
    if not model or not os.path.isfile(_LMS_EXE):
        return
    try:
        subprocess.Popen(
            [_LMS_EXE, "load", model, "--gpu", "max"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        log("reformat", f"lms load {model!r} dispatched")
    except Exception as exc:
        warn("reformat", f"lms load failed: {exc}")


def _launch_lmstudio_server(model: str) -> bool:
    if not os.path.isfile(_LMS_EXE):
        warn("reformat", f"lms.exe not found at {_LMS_EXE}")
        return False

    log("reformat", "starting LM Studio server via lms...")
    try:
        subprocess.Popen(
            [_LMS_EXE, "server", "start"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception as exc:
        warn("reformat", f"lms server start failed: {exc}")
        return False

    for i in range(30):
        time.sleep(1)
        if _probe_lmstudio():
            log("reformat", f"LM Studio server up after {i + 1}s")
            _load_lmstudio_model(model)
            return True

    warn("reformat", "LM Studio server did not come up within 30s")
    return False


def load(model: str = "qwen/qwen2.5-1.5b-instruct") -> None:
    """Start LM Studio and load the model. Call in a background thread."""
    global _model
    _model = model
    try:
        if _probe_lmstudio():
            log("reformat", "LM Studio already running")
            if model and not _model_loaded(model):
                warn("reformat", f"{model!r} not loaded — loading now")
                _load_lmstudio_model(model)
                if _wait_for_model_loaded(model, timeout_s=30):
                    log("reformat", f"{model!r} loaded")
                else:
                    warn("reformat", f"{model!r} did not load within 30s")
        else:
            _launch_lmstudio_server(model)
    except Exception as exc:
        warn("reformat", f"load failed: {exc}")
    finally:
        _ready.set()


def is_ready() -> bool:
    return _ready.is_set()


def unload() -> None:
    """Unload the model from VRAM. Call when agent command mode is disabled."""
    if _model and os.path.isfile(_LMS_EXE):
        try:
            subprocess.Popen(
                [_LMS_EXE, "unload", _model],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            log("reformat", f"lms unload {_model!r} dispatched")
        except Exception as exc:
            warn("reformat", f"lms unload failed: {exc}")
    _ready.clear()
    log("reformat", "LLM unloaded")
