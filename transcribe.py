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
) -> str | None:
    """Return postprocessed text, or None if audio was too short / empty."""
    if not chunks:
        return None
    audio = np.concatenate(chunks, axis=0).flatten()
    if len(audio) / SAMPLE_RATE < min_seconds:
        return None
    _ready.wait()  # safety net — hotkey already guards this
    segments, _ = _model.transcribe(audio, language=language, vad_filter=vad_filter)
    raw = " ".join(seg.text for seg in segments).strip()
    return _postprocess(raw, filler_words)


def _postprocess(text: str, filler_words: list) -> str:
    text = _strip_fillers(text, filler_words)
    if not text:
        return text
    return text[0].upper() + text[1:] + " "


def _strip_fillers(text: str, fillers: list) -> str:
    # Longest fillers first so multi-word phrases match before their sub-words
    for fw in sorted(fillers, key=len, reverse=True):
        escaped = re.escape(fw)
        pattern = r"\b" + escaped + r"\b"
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    return " ".join(text.split())
