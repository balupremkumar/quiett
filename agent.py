"""Voice command agent mode.

Triggered by Ctrl+Shift+C hotkey. Uses LM Studio (local LLM) for intent
classification, then executes safe actions immediately. run_command shows a
confirm gate first.

Actions
-------
open_url    — open a website in the default browser (new tab in existing window)
open_app    — start a local application by name
add_task    — add an item to TaskFlow
search      — open a Google search
type_text   — inject text via the normal dictation path
run_command — run a shell command (confirm gate required)
unknown     — unrecognised command; surfaced as a toast
"""

import ctypes
import json
import re
import subprocess
import urllib.parse
import urllib.request

from logger import log, warn

_AGENT_SYSTEM = """You interpret voice commands and return a JSON array of actions. Output ONLY the JSON array — no explanation, no markdown.

Actions:
{"action": "open_url",    "url":   "<full https URL>"}
{"action": "open_app",    "app":   "<local app name>"}
{"action": "add_task",    "title": "<task description>"}
{"action": "search",      "query": "<search query>"}
{"action": "type_text",   "text":  "<text to type>"}
{"action": "run_command", "cmd":   "<shell command>"}
{"action": "unknown"}

URL reference (always use https://):
YouTube     → https://www.youtube.com
Gmail       → https://mail.google.com
Google      → https://www.google.com
GitHub      → https://github.com
Facebook    → https://www.facebook.com
Twitter/X   → https://x.com
LinkedIn    → https://www.linkedin.com
Netflix     → https://www.netflix.com
Amazon      → https://www.amazon.com
Reddit      → https://www.reddit.com
Outlook     → https://outlook.live.com
Spotify     → https://open.spotify.com
ChatGPT     → https://chatgpt.com
Claude      → https://claude.ai

Rules:
- Always output a JSON array [...] even for a single action.
- Use open_url for websites and web apps; use open_app for local installed apps (notepad, explorer, calc, paint, etc.).
- For add_task, strip the trigger phrase and keep only the task description.
- Keep run_command safe — no destructive operations (no delete, format, shutdown, etc.).
- If the command is ambiguous or not actionable, use unknown.

Examples:
"open YouTube" → [{"action": "open_url", "url": "https://www.youtube.com"}]
"open Gmail and YouTube" → [{"action": "open_url", "url": "https://mail.google.com"}, {"action": "open_url", "url": "https://www.youtube.com"}]
"open YouTube, Gmail and Notepad" → [{"action": "open_url", "url": "https://www.youtube.com"}, {"action": "open_url", "url": "https://mail.google.com"}, {"action": "open_app", "app": "notepad"}]
"open notepad" → [{"action": "open_app", "app": "notepad"}]
"add call dentist to my tasks" → [{"action": "add_task", "title": "call dentist"}]
"add task buy milk" → [{"action": "add_task", "title": "buy milk"}]
"search for Python tutorials" → [{"action": "search", "query": "Python tutorials"}]
"run git status" → [{"action": "run_command", "cmd": "git status"}]
"""

_LMSTUDIO_URL = "http://localhost:1234/v1/chat/completions"
_model = "qwen2.5-1.5b-instruct"

_BLOCKED_CMD_PATTERNS = re.compile(
    r"\b(rm\s+-[rRf]|format\s+[a-zA-Z]:?|del\s+/[sS]|shutdown|"
    r"taskkill|reg\s+delete|mkfs|dd\s+if=)\b",
    re.IGNORECASE,
)


def set_model(model: str) -> None:
    global _model
    _model = model


def interpret(command: str) -> list[dict]:
    """Classify a voice command via LM Studio. Returns a list of action dicts."""
    if not command.strip():
        return [{"action": "unknown"}]

    payload = json.dumps({
        "model": _model,
        "messages": [
            {"role": "system", "content": _AGENT_SYSTEM},
            {"role": "user",   "content": command.strip()},
        ],
        "max_tokens": 256,
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

        # Try JSON array first
        arr_match = re.search(r"\[.*\]", content, re.DOTALL)
        if arr_match:
            parsed = json.loads(arr_match.group(0))
            if isinstance(parsed, list) and parsed:
                log("agent", f"interpreted {command!r} → {parsed}")
                return parsed

        # Fall back to single object wrapped in list
        obj_match = re.search(r"\{[^{}]+\}", content, re.DOTALL)
        if obj_match:
            action = json.loads(obj_match.group(0))
            log("agent", f"interpreted {command!r} → [{action}]")
            return [action]

    except Exception as exc:
        warn("agent", f"interpret error: {exc}")

    return [{"action": "unknown"}]


def describe(actions: list[dict]) -> str:
    """Human-readable description of a list of actions for the confirm gate."""
    parts = []
    for action in actions:
        act = action.get("action", "unknown")
        if act == "open_url":
            parts.append(f"Open: {action.get('url', '?')}")
        elif act == "open_app":
            parts.append(f"Open app: {action.get('app', '?')}")
        elif act == "add_task":
            parts.append(f"Add task: {action.get('title', '?')}")
        elif act == "run_command":
            parts.append(f"Run: {action.get('cmd', '?')}")
        elif act == "search":
            parts.append(f"Search: {action.get('query', '?')}")
        elif act == "type_text":
            parts.append(f"Type: {action.get('text', '?')}")
    return "\n".join(parts) if parts else "Unknown command"


def execute(action: dict, inject_fn=None, add_task_fn=None) -> bool:
    """Execute a single action. Returns True on success."""
    act = action.get("action", "unknown")

    if act == "open_url":
        url = (action.get("url") or "").strip()
        if not url or not url.startswith("http"):
            return False
        try:
            ret = ctypes.windll.shell32.ShellExecuteW(None, "open", url, None, None, 1)
            if ret <= 32:
                raise OSError(f"ShellExecuteW returned {ret}")
            log("agent", f"opened url: {url}")
            return True
        except Exception as exc:
            warn("agent", f"open_url failed: {exc}")
            return False

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

    if act == "add_task":
        title = (action.get("title") or "").strip()
        if not title or not add_task_fn:
            return False
        try:
            ok = add_task_fn(title)
            if ok:
                log("agent", f"added task: {title!r}")
            return bool(ok)
        except Exception as exc:
            warn("agent", f"add_task failed: {exc}")
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
            inject_fn(text, 0)
            log("agent", f"typed: {text!r}")
            return True
        except Exception as exc:
            warn("agent", f"type_text failed: {exc}")
            return False

    warn("agent", f"unhandled action: {act}")
    return False
