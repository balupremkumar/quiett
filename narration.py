"""Turns raw text into timed speech segments for tts.py to play.

Why this exists: qwentts.cpp's talker takes plain text and has no prosody
control at all, no SSML, no pause markup, no rate knob per word. The only
way to get a tutor-like read (pauses where meaning breaks, headings and
lists set apart, dense sentences slowed down) is to work it out from the
text's own structure before synthesis. Parsing punctuation, line breaks and
list markers is deterministic, offline, needs no VRAM, adds no latency and
is debuggable when it sounds wrong, whereas asking an LLM to annotate
prosody would cost all four of those and still sometimes be confidently
wrong about which word to stress. See STUDY_MODE_PLAN.md P1 for the design.

plan() never rewrites, reorders or drops text; it only decides where to cut
it into segments and how long to pause between them. tts.py is the consumer:
it synthesises each segment's text and holds pause_after_seconds of silence
before the next one, at the given speed.
"""
import re

_SPEED_MIN, _SPEED_MAX = 0.5, 2.0

# --- Flat (non-study) mode: same numbers tts._chunk_text has used since P0 ---
_GAP_SENTENCE_S = 0.25  # pause after a segment that ended a sentence
_GAP_CLAUSE_S = 0.12    # pause after a segment that is mid-sentence (hard split)

# --- Study mode pause table (STUDY_MODE_PLAN.md P1) - tune by ear, not by hand ---
_PAUSE_COMMA = 0.15          # comma, semicolon or colon boundary inside a sentence
_PAUSE_SENTENCE = 0.45       # sentence end
_PAUSE_CLAUSE_HARD = 0.20    # clause break forced by a run-on sentence hitting budget
_PAUSE_LINE = 0.35           # single newline inside a paragraph
_PAUSE_PARAGRAPH = 0.90      # blank line between paragraphs
_PAUSE_LIST_ITEM = 0.55      # after each list item
_PAUSE_LIST_FIRST_EXTRA = 0.30  # extra, added before the first item of a list
_PAUSE_HEADING_AFTER = 1.00  # after a heading
_PAUSE_HEADING_BEFORE = 0.60  # extra, added before a heading
_PAUSE_QUOTE_EDGE = 0.20     # extra, added on both sides of a wholly-quoted segment

# --- Study mode rate shaping - multiplicative on base_speed ---
_RATE_HEADING = 0.95
_RATE_PARAGRAPH_FIRST_SENTENCE = 0.97
_RATE_LONG_SENTENCE = 0.95
_RATE_QUOTE = 0.97
_LONG_SENTENCE_WORDS = 40   # sentences longer than this are where a listener falls behind
_HEADING_MAX_CHARS = 60     # heading candidates must be shorter than this

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])[\"'’”)\]]*\s+")
_ENDS_SENTENCE = re.compile(r"[.!?…][\"'’”)\]]*$")
_CLAUSE_SPLIT = re.compile(r"(?<=[,;:])\s+")
_LIST_MARKER = re.compile(r"^(?:[-*•]|\d+[.)]|[a-zA-Z][.)])\s+")
_HEADING_TERMINAL = re.compile(r"[.!?:;,]$")
_PARAGRAPH_SPLIT = re.compile(r"\n[ \t]*\n+")
_QUOTE_PAIRS = (('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’"))


def plan(text: str, *, budget: int, base_speed: float,
         study: bool = False, pause_scale: float = 1.0) -> list[tuple[str, float, float]]:
    """Returns a list of (text_to_speak, pause_after_seconds, speed) segments.

    budget <= 0 disables splitting entirely (the whole text is one segment),
    same contract as the old tts._chunk_text. study=False reproduces that
    function's sentence-merge behaviour exactly, so callers not using Study
    Mode see no change. study=True runs the pause table and rate shaping.
    """
    if not text or not text.strip():
        return []
    if budget <= 0:
        return [(text.strip(), 0.0, _clamp_speed(base_speed))]
    if not study:
        return _plan_flat(text, budget, base_speed, pause_scale)
    return _plan_study(text, budget, base_speed, pause_scale)


def _clamp_speed(speed: float) -> float:
    return min(_SPEED_MAX, max(_SPEED_MIN, speed))


# --------------------------------------------------------------------------
# Flat mode - ported from tts._chunk_text, plus the pause it always implied
# --------------------------------------------------------------------------

def _plan_flat(text: str, budget: int, base_speed: float,
               pause_scale: float) -> list[tuple[str, float, float]]:
    chunks = _chunk_flat(text, budget)
    speed = _clamp_speed(base_speed)
    segments = []
    for i, chunk in enumerate(chunks):
        if i == len(chunks) - 1:
            pause = 0.0
        else:
            gap = _GAP_SENTENCE_S if _ENDS_SENTENCE.search(chunk) else _GAP_CLAUSE_S
            pause = gap * pause_scale
        segments.append((chunk, pause, speed))
    return segments


def _chunk_flat(text: str, budget: int) -> list[str]:
    """Sentence-merge chunker, identical logic to tts._chunk_text."""
    sentences: list[str] = []
    for line in text.splitlines():
        sentences.extend(s.strip() for s in _SENTENCE_SPLIT.split(line) if s.strip())
    pieces: list[str] = []
    for s in sentences:
        while len(s) > budget:  # a run-on sentence still has to be broken up
            cut = max(s.rfind(", ", 0, budget), s.rfind("; ", 0, budget),
                      s.rfind(" ", 0, budget))
            if cut <= 0:
                cut = budget - 1
            pieces.append(s[:cut + 1].strip())
            s = s[cut + 1:].strip()
        if s:
            pieces.append(s)
    chunks: list[str] = []
    for p in pieces:
        if chunks and len(chunks[-1]) + 1 + len(p) <= budget:
            chunks[-1] = f"{chunks[-1]} {p}"
        else:
            chunks.append(p)
    return chunks or [text.strip()]


# --------------------------------------------------------------------------
# Study mode - structure-driven pauses and rate shaping
# --------------------------------------------------------------------------

def _plan_study(text: str, budget: int, base_speed: float,
                 pause_scale: float) -> list[tuple[str, float, float]]:
    lines = _structure_lines(text)
    if not lines:
        return []

    segments: list[list] = []  # mutable [text, pause, speed] until the final pass
    first_sentence_of_para = True
    prev_para_idx = None

    for line in lines:
        if line["para_idx"] != prev_para_idx:
            first_sentence_of_para = True
            prev_para_idx = line["para_idx"]

        # "Before" pauses land on the previous segment, not this line's own.
        if segments:
            if line["kind"] == "heading":
                segments[-1][1] += _PAUSE_HEADING_BEFORE
            elif line["is_first_list"]:
                segments[-1][1] += _PAUSE_LIST_FIRST_EXTRA

        # Headings and list markers are not prose sentences; "1. Introduction"
        # must not be read as a sentence ending after "1.".
        split_sentences = line["kind"] == "text"
        line_segs = _segments_for_line(line["text"], budget, split_sentences)
        for idx, seg in enumerate(line_segs):
            rate = 1.0
            pause = seg["pause"]
            text_piece = seg["text"]

            if _is_wholly_quoted(text_piece):
                rate *= _RATE_QUOTE
                pause += _PAUSE_QUOTE_EDGE
                if segments:
                    segments[-1][1] += _PAUSE_QUOTE_EDGE

            if line["kind"] == "heading":
                rate *= _RATE_HEADING
            if line["kind"] == "text":
                if seg["sentence_idx"] == 0 and first_sentence_of_para:
                    rate *= _RATE_PARAGRAPH_FIRST_SENTENCE
                if seg["words"] > _LONG_SENTENCE_WORDS:
                    rate *= _RATE_LONG_SENTENCE

            if idx == len(line_segs) - 1:
                pause = max(pause, _transition_pause(line))

            segments.append([text_piece, pause, _clamp_speed(base_speed * rate)])

        if line["kind"] == "text" and line_segs:
            first_sentence_of_para = False

    for seg in segments:
        seg[1] *= pause_scale
    if segments:
        segments[-1][1] = 0.0
    return [tuple(seg) for seg in segments]


def _structure_lines(text: str) -> list[dict]:
    """Flattens text into lines tagged with paragraph index, kind (heading /
    list / text) and what follows (line break, paragraph break, or end)."""
    paragraphs = [p for p in _PARAGRAPH_SPLIT.split(text) if p.strip()]
    lines: list[dict] = []
    for pi, para in enumerate(paragraphs):
        para_lines = [ln.strip() for ln in para.splitlines() if ln.strip()]
        prev_kind = None
        for li, raw in enumerate(para_lines):
            if li < len(para_lines) - 1:
                break_after = "line"
            elif pi < len(paragraphs) - 1:
                break_after = "paragraph"
            else:
                break_after = "end"
            kind = _classify_line(raw, li, para_lines, break_after)
            is_first_list = kind == "list" and prev_kind != "list"
            lines.append({"text": raw, "kind": kind, "is_first_list": is_first_list,
                          "break_after": break_after, "para_idx": pi})
            prev_kind = kind
    return lines


def _classify_line(raw: str, li: int, para_lines: list[str], break_after: str) -> str:
    if _LIST_MARKER.match(raw):
        return "list"
    if len(raw) < _HEADING_MAX_CHARS and not _HEADING_TERMINAL.search(raw):
        followed_by_blank = break_after == "paragraph"
        followed_by_longer = li + 1 < len(para_lines) and len(para_lines[li + 1]) > len(raw)
        if followed_by_blank or followed_by_longer:
            return "heading"
    return "text"


def _transition_pause(line: dict) -> float:
    if line["kind"] == "heading":
        return _PAUSE_HEADING_AFTER
    if line["kind"] == "list":
        return _PAUSE_LIST_ITEM
    if line["break_after"] == "line":
        return _PAUSE_LINE
    if line["break_after"] == "paragraph":
        return _PAUSE_PARAGRAPH
    return 0.0  # break_after == "end" - the true final line gets 0 anyway


def _segments_for_line(raw: str, budget: int, split_sentences: bool = True) -> list[dict]:
    """Splits one line at sentence and clause boundaries, then hard-splits
    anything still over budget. Every resulting piece carries the pause its
    own boundary implies; the caller decides whether the line's last piece
    should defer to a bigger transition pause instead.

    split_sentences=False keeps the whole line as one "sentence": headings
    and list items ("1. Introduction") are not prose, and running them
    through the sentence splitter would read "1." as ending a sentence."""
    if split_sentences:
        sentences = [s.strip() for s in _SENTENCE_SPLIT.split(raw) if s.strip()] or [raw]
    else:
        sentences = [raw]
    segs: list[dict] = []
    for si, sentence in enumerate(sentences):
        is_last_sentence = si == len(sentences) - 1
        words = len(sentence.split())
        clauses = [c.strip() for c in _CLAUSE_SPLIT.split(sentence) if c.strip()] or [sentence]
        for ci, clause in enumerate(clauses):
            is_last_clause = ci == len(clauses) - 1
            hard_pieces = _hard_split(clause, budget)
            for hi, piece in enumerate(hard_pieces):
                is_last_hard = hi == len(hard_pieces) - 1
                if not is_last_hard:
                    pause = _PAUSE_CLAUSE_HARD
                elif not is_last_clause:
                    pause = _PAUSE_COMMA
                elif not is_last_sentence:
                    pause = _PAUSE_SENTENCE
                else:
                    pause = 0.0  # true end of the line; caller applies the transition
                segs.append({"text": piece, "pause": pause, "sentence_idx": si, "words": words})
    return segs


def _hard_split(piece: str, budget: int) -> list[str]:
    """Breaks a single clause at word boundaries so no segment exceeds
    budget; falls back to a hard character cut for an unbreakable run."""
    pieces: list[str] = []
    s = piece
    while len(s) > budget:
        cut = s.rfind(" ", 0, budget)
        if cut <= 0:
            cut = budget - 1
        pieces.append(s[:cut + 1].strip())
        s = s[cut + 1:].strip()
    if s:
        pieces.append(s)
    return pieces or [piece]


def _is_wholly_quoted(text: str) -> bool:
    t = text.strip()
    return any(len(t) >= 2 and t[0] == open_c and t[-1] == close_c
               for open_c, close_c in _QUOTE_PAIRS)
