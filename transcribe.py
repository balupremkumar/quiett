"""Whisper transcription via local whisper.cpp HTTP server (Vulkan GPU).

Runs whisper-server.exe as a subprocess on startup (like LM Studio for the
reformatter) and POSTs audio to /inference for each recording. The server keeps
the model resident in VRAM between requests, so per-recording latency is just
the inference time itself.

Configuration via main.py:
  - model_name: "small" | "medium" | "large-v3-turbo" — resolves to a ggml file
    in models/. If the file is missing, raises a clear error at load() time.
  - The Vulkan-built whisper-server.exe lives at
    third_party/whisper.cpp/build/bin/Release/whisper-server.exe.

If the binary or model is missing, load() raises — main.py shows a toast and the
user can fall back manually by editing config.json.
"""

import io
import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave

import numpy as np

from audio import SAMPLE_RATE
from logger import log, warn

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
_SERVER_EXE = os.path.join(
    _PROJECT_ROOT, "third_party", "whisper.cpp", "build", "bin", "Release",
    "whisper-server.exe",
)
_MODELS_DIR = os.path.join(_PROJECT_ROOT, "models")

# Map config "model" names to ggml file names in models/
_MODEL_FILES = {
    "tiny":             "ggml-tiny.bin",
    "base":             "ggml-base.bin",
    "small":            "ggml-small.bin",
    "medium":           "ggml-medium.bin",
    "large-v3":         "ggml-large-v3.bin",
    "large-v3-turbo":   "ggml-large-v3-turbo-q5_0.bin",
}

_HOST = "127.0.0.1"
_PORT = 8089
_BASE_URL = f"http://{_HOST}:{_PORT}"
_INFERENCE_URL = f"{_BASE_URL}/inference"

_proc: subprocess.Popen | None = None
_ready = threading.Event()
_load_error: str | None = None
_device_used: str = "vulkan"


def _resolve_model_path(model_name: str) -> str:
    fname = _MODEL_FILES.get(model_name, model_name)
    if os.path.isabs(fname):
        return fname
    return os.path.join(_MODELS_DIR, fname)


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        try:
            s.connect((host, port))
            return True
        except OSError:
            return False


def _server_alive() -> bool:
    try:
        urllib.request.urlopen(f"{_BASE_URL}/", timeout=1.0)
        return True
    except urllib.error.HTTPError:
        return True  # server responded with non-200, still alive
    except Exception:
        return False


def load(model_name: str) -> None:
    """Start whisper-server.exe with the requested model. Blocks until ready."""
    global _proc, _load_error, _device_used

    if not os.path.isfile(_SERVER_EXE):
        _load_error = f"whisper-server.exe not found at {_SERVER_EXE}"
        log("transcribe", _load_error)
        raise FileNotFoundError(_load_error)

    model_path = _resolve_model_path(model_name)
    if not os.path.isfile(model_path):
        _load_error = f"model file not found: {model_path}"
        log("transcribe", _load_error)
        raise FileNotFoundError(_load_error)

    if _port_in_use(_HOST, _PORT) and _server_alive():
        log("transcribe", f"whisper-server already running on :{_PORT}")
        _device_used = "vulkan"
        _ready.set()
        return

    cmd = [
        _SERVER_EXE,
        "--model", model_path,
        "--host", _HOST,
        "--port", str(_PORT),
        "--language", "en",
        "--threads", "4",
        "--flash-attn",
    ]
    log("transcribe", f"starting whisper-server: {' '.join(cmd)}")
    _proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

    # Wait up to 60s for the server to come up (model load + Vulkan init)
    for i in range(60):
        time.sleep(1)
        if _server_alive():
            log("transcribe", f"whisper-server ready after {i + 1}s")
            _device_used = "vulkan"
            _ready.set()
            return

    _load_error = "whisper-server did not start within 60s"
    log("transcribe", _load_error)
    raise RuntimeError(_load_error)


def is_ready() -> bool:
    return _ready.is_set()


def load_error() -> str | None:
    return _load_error


def device_used() -> str:
    return _device_used


def shutdown() -> None:
    """Stop the subprocess (called on app exit)."""
    global _proc
    if _proc and _proc.poll() is None:
        try:
            _proc.terminate()
            _proc.wait(timeout=5)
        except Exception:
            try:
                _proc.kill()
            except Exception:
                pass
    _proc = None
    _ready.clear()


def _chunks_to_wav_bytes(chunks: list) -> bytes:
    """Concatenate float32 chunks to a 16-bit mono WAV byte string."""
    audio = np.concatenate(chunks, axis=0).flatten()
    # Convert float32 [-1, 1] to int16
    audio_i16 = np.clip(audio * 32767.0, -32768, 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio_i16.tobytes())
    return buf.getvalue()


def _post_inference(wav_bytes: bytes, language: str, initial_prompt: str | None) -> dict:
    """POST WAV to /inference, return parsed JSON response."""
    boundary = f"----vd{uuid.uuid4().hex}"

    def field(name: str, value: str) -> bytes:
        return (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode()

    body = bytearray()
    body += field("temperature", "0.0")
    body += field("temperature_inc", "0.2")
    body += field("response_format", "verbose_json")
    body += field("language", language or "en")
    if initial_prompt:
        body += field("prompt", initial_prompt)

    # File part
    body += (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
        f"Content-Type: audio/wav\r\n\r\n"
    ).encode()
    body += wav_bytes
    body += f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        _INFERENCE_URL,
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


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
    wav_bytes = _chunks_to_wav_bytes(chunks)

    t0 = time.time()
    try:
        result = _post_inference(wav_bytes, language, prompt)
    except Exception as exc:
        warn("transcribe", f"inference failed: {exc}")
        return None, None, None

    raw, words, confidence = _parse_response(result)
    log("transcribe", f"dur={duration:.1f}s inf={time.time()-t0:.2f}s "
                      f"conf={confidence} chars={len(raw)} words={len(words)}")

    all_rules = {**(profile_rules or {}), **(corrections or {})}
    text = _postprocess(raw, filler_words, all_rules)
    return (text if text else None), confidence, (words or None)


def _parse_response(result: dict) -> tuple[str, list, float | None]:
    """Extract text, word list, and avg confidence from a whisper-server response."""
    raw = (result.get("text") or "").strip()
    words: list = []
    seg_conf: list = []

    for seg in result.get("segments", []) or []:
        # Some builds emit "words" with "probability"; older builds emit "tokens"
        for w in seg.get("words", []) or []:
            txt = (w.get("word") or w.get("text") or "").strip()
            prob = float(w.get("probability", w.get("p", 1.0)))
            if txt:
                words.append({"text": txt, "prob": prob})
        if "avg_logprob" in seg:
            seg_conf.append(float(seg["avg_logprob"]))
        elif "no_speech_prob" in seg:
            seg_conf.append(1.0 - float(seg["no_speech_prob"]))

    if seg_conf:
        avg = sum(seg_conf) / len(seg_conf)
        if avg < 0:  # avg_logprob → normalised confidence
            confidence = max(0.0, min(1.0, (avg + 0.8) / 0.6))
        else:
            confidence = max(0.0, min(1.0, avg))
    elif words:
        confidence = sum(w["prob"] for w in words) / len(words)
    else:
        confidence = None

    return raw, words, confidence


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
