import re
import threading

import numpy as np
from faster_whisper import WhisperModel

from audio import SAMPLE_RATE
from logger import log

_model = None
_ready = threading.Event()
_load_error: str | None = None
_device_used: str = "cpu"


def _detect_device() -> tuple[str, str]:
    """Return (device, compute_type). Prefer CUDA when available; fall back to CPU int8."""
    try:
        import ctypes
        ctypes.CDLL("cudart64_12.dll")
        return "cuda", "float16"
    except (OSError, ImportError):
        pass
    try:
        import torch  # optional
        if torch.cuda.is_available():
            return "cuda", "float16"
    except Exception:
        pass
    return "cpu", "int8"


def load(model_name: str) -> None:
    global _model, _load_error, _device_used
    device, compute_type = _detect_device()
    _device_used = device
    log("transcribe", f"loading model={model_name} device={device} compute={compute_type}")
    try:
        _model = WhisperModel(model_name, device=device, compute_type=compute_type)
        _ready.set()
        _load_error = None
        log("transcribe", "model ready")
    except Exception as exc:
        _load_error = str(exc)
        log("transcribe", f"model load failed: {exc}")
        if device != "cpu":
            log("transcribe", "retrying on CPU")
            try:
                _model = WhisperModel(model_name, device="cpu", compute_type="int8")
                _device_used = "cpu"
                _ready.set()
                _load_error = None
                log("transcribe", "model ready (cpu fallback)")
            except Exception as exc2:
                _load_error = str(exc2)
                log("transcribe", f"cpu fallback failed: {exc2}")
                raise
        else:
            raise


def is_ready() -> bool:
    return _ready.is_set()


def load_error() -> str | None:
    return _load_error


def device_used() -> str:
    return _device_used


def run(
    chunks: list,
    language: str,
    min_seconds: float,
    filler_words: list,
    vad_filter: bool = False,
    profile_rules: dict | None = None,
    corrections: dict | None = None,
    initial_prompt: str | None = None,
    custom_vocabulary: list | None = None,
) -> tuple[str | None, float | None, list | None]:
    """Return (postprocessed_text, confidence, words) or (None, None, None)."""
    if not chunks:
        return None, None, None
    audio = np.concatenate(chunks, axis=0).flatten()
    duration = len(audio) / SAMPLE_RATE
    if duration < min_seconds:
        return None, None, None
    _ready.wait()

    prompt = _build_prompt(initial_prompt, custom_vocabulary)

    segments, _ = _model.transcribe(
        audio,
        language=language,
        vad_filter=vad_filter,
        initial_prompt=prompt,
        word_timestamps=True,
    )
    seg_list = list(segments)
    raw = " ".join(seg.text for seg in seg_list).strip()

    words = []
    for s in seg_list:
        for w in (s.words or []):
            words.append({"text": w.word.strip(), "prob": float(w.probability)})

    if seg_list:
        avg_logprob = sum(s.avg_logprob for s in seg_list) / len(seg_list)
        confidence = max(0.0, min(1.0, (avg_logprob + 0.8) / 0.6))
    else:
        confidence = None

    log("transcribe", f"dur={duration:.1f}s conf={confidence} chars={len(raw)} words={len(words)}")

    all_rules = {**(profile_rules or {}), **(corrections or {})}
    text = _postprocess(raw, filler_words, all_rules)
    return (text if text else None), confidence, (words or None)


def _build_prompt(initial_prompt: str | None, vocab: list | None) -> str | None:
    parts = []
    if initial_prompt:
        parts.append(initial_prompt.strip())
    if vocab:
        terms = [str(t).strip() for t in vocab if str(t).strip()]
        if terms:
            parts.append("Vocabulary: " + ", ".join(terms) + ".")
    return " ".join(parts) if parts else None


def _postprocess(text: str, filler_words: list, profile_rules: dict) -> str:
    text = _strip_fillers(text, filler_words)
    text = _collapse_acronyms(text)
    text = _apply_spoken_punctuation(text)
    text = _apply_profile(text, profile_rules)
    text = _words_to_digits(text)
    text = _cleanup_whitespace_around_punct(text)
    if not text:
        return text
    return text[0].upper() + text[1:] + " "


def _apply_profile(text: str, rules: dict) -> str:
    for whisper_out, correct_out in rules.items():
        if not whisper_out:
            continue
        escaped = re.escape(whisper_out)
        text = re.sub(r"\b" + escaped + r"\b", correct_out, text, flags=re.IGNORECASE)
    return text


def _strip_fillers(text: str, fillers: list) -> str:
    for fw in sorted(fillers, key=len, reverse=True):
        escaped = re.escape(fw)
        pattern = r"(?<!\w)" + escaped + r"(?!\w)"
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    return " ".join(text.split())


_SPOKEN_PUNCT = [
    (r"\bnew paragraph\b", "\n\n"),
    (r"\bnew line\b",      "\n"),
    (r"\bquestion mark\b", "?"),
    (r"\bexclamation mark\b", "!"),
    (r"\bexclamation point\b", "!"),
    (r"\bsemicolon\b",     ";"),
    (r"\bcolon\b",         ":"),
    (r"\bcomma\b",         ","),
    (r"\bperiod\b",        "."),
    (r"\bfull stop\b",     "."),
    (r"\bopen parenthesis\b", "("),
    (r"\bclose parenthesis\b", ")"),
    (r"\bopen quote\b",    "\""),
    (r"\bclose quote\b",   "\""),
    (r"\bdash\b",          "-"),
    (r"\bhyphen\b",        "-"),
]


def _apply_spoken_punctuation(text: str) -> str:
    for pat, repl in _SPOKEN_PUNCT:
        text = re.sub(pat, repl, text, flags=re.IGNORECASE)
    return text


def _cleanup_whitespace_around_punct(text: str) -> str:
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"([,.;:!?])(?=\S)", r"\1 ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return text.strip()


def _collapse_acronyms(text: str) -> str:
    """Collapse runs of 2+ single letters (A W S or A. W. S.) into ALLCAPS acronym."""
    def repl(m: re.Match) -> str:
        letters = re.findall(r"[A-Za-z]", m.group(0))
        return "".join(letters).upper()
    return re.sub(r"\b(?:[A-Za-z]\.? ){1,}[A-Za-z]\.?(?=\b)", repl, text)


_NUM_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_NUM_SCALES = {"hundred": 100, "thousand": 1000, "million": 1_000_000, "billion": 1_000_000_000}


def _words_to_digits(text: str) -> str:
    """Convert runs of number words into digits. Conservative: only consecutive number words."""
    tokens = re.split(r"(\s+|[^\w\s'-]+)", text)
    out = []
    buf = []

    def flush():
        if not buf:
            return
        val = _parse_number_words(buf)
        if val is not None:
            out.append(str(val))
        else:
            out.extend(buf)
        buf.clear()

    for tok in tokens:
        low = tok.lower().strip("-,")
        if low in _NUM_WORDS or low in _NUM_SCALES or low == "and" and buf:
            buf.append(tok)
        elif tok.isspace() and buf:
            buf.append(tok)
        else:
            # strip trailing whitespace from buf before flush
            while buf and buf[-1].isspace():
                trailing = buf.pop()
                flush()
                out.append(trailing)
                break
            else:
                flush()
            out.append(tok)
    flush()
    return "".join(out)


def _parse_number_words(tokens: list) -> int | None:
    words = [t.lower().strip("-,") for t in tokens if not t.isspace() and t.lower().strip("-,") != "and"]
    if not words:
        return None
    if not all(w in _NUM_WORDS or w in _NUM_SCALES for w in words):
        return None
    total = 0
    current = 0
    for w in words:
        if w in _NUM_WORDS:
            current += _NUM_WORDS[w]
        else:
            scale = _NUM_SCALES[w]
            if scale == 100:
                current = max(current, 1) * 100
            else:
                total += max(current, 1) * scale
                current = 0
    return total + current
