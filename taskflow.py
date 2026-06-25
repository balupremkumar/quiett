"""TaskFlow integration — direct-capture task creation + startup auto-launch.

TaskFlow is a separate Windows tray app with a stable local HTTP API. This
module owns all I/O with it: discovering its port/install path from
%APPDATA%\\TaskFlow\\*.json, health checks, task creation, and the
auto-launch-with-backoff routine run once at voice-dictation startup.
Stateless by design (the port can change across TaskFlow restarts, so it's
never cached) — every function re-reads what it needs and returns
None/False on failure instead of raising, so callers never need try/except.

Contract (frozen — see Docs/HANDOVER-voice-dictation.md):
  port.json     {"port", "pid", "startedAt"}   — re-read fresh every call
  app-path.json {"exePath"}
  GET  /health  -> 200 {"status": "ok"}
  POST /tasks   body {"title", notes?, dueDate?, priority?, projectId?, source?} -> 201 + task JSON
  launch: <exePath> --hidden
"""
import json
import os
import subprocess
import time
import urllib.error
import urllib.request

from logger import log, warn, error as log_error

_APPDATA_DIR = os.path.join(os.environ.get("APPDATA", ""), "TaskFlow")
_PORT_FILE = os.path.join(_APPDATA_DIR, "port.json")
_APPPATH_FILE = os.path.join(_APPDATA_DIR, "app-path.json")

_HEALTH_TIMEOUT_S = 1.5
_TASK_TIMEOUT_S = 5.0

_TRIGGER_STRIP_CHARS = " ,.:;-"


def match_trigger(text: str, phrases: list[str]) -> tuple[str, str] | None:
    """Case-insensitive prefix match against `phrases`, checked in list order
    (first match wins). Returns (matched_phrase, remainder) or None.
    """
    stripped = text.strip()
    lower = stripped.lower()
    for phrase in phrases:
        p = phrase.strip().lower()
        if not p:
            continue
        if lower.startswith(p):
            remainder = stripped[len(phrase.strip()):]
            remainder = remainder.lstrip(_TRIGGER_STRIP_CHARS)
            return phrase, remainder
    return None


def _read_port() -> int | None:
    try:
        with open(_PORT_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return int(data["port"])
    except Exception:
        return None


def _read_app_path() -> str | None:
    try:
        with open(_APPPATH_FILE, encoding="utf-8") as f:
            data = json.load(f)
        path = data.get("exePath")
        return path if path else None
    except Exception:
        return None


def is_installed() -> bool:
    """True if app-path.json exists — i.e. TaskFlow was installed at some point."""
    return os.path.isfile(_APPPATH_FILE)


def check_health(timeout: float = _HEALTH_TIMEOUT_S) -> bool:
    """GET /health. False on any failure (no port file, connection refused, timeout, bad body)."""
    port = _read_port()
    if port is None:
        return False
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=timeout
        ) as resp:
            body = json.loads(resp.read())
            return resp.status == 200 and body.get("status") == "ok"
    except Exception:
        return False


def create_task(title: str, notes: str | None = None, due_date: str | None = None,
                 priority: str | None = None, project_id: str | None = None,
                 timeout: float = _TASK_TIMEOUT_S) -> dict | None:
    """POST /tasks. Returns the created task dict on 201, None on any failure.

    Always sets source="voice-dictation". Does not check health first — the
    caller decides what "unhealthy" vs. "POST failed" should look like in UI.
    """
    port = _read_port()
    if port is None:
        warn("taskflow", "create_task: no port.json found")
        return None
    body = {"title": title, "source": "voice-dictation"}
    if notes:
        body["notes"] = notes
    if due_date:
        body["dueDate"] = due_date
    if priority:
        body["priority"] = priority
    if project_id:
        body["projectId"] = project_id
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/tasks",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 201:
                warn("taskflow", f"create_task: unexpected status {resp.status}")
                return None
            created = json.loads(resp.read())
        log("taskflow", f"task created: {title!r}")
        return created
    except urllib.error.URLError as exc:
        warn("taskflow", f"create_task: unreachable: {exc}")
        return None
    except Exception as exc:
        log_error("taskflow", f"create_task failed: {exc}")
        return None


def launch_hidden() -> bool:
    """Launch <exePath> --hidden. Returns True if the process spawned (does not
    wait for health — caller polls separately via ensure_running)."""
    exe = _read_app_path()
    if not exe or not os.path.isfile(exe):
        return False
    try:
        subprocess.Popen(
            [exe, "--hidden"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        log("taskflow", "launched hidden")
        return True
    except Exception as exc:
        warn("taskflow", f"launch_hidden failed: {exc}")
        return False


def ensure_running(max_attempts: int = 4, initial_backoff: float = 2.0) -> None:
    """Startup routine: if TaskFlow isn't healthy and was ever installed, launch
    it hidden and retry health with backoff. Never raises — run on its own
    daemon thread so it never blocks voice-dictation startup.

    Skips silently if app-path.json doesn't exist — TaskFlow was never
    installed, this is a no-op bonus integration.
    """
    if check_health():
        log("taskflow", "already healthy at startup")
        return
    if not is_installed():
        log("taskflow", "not installed (no app-path.json) — skipping auto-launch")
        return
    if not launch_hidden():
        warn("taskflow", "auto-launch failed (exe missing/uninstalled?)")
        return
    backoff = initial_backoff
    for attempt in range(max_attempts):
        time.sleep(backoff)
        if check_health():
            log("taskflow", f"healthy after auto-launch (attempt {attempt + 1})")
            return
        backoff *= 1.7
    warn("taskflow", f"still unhealthy after {max_attempts} attempts post-launch")
