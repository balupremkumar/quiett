"""Vibe-coding prompt reformatter.

Converts raw Whisper speech into structured, concise coding prompts.
Uses Claude Haiku via Anthropic API (fast, ~300-500ms) when a key is available,
falling back to a rule-based cleaner when offline or key is absent.

Config keys (config.json):
  vibe_mode: false            -- master toggle
  vibe_mode_backend: "api"    -- "api" | "rules"
"""

import os
import re
import threading

from logger import log, warn

_client = None
_ready  = threading.Event()
_lock   = threading.Lock()
_backend = "rules"  # updated in load()

_SYSTEM_PROMPT = """\
Convert raw voice dictation into a concise, structured coding prompt.
Rules:
- Imperative mood (Add, Create, Fix — not "I want to add")
- Strip filler openers: "I want to", "can you", "please", "basically", "let's", "I need to"
- Break multi-step actions into comma-separated steps or short lines
- Preserve every technical term, file name, and proper noun exactly
- Do NOT add any detail the speaker did not say
- Output ONLY the reformatted prompt, nothing else"""

# Rule-based patterns used as offline fallback
_OPENER_RE = re.compile(
    r'^(?:i\s+want\s+to|i\s+need\s+to|i\'?d\s+like\s+to|can\s+you(?:\s+please)?\s+|'
    r'please\s+|let\'s\s+|we\s+(?:need\s+to|want\s+to)\s+|i\'m\s+going\s+to\s+|'
    r'basically\s+|essentially\s+|so\s+(?:i\s+want\s+to\s+)?|'
    r'i\s+also\s+(?:want\s+to\s+|need\s+to\s+))',
    re.IGNORECASE,
)


def _rule_based(text: str) -> str:
    """Minimal rule-based cleaner used as fallback when API is unavailable."""
    t = text.strip()
    m = _OPENER_RE.match(t)
    if m:
        rest = t[m.end():].strip()
        t = (rest[0].upper() + rest[1:]) if rest else t
    if t and t[-1] not in ".!?":
        t += "."
    return t


def load(backend: str = "api") -> None:
    """Initialise the reformatter. Call in a background thread at startup."""
    global _client, _backend
    try:
        if backend == "api":
            api_key = os.environ.get("ANTHROPIC_API_KEY", "")
            if not api_key:
                warn("reformat", "ANTHROPIC_API_KEY not set — falling back to rules")
                _backend = "rules"
            else:
                import anthropic
                _client = anthropic.Anthropic(api_key=api_key)
                _backend = "api"
                log("reformat", "API backend ready (haiku-4-5)")
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
    """Reformat raw dictation. Returns original text if reformatting fails."""
    if not text.strip():
        return text

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
            warn("reformat", f"API call failed, using rules: {exc}")

    result = _rule_based(text)
    log("reformat", f"rules: {len(text)}→{len(result)} chars")
    return result
