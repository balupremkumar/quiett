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
_last_backend_used = "rules"  # which backend actually produced the most recent run() result
_model   = ""  # model name last passed to load(), needed by unload()

_LMSTUDIO_URL  = "http://localhost:1234/v1/chat/completions"
_LMSTUDIO_MODELS_URL = "http://localhost:1234/v1/models"

# lms.exe location — standard install path on Windows
_LMS_EXE = os.path.expandvars(r"%USERPROFILE%\.lmstudio\bin\lms.exe")

_CODING_PROMPT = (
    "You rewrite raw voice dictation as a concise coding instruction.\n"
    "Start with an imperative verb (Add, Create, Fix, Refactor, Remove).\n"
    "Keep every file name, function name, technical term, and proper noun exactly as spoken.\n"
    "Use comma-separated steps for multi-action requests.\n"
    "Output only the rewritten instruction. No preamble. No explanation.\n"
    "\n"
    "Example 1\n"
    "Raw: so I want to basically add a new button to the settings panel that says reset profile and when clicked it clears the SQLite database\n"
    "Rewrite: Add a 'Reset Profile' button to the settings panel that clears the SQLite database on click.\n"
    "\n"
    "Example 2\n"
    "Raw: can you please fix the bug in inject.py where the paste fails in VS Code because of focus race\n"
    "Rewrite: Fix paste focus race in inject.py for VS Code.\n"
    "\n"
    "Example 3\n"
    "Raw: I need to refactor the audio module to support multiple microphones and also add a device dropdown in the settings UI\n"
    "Rewrite: Refactor audio module to support multiple microphones, add device dropdown to settings UI."
)

_CHAT_PROMPT = (
    "You rewrite raw voice dictation as a clear chat message.\n"
    "Keep the user's intent and tone, but fix grammar, remove filler, and tighten phrasing.\n"
    "Use natural sentence punctuation.\n"
    "Output only the rewritten message. No preamble.\n"
    "\n"
    "Example 1\n"
    "Raw: hey just wanted to like check in on that PR you were going to look at yesterday\n"
    "Rewrite: Hey — just checking in on that PR you were going to look at yesterday.\n"
    "\n"
    "Example 2\n"
    "Raw: yeah um sounds good let's do tuesday like 2 pm works for me\n"
    "Rewrite: Sounds good, let's do Tuesday — 2pm works for me."
)

_LONGFORM_PROMPT = (
    "You rewrite raw voice dictation as a polished paragraph.\n"
    "Preserve the speaker's voice. Fix grammar, remove filler, and join fragments into flowing prose.\n"
    "Output only the rewritten paragraph. No preamble.\n"
    "\n"
    "Example\n"
    "Raw: so basically the thing about local first apps is that you don't have to worry about server costs and also they work offline which is huge for power users\n"
    "Rewrite: Local-first apps remove server costs and work offline — both of which matter enormously to power users."
)

PROFILES: dict[str, str] = {
    "coding":   _CODING_PROMPT,
    "chat":     _CHAT_PROMPT,
    "longform": _LONGFORM_PROMPT,
}

_active_profile = "coding"


def set_profile(name: str) -> None:
    """Switch the active reformat profile. Falls back to 'coding' if name unknown."""
    global _active_profile
    if name in PROFILES:
        _active_profile = name
        log("reformat", f"active profile -> {name}")
    else:
        warn("reformat", f"unknown profile {name!r}, keeping {_active_profile!r}")


def get_profile() -> str:
    return _active_profile


def _system_prompt() -> str:
    return PROFILES.get(_active_profile, _CODING_PROMPT)


# Back-compat alias for code that still imports _SYSTEM_PROMPT
_SYSTEM_PROMPT = _CODING_PROMPT

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
    # /no_think disables Qwen3's reasoning mode so we don't burn tokens on <think> blocks
    payload = json.dumps({
        "model": "local-model",
        "messages": [
            {"role": "system", "content": _system_prompt() + "\n/no_think"},
            {"role": "user",   "content": f"Raw: {text}\nRewrite:"},
        ],
        "max_tokens": 512,
        "temperature": 0.15,
        "stream": False,
    }).encode()

    req = urllib.request.Request(
        _LMSTUDIO_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read())
    content = body["choices"][0]["message"]["content"]
    # Strip any residual <think>...</think> block if /no_think was ignored
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    # Drop a leading "Rewrite:" if the model echoed the prefix
    content = re.sub(r"^\s*Rewrite\s*:\s*", "", content, flags=re.IGNORECASE)
    return content.strip()


def _probe_lmstudio() -> bool:
    """Return True if LM Studio server is reachable."""
    try:
        urllib.request.urlopen(_LMSTUDIO_MODELS_URL, timeout=2)
        return True
    except Exception:
        return False


def _query_loaded_models() -> list[str]:
    """Return the model ids LM Studio currently has loaded into memory."""
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
            [_LMS_EXE, "load", model, "--gpu", "max"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        log("reformat", f"lms load {model!r} dispatched")
    except Exception as exc:
        warn("reformat", f"lms load failed: {exc}")


def load(backend: str = "lmstudio", model: str = "qwen/qwen3-8b") -> None:
    """Initialise the reformatter. Call in a background thread at startup."""
    global _client, _backend, _model
    _model = model
    try:
        if backend == "lmstudio":
            if _probe_lmstudio():
                _backend = "lmstudio"
                log("reformat", "LM Studio already running — backend ready")
                # The server being up doesn't mean OUR model is loaded — if the
                # server was already running before this app started, the
                # "already running" branch used to skip loading entirely, and
                # runtime chat calls would silently fall back to rules.
                if model and not _model_loaded(model):
                    warn("reformat", f"LM Studio running but {model!r} not loaded — loading now")
                    _load_lmstudio_model(model)
                    if _wait_for_model_loaded(model, timeout_s=30):
                        log("reformat", f"{model!r} loaded")
                    else:
                        warn("reformat", f"{model!r} did not load within 30s — chat calls will fall back to rules")
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


def unload() -> None:
    """Unload the LLM model from VRAM. Call when vibe mode is disabled."""
    global _backend, _client
    if _backend == "lmstudio" and _model and os.path.isfile(_LMS_EXE):
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
    _backend = "rules"
    _client = None
    _ready.clear()
    log("reformat", "LLM unloaded — backend reset to rules")


def last_backend_used() -> str:
    """Which backend actually produced the most recent run() result: 'lmstudio' | 'api' | 'rules'."""
    return _last_backend_used


def run(text: str) -> str:
    """Reformat raw dictation. Falls back gracefully on any error."""
    global _last_backend_used
    if not text.strip():
        return text

    if _backend == "lmstudio":
        try:
            result = _call_lmstudio(text)
            _last_backend_used = "lmstudio"
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
                    "text": _system_prompt(),
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": f"Raw: {text}"}],
            )
            result = resp.content[0].text.strip()
            _last_backend_used = "api"
            log("reformat", f"api: {len(text)}→{len(result)} chars")
            return result if result else text
        except Exception as exc:
            warn("reformat", f"API error: {exc}, using rules")

    result = _rule_based(text)
    _last_backend_used = "rules"
    log("reformat", f"rules: {len(text)}→{len(result)} chars")
    return result
