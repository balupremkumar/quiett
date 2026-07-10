import json
import os
import threading
from datetime import datetime, timedelta

HISTORY_FILE = "history.json"
_CONFIG_FILE = "config.json"
_RECORDINGS_DIR = "recordings"
MAX_ENTRIES = 100  # default cap; overridden by config.json's history_max_entries

_lock = threading.Lock()


def save(text: str, source: str | None = None, audio: str | None = None) -> None:
    with _lock:
        entries = _load()
        entry = {"timestamp": datetime.now().isoformat(), "text": text}
        if source:
            entry["source"] = source
        if audio:
            entry["audio"] = audio  # filename in recordings/ (voice-sample dataset)
        entries.insert(0, entry)
        cfg = _read_cfg()
        entries = _enforce_cap(entries, _cap_from_cfg(cfg))
        _purge_expired_audio(entries, cfg)
        _write(entries)


def load() -> list:
    with _lock:
        return _load()


def clear() -> None:
    with _lock:
        _write([])


def set_pinned(index: int, pinned: bool) -> bool:
    """Pin/unpin the entry at index (as returned by get_history). Pinned
    entries are exempt from the entry cap and from audio auto-delete."""
    with _lock:
        entries = _load()
        if not (0 <= index < len(entries)):
            return False
        if pinned:
            entries[index]["pinned"] = True
        else:
            entries[index].pop("pinned", None)
        _write(entries)
        return True


def purge() -> None:
    """Enforce the configured entry cap and recording-age retention. Safe to
    call repeatedly (idempotent); pinned entries are exempt from both."""
    with _lock:
        entries = _load()
        cfg = _read_cfg()
        entries = _enforce_cap(entries, _cap_from_cfg(cfg))
        _purge_expired_audio(entries, cfg)
        _write(entries)


def _cap_from_cfg(cfg: dict) -> int:
    try:
        return max(1, int(cfg.get("history_max_entries", MAX_ENTRIES)))
    except (TypeError, ValueError):
        return MAX_ENTRIES


def _enforce_cap(entries: list, max_entries: int) -> list:
    """Trim to max_entries, oldest-first, without ever evicting a pinned
    entry (so the true entry count can exceed max_entries if enough are
    pinned). Entries are assumed newest-first."""
    n_pinned = sum(1 for e in entries if e.get("pinned"))
    budget = max(0, max_entries - n_pinned)
    result = []
    for e in entries:
        if e.get("pinned"):
            result.append(e)
        elif budget > 0:
            result.append(e)
            budget -= 1
        # else: oldest non-pinned entries past the budget are dropped
    return result


def _purge_expired_audio(entries: list, cfg: dict) -> bool:
    """Delete recordings/ files older than the configured retention window.
    Transcripts stay in history; only the "audio" field/file is removed.
    Pinned entries are always exempt. Returns True if anything changed."""
    try:
        days = int(cfg.get("recording_retention_days", 0) or 0)
    except (TypeError, ValueError):
        days = 0
    if days <= 0:
        return False
    cutoff = datetime.now() - timedelta(days=days)
    changed = False
    for e in entries:
        audio = e.get("audio")
        if not audio or e.get("pinned"):
            continue
        try:
            ts = datetime.fromisoformat(e.get("timestamp", ""))
        except ValueError:
            continue
        if ts >= cutoff:
            continue
        path = os.path.join(_RECORDINGS_DIR, audio)
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass
        del e["audio"]
        changed = True
    return changed


def _read_cfg() -> dict:
    try:
        with open(_CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


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
