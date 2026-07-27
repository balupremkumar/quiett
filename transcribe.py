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


def server_alive() -> bool:
    return _server_alive()


def _server_alive() -> bool:
    try:
        urllib.request.urlopen(f"{_BASE_URL}/", timeout=1.0)
        return True
    except urllib.error.HTTPError:
        return True  # server responded with non-200, still alive
    except Exception:
        return False


def check_prerequisites(model_name: str) -> list[str]:
    """Return human-readable problems with the local setup, or [] if everything's in place."""
    problems = []
    if not os.path.isfile(_SERVER_EXE):
        problems.append(f"whisper-server.exe not found at {_SERVER_EXE}")
    model_path = _resolve_model_path(model_name)
    if not os.path.isfile(model_path):
        problems.append(f"model file not found: {model_path}")
    return problems


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


def _floats_to_wav_bytes(audio: np.ndarray) -> bytes:
    """Encode a flat float32 [-1, 1] array as a 16-bit mono WAV byte string."""
    audio_i16 = np.clip(audio * 32767.0, -32768, 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio_i16.tobytes())
    return buf.getvalue()


def _chunks_to_wav_bytes(chunks: list) -> bytes:
    """Concatenate float32 chunks to a 16-bit mono WAV byte string."""
    return _floats_to_wav_bytes(np.concatenate(chunks, axis=0).flatten())


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
) -> tuple[str | None, float | None, list | None, str | None]:
    """Return (postprocessed_text, confidence, words, raw_text) or (None, None, None, None).

    raw_text is the unmodified Whisper transcript before filler-stripping,
    spoken-punctuation, and correction cleanup — kept alongside the cleaned
    text so the preview panel can offer a Raw/Cleaned toggle (item 9).
    """
    if not chunks:
        return None, None, None, None
    audio = np.concatenate(chunks, axis=0).flatten()
    duration = len(audio) / SAMPLE_RATE
    if duration < min_seconds:
        return None, None, None, None
    if not _ready.wait(timeout=30):
        warn("transcribe", "model not ready after 30s, aborting transcription")
        return None, None, None, None

    prompt = _build_prompt(initial_prompt, custom_vocabulary)
    wav_bytes = _chunks_to_wav_bytes(chunks)

    t0 = time.time()
    try:
        result = _post_inference(wav_bytes, language, prompt)
    except Exception as exc:
        warn("transcribe", f"inference failed: {exc}")
        return None, None, None, None

    raw, words, confidence = _parse_response(result)
    log("transcribe", f"dur={duration:.1f}s inf={time.time()-t0:.2f}s "
                      f"conf={confidence} chars={len(raw)} words={len(words)}")

    all_rules = {**(profile_rules or {}), **(corrections or {})}
    text = _postprocess(raw, filler_words, all_rules, custom_vocabulary)
    return (text if text else None), confidence, (words or None), (raw or None)


_PARTIAL_MAX_WINDOW_SECONDS = 20.0  # cap audio sent per partial — otherwise a
                                    # long hold makes each re-transcription of
                                    # the whole buffer slower than the last,
                                    # and the live line falls further behind
                                    # the longer someone keeps talking.


def run_partial(chunks: list, language: str) -> str | None:
    """Quick raw transcription of the in-progress recording for the live badge.

    Sends the whole buffer so far (matches what the final pass will see) —
    except once the recording exceeds _PARTIAL_MAX_WINDOW_SECONDS, when only
    the most recent window is sent, so per-partial latency stays roughly
    constant instead of growing with recording length. No prompt, no
    postprocess — speed over polish. Returns None on any failure so the
    caller can just skip the update.
    """
    if not chunks or not _ready.is_set():
        return None
    try:
        audio_arr = np.concatenate(chunks, axis=0).flatten()
        max_samples = int(_PARTIAL_MAX_WINDOW_SECONDS * SAMPLE_RATE)
        if len(audio_arr) > max_samples:
            audio_arr = audio_arr[-max_samples:]
        wav_bytes = _floats_to_wav_bytes(audio_arr)
        result = _post_inference(wav_bytes, language or "en", None)
        return (result.get("text") or "").strip()
    except Exception:
        return None


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


def _rejoin_vocabulary_splits(text: str, vocab: list | None) -> str:
    """Repair a vocabulary term Whisper broke apart, and canonicalise its
    spelling while we're there.

    Whisper splits unfamiliar compounds mid-word: "Share Point", "MyF
    itnessPal", "Wai kato", "Cop ilot", "Data verse" all shipped in real
    dictations. Rather than collecting one correction per casualty, match each
    single-word vocabulary term letter by letter with optional spaces and
    rewrite it back to the spelling the user configured — which also turns
    "dataverse" into "Dataverse" for free. Terms shorter than six letters are
    skipped: they produce too many accidental matches."""
    if not vocab:
        return text
    for term in vocab:
        canonical = str(term).strip()
        if len(canonical) < 6 or not canonical.isalnum():
            continue
        pattern = r"\b" + r"[ ]?".join(re.escape(c) for c in canonical) + r"\b"

        def _repl(m: re.Match, canonical=canonical) -> str:
            # Two spaces is already a stretch ("Peg as us"); beyond that the
            # match is more likely to be a coincidence than a split word.
            return canonical if m.group(0).count(" ") <= 2 else m.group(0)

        text = re.sub(pattern, _repl, text, flags=re.IGNORECASE)
    return text


def _postprocess(text: str, filler_words: list, profile_rules: dict,
                 vocabulary: list | None = None) -> str:
    text = _strip_nonspeech_tags(text)
    text = _drop_hallucinated_transcript(text)
    text = _collapse_repeated_sentences(text)
    text = _strip_turn_dashes(text)
    text = _strip_fillers(text, filler_words)
    # Spoken punctuation first, so "dot dot dot" isn't eaten as a stutter
    text = _apply_spoken_punctuation(text)
    text = _collapse_stutters(text)
    text = _apply_self_corrections(text)
    text = _collapse_acronyms(text)
    text = _apply_case_commands(text)
    text = _rejoin_vocabulary_splits(text, vocabulary)
    text = _apply_profile(text, profile_rules)
    text = _words_to_digits(text)
    text = _cleanup_whitespace_around_punct(text)
    text = _join_split_hyphens(text)
    text = _fix_uptalk_questions(text)
    if not text:
        return text
    return text[0].upper() + text[1:] + " "


# Whisper narrates non-speech instead of returning nothing: a breath or a
# silent tail comes back as "*sad music*", "[BLANK_AUDIO]" or "(upbeat music)".
# Real dictations of Balu's have shipped with these in them.
_NONSPEECH_TAG = re.compile(
    r"""(?x)
    \*[^*\n]{1,40}\*            # *sad music*
    | \[[^\]\n]{1,40}\]         # [BLANK_AUDIO], [MUSIC]
    | \([^)\n]*                 # (upbeat music) — only sound-ish parentheticals,
      (?:music|silence|laugh|applause|noise|sound|blank|inaudible)
      [^)\n]*\)
    | [♪♫♩][^\n]*?[♪♫♩]   # ♪ ... ♪
    """,
    re.IGNORECASE,
)


def _strip_nonspeech_tags(text: str) -> str:
    """Drop Whisper's non-speech annotations. Parentheses are only removed when
    they name a sound, so a genuine aside the user dictated survives."""
    return " ".join(_NONSPEECH_TAG.sub(" ", text).split())


# Whisper's stock hallucinations on silence or breath — artefacts of its
# YouTube training data. One real recording of Balu's came back as "*sad music*"
# and, re-run, as "Thank you.". Only ever dropped when the phrase IS the whole
# transcript: mid-dictation these are perfectly normal words.
_HALLUCINATED_WHOLE = {
    "thank you", "thanks for watching", "thank you for watching",
    "please subscribe", "subscribe to my channel", "bye", "you",
    "amara.org", "subtitles by the amara.org community",
}


def _drop_hallucinated_transcript(text: str) -> str:
    stripped = text.strip().strip(".!?,").lower()
    if stripped in _HALLUCINATED_WHOLE:
        return ""
    return text


def _strip_turn_dashes(text: str) -> str:
    """Drop the leading dash Whisper puts in front of a new speaker turn.

    Subtitle-style transcripts mark speaker changes with "- "; on a solo
    dictation it just shows up as a stray dash at the start of the text, or
    after a pause mid-dictation ("...get it all. - Would it be faster..."). Runs
    before spoken punctuation, so a dash the user actually asked for is safe."""
    text = re.sub(r"^\s*-\s+", "", text)
    return re.sub(r"(?<=[.!?])\s+-\s+(?=[A-Z])", " ", text)


_MAX_SENTENCE_REPEATS = 2


def _collapse_repeated_sentences(text: str) -> str:
    """Collapse a sentence that repeats back-to-back more than twice.

    Whisper's decoder can fall into a repetition loop on a long recording: one
    real dictation came back with "Shouldn't we be building everything from the
    Power Platform directory?" twelve times in a row. Nobody says the same
    sentence three times running, so anything past the second copy is the
    model looping, not the user."""
    parts = re.split(r"(?<=[.!?])\s+", text)
    out: list[str] = []
    run = 0
    for part in parts:
        key = part.strip().lower()
        if out and key and key == out[-1].strip().lower():
            run += 1
            if run >= _MAX_SENTENCE_REPEATS:
                continue
        else:
            run = 0
        out.append(part)
    return " ".join(p for p in out if p)


def _join_split_hyphens(text: str) -> str:
    """Rejoin a hyphenated word Whisper split across the hyphen: "front -end",
    "co- authoring", "pop -ups". Only when the space is on one side — a hyphen
    spaced on both sides is punctuation, not a broken word."""
    text = re.sub(r"(?<=\w)\s+-(?=\w)", "-", text)
    text = re.sub(r"(?<=\w)-\s+(?=\w)", "-", text)
    return text


def _apply_profile(text: str, rules: dict) -> str:
    for whisper_out, correct_out in rules.items():
        if not whisper_out:
            continue
        escaped = re.escape(whisper_out)
        text = re.sub(r"\b" + escaped + r"\b", correct_out, text, flags=re.IGNORECASE)
    return text


_SC_MARKERS = r"(?:actually|no wait|wait,? no|sorry|I mean)"


def _apply_self_corrections(text: str) -> str:
    """Apply mid-utterance self-corrections, conservatively.

    - Number swap: "at 2, actually 3" → "at 3" (any correction marker)
    - Proper-noun swap, "I mean" only: "Tuesday, I mean Wednesday" → "Wednesday"
    - "scratch that" removes the clause/sentence spoken before it

    A whole-utterance "scratch that" is left alone — main.py treats that as an
    undo command for the previous insert, not text to clean.
    """
    if re.fullmatch(r"\s*(?:scratch|undo) that[.!?]?\s*", text, flags=re.IGNORECASE):
        return text
    text = re.sub(
        rf"\b(\d[\w.:]*)\s*[,-]?\s*(?:no,?\s+)?{_SC_MARKERS}\s*,?\s+(\d[\w.:]*)",
        r"\2", text, flags=re.IGNORECASE)
    text = re.sub(r"\b([A-Z][\w'-]*)\s*,\s*I mean,?\s+([A-Z][\w'-]*)", r"\2", text)
    text = re.sub(r"[^.!?\n]*[.!?,;]?\s*\bscratch that\b[.,]?\s*", "", text,
                  flags=re.IGNORECASE)
    return text


def _apply_case_commands(text: str) -> str:
    """Dragon-style "all caps <word>" → uppercase that word."""
    return re.sub(r"\ball caps ([\w'-]+)", lambda m: m.group(1).upper(), text,
                  flags=re.IGNORECASE)


def _collapse_stutters(text: str) -> str:
    """Collapse dictation stutters — a word immediately repeated, optionally with
    a comma between ("into, into", "also, also", "the the"), becomes one
    occurrence. Letters/apostrophes only, so counting ("1 1 2") is untouched.
    """
    return re.sub(r"\b([A-Za-z']+)((?:\s*,\s*|\s+)\1\b)+", r"\1", text,
                  flags=re.IGNORECASE)


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
    (r"\bnew bullet\b",    "\n- "),
    (r"\bbullet point\b",  "\n- "),
    (r"\bdot dot dot\b",   "..."),
    (r"\bellipsis\b",      "..."),
    (r"\bat sign\b",       "@"),
    (r"\bampersand\b",     "&"),
    (r"\bunderscore\b",    "_"),
    (r"\bpercent sign\b",  "%"),
    (r"\bdollar sign\b",   "$"),
    (r"\basterisk\b",      "*"),
    (r"\bforward slash\b", "/"),
    (r"\bback slash\b",    "\\\\"),
]


def _apply_spoken_punctuation(text: str) -> str:
    for pat, repl in _SPOKEN_PUNCT:
        text = re.sub(pat, repl, text, flags=re.IGNORECASE)
    return text


def _cleanup_whitespace_around_punct(text: str) -> str:
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    # Filler/stutter removal leaves orphaned commas behind: "and, , which",
    # ", so" at the start, ", ." before a full stop, ". , Next" after one.
    text = re.sub(r",{2,}", ",", text)
    text = re.sub(r",(?=[.;:!?])", "", text)
    text = re.sub(r"([.!?]),", r"\1", text)
    text = re.sub(r"^,+\s*", "", text)
    # Join a stray space before a contraction suffix: "that 's" -> "that's", "I 'm" -> "I'm"
    text = re.sub(r"\s+'(s|t|re|ll|ve|m|d)\b", r"'\1", text, flags=re.IGNORECASE)
    # Protect ellipses, and any dot or comma that sits *inside* a token, from
    # the "space after punctuation" rule below. Without this it turned
    # "make.powerapps.com" into "make. powerapps. com", "kove.nz" into
    # "kove. nz", "anime.js" into "anime. js" and "2,600" into "2, 600" — all
    # seen in real dictations. A genuine sentence break is always followed by
    # a capital or a space in Whisper's output, so requiring a lowercase or
    # digit on the right keeps "...done.Next..." splitting as it should.
    text = re.sub(r"\.{3}", "\x01", text)
    text = re.sub(r"(?<=\w)\.(?=[a-z0-9])", "\x00", text)
    text = re.sub(r"(?<=\d),(?=\d)", "\x02", text)
    text = re.sub(r"([,.;:!?])(?=\S)", r"\1 ", text)
    text = text.replace("\x00", ".")
    text = text.replace("\x02", ",")
    text = text.replace("\x01", "...")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return text.strip()


def _collapse_acronyms(text: str) -> str:
    """Collapse runs of 2+ single UPPERCASE letters (A W S / A.W.S / I.B.M.) into an acronym.

    Only *uppercase* runs are collapsed. This is deliberate: it stops the old rule from
    mangling contractions like "it's a" -> "it'SA" and "I'm a" -> "I'MA" (the apostrophe
    creates a word boundary, so the lowercase "s a" / "m a" used to be eaten as an acronym).
    Whisper emits genuine spelled-out acronyms in capitals, so requiring uppercase keeps them.
    """
    def repl(m: re.Match) -> str:
        letters = re.findall(r"[A-Za-z]", m.group(0))
        return "".join(letters).upper()
    return re.sub(r"\b[A-Z](?:\.?[ ]?[A-Z]){1,}\.?\b", repl, text)


_NUM_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_NUM_SCALES = {"hundred": 100, "thousand": 1000, "million": 1_000_000, "billion": 1_000_000_000}


# When "one"/"two" stands alone next to one of these, it's a pronoun ("no one", "the one
# who", "next one", "one of"), not a count — keep it as a word instead of digit-ising it.
_PRONOUN_STOP_PREV = {
    "no", "the", "this", "that", "which", "every", "each", "any", "only",
    "next", "another", "is", "are", "big", "last",
}
_PRONOUN_NEXT = {"of", "who", "that", "more"}


def _words_to_digits(text: str) -> str:
    tokens = re.split(r"(\s+|[^\w\s'-]+)", text)
    out = []
    buf = []

    def last_word() -> str | None:
        for t in reversed(out):
            if t and not t.isspace():
                return t.lower().strip("-,.")
        return None

    def flush(next_tok: str | None = None):
        if not buf:
            return
        val = _parse_number_words(buf)
        words = [t for t in buf if not t.isspace()]
        single = words[0].lower().strip("-,") if len(words) == 1 else ""
        # A bare "one" stays a word. In dictated prose it is nearly always
        # part of a phrase, not a count — "in one solution", "a good one",
        # "do one about SharePoint", "more than one phase", "maybe one a day"
        # all came back as "1" and read as typos. Inside a larger number
        # ("twenty one", "one hundred") it still digitises, because there
        # `words` holds more than this token.
        keep_as_word = single == "one" or (
            single == "two"
            and (last_word() in _PRONOUN_STOP_PREV
                 or (next_tok or "").lower().strip("-,.") in _PRONOUN_NEXT)
        )
        # Scale words with no count ("8 billion", "a billion") must stay words —
        # converting bare "billion" to 1000000000 mangles "the 8 billion model".
        only_scales = all(w.lower().strip("-,") in _NUM_SCALES for w in words)
        if val is not None and not keep_as_word and not only_scales:
            out.append(str(val))
        else:
            out.extend(buf)
        buf.clear()

    for tok in tokens:
        low = tok.lower().strip("-,")
        if low in _NUM_WORDS or low in _NUM_SCALES or (low == "and" and buf):
            buf.append(tok)
        elif tok.isspace() and buf:
            buf.append(tok)
        else:
            if buf and buf[-1].isspace():
                trailing = buf.pop()
                flush(next_tok=tok)
                out.append(trailing)
            else:
                flush(next_tok=tok)
            out.append(tok)
    flush()
    return "".join(out)


_QUESTION_STARTERS = {
    "who", "what", "where", "when", "why", "how", "which", "whose", "whom",
    "do", "does", "did", "is", "are", "am", "was", "were", "can", "could",
    "will", "would", "should", "shall", "may", "might", "have", "has", "had",
    "isn't", "aren't", "don't", "doesn't", "didn't", "can't", "couldn't",
    "won't", "wouldn't", "shouldn't", "wasn't", "weren't", "haven't",
    "hasn't", "hadn't", "shall", "ain't",
}
_QUESTION_TAGS = (
    "right", "yeah", "isn't it", "aren't they", "does it", "doesn't it",
    "do you", "you think", "won't you", "wouldn't you", "can you", "could you",
)


def _fix_uptalk_questions(text: str) -> str:
    """NZ 'uptalk' (High Rising Terminal) makes Whisper hear declarative statements as
    questions and append '?'. Downgrade a trailing '?' to '.' unless the clause is
    structurally a question (starts with an interrogative word, or has a question tag).
    Conservative by design — when in doubt, the '?' is kept."""
    def repl(m: re.Match) -> str:
        clause = m.group(1)
        low = clause.lower().strip()
        words = re.findall(r"[a-z']+", low)
        if not words:
            return m.group(0)
        if words[0] in _QUESTION_STARTERS:
            return m.group(0)
        if any(low.endswith(tag) for tag in _QUESTION_TAGS):
            return m.group(0)
        return clause + "."

    return re.sub(r"([^.!?]*)\?", repl, text)


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
