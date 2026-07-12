"""
Local FitnessPal client — voice-driven macro logging.

Utterances that START with a food-log trigger phrase ("food log ...") are
diverted here and POSTed verbatim to the Local FitnessPal server
(http://127.0.0.1:8091), which stores the raw transcript and parses it into
food-diary entries. All I/O functions follow the taskflow.py contract: they
never raise, log failures via logger, and return None/False so callers need
no try/except.

Unlike TaskFlow there is no port discovery or app-launch machinery: the
fitness server owns a fixed port (8091, see that project's ARCHITECTURE.md)
and runs as its own always-on process. If it is down, the caller falls back
to the clipboard so the dictation is never lost.
"""

import json
import urllib.error
import urllib.request

from logger import log, warn, error as log_error
from taskflow import match_trigger  # same start-anchored prefix semantics, reused

__all__ = ["match_trigger", "check_health", "log_raw", "format_result"]

_BASE_URL = "http://127.0.0.1:8091"

_HEALTH_TIMEOUT_S = 1.5
# LM Studio JIT-loads the parse model on first use, so a cold parse can take
# tens of seconds; this timeout is deliberately generous. Callers run the
# POST on a daemon thread, so blocking here never stalls the dictation flow.
_LOG_TIMEOUT_S = 90.0


def check_health(timeout: float = _HEALTH_TIMEOUT_S) -> bool:
    """GET /health. False on any failure (connection refused, timeout, bad body)."""
    try:
        with urllib.request.urlopen(f"{_BASE_URL}/health", timeout=timeout) as resp:
            body = json.loads(resp.read())
            return resp.status == 200 and body.get("status") == "ok"
    except Exception:
        return False


def log_raw(transcript: str, timeout: float = _LOG_TIMEOUT_S) -> dict | None:
    """POST /api/log/raw with the FULL transcript (trigger phrase included —
    the server's parser strips it). Returns the response dict on 2xx, None on
    any failure. The server inserts into raw_logs before parsing, so a 2xx
    means the dictation is durably stored even if parsing was partial.
    """
    body = {"transcript": transcript, "source": "voice-dictation"}
    try:
        req = urllib.request.Request(
            f"{_BASE_URL}/api/log/raw",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status not in (200, 201):
                warn("fitness", f"log_raw: unexpected status {resp.status}")
                return None
            result = json.loads(resp.read())
        log("fitness", f"logged: {result.get('summary', '')!r}")
        return result
    except urllib.error.URLError as exc:
        warn("fitness", f"log_raw: unreachable: {exc}")
        return None
    except Exception as exc:
        log_error("fitness", f"log_raw failed: {exc}")
        return None


def format_result(resp: dict) -> str:
    """Toast line for a log_raw response. The server supplies a ready-made
    `summary` string; fall back to something honest if it ever doesn't."""
    summary = resp.get("summary")
    if isinstance(summary, str) and summary.strip():
        return summary.strip()
    return "Food log saved."
