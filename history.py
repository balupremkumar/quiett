import json
import os
import threading
from datetime import datetime

HISTORY_FILE = "history.json"
MAX_ENTRIES = 100

_lock = threading.Lock()


def save(text: str, source: str | None = None) -> None:
    with _lock:
        entries = _load()
        entry = {"timestamp": datetime.now().isoformat(), "text": text}
        if source:
            entry["source"] = source
        entries.insert(0, entry)
        _write(entries[:MAX_ENTRIES])


def load() -> list:
    with _lock:
        return _load()


def clear() -> None:
    with _lock:
        _write([])


def _load() -> list:
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _write(entries: list) -> None:
    tmp = HISTORY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, HISTORY_FILE)
