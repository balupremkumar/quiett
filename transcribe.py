import re
import threading

import numpy as np
from faster_whisper import WhisperModel

from audio import SAMPLE_RATE

_model = None
_ready = threading.Event()


def load(model_name: str) -> None:
    global _model
    _model = WhisperModel(model_name, device="cpu", compute_type="int8")
    _ready.set()


def is_ready() -> bool:
    return _ready.is_set()


def run(
    chunks: list,
    language: str,
    min_seconds: float,
    filler_words: list,
    vad_filter: bool = False,
    profile_rules: dict | None = None,
    corrections: dict | None = None,
) -> tuple[str | None, float | None]:
    """Return (postprocessed_text, confidence) or (None, None) if audio too short/empty.

    confidence is a 0–1 float: 1.0 = very confident, 0.0 = uncertain.
    Derived from Whisper's per-segment avg_logprob.
    """
    if not chunks:
        return None, None
    audio = np.concatenate(chunks, axis=0).flatten()
    if len(audio) / SAMPLE_RATE < min_seconds:
        return None, None
    _ready.wait()
    segments, _ = _model.transcribe(audio, language=language, vad_filter=vad_filter)
    seg_list = list(segments)  # materialise generator — needed for confidence + profile

    raw = " ".join(seg.text for seg in seg_list).strip()

    # avg_logprob: -0.2 = confident, -0.8 = uncertain. Normalise to 0–1.
    if seg_list:
        avg_logprob = sum(s.avg_logprob for s in seg_list) / len(seg_list)
        confidence = max(0.0, min(1.0, (avg_logprob + 0.8) / 0.6))
    else:
        confidence = None

    # Config corrections override profile rules when both define the same key
    all_rules = {**(profile_rules or {}), **(corrections or {})}
    text = _postprocess(raw, filler_words, all_rules)
    return (text if text else None), confidence


def _postprocess(text: str, filler_words: list, profile_rules: dict) -> str:
    text = _strip_fillers(text, filler_words)
    text = _apply_profile(text, profile_rules)
    if not text:
        return text
    return text[0].upper() + text[1:] + " "


def _apply_profile(text: str, rules: dict) -> str:
    """Apply learned correction rules via word-boundary replacement."""
    for whisper_out, correct_out in rules.items():
        if not whisper_out:
            continue
        escaped = re.escape(whisper_out)
        text = re.sub(r"\b" + escaped + r"\b", correct_out, text, flags=re.IGNORECASE)
    return text


def _strip_fillers(text: str, fillers: list) -> str:
    for fw in sorted(fillers, key=len, reverse=True):
        escaped = re.escape(fw)
        pattern = r"\b" + escaped + r"\b"
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    return " ".join(text.split())
