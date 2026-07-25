"""Local HTTP API server for VoiceDictate — runs on loopback :8090.

Endpoints
---------
GET  /health                 ping
GET  /history                recent dictation history (last N entries)
GET  /config                 current config.json
POST /config                 patch config.json fields (JSON body)
POST /dictate                trigger a dictation capture programmatically
GET  /diagnostics            whisper/hotkey/mic status for the dashboard's Diagnostics page
POST /diagnostics/mic-probe  start a ~5s mic level test (409 while a real recording is active)
GET  /diagnostics/mic-level  poll the live level of the running mic probe
"""

import json
import threading
import time

import numpy as np
import sounddevice as sd
from flask import Flask, jsonify, request

import audio
import transcribe
from logger import log, warn

_app = Flask(__name__)
_port = 8090

# Callbacks set by main.py at startup
_get_config_fn   = None   # () -> dict
_get_history_fn  = None   # () -> list
_trigger_dictate_fn = None  # () -> None
_patch_config_fn = None   # (dict) -> None


def configure(get_config, get_history, trigger_dictate, patch_config,
              port: int = 8090) -> None:
    global _get_config_fn, _get_history_fn
    global _trigger_dictate_fn, _patch_config_fn, _port
    _get_config_fn      = get_config
    _get_history_fn     = get_history
    _trigger_dictate_fn = trigger_dictate
    _patch_config_fn    = patch_config
    _port               = port


@_app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "VoiceDictate"})


@_app.route("/history", methods=["GET"])
def get_history():
    n = request.args.get("n", 50, type=int)
    try:
        entries = (_get_history_fn() or []) if _get_history_fn else []
        return jsonify({"entries": entries[-n:]})
    except Exception as exc:
        warn("api", f"/history error: {exc}")
        return jsonify({"error": str(exc)}), 500


@_app.route("/config", methods=["GET"])
def get_config():
    try:
        cfg = _get_config_fn() if _get_config_fn else {}
        return jsonify(cfg)
    except Exception as exc:
        warn("api", f"/config GET error: {exc}")
        return jsonify({"error": str(exc)}), 500


@_app.route("/config", methods=["POST"])
def patch_config():
    try:
        data = request.get_json(force=True) or {}
        if _patch_config_fn:
            _patch_config_fn(data)
        return jsonify({"status": "ok", "updated": list(data.keys())})
    except Exception as exc:
        warn("api", f"/config PATCH error: {exc}")
        return jsonify({"error": str(exc)}), 500


@_app.route("/dictate", methods=["POST"])
def trigger_dictate():
    """Programmatically trigger a dictation recording.

    Returns immediately; the recording starts in the background.
    """
    try:
        if not _trigger_dictate_fn:
            return jsonify({"error": "dictation trigger not available"}), 503
        _trigger_dictate_fn()
        log("api", "/dictate triggered")
        return jsonify({"status": "recording_started"})
    except Exception as exc:
        warn("api", f"/dictate error: {exc}")
        return jsonify({"error": str(exc)}), 500


def _resolve_mic_status(cfg: dict) -> dict:
    """Currently configured input device: name, whether it still exists, and
    whether a real dictation recording is in progress right now."""
    input_device = cfg.get("input_device")
    try:
        recording = audio.is_recording()
    except Exception:
        recording = False
    device_name = "System default" if input_device is None else str(input_device)
    device_exists = False
    try:
        if input_device is None:
            info = sd.query_devices(kind="input")
            device_name = info.get("name") or "System default"
            device_exists = True
        else:
            info = sd.query_devices(input_device)
            device_name = info.get("name") or str(input_device)
            device_exists = int(info.get("max_input_channels", 0)) > 0
    except Exception:
        device_exists = False
    return {"device_name": device_name, "device_exists": device_exists, "recording": recording}


@_app.route("/diagnostics", methods=["GET"])
def diagnostics():
    try:
        cfg = _get_config_fn() if _get_config_fn else {}
    except Exception as exc:
        warn("api", f"/diagnostics config read failed: {exc}")
        cfg = {}

    t0 = time.time()
    try:
        whisper_up = transcribe.server_alive()
    except Exception:
        whisper_up = False
    latency_ms = round((time.time() - t0) * 1000, 1) if whisper_up else None

    whisper_info = {
        "up": whisper_up,
        "latency_ms": latency_ms,
        "model": cfg.get("model", "unknown"),
        "device": transcribe.device_used(),
    }
    # keyboard.hook() has no observable "is it still alive" state read-only.
    # This reports successful registration at startup, not a live heartbeat.
    hotkey_info = {
        "state": "registered_at_startup",
        "combo": cfg.get("hotkey", "ctrl+alt"),
        "note": "Hook health can't be independently measured while running. This "
                "reflects successful registration when the app started.",
    }
    mic_info = _resolve_mic_status(cfg)

    return jsonify({"whisper": whisper_info, "hotkey": hotkey_info, "mic": mic_info})


# ── Mic level probe ──────────────────────────────────────────────────────────
# Short-lived diagnostic recording so the dashboard can show a live level bar.
# Entirely separate from audio.py's real dictation stream; refuses to start
# while a real recording is active so the two never contend for the device.

_PROBE_DURATION_S = 5.0
_probe_lock = threading.Lock()
_probe_active = threading.Event()
_probe_levels: list = []
_probe_stream = None
_probe_started_at = 0.0


def _probe_callback(indata, frames, time_info, status) -> None:
    try:
        peak = float(np.max(np.abs(indata)))
        rms = float(np.sqrt(np.mean(indata ** 2)))
    except Exception:
        return
    with _probe_lock:
        _probe_levels.append({"t": round(time.time() - _probe_started_at, 3),
                               "peak": peak, "rms": rms})
        del _probe_levels[:-200]


def _stop_probe() -> None:
    global _probe_stream
    with _probe_lock:
        s = _probe_stream
        _probe_stream = None
    if s:
        try:
            s.stop()
            s.close()
        except Exception:
            pass
    _probe_active.clear()
    log("api", "mic probe stopped")


@_app.route("/diagnostics/mic-probe", methods=["POST"])
def mic_probe():
    if audio.is_recording():
        return jsonify({"error": "A dictation recording is in progress. Try again once it finishes."}), 409
    if _probe_active.is_set():
        return jsonify({"error": "Mic test is already running."}), 409

    global _probe_stream, _probe_started_at
    try:
        cfg = _get_config_fn() if _get_config_fn else {}
        input_device = cfg.get("input_device")
        with _probe_lock:
            _probe_levels.clear()
        _probe_started_at = time.time()
        _probe_stream = sd.InputStream(
            samplerate=16000, channels=1, dtype="float32",
            device=input_device, callback=_probe_callback,
        )
        _probe_stream.start()
        _probe_active.set()
        t = threading.Timer(_PROBE_DURATION_S, _stop_probe)
        t.daemon = True
        t.start()
        log("api", "mic probe started")
        return jsonify({"status": "started", "duration_s": _PROBE_DURATION_S})
    except Exception as exc:
        _probe_active.clear()
        _probe_stream = None
        warn("api", f"/diagnostics/mic-probe failed: {exc}")
        return jsonify({"error": str(exc)}), 500


@_app.route("/diagnostics/mic-level", methods=["GET"])
def mic_level():
    with _probe_lock:
        levels = list(_probe_levels)
    peak = max((l["peak"] for l in levels), default=0.0)
    current_rms = levels[-1]["rms"] if levels else 0.0
    return jsonify({
        "active": _probe_active.is_set(),
        "levels": levels[-64:],
        "peak": peak,
        "current_rms": current_rms,
    })


def start() -> None:
    """Start the Flask server in a daemon thread. Returns immediately."""
    def _run():
        log("api", f"HTTP API server starting on 127.0.0.1:{_port}")
        try:
            import logging as _logging
            _logging.getLogger("werkzeug").setLevel(_logging.ERROR)
            _app.run(host="127.0.0.1", port=_port, debug=False, use_reloader=False)
        except Exception as exc:
            warn("api", f"server failed: {exc}")

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    log("api", "HTTP API thread launched")
