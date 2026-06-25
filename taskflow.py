"""TaskFlow integration — direct-capture task creation + startup auto-launch.

TaskFlow is a separate Windows tray app with a stable local HTTP API. This
module owns all I/O with it: discovering its port/install path from
%APPDATA%\\TaskFlow\\*.json, health checks, task CRUD, project lookup, and the
auto-launch-with-backoff routine run once at voice-dictation startup.
Stateless by design (the port can change across TaskFlow restarts, so it's
never cached) — every I/O function re-reads what it needs and returns
None/False on failure instead of raising, so callers never need try/except.

Contract (frozen — see Docs/HANDOVER-voice-dictation.md for the original
POST /tasks + health/launch contract; GET/PATCH/DELETE /tasks and
GET /projects were confirmed directly against TaskFlow.Core/Api/ApiServer.cs
when building the richer integration):
  port.json     {"port", "pid", "startedAt"}   — re-read fresh every call
  app-path.json {"exePath"}
  GET    /health        -> 200 {"status": "ok"}
  GET    /tasks          -> 200 [task...]   (?completed=true|false, ?projectId=)
  POST   /tasks          body {title, notes?, dueDate?, priority?, projectId?, source?} -> 201 + task
  PATCH  /tasks/{id}      body {any task field} -> 200 + task | 404
  DELETE /tasks/{id}      -> 200 {"deleted":true} | 404
  GET    /projects       -> 200 [{"id","name","color","createdAt","archived"}...]
  launch: <exePath> --hidden
"""
import difflib
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

from logger import log, warn, error as log_error

_APPDATA_DIR = os.path.join(os.environ.get("APPDATA", ""), "TaskFlow")
_PORT_FILE = os.path.join(_APPDATA_DIR, "port.json")
_APPPATH_FILE = os.path.join(_APPDATA_DIR, "app-path.json")

_HEALTH_TIMEOUT_S = 1.5
_TASK_TIMEOUT_S = 5.0

_TRIGGER_STRIP_CHARS = " ,.:;-"


# ---------------------------------------------------------------------------
# Trigger matching & mid-utterance segmentation
# ---------------------------------------------------------------------------

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


def _match_trailing(sentence: str, phrases: list[str]) -> str | None:
    """Case-insensitive suffix match — for phrasing like 'buy milk, add that
    to my list'. Returns the leading content with the phrase stripped, or
    None.
    """
    stripped = sentence.strip().rstrip(".!?")
    lower = stripped.lower()
    for phrase in phrases:
        p = phrase.strip().lower()
        if not p:
            continue
        if lower.endswith(p) and len(stripped) > len(p):
            content = stripped[: len(stripped) - len(p)]
            content = content.rstrip(_TRIGGER_STRIP_CHARS)
            return content if content else None
    return None


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_NEGATION_PREFIXES = ("don't ", "do not ", "doesn't ", "didn't ", "won't ", "never ", "not ")
_NEAR_MISS_KEYWORDS = ("to-do", "todo", "to do", "task", "taskflow", "task flow")


def _split_sentences(text: str) -> list[str]:
    parts = _SENTENCE_SPLIT_RE.split(text.strip())
    return [p for p in parts if p.strip()]


def _is_negated(sentence_lower: str, leading_phrases: list[str]) -> bool:
    for neg in _NEGATION_PREFIXES:
        if sentence_lower.startswith(neg):
            rest = sentence_lower[len(neg):]
            if any(rest.startswith(p.strip().lower()) for p in leading_phrases if p.strip()):
                return True
    return False


def _log_near_miss(sentence: str) -> None:
    """A sentence mentions to-do/task vocabulary but matched no trigger —
    log it so phrase coverage can be tuned from real usage (this is exactly
    how the original 'Task Flow' Whisper-split bug was diagnosed)."""
    lower = sentence.lower()
    if any(kw in lower for kw in _NEAR_MISS_KEYWORDS):
        warn("taskflow", f"near-miss (mentions task/to-do, no trigger matched): {sentence!r}")


def extract_tasks(text: str, leading_phrases: list[str],
                   trailing_phrases: list[str]) -> tuple[list[str], str]:
    """Scan `text` sentence-by-sentence for task-creation commands, so a
    trigger phrase can be said mid-conversation instead of only at the very
    start of an isolated recording.

    Returns (task_texts, remainder): task_texts is the raw (unparsed) task
    content for each matched sentence in order; remainder is everything else,
    joined back together, ready for normal paste/vibe-mode handling. If
    nothing matched, returns ([], text) unchanged.

    A sentence beginning with a negation ("don't add this to my list") is
    never treated as a match — it's left in the remainder untouched.
    """
    sentences = _split_sentences(text)
    if not sentences:
        return [], text

    task_texts: list[str] = []
    remainder_sentences: list[str] = []

    for sent in sentences:
        stripped = sent.strip()
        if _is_negated(stripped.lower(), leading_phrases):
            remainder_sentences.append(sent)
            continue

        match = match_trigger(stripped, leading_phrases)
        if match:
            _, content = match
            content = content.rstrip(_TRIGGER_STRIP_CHARS + ".")
            if content:
                task_texts.append(content)
                continue

        content = _match_trailing(stripped, trailing_phrases)
        if content:
            task_texts.append(content)
            continue

        _log_near_miss(stripped)
        remainder_sentences.append(sent)

    remainder = " ".join(remainder_sentences).strip()
    return task_texts, remainder


# ---------------------------------------------------------------------------
# Task content parsing — due dates, priority, project, title/notes split
# ---------------------------------------------------------------------------

_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
             "friday": 4, "saturday": 5, "sunday": 6}

_DATE_RE = re.compile(
    r"\b(?:by|on|due|for)?\s*("
    r"today|tomorrow|"
    r"next\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"in\s+\d+\s+days?"
    r")\b",
    re.IGNORECASE,
)

_PRIORITY_HIGH_RE = re.compile(r"\b(urgent|asap|high priority|critical|important)\b", re.IGNORECASE)
_PRIORITY_LOW_RE = re.compile(r"\b(low priority|whenever|no rush|not urgent)\b", re.IGNORECASE)
_PROJECT_PHRASE_RE = re.compile(r"\b(?:to|in|under)\s+my\s+([a-z0-9 ]{2,30}?)\s+(?:list|project)\b",
                                 re.IGNORECASE)


def _strip_match(text: str, match: re.Match) -> str:
    cleaned = text[:match.start()] + text[match.end():]
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.")
    return cleaned


def _resolve_date_phrase(phrase: str, today: date) -> date | None:
    p = phrase.lower().strip()
    if p == "today":
        return today
    if p == "tomorrow":
        return today + timedelta(days=1)
    m = re.match(r"in\s+(\d+)\s+days?", p)
    if m:
        return today + timedelta(days=int(m.group(1)))
    next_week = p.startswith("next ")
    day_name = p.replace("next ", "").strip()
    idx = _WEEKDAYS.get(day_name)
    if idx is None:
        return None
    delta = (idx - today.weekday()) % 7
    if delta == 0:
        delta = 7
    if next_week:
        delta += 7
    return today + timedelta(days=delta)


def parse_due_date(text: str, today: date | None = None) -> tuple[str, str | None]:
    """Strip a spoken date phrase ('by tomorrow', 'next Friday', 'in 3 days')
    from `text`, returning (cleaned_text, iso_date_or_None)."""
    today = today or date.today()
    m = _DATE_RE.search(text)
    if not m:
        return text, None
    resolved = _resolve_date_phrase(m.group(1), today)
    if resolved is None:
        return text, None
    return _strip_match(text, m), resolved.isoformat()


def parse_priority(text: str) -> tuple[str, str | None]:
    """Strip a spoken priority phrase from `text`, returning
    (cleaned_text, 'high'|'low'|None)."""
    m = _PRIORITY_HIGH_RE.search(text)
    if m:
        return _strip_match(text, m), "high"
    m = _PRIORITY_LOW_RE.search(text)
    if m:
        return _strip_match(text, m), "low"
    return text, None


def parse_project(text: str, projects: list[dict] | None) -> tuple[str, str | None]:
    """Strip a spoken project phrase ('to my Work list') from `text`,
    resolving it against `projects` (as returned by list_projects()).
    Returns (cleaned_text, project_id_or_None)."""
    m = _PROJECT_PHRASE_RE.search(text)
    if not m or not projects:
        return text, None
    spoken = m.group(1).strip().lower()
    for proj in projects:
        if (proj.get("name") or "").strip().lower() == spoken:
            return _strip_match(text, m), proj.get("id")
    for proj in projects:
        name = (proj.get("name") or "").strip().lower()
        if name and (name in spoken or spoken in name):
            return _strip_match(text, m), proj.get("id")
    return text, None


def split_title_notes(text: str, max_title_words: int = 8) -> tuple[str, str | None]:
    """Split a long task content string into a short title plus overflow
    notes, so a rambling task doesn't become one giant unreadable title."""
    text = text.strip()
    m = re.search(r"[,;]| and | then ", text, re.IGNORECASE)
    if m and m.start() > 0:
        title = text[:m.start()].strip()
        rest = text[m.end():].strip()
        if title and rest and len(title.split()) <= max_title_words:
            return title, rest
    words = text.split()
    if len(words) > max_title_words:
        return " ".join(words[:max_title_words]), " ".join(words[max_title_words:])
    return text, None


def build_task_spec(raw_content: str, default_project_name: str | None = None) -> dict:
    """Turn raw spoken task content into a TaskFlow-ready spec dict (title,
    and any of notes/dueDate/priority/projectId that could be parsed out of
    the speech). Only calls list_projects() (a network round trip) if a
    project phrase was actually spoken or a default project is configured —
    most callers pay zero extra latency for this.
    """
    text = raw_content.strip()
    text, due_date = parse_due_date(text)
    text, priority = parse_priority(text)

    project_id = None
    if _PROJECT_PHRASE_RE.search(text) or default_project_name:
        projects = list_projects()
        text, project_id = parse_project(text, projects)
        if project_id is None and default_project_name and projects:
            wanted = default_project_name.strip().lower()
            for proj in projects:
                if (proj.get("name") or "").strip().lower() == wanted:
                    project_id = proj.get("id")
                    break

    title, notes = split_title_notes(text)
    spec: dict = {"title": title}
    if notes:
        spec["notes"] = notes
    if due_date:
        spec["dueDate"] = due_date
    if priority:
        spec["priority"] = priority
    if project_id:
        spec["projectId"] = project_id
    return spec


# ---------------------------------------------------------------------------
# Local discovery
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# TaskFlow HTTP API
# ---------------------------------------------------------------------------

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
        _remember_title(title)
        return created
    except urllib.error.URLError as exc:
        warn("taskflow", f"create_task: unreachable: {exc}")
        return None
    except Exception as exc:
        log_error("taskflow", f"create_task failed: {exc}")
        return None


def create_task_from_spec(spec: dict, timeout: float = _TASK_TIMEOUT_S) -> dict | None:
    """Convenience wrapper around create_task() for a dict as built by
    build_task_spec() (camelCase keys matching the TaskFlow API)."""
    return create_task(
        title=spec.get("title", ""),
        notes=spec.get("notes"),
        due_date=spec.get("dueDate"),
        priority=spec.get("priority"),
        project_id=spec.get("projectId"),
        timeout=timeout,
    )


def list_tasks(completed: bool | None = None, project_id: str | None = None,
               timeout: float = _TASK_TIMEOUT_S) -> list[dict] | None:
    """GET /tasks, optionally filtered. None on failure, [] if genuinely empty."""
    port = _read_port()
    if port is None:
        return None
    params = {}
    if completed is not None:
        params["completed"] = "true" if completed else "false"
    if project_id:
        params["projectId"] = project_id
    query = ("?" + urllib.parse.urlencode(params)) if params else ""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/tasks{query}", timeout=timeout
        ) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read())
    except Exception as exc:
        warn("taskflow", f"list_tasks failed: {exc}")
        return None


def update_task(task_id: str, timeout: float = _TASK_TIMEOUT_S, **fields) -> dict | None:
    """PATCH /tasks/{id} with any subset of task fields. None on failure
    (including 404 — task_id no longer exists)."""
    port = _read_port()
    if port is None:
        return None
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/tasks/{task_id}",
            data=json.dumps(fields).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="PATCH",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read())
    except Exception as exc:
        warn("taskflow", f"update_task failed: {exc}")
        return None


def delete_task(task_id: str, timeout: float = _TASK_TIMEOUT_S) -> bool:
    """DELETE /tasks/{id}. Used for the auto-create 'Undo' action. False on
    any failure, including 404."""
    port = _read_port()
    if port is None:
        return False
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/tasks/{task_id}",
            method="DELETE",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                log("taskflow", f"task {task_id} deleted (undo)")
                return True
            return False
    except Exception as exc:
        warn("taskflow", f"delete_task failed: {exc}")
        return False


def list_projects(timeout: float = _HEALTH_TIMEOUT_S) -> list[dict] | None:
    """GET /projects. None on failure."""
    port = _read_port()
    if port is None:
        return None
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/projects", timeout=timeout
        ) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read())
    except Exception as exc:
        warn("taskflow", f"list_projects failed: {exc}")
        return None


def find_open_task_by_title(spoken: str) -> dict | None:
    """Fuzzy-match `spoken` against open (incomplete) task titles, for the
    'mark X as done' voice command. Returns the best match if reasonably
    close, else None."""
    tasks = list_tasks(completed=False)
    if not tasks:
        return None
    spoken_l = spoken.strip().lower()
    best, best_score = None, 0.0
    for t in tasks:
        title_l = (t.get("title") or "").strip().lower()
        if not title_l:
            continue
        score = difflib.SequenceMatcher(None, spoken_l, title_l).ratio()
        if title_l in spoken_l or spoken_l in title_l:
            score = max(score, 0.85)
        if score > best_score:
            best, best_score = t, score
    return best if best_score >= 0.6 else None


def format_task_list(tasks: list[dict]) -> str:
    """Render an open-task list as a short spoken/displayed summary, for the
    'what's on my to-do list' read-back command."""
    titles = [(t.get("title") or "").strip() for t in (tasks or []) if (t.get("title") or "").strip()]
    if not titles:
        return "Your to-do list is empty."
    if len(titles) == 1:
        return f"You have 1 task: {titles[0]}."
    return f"You have {len(titles)} tasks: " + "; ".join(titles) + "."


_COMPLETE_RE = re.compile(
    r"^(?:mark|complete|finish)\s+(.+?)\s+as\s+(?:done|complete|finished)$",
    re.IGNORECASE,
)


def match_complete_command(text: str) -> str | None:
    """Returns the spoken task title if `text` looks like a 'mark X as done' /
    'complete X as done' / 'finish X as done' command, else None. Deliberately
    requires the explicit 'as done/complete/finished' suffix (no bare 'mark X'
    fallback) — 'mark' is a common first name, and a looser pattern would
    false-positive on ordinary sentences like 'Mark is going to call me.'
    """
    stripped = text.strip().rstrip(".!?")
    m = _COMPLETE_RE.match(stripped)
    return m.group(1).strip() if m else None


# ---------------------------------------------------------------------------
# Reliability: duplicate guard, repeated-failure tracking
# ---------------------------------------------------------------------------

_recent_titles: dict[str, float] = {}
_DUP_WINDOW_S = 60.0


def is_duplicate(title: str) -> bool:
    """True if the same title (case-insensitive) was created within the last
    minute — guards against a repeated phrase or a Whisper double-fire
    creating the same task twice."""
    key = title.strip().lower()
    last = _recent_titles.get(key)
    return last is not None and (time.time() - last) < _DUP_WINDOW_S


def _remember_title(title: str) -> None:
    _recent_titles[title.strip().lower()] = time.time()


_consecutive_health_failures = 0
_FAILURE_PROMPT_THRESHOLD = 3


def record_health_check(ok: bool) -> bool:
    """Track consecutive check_health() failures across a session. Returns
    True exactly once, the moment the failure threshold is crossed, so the
    caller can surface a one-time 'TaskFlow seems down, relaunch?' prompt
    instead of nagging on every single attempt."""
    global _consecutive_health_failures
    if ok:
        _consecutive_health_failures = 0
        return False
    _consecutive_health_failures += 1
    return _consecutive_health_failures == _FAILURE_PROMPT_THRESHOLD


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

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
