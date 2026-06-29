"""Thin LM Studio client with task-complexity routing.

Both "small" and "large" route to the same LM Studio server (port 1234).
The routing distinction is in max_tokens and temperature:
  - small: quick reformats, short answers (max_tokens=256, temp=0.15)
  - large: rewrites with instructions, agent planning (max_tokens=1024, temp=0.2)

LM Studio only loads one model at a time, so the caller is responsible for
ensuring the right model is loaded via lms CLI before calling.
"""

import json
import urllib.request
import urllib.error

from logger import warn

_LMSTUDIO_URL = "http://localhost:1234/v1/chat/completions"

_PRESETS = {
    "small": {"max_tokens": 256,  "temperature": 0.15},
    "large": {"max_tokens": 1024, "temperature": 0.20},
}


def call(messages: list, size: str = "small", timeout: int = 20) -> str:
    """Call LM Studio. Returns response text. Raises on any error.

    messages: OpenAI-format [{"role": ..., "content": ...}, ...]
    size:     "small" | "large" — controls token budget and temperature
    """
    preset = _PRESETS.get(size, _PRESETS["small"])
    payload = json.dumps({
        "model": "local-model",
        "messages": messages,
        "max_tokens": preset["max_tokens"],
        "temperature": preset["temperature"],
        "stream": False,
    }).encode()

    req = urllib.request.Request(
        _LMSTUDIO_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read())

    content = body["choices"][0]["message"]["content"]
    return (content or "").strip()


def is_available() -> bool:
    """Quick probe — True if LM Studio server is responding."""
    try:
        urllib.request.urlopen("http://localhost:1234/v1/models", timeout=2)
        return True
    except Exception:
        return False
