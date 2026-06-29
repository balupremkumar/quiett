"""Local HTTP API server for VoiceDictate — runs on loopback :8090.

Endpoints
---------
GET  /health          ping
GET  /history         recent dictation history (last N entries)
GET  /config          current config.json
POST /config          patch config.json fields (JSON body)
POST /task            create a task via TaskFlow
POST /dictate         trigger a dictation capture programmatically
"""

import json
import threading

from flask import Flask, jsonify, request

from logger import log, warn

_app = Flask(__name__)
_port = 8090

# Callbacks set by main.py at startup
_get_config_fn   = None   # () -> dict
_get_history_fn  = None   # () -> list
_create_task_fn  = None   # (title: str, project: str | None) -> dict | None
_trigger_dictate_fn = None  # () -> None
_patch_config_fn = None   # (dict) -> None


def configure(get_config, get_history, create_task, trigger_dictate, patch_config,
              port: int = 8090) -> None:
    global _get_config_fn, _get_history_fn, _create_task_fn
    global _trigger_dictate_fn, _patch_config_fn, _port
    _get_config_fn      = get_config
    _get_history_fn     = get_history
    _create_task_fn     = create_task
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


@_app.route("/task", methods=["POST"])
def create_task():
    """Create a task in TaskFlow.

    Body: {"title": "...", "project": "..."} — project is optional.
    Returns the created task object or an error.
    """
    try:
        data = request.get_json(force=True) or {}
        title   = (data.get("title") or "").strip()
        project = data.get("project") or None
        if not title:
            return jsonify({"error": "title is required"}), 400
        if not _create_task_fn:
            return jsonify({"error": "task creation not available"}), 503
        result = _create_task_fn(title, project)
        if result is None:
            return jsonify({"error": "TaskFlow unreachable"}), 503
        log("api", f"/task created: {title!r}")
        return jsonify({"status": "created", "task": result})
    except Exception as exc:
        warn("api", f"/task error: {exc}")
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
