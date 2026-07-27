import contextlib
import json
import msvcrt
import os
import re
import threading
import time
from datetime import datetime, timedelta

from logger import warn as log_warn

HISTORY_FILE = "history.json"
_CONFIG_FILE = "config.json"
_RECORDINGS_DIR = "recordings"
_LOCK_FILE = "history.lock"
MAX_ENTRIES = 100  # default cap; overridden by config.json's history_max_entries
_REDACTED = "▊▊▊"

_lock = threading.Lock()


@contextlib.contextmanager
def transaction():
    """Guard a read-modify-write of history.json across threads and processes.

    The dashboard runs in its own process, so a delete or a pin there and a
    dictation saved by the main app can interleave: both read the same list,
    both write it back, and one of the two changes vanishes. The file lock
    makes the pair serialise. Never blocks forever — after ~2.5s it proceeds
    anyway, on the grounds that losing one edit beats hanging the UI."""
    with _lock:
        handle = None
        locked = False
        try:
            handle = open(_LOCK_FILE, "a+b")
            for _ in range(50):
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError:
                    time.sleep(0.05)
        except OSError as exc:  # read-only dir, AV lock — degrade, don't crash
            log_warn("history", f"file lock unavailable: {exc}")
        try:
            yield
        finally:
            if handle is not None:
                if locked:
                    try:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                handle.close()


def _redact(text: str, patterns: list) -> str:
    """Apply redact_patterns (item 86) at save-time only — the pasted text
    itself is never touched. Invalid regexes are skipped with a log warning,
    never allowed to crash a save."""
    if not patterns:
        return text
    for pat in patterns:
        if not isinstance(pat, str) or not pat.strip():
            continue
        try:
            text = re.sub(pat, _REDACTED, text)
        except re.error as exc:
            log_warn("history", f"skipped invalid redact_patterns entry {pat!r}: {exc}")
    return text


def save(text: str, source: str | None = None, audio: str | None = None) -> None:
    with transaction():
        cfg = _read_cfg()
        text = _redact(text, cfg.get("redact_patterns", []))
        entries = _load()
        entry = {"timestamp": datetime.now().isoformat(), "text": text}
        if source:
            entry["source"] = source
        if audio:
            entry["audio"] = audio  # filename in recordings/ (voice-sample dataset)
        entries.insert(0, entry)
        entries = _enforce_cap(entries, _cap_from_cfg(cfg))
        _purge_expired_audio(entries, cfg)
        _write(entries)


def load() -> list:
    with _lock:
        return _load()


def clear() -> None:
    with transaction():
        _write([])


def set_pinned(index: int, pinned: bool, stamp: str = "") -> bool:
    """Pin/unpin the entry at index (as returned by get_history). Pinned
    entries are exempt from the entry cap and from audio auto-delete.

    stamp is the entry's timestamp; when given it decides which row is meant,
    since a dictation arriving in between shifts every index down one."""
    with transaction():
        entries = _load()
        index = resolve_index(entries, index, stamp)
        if index < 0:
            return False
        if pinned:
            entries[index]["pinned"] = True
        else:
            entries[index].pop("pinned", None)
        _write(entries)
        return True


def resolve_index(entries: list, idx: int, stamp: str) -> int:
    """Map a row a UI is showing onto its position in the list now.

    New dictations are inserted at the front, so any index held by an open
    window goes stale the moment the user speaks, and acting on it would hit
    the wrong dictation. Timestamps carry microseconds, so they identify a row
    exactly. Returns -1 when the row is gone."""
    if not stamp:
        return idx if 0 <= idx < len(entries) else -1
    if 0 <= idx < len(entries) and entries[idx].get("timestamp") == stamp:
        return idx
    for i, entry in enumerate(entries):
        if entry.get("timestamp") == stamp:
            return i
    return -1


def purge() -> None:
    """Enforce the configured entry cap and recording-age retention. Safe to
    call repeatedly (idempotent); pinned entries are exempt from both."""
    with transaction():
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
