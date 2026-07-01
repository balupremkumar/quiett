"""Voice command agent mode.

Triggered by Ctrl+Alt+C hotkey or voice trigger phrases (e.g. "hey computer").
Uses LM Studio (local LLM) for intent classification, then shows a confirm gate
before executing any action.

Actions
-------
open_app    — start an application by name
run_command — run a shell command in the background
search      — open a browser search
type_text   — inject text via the normal dictation path
unknown     — unrecognised command; surfaced as a toast
"""

import ctypes
import json
import re
import subprocess
import urllib.parse
import urllib.request

from logger import log, warn

_AGENT_SYSTEM = """You interpret voice commands and output a JSON action object.

Available actions (output ONLY the JSON — no explanation):
{"action": "open_app",    "app":   "<app name>"}
{"action": "run_command", "cmd":   "<shell command>"}
{"action": "search",      "query": "<search query>"}
{"action": "type_text",   "text":  "<text to type>"}
{"action": "unknown"}

Rules:
- If the command is ambiguous, prefer "unknown".
- Keep shell commands safe — no rm -rf, no format, no shutdown.
- For "open_app", use the plain app name (chrome, notepad, explorer, etc.).

Examples:
"open Chrome"                → {"action": "open_app",    "app":   "chrome"}
"search for Python tutorials"→ {"action": "search",      "query": "Python tutorials"}
"run git status"             → {"action": "run_command", "cmd":   "git status"}
"type Hello world"           → {"action": "type_text",   "text":  "Hello world"}
"what time is it"            → {"action": "search",      "query": "current time"}
"""

_LMSTUDIO_URL = "http://localhost:1234/v1/chat/completions"
_model = "qwen2.5-1.5b-instruct"


def set_model(model: str) -> None:
    global _model
    _model = model


# Commands that are explicitly blocked for safety
_BLOCKED_CMD_PATTERNS = re.compile(
    r"\b(rm\s+-[rRf]|format\s+[a-zA-Z]:?|del\s+/[sS]|shutdown|"
    r"taskkill|reg\s+delete|mkfs|dd\s+if=)\b",
    re.IGNORECASE,
)


def interpret(command: str) -> dict:
    """Classify a voice command via LM Studio. Returns an action dict."""
    if not command.strip():
        return {"action": "unknown"}

    payload = json.dumps({
        "model": _model,
        "messages": [
            {"role": "system", "content": _AGENT_SYSTEM},
            {"role": "user",   "content": command.strip()},
        ],
        "max_tokens": 128,
        "temperature": 0.05,
        "stream": False,
    }).encode()

    try:
        req = urllib.request.Request(
            _LMSTUDIO_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
        content = body["choices"][0]["message"]["content"] or ""
        # Extract first JSON object from the response
        match = re.search(r"\{[^{}]+\}", content, re.DOTALL)
        if match:
            action = json.loads(match.group(0))
            log("agent", f"interpreted {command!r} → {action}")
            return action
    except Exception as exc:
        warn("agent", f"interpret error: {exc}")

    return {"action": "unknown"}


def describe(action: dict) -> str:
    """Human-readable description of an action for the confirm gate."""
    act = action.get("action", "unknown")
    if act == "open_app":
        return f"Open app: {action.get('app', '?')}"
    if act == "run_command":
        return f"Run: {action.get('cmd', '?')}"
    if act == "search":
        return f"Search: {action.get('query', '?')}"
    if act == "type_text":
        return f"Type: {action.get('text', '?')}"
    return "Unknown command"


def execute(action: dict, inject_fn=None) -> bool:
    """Execute an action. Returns True on success.

    inject_fn: callable(text, hwnd=0) used for type_text actions.
    """
    act = action.get("action", "unknown")

    if act == "open_app":
        app = (action.get("app") or "").strip()
        if not app:
            return False
        try:
            ret = ctypes.windll.shell32.ShellExecuteW(None, "open", app, None, None, 1)
            if ret <= 32:
                raise OSError(f"ShellExecuteW returned {ret}")
            log("agent", f"opened app: {app}")
            return True
        except Exception as exc:
            warn("agent", f"open_app failed: {exc}")
            return False

    if act == "run_command":
        cmd = (action.get("cmd") or "").strip()
        if not cmd:
            return False
        if _BLOCKED_CMD_PATTERNS.search(cmd):
            warn("agent", f"blocked unsafe command: {cmd!r}")
            return False
        try:
            subprocess.Popen(
                cmd, shell=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            log("agent", f"ran command: {cmd}")
            return True
        except Exception as exc:
            warn("agent", f"run_command failed: {exc}")
            return False

    if act == "search":
        query = (action.get("query") or "").strip()
        if not query:
            return False
        try:
            url = "https://www.google.com/search?q=" + urllib.parse.quote(query)
            ret = ctypes.windll.shell32.ShellExecuteW(None, "open", url, None, None, 1)
            if ret <= 32:
                raise OSError(f"ShellExecuteW returned {ret}")
            log("agent", f"search: {query}")
            return True
        except Exception as exc:
            warn("agent", f"search failed: {exc}")
            return False

    if act == "type_text":
        text = (action.get("text") or "").strip()
        if not text or not inject_fn:
            return False
        try:
            inject_fn(text, 0)  # hwnd=0 uses foreground
            log("agent", f"typed: {text!r}")
            return True
        except Exception as exc:
            warn("agent", f"type_text failed: {exc}")
            return False

    warn("agent", f"unhandled action: {act}")
    return False
