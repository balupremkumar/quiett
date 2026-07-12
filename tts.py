"""Cloned-voice TTS — manages the qwentts.cpp tts-server subprocess.

Same lifecycle pattern as transcribe.py's whisper-server, but stricter about
resources: the server only starts on first speak and is killed after
tts_unload_idle_seconds without playback, so the ~2.3 GB of VRAM is never
held idle. Voice is registered once per server start from voice_profile/
(POST /v1/voices); synthesis streams s16le 24 kHz PCM straight into a
sounddevice output stream, so playback starts on the first chunk.
"""
import base64
import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request

import sounddevice as sd

from logger import log, warn, error as log_error

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
_SERVER_EXE = os.path.join(_PROJECT_ROOT, "third_party", "qwentts.cpp", "bin", "tts-server.exe")
_TALKER_GGUF = os.path.join(_PROJECT_ROOT, "third_party", "qwentts.cpp", "models",
                            "qwen-talker-1.7b-base-Q8_0.gguf")
_CODEC_GGUF = os.path.join(_PROJECT_ROOT, "third_party", "qwentts.cpp", "models",
                           "qwen-tokenizer-12hz-Q8_0.gguf")
_PROFILE_DIR = os.path.join(_PROJECT_ROOT, "voice_profile")
_MANIFEST = os.path.join(_PROFILE_DIR, "manifest.json")
_WHISPER_INFERENCE = "http://127.0.0.1:8089/inference"

_HOST = "127.0.0.1"
_VOICE = "user"
_SAMPLE_RATE = 24000
_CHUNK_BYTES = 8192
_IDLE_POLL_S = 30

_get_cfg = lambda: {}  # replaced by set_cfg_getter at wiring time

_lock = threading.Lock()          # guards _proc / _voice_ok lifecycle
_proc: subprocess.Popen | None = None
_voice_ok = False
_last_used = 0.0
_reaper_started = False
_abort = threading.Event()
_speaking = threading.Event()


def set_cfg_getter(fn) -> None:
    global _get_cfg
    _get_cfg = fn


def _port() -> int:
    try:
        return int(_get_cfg().get("tts_port", 8092))
    except (TypeError, ValueError):
        return 8092


def _base_url() -> str:
    return f"http://{_HOST}:{_port()}"


def check_prerequisites() -> list[str]:
    """Human-readable setup problems, or [] if the engine can run."""
    problems = []
    for path, what in ((_SERVER_EXE, "tts-server.exe"),
                       (_TALKER_GGUF, "talker model"),
                       (_CODEC_GGUF, "codec model")):
        if not os.path.isfile(path):
            problems.append(f"{what} not found at {path}")
    if not os.path.isfile(_MANIFEST):
        problems.append("no voice profile built yet (voice_profile/manifest.json missing)")
    return problems


def _alive() -> bool:
    try:
        urllib.request.urlopen(f"{_base_url()}/health", timeout=1.0)
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def _post_json(path: str, payload: dict, timeout: float = 60.0):
    req = urllib.request.Request(
        f"{_base_url()}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=timeout)


def _ref_from_profile() -> tuple[str, str]:
    """Reference sample path + its transcript ('' if unavailable).
    config tts_reference (a filename in voice_profile/) overrides the
    manifest's automatic best pick — set from the ear-picked winner of a
    reference sweep."""
    with open(_MANIFEST, encoding="utf-8") as f:
        manifest = json.load(f)
    chosen = _get_cfg().get("tts_reference") or manifest["best"]
    wav = os.path.join(_PROFILE_DIR, chosen)
    if not os.path.isfile(wav):
        warn("tts", f"tts_reference {chosen!r} not found, falling back to manifest best")
        chosen = manifest["best"]
        wav = os.path.join(_PROFILE_DIR, chosen)
    # Transcript: sidecar .txt beats the manifest (pinned references live
    # outside the manifest and survive profile rebuilds), whisper as fallback.
    text = ""
    sidecar = os.path.splitext(wav)[0] + ".txt"
    if os.path.isfile(sidecar):
        with open(sidecar, encoding="utf-8") as f:
            text = f.read().strip()
    if not text:
        for s in manifest.get("samples", []):
            if s.get("file") == chosen:
                text = (s.get("transcript") or "").strip()
                break
    if not text:
        text = _transcribe_ref(wav)
    return wav, text


def _transcribe_ref(wav_path: str) -> str:
    """Get the reference transcript from the app's own whisper-server.
    ref_text enables the stronger ICL clone mode; '' degrades gracefully
    to x-vector-only cloning, so failures here are non-fatal."""
    try:
        with open(wav_path, "rb") as f:
            wav_bytes = f.read()
        boundary = "----ttsref"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="ref.wav"\r\n'
            f"Content-Type: audio/wav\r\n\r\n"
        ).encode() + wav_bytes + (
            f"\r\n--{boundary}\r\n"
            f'Content-Disposition: form-data; name="response_format"\r\n\r\n'
            f"json\r\n--{boundary}--\r\n"
        ).encode()
        req = urllib.request.Request(
            _WHISPER_INFERENCE, data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return (json.loads(resp.read()).get("text") or "").strip()
    except Exception as exc:
        warn("tts", f"reference transcription failed ({exc}); using x-vector-only clone")
        return ""


def _register_voice() -> None:
    global _voice_ok
    wav, ref_text = _ref_from_profile()
    with open(wav, "rb") as f:
        payload = {"name": _VOICE, "wav_b64": base64.b64encode(f.read()).decode()}
    if ref_text:
        payload["ref_text"] = ref_text
    with _post_json("/v1/voices", payload, timeout=60) as resp:
        resp.read()
    _voice_ok = True
    log("tts", f"voice registered from {os.path.basename(wav)}"
               f" ({'ICL' if ref_text else 'x-vector'} mode)")


def ensure_ready(timeout_s: int = 45) -> None:
    """Start tts-server and register the voice if not already up. Blocking."""
    global _proc, _voice_ok, _reaper_started
    with _lock:
        if _alive() and _voice_ok:
            return
        problems = check_prerequisites()
        if problems:
            raise RuntimeError("; ".join(problems))
        if not _alive():
            _voice_ok = False
            cmd = [_SERVER_EXE, "--model", _TALKER_GGUF, "--codec", _CODEC_GGUF,
                   "--host", _HOST, "--port", str(_port())]
            log("tts", f"starting tts-server on :{_port()}")
            _proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            t0 = time.monotonic()
            while time.monotonic() - t0 < timeout_s:
                time.sleep(0.5)
                if _alive():
                    break
            else:
                _shutdown_locked()
                raise RuntimeError(f"tts-server did not come up within {timeout_s}s")
            log("tts", f"tts-server ready after {time.monotonic() - t0:.1f}s")
        if not _voice_ok:
            _register_voice()
        if not _reaper_started:
            _reaper_started = True
            threading.Thread(target=_idle_reaper, daemon=True).start()


def speak(text: str) -> None:
    """Synthesise text in the cloned voice and play it. Blocking — run in a
    worker thread. stop() aborts from any thread."""
    global _last_used
    text = (text or "").strip()
    if not text:
        return
    if _speaking.is_set():
        return  # one utterance at a time; caller stops first if they want new audio
    _abort.clear()
    _speaking.set()
    try:
        ensure_ready()
        _last_used = time.monotonic()
        # seed 42 matches the P0 bench renders Balu picked the voice from;
        # fixed seed keeps delivery consistent between plays of the same text.
        resp = _post_json("/v1/audio/speech",
                          {"input": text, "voice": _VOICE, "response_format": "pcm",
                           "seed": 42},
                          timeout=120)
        device = _get_cfg().get("tts_output_device") or None
        with resp, sd.RawOutputStream(samplerate=_SAMPLE_RATE, channels=1,
                                      dtype="int16", device=device) as stream:
            log("tts", f"speaking {len(text)} chars")
            while not _abort.is_set():
                chunk = resp.read(_CHUNK_BYTES)
                if not chunk:
                    break
                stream.write(chunk)
    finally:
        _speaking.clear()
        _last_used = time.monotonic()


def stop() -> None:
    _abort.set()


def is_speaking() -> bool:
    return _speaking.is_set()


def _idle_reaper() -> None:
    while True:
        time.sleep(_IDLE_POLL_S)
        try:
            idle_after = float(_get_cfg().get("tts_unload_idle_seconds", 300))
        except (TypeError, ValueError):
            idle_after = 300.0
        if idle_after <= 0:
            continue  # 0 disables auto-unload
        with _lock:
            if (_proc is not None and not _speaking.is_set()
                    and time.monotonic() - _last_used > idle_after):
                log("tts", f"idle {idle_after:.0f}s — unloading tts-server to free VRAM")
                _shutdown_locked()


def _shutdown_locked() -> None:
    global _proc, _voice_ok
    if _proc is not None:
        try:
            _proc.kill()
        except Exception:
            pass
        _proc = None
    _voice_ok = False


def shutdown() -> None:
    stop()
    with _lock:
        _shutdown_locked()
