"""
Speech profile — learns from user corrections over time.

Every time the user edits transcribed text in the preview panel before pasting,
the diff is logged here. Pairs that appear >= MIN_OCCURRENCES times are promoted
to active substitution rules and applied automatically on future transcriptions.

Storage: SQLite (profile.db), stdlib only, zero new dependencies.
"""

import difflib
import sqlite3
import threading
from datetime import datetime, timedelta

DB_FILE = "profile.db"
MIN_OCCURRENCES = 3  # corrections needed before a rule auto-applies
PRUNE_DAYS = 90      # delete raw correction log entries older than this

_lock = threading.Lock()
_init_lock = threading.Lock()
_initialized = False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def init() -> None:
    """Create tables if they don't exist. Safe to call multiple times."""
    _ensure_init()


def log_correction(raw_text: str, edited_text: str) -> list[tuple[str, str]]:
    """Diff raw transcription against user edit and update substitution counts.

    Returns the (whisper_out, correct_out) pairs that just crossed
    MIN_OCCURRENCES with this correction — i.e. rules newly promoted to
    auto-apply — so the caller can surface them to the user.
    """
    _ensure_init()
    raw_words  = raw_text.strip().split()
    edit_words = edited_text.strip().split()
    if raw_words == edit_words:
        return []

    matcher = difflib.SequenceMatcher(None, raw_words, edit_words, autojunk=False)
    now = datetime.now().isoformat()
    pairs: list[tuple[str, str]] = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "replace":
            whisper_out = " ".join(raw_words[i1:i2]).lower().strip()
            correct_out = " ".join(edit_words[j1:j2]).strip()
            if whisper_out:
                pairs.append((whisper_out, correct_out))
        elif tag == "delete":
            # User removed words — treat as replace-with-empty
            whisper_out = " ".join(raw_words[i1:i2]).lower().strip()
            if whisper_out:
                pairs.append((whisper_out, ""))

    if not pairs:
        return []

    promoted: list[tuple[str, str]] = []
    with _lock:
        with _connect() as conn:
            for whisper_out, correct_out in pairs:
                conn.execute(
                    "INSERT INTO corrections(whisper_out, user_edit, created_at) VALUES (?,?,?)",
                    (whisper_out, correct_out, now),
                )
                conn.execute("""
                    INSERT INTO substitutions(whisper_out, correct_out, count, last_seen)
                    VALUES (?,?,1,?)
                    ON CONFLICT(whisper_out, correct_out)
                    DO UPDATE SET count=count+1, last_seen=excluded.last_seen
                """, (whisper_out, correct_out, now))
                row = conn.execute(
                    "SELECT count FROM substitutions WHERE whisper_out=? AND correct_out=?",
                    (whisper_out, correct_out),
                ).fetchone()
                if row and row[0] == MIN_OCCURRENCES:
                    conn.execute(
                        "UPDATE substitutions SET promoted_at=? "
                        "WHERE whisper_out=? AND correct_out=?",
                        (now, whisper_out, correct_out),
                    )
                    promoted.append((whisper_out, correct_out))
    return promoted


def get_active_rules() -> dict:
    """Return {whisper_out: correct_out} for rules with count >= MIN_OCCURRENCES."""
    _ensure_init()
    with _lock:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT whisper_out, correct_out FROM substitutions WHERE count >= ?",
                (MIN_OCCURRENCES,)
            ).fetchall()
    return {r[0]: r[1] for r in rows}


def get_all_rules() -> list[dict]:
    """Return all substitution rules, active and pending, for the profile viewer."""
    _ensure_init()
    with _lock:
        with _connect() as conn:
            rows = conn.execute("""
                SELECT whisper_out, correct_out, count, last_seen
                FROM substitutions
                ORDER BY count DESC, last_seen DESC
            """).fetchall()
    return [
        {
            "whisper_out": r[0],
            "correct_out": r[1],
            "count":       r[2],
            "last_seen":   r[3],
            "active":      r[2] >= MIN_OCCURRENCES,
        }
        for r in rows
    ]


def recent_promotions(days: int = 7) -> list[dict]:
    """Rules promoted to auto-apply within the last `days` days, newest
    first. Contract consumed verbatim by the dashboard's Dictionary "Learned
    this week" feed (QUIETT_UI_PLAN P6):
    {"raw": str, "fixed": str, "count": int, "promoted_at": iso8601 str}.
    """
    _ensure_init()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    with _lock:
        with _connect() as conn:
            rows = conn.execute("""
                SELECT whisper_out, correct_out, count, promoted_at
                FROM substitutions
                WHERE promoted_at IS NOT NULL AND promoted_at >= ?
                ORDER BY promoted_at DESC
            """, (cutoff,)).fetchall()
    return [
        {"raw": r[0], "fixed": r[1], "count": r[2], "promoted_at": r[3]}
        for r in rows
    ]


def delete_rule(whisper_out: str, correct_out: str) -> None:
    """Remove a rule and its underlying correction log entries."""
    _ensure_init()
    with _lock:
        with _connect() as conn:
            conn.execute(
                "DELETE FROM substitutions WHERE whisper_out=? AND correct_out=?",
                (whisper_out, correct_out),
            )
            conn.execute(
                "DELETE FROM corrections WHERE whisper_out=?",
                (whisper_out,),
            )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ensure_init() -> None:
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        with _connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS corrections (
                    id          INTEGER PRIMARY KEY,
                    whisper_out TEXT NOT NULL,
                    user_edit   TEXT NOT NULL,
                    created_at  TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_corr_whisper
                    ON corrections(whisper_out);

                CREATE TABLE IF NOT EXISTS substitutions (
                    whisper_out  TEXT NOT NULL,
                    correct_out  TEXT NOT NULL,
                    count        INTEGER DEFAULT 1,
                    last_seen    TEXT NOT NULL,
                    PRIMARY KEY (whisper_out, correct_out)
                );
            """)
            _migrate_promoted_at(conn)
        _prune()
        _initialized = True


def _migrate_promoted_at(conn: sqlite3.Connection) -> None:
    """Idempotent migration: add the promoted_at column (BACKLOG/QUIETT_UI_PLAN
    P6) if it's missing, never touching an existing profile.db's rules.
    Legacy rules that already crossed MIN_OCCURRENCES before this column
    existed get a best-effort promoted_at, derived from the created_at of the
    Nth correction that formed the rule — new rules get a real timestamp from
    here on via log_correction()."""
    cols = [row[1] for row in conn.execute("PRAGMA table_info(substitutions)").fetchall()]
    if "promoted_at" in cols:
        return
    conn.execute("ALTER TABLE substitutions ADD COLUMN promoted_at TEXT")
    already_active = conn.execute(
        "SELECT whisper_out, correct_out FROM substitutions WHERE count >= ?",
        (MIN_OCCURRENCES,),
    ).fetchall()
    for whisper_out, correct_out in already_active:
        nth = conn.execute("""
            SELECT created_at FROM corrections
            WHERE whisper_out=? AND user_edit=?
            ORDER BY created_at ASC
            LIMIT 1 OFFSET ?
        """, (whisper_out, correct_out, MIN_OCCURRENCES - 1)).fetchone()
        if nth:
            conn.execute(
                "UPDATE substitutions SET promoted_at=? WHERE whisper_out=? AND correct_out=?",
                (nth[0], whisper_out, correct_out),
            )


def _prune() -> None:
    cutoff = (datetime.now() - timedelta(days=PRUNE_DAYS)).isoformat()
    with _connect() as conn:
        conn.execute("DELETE FROM corrections WHERE created_at < ?", (cutoff,))


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(DB_FILE)
