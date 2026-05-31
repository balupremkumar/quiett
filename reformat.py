"""Vibe-coding prompt reformatter.

Converts raw Whisper speech into structured, concise coding prompts.

Backends (set via config.json "vibe_mode_backend"):
  "lmstudio"  -- LM Studio local server at localhost:1234 (recommended)
  "api"       -- Claude Haiku via Anthropic API (requires ANTHROPIC_API_KEY)
  "rules"     -- Rule-based cleaner, instant, no model needed

LM Studio setup:
  1. Open LM Studio → Local Server tab
  2. Load any instruct model (recommended: Qwen2.5-0.5B-Instruct Q4_K_M, ~350MB)
  3. Click Start Server
  That's it — no config changes needed here.
"""

import json
import os
import re
import threading
import urllib.error
import urllib.request

from logger import log, warn

_client  = None   # Anthropic client, only used for "api" backend
_ready   = threading.Event()
_backend = "rules"

_LMSTUDIO_URL = "http://localhost:1234/v1/chat/completions"

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
        req = urllib.request.Request(
            "http://localhost:1234/v1/models",
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=2)
        return True
    except Exception:
        return False


def load(backend: str = "lmstudio") -> None:
    """Initialise the reformatter. Call in a background thread at startup."""
    global _client, _backend
    try:
        if backend == "lmstudio":
            if _probe_lmstudio():
                _backend = "lmstudio"
                log("reformat", "LM Studio backend ready (localhost:1234)")
            else:
                warn("reformat", "LM Studio not reachable — falling back to rules. "
                     "Open LM Studio, load a model, and start the server.")
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
