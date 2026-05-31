"""Vibe-coding prompt reformatter.

Converts raw Whisper speech into structured, concise coding prompts.

Backends (set via config.json "vibe_mode_backend"):
  "lmstudio"  -- LM Studio local server, auto-launched if not running
  "api"       -- Claude Haiku via Anthropic API (requires ANTHROPIC_API_KEY)
  "rules"     -- Rule-based cleaner, instant, no model needed

LM Studio auto-launch: the app finds lms.exe, starts the server, and loads
the model configured in "lmstudio_model" (default: qwen2.5-0.5b-instruct).
No manual steps required after initial LM Studio install.
"""

import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request

from logger import log, warn

_client  = None
_ready   = threading.Event()
_backend = "rules"

_LMSTUDIO_URL  = "http://localhost:1234/v1/chat/completions"
_LMSTUDIO_MODELS_URL = "http://localhost:1234/v1/models"

# lms.exe location — standard install path on Windows
_LMS_EXE = os.path.expandvars(r"%USERPROFILE%\.lmstudio\bin\lms.exe")

_SYSTEM_PROMPT = (
    "Convert raw voice dictation into a concise, structured coding prompt.\n"
    "Rules:\n"
    "- Imperative mood (Add, Create, Fix — not 'I want to add')\n"
    "- Strip filler openers: 'I want to', 'can you', 'please', 'basically', 'let's', 'I need to'\n"
    "- Break multi-step actions into comma-separated steps or short lines\n"
    "- Preserve every technical term, file name, and proper noun exactly\n"
    "- Do NOT add any detail the speaker did not say\n"
    "- Output ONLY the reformatted prompt, nothing else"
)

_OPENER_RE = re.compile(
    r'^(?:i\s+want\s+to|i\s+need\s+to|i\'?d\s+like\s+to|can\s+you(?:\s+please)?\s+|'
    r'please\s+|let\'s\s+|we\s+(?:need\s+to|want\s+to)\s+|i\'m\s+going\s+to\s+|'
    r'basically\s+|essentially\s+|so\s+(?:i\s+want\s+to\s+)?|'
    r'i\s+also\s+(?:want\s+to\s+|need\s+to\s+))',
    re.IGNORECASE,
)


def _rule_based(text: str) -> str:
    t = text.strip()
    m = _OPENER_RE.match(t)
    if m:
        rest = t[m.end():].strip()
        t = (rest[0].upper() + rest[1:]) if rest else t
    if t and t[-1] not in ".!?":
        t += "."
    return t


def _call_lmstudio(text: str) -> str:
    payload = json.dumps({
        "model": "local-model",
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",   "content": f"Raw: {text}"},
        ],
        "max_tokens": 256,
        "temperature": 0.15,
        "stream": False,
    }).encode()

    req = urllib.request.Request(
        _LMSTUDIO_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = json.loads(resp.read())
    return body["choices"][0]["message"]["content"].strip()


def _probe_lmstudio() -> bool:
    """Return True if LM Studio server is reachable."""
    try:
        urllib.request.urlopen(_LMSTUDIO_MODELS_URL, timeout=2)
        return True
    except Exception:
        return False


def _launch_lmstudio_server(model: str) -> bool:
    """Start LM Studio server via lms CLI and wait for it to come up.

    Returns True if server is ready within 30s, False otherwise.
    """
    if not os.path.isfile(_LMS_EXE):
        warn("reformat", f"lms.exe not found at {_LMS_EXE}")
        return False

    log("reformat", "starting LM Studio server via lms...")
    try:
        # lms server start is non-blocking (it daemonizes the server process)
        subprocess.Popen(
            [_LMS_EXE, "server", "start"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception as exc:
        warn("reformat", f"lms server start failed: {exc}")
        return False

    # Poll until server is up (up to 30s)
    for i in range(30):
        time.sleep(1)
        if _probe_lmstudio():
            log("reformat", f"LM Studio server up after {i + 1}s")
            # Load the configured model
            _load_lmstudio_model(model)
            return True

    warn("reformat", "LM Studio server did not come up within 30s")
    return False


def _load_lmstudio_model(model: str) -> None:
    """Ask lms to load the model if not already loaded."""
    if not model or not os.path.isfile(_LMS_EXE):
        return
    try:
        subprocess.Popen(
            [_LMS_EXE, "load", model, "--gpu", "off"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        log("reformat", f"lms load {model!r} dispatched")
    except Exception as exc:
        warn("reformat", f"lms load failed: {exc}")


def load(backend: str = "lmstudio", model: str = "qwen2.5-0.5b-instruct") -> None:
    """Initialise the reformatter. Call in a background thread at startup."""
    global _client, _backend
    try:
        if backend == "lmstudio":
            if _probe_lmstudio():
                _backend = "lmstudio"
                log("reformat", "LM Studio already running — backend ready")
            elif _launch_lmstudio_server(model):
                _backend = "lmstudio"
                log("reformat", "LM Studio auto-launched — backend ready")
            else:
                warn("reformat", "LM Studio unavailable — falling back to rules")
                _backend = "rules"

        elif backend == "api":
            api_key = os.environ.get("ANTHROPIC_API_KEY", "")
            if not api_key:
                warn("reformat", "ANTHROPIC_API_KEY not set — falling back to rules")
                _backend = "rules"
            else:
                import anthropic
                _client = anthropic.Anthropic(api_key=api_key)
                _backend = "api"
                log("reformat", "Anthropic API backend ready (haiku-4-5)")

        else:
            _backend = "rules"
            log("reformat", "rules backend ready")


    except Exception as exc:
        warn("reformat", f"load failed, using rules: {exc}")
        _backend = "rules"
    finally:
        _ready.set()


def is_ready() -> bool:
    return _ready.is_set()


def run(text: str) -> str:
    """Reformat raw dictation. Falls back gracefully on any error."""
    if not text.strip():
        return text

    if _backend == "lmstudio":
        try:
            result = _call_lmstudio(text)
            log("reformat", f"lmstudio: {len(text)}→{len(result)} chars")
            return result if result else text
        except urllib.error.URLError:
            warn("reformat", "LM Studio unreachable mid-session, using rules")
        except Exception as exc:
            warn("reformat", f"LM Studio error: {exc}, using rules")

    if _backend == "api" and _client is not None:
        try:
            import anthropic
            resp = _client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                system=[{
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": f"Raw: {text}"}],
            )
            result = resp.content[0].text.strip()
            log("reformat", f"api: {len(text)}→{len(result)} chars")
            return result if result else text
        except Exception as exc:
            warn("reformat", f"API error: {exc}, using rules")

    result = _rule_based(text)
    log("reformat", f"rules: {len(text)}→{len(result)} chars")
    return result
