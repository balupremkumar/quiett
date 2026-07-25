"""Cloned-voice TTS — manages the qwentts.cpp tts-server subprocess.

Same lifecycle pattern as transcribe.py's whisper-server, but stricter about
resources: the server only starts on first speak and is killed after
tts_unload_idle_seconds without playback, so the ~2.3 GB of VRAM is never
held idle. Voice is registered once per server start from voice_profile/
(POST /v1/voices); synthesis streams s16le 24 kHz PCM straight into a
sounddevice output stream, so playback starts on the first chunk.

Long text is synthesised a segment at a time (the talker speeds up the more it
is given in one request) and can be slowed down or sped up on playback with a
pitch-preserving stretch, since the server itself has no rate control.
narration.py decides the segments, the pause after each one, and its rate;
study mode is simply a different set of those decisions over the same text.
"""
import base64
import json
import os
import queue
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request

import numpy as np
import sounddevice as sd

import narration
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
_SPEED_MIN, _SPEED_MAX = 0.5, 2.0
_STRETCH_FRAME = 1024   # ~43 ms analysis window at 24 kHz
_STRETCH_SEARCH = 256   # ±10.7 ms alignment hunt — covers a full pitch period
_STRETCH_MATCH = 256    # samples compared per candidate; a full frame is 8x the
                        # memory traffic for no audible gain
# The talker accelerates the longer the passage it is given: measured against
# the live server, 1-2 sentences hold 2.40 s per sentence, 3 drop to 2.16 s and
# 6 to 1.87 s (23 % faster). Synthesising a chunk at a time holds one pace.
_CHUNK_CHARS = 120
_TAIL_MAX_S = 0.35      # each render's own end pause, capped so it stays even
_PREBUFFER_S = 1.0      # head start before playback opens, multi-chunk only
_QUEUE_MAX = 512        # bounded so a long passage cannot balloon memory
_STUDY_SPEED = 0.95     # study mode reads slower than the base rate by default

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


def _study_mode() -> bool:
    """Study mode narrates the same selection with structural pauses and a
    slower base rate. Same hotkey, same flow, different delivery."""
    return bool(_get_cfg().get("study_mode", False))


def _speed() -> float:
    """Playback rate from config: 1.0 = the model's natural pace, 0.8 = slower.
    Study mode has its own rate so switching modes does not overwrite the
    speed picked for ordinary read-aloud."""
    key, fallback = (("study_speed", _STUDY_SPEED) if _study_mode()
                     else ("tts_speed", 1.0))
    try:
        speed = float(_get_cfg().get(key, fallback))
    except (TypeError, ValueError):
        return fallback
    return min(_SPEED_MAX, max(_SPEED_MIN, speed))


def _pause_scale() -> float:
    """Multiplies every study-mode pause, for tuning the delivery by ear."""
    try:
        return max(0.0, min(3.0, float(_get_cfg().get("study_pause_scale", 1.0))))
    except (TypeError, ValueError):
        return 1.0


class _TimeStretcher:
    """Pitch-preserving time stretch (WSOLA) over streamed s16le mono PCM.

    The server has no speed knob, and resampling would shift pitch — which
    would wreck a voice clone Balu picked by ear. WSOLA instead overlap-adds
    frames at a shifted analysis hop, hunting ±_STRETCH_SEARCH samples for the
    best-correlated frame so periods stay aligned and there is no burble.
    Streaming-safe: feed() returns whatever is ready, flush() drains the tail.
    """

    def __init__(self, speed: float) -> None:
        self._speed = speed
        self._win = np.hanning(_STRETCH_FRAME + 1)[:-1].astype(np.float32)  # periodic
        self._hop_s = _STRETCH_FRAME // 2
        self._hop_a = max(1, int(round(self._hop_s * speed)))
        self._need = _STRETCH_SEARCH + _STRETCH_FRAME + self._hop_s
        self._buf = np.zeros(0, dtype=np.float32)
        self._pos = 0          # nominal analysis position; only this sets the rate
        self._tail = np.zeros(self._hop_s, dtype=np.float32)
        self._template: np.ndarray | None = None
        self._fed = 0
        self._emitted = 0

    def feed(self, pcm: bytes) -> bytes:
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        self._fed += len(x)
        self._buf = np.concatenate((self._buf, x))
        return self._emit(self._run())

    def flush(self) -> bytes:
        # Zero-pad so the last real samples still get a full analysis window,
        # then trim to the exact stretched length: without this the final
        # ~75 ms of the utterance would be dropped.
        self._buf = np.concatenate((self._buf, np.zeros(self._need, dtype=np.float32)))
        return self._emit(np.concatenate((self._run(), self._tail)), final=True)

    def _best_offset(self) -> int:
        """Shift (within ±_STRETCH_SEARCH) whose frame best continues the last one."""
        lo = max(0, self._pos - _STRETCH_SEARCH)
        hi = min(len(self._buf), self._pos + _STRETCH_SEARCH + _STRETCH_MATCH)
        if hi - lo < _STRETCH_MATCH + 1:
            return 0
        cands = np.lib.stride_tricks.sliding_window_view(self._buf[lo:hi], _STRETCH_MATCH)
        return int(lo + np.argmax(cands @ self._template) - self._pos)

    def _run(self) -> np.ndarray:
        out = []
        while self._pos + self._need <= len(self._buf):
            # The search shift moves which samples are read, never the nominal
            # hop — otherwise the shifts accumulate and the rate drifts.
            q = self._pos if self._template is None else self._pos + self._best_offset()
            frame = self._buf[q:q + _STRETCH_FRAME] * self._win
            out.append(self._tail + frame[:self._hop_s])
            self._tail = frame[self._hop_s:].copy()
            # Where the audio would have gone next if we had not jumped — the
            # target the next frame is matched against.
            self._template = self._buf[q + self._hop_s:q + self._hop_s + _STRETCH_MATCH].copy()
            self._pos += self._hop_a
        drop = max(0, self._pos - _STRETCH_SEARCH)
        if drop:
            self._buf = self._buf[drop:]
            self._pos -= drop
        return np.concatenate(out) if out else np.zeros(0, dtype=np.float32)

    def _emit(self, samples: np.ndarray, final: bool = False) -> bytes:
        if final:
            samples = samples[:max(0, int(round(self._fed / self._speed)) - self._emitted)]
        self._emitted += len(samples)
        return _to_pcm(samples)


def _to_pcm(samples: np.ndarray) -> bytes:
    return np.clip(samples, -32768, 32767).astype(np.int16).tobytes()


class _SilenceGate:
    """Trims the lead-in and tail of one chunk render as it streams.

    Every render carries ~0.4 s of lead-in and up to 0.5 s of tail. Kept as
    they are, chunked playback would stack a ragged pause onto every sentence,
    so they come off here and speak() puts back a fixed gap instead. Pauses
    inside the chunk survive: held silence is released as soon as speech
    resumes, and only dropped at the end of the render.
    """
    _FRAME = 480             # 20 ms at 24 kHz
    _FRAME_B = _FRAME * 2
    _FLOOR = 200.0           # room tone in these renders sits near 100 RMS
    _RELATIVE = 0.08

    def __init__(self) -> None:
        self._buf = b""
        self._held = b""
        self._started = False
        self._peak = 0.0

    def feed(self, pcm: bytes) -> bytes:
        self._buf += pcm
        n = len(self._buf) // self._FRAME_B
        if n == 0:
            return b""
        whole, self._buf = self._buf[:n * self._FRAME_B], self._buf[n * self._FRAME_B:]
        x = np.frombuffer(whole, dtype=np.int16).reshape(n, self._FRAME).astype(np.float32)
        rms = np.sqrt((x ** 2).mean(axis=1))
        self._peak = max(self._peak, float(rms.max()))
        loud = np.flatnonzero(rms > max(self._FLOOR, self._RELATIVE * self._peak))
        if not len(loud):
            if self._started:
                self._held += whole
            return b""
        first, last = int(loud[0]), int(loud[-1])
        if self._started:
            out = self._held + whole[:(last + 1) * self._FRAME_B]
        else:
            self._started = True
            out = whole[first * self._FRAME_B:(last + 1) * self._FRAME_B]
        self._held = whole[(last + 1) * self._FRAME_B:]
        return out

    def end(self, keep_seconds: float = 0.0) -> bytes:
        """Finish the render, returning at most keep_seconds of its tail."""
        tail = self._held[:int(_SAMPLE_RATE * keep_seconds) * 2]
        self._held = b""
        self._buf = b""
        return tail


def _chunk_budget() -> int:
    """Max characters per synthesis request; 0 turns chunking off."""
    try:
        return max(0, int(_get_cfg().get("tts_max_chunk_chars", _CHUNK_CHARS)))
    except (TypeError, ValueError):
        return _CHUNK_CHARS


def _silence(seconds: float) -> bytes:
    return b"\x00" * (int(_SAMPLE_RATE * seconds) * 2)


def _put(q: queue.Queue, item) -> None:
    """Blocking put that still notices stop()."""
    while not _abort.is_set():
        try:
            q.put(item, timeout=0.2)
            return
        except queue.Full:
            continue


def _prebuffer(q: queue.Queue) -> list:
    """Collect a head start before opening the device. Synthesis runs at about
    0.9x real time, so a multi-chunk passage needs a small cushion to absorb
    the per-request gap without the output stream running dry mid-sentence."""
    want = int(_SAMPLE_RATE * _PREBUFFER_S) * 2
    held: list = []
    got = 0
    while got < want and not _abort.is_set():
        try:
            pcm = q.get(timeout=0.2)
        except queue.Empty:
            continue
        held.append(pcm)
        if pcm is None:
            break
        got += len(pcm)
    return held


def _synthesise(segments: list[tuple[str, float, float]], q: queue.Queue,
                err: list) -> None:
    """Producer thread: one request per segment, stretched to that segment's
    own rate, PCM into the queue. The stretch happens here rather than in the
    consumer because rate varies per segment (study mode slows a heading or a
    dense sentence), and only the producer knows which segment it is on."""
    try:
        for i, (chunk, pause_after, speed) in enumerate(segments):
            if _abort.is_set():
                break
            # seed 42 matches the P0 bench renders Balu picked the voice from;
            # fixed seed keeps delivery consistent between plays of the same text.
            resp = _post_json("/v1/audio/speech",
                              {"input": chunk, "voice": _VOICE, "response_format": "pcm",
                               "seed": 42},
                              timeout=180)
            gate = _SilenceGate()
            stretcher = _TimeStretcher(speed) if speed != 1.0 else None
            with resp:
                while not _abort.is_set():
                    piece = resp.read(_CHUNK_BYTES)
                    if not piece:
                        break
                    piece = gate.feed(piece)
                    if stretcher is not None and piece:
                        piece = stretcher.feed(piece)
                    if piece:
                        _put(q, piece)
            last = i == len(segments) - 1
            tail = gate.end(0.0 if last else _TAIL_MAX_S)
            if stretcher is not None:
                # Must run even when the tail is empty: the stretcher holds up
                # to a frame of audio that only flush() releases, and dropping
                # it would clip the end of every segment.
                tail = stretcher.feed(tail) + stretcher.flush()
            if tail:
                _put(q, tail)
            if not last:
                # The render's own end pause doubles as the buffer that keeps
                # playback ahead of the next segment's synthesis, topped up to
                # the pause narration asked for so sentences never run together.
                held = len(tail) / 2 / _SAMPLE_RATE
                gap = max(0.0, pause_after - held)
                if gap:
                    _put(q, _silence(gap))
    except Exception as exc:  # surfaced by speak() on the calling thread
        err.append(exc)
    finally:
        _put(q, None)


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
        study = _study_mode()
        speed = _speed()
        segments = narration.plan(text, budget=_chunk_budget(), base_speed=speed,
                                  study=study, pause_scale=_pause_scale())
        if not segments:
            return
        device = _get_cfg().get("tts_output_device") or None
        q: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX)
        err: list = []
        # Synthesis runs one segment ahead of playback (RTF ~0.9), so only the
        # first costs latency; the rest are ready before they are needed.
        threading.Thread(target=_synthesise, args=(segments, q, err), daemon=True).start()
        pending = _prebuffer(q) if len(segments) > 1 else []
        with sd.RawOutputStream(samplerate=_SAMPLE_RATE, channels=1,
                                dtype="int16", device=device) as stream:
            log("tts", f"speaking {len(text)} chars in {len(segments)} segment(s) "
                       f"at {speed:g}x{' (study mode)' if study else ''}")
            while not _abort.is_set():
                if pending:
                    pcm = pending.pop(0)
                else:
                    try:
                        pcm = q.get(timeout=0.2)
                    except queue.Empty:
                        continue
                if pcm is None:
                    break
                stream.write(pcm)
        if err:
            raise err[0]
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
