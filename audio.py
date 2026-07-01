import threading
import time
from collections import deque

import numpy as np
import sounddevice as sd

import chime

try:
    import webrtcvad
    _vad_available = True
except Exception:
    webrtcvad = None
    _vad_available = False

SAMPLE_RATE = 16000

# webrtcvad frame size: 30ms @ 16kHz = 480 samples
_VAD_FRAME_SAMPLES = 480
_vad: "webrtcvad.Vad | None" = None
_vad_mode = False   # when True, VAD replaces RMS for silence detection

# Ring buffer of recent per-callback RMS values (newest at right).
# Sized for ~3s of history at sounddevice's default ~50ms blocks.
_LEVEL_HISTORY = 64
_levels: deque = deque([0.0] * _LEVEL_HISTORY, maxlen=_LEVEL_HISTORY)

_recording_event = threading.Event()   # replaces bare bool — is_set() is thread-safe
_session_chunks = None
_stream = None
_on_stop = None
_timer = None
_lock = threading.Lock()

_max_duration       = 120.0
_silence_timeout    = 3.0
_silence_threshold  = 0.01
_input_device       = None  # None = system default; int or str (device name/index)

# Silence tracking — written only from the sounddevice callback thread
_had_voice       = False
_last_voice_t    = 0.0
_silence_triggered = False  # prevents spawning multiple stop threads

_start_time: float = 0.0


def configure(on_stop, max_duration_seconds: float = 120.0,
              silence_timeout_seconds: float = 3.0,
              silence_threshold: float = 0.01,
              input_device=None,
              vad_silence_mode: bool = False,
              vad_aggressiveness: int = 2) -> None:
    global _on_stop, _max_duration, _silence_timeout, _silence_threshold, _input_device
    global _vad, _vad_mode
    _on_stop           = on_stop
    _max_duration      = max_duration_seconds
    _silence_timeout   = silence_timeout_seconds
    _silence_threshold = silence_threshold
    _input_device      = input_device

    _vad_mode = bool(vad_silence_mode and _vad_available)
    if _vad_mode:
        _vad = webrtcvad.Vad(max(0, min(3, vad_aggressiveness)))
    else:
        _vad = None


def list_input_devices() -> list[dict]:
    """Return [{index, name}] for input-capable devices."""
    out = []
    try:
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_input_channels", 0) > 0:
                out.append({"index": i, "name": d.get("name", f"Device {i}")})
    except Exception:
        pass
    return out


def is_recording() -> bool:
    return _recording_event.is_set()


def start() -> None:
    global _session_chunks, _stream, _timer, _had_voice, _last_voice_t, _silence_triggered, _start_time
    with _lock:
        if _recording_event.is_set():
            return
        _session_chunks    = []
        _had_voice         = False
        _start_time        = time.time()
        _last_voice_t      = time.time()
        _silence_triggered = False
        _levels.clear()
        _levels.extend([0.0] * _LEVEL_HISTORY)
        _recording_event.set()

    _stream = sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="float32",
        device=_input_device,
        callback=_callback,
    )
    _stream.start()

    _timer = threading.Timer(_max_duration, stop)
    _timer.daemon = True
    _timer.start()

    chime.play_start()


def stop() -> None:
    global _stream, _timer
    with _lock:
        if not _recording_event.is_set():
            return
        _recording_event.clear()
        captured = _session_chunks
        t = _timer
        _timer = None

    if t:
        t.cancel()
    s = _stream
    _stream = None
    if s:
        s.stop()
        s.close()

    chime.play_stop()
    if _on_stop and captured is not None:
        _on_stop(captured)


def cancel() -> None:
    """Stop recording silently — no transcription triggered (used for too-short presses)."""
    global _stream, _timer
    with _lock:
        if not _recording_event.is_set():
            return
        _recording_event.clear()
        t = _timer
        _timer = None
    if t:
        t.cancel()
    s = _stream
    _stream = None
    if s:
        try:
            s.stop()
            s.close()
        except Exception:
            pass


def get_elapsed() -> float:
    """Seconds since recording started. 0 if not recording."""
    if not _recording_event.is_set():
        return 0.0
    return time.time() - _start_time


def get_silence_elapsed() -> float:
    """Seconds of continuous silence since last voice detected. 0 if none."""
    if not _recording_event.is_set() or not _had_voice:
        return 0.0
    return max(0.0, time.time() - _last_voice_t)


def get_silence_timeout() -> float:
    return _silence_timeout


def get_chunks_snapshot() -> list:
    """Copy of the chunks recorded so far this session (for live partial preview)."""
    chunks = _session_chunks
    return list(chunks) if chunks else []


def get_recent_levels(n: int = 32) -> list[float]:
    """Return the n most recent RMS values (oldest first). Used for the recording waveform."""
    if n >= len(_levels):
        return list(_levels)
    return list(_levels)[-n:]


def _is_voice_vad(indata) -> bool:
    """Slice the incoming block into 30ms frames and return True if any contains speech."""
    if _vad is None:
        return False
    try:
        pcm = (np.clip(indata[:, 0], -1.0, 1.0) * 32767).astype(np.int16).tobytes()
    except Exception:
        return False
    step = _VAD_FRAME_SAMPLES * 2  # 2 bytes per int16 sample
    for i in range(0, len(pcm) - step + 1, step):
        try:
            if _vad.is_speech(pcm[i:i + step], SAMPLE_RATE):
                return True
        except Exception:
            return False
    return False


def _callback(indata, frames, time_info, status) -> None:
    global _had_voice, _last_voice_t, _silence_triggered
    if not _recording_event.is_set() or _session_chunks is None:
        return
    _session_chunks.append(indata.copy())

    rms = float(np.sqrt(np.mean(indata ** 2)))
    _levels.append(rms)

    # Silence auto-stop: only activates once voice has been detected at least once
    if _silence_timeout > 0:
        if _vad_mode:
            is_voice = _is_voice_vad(indata)
        else:
            is_voice = rms > _silence_threshold

        if is_voice:
            _had_voice         = True
            _last_voice_t      = time.time()
            _silence_triggered = False
        elif _had_voice and not _silence_triggered and (time.time() - _last_voice_t) > _silence_timeout:
            _silence_triggered = True
            threading.Thread(target=stop, daemon=True).start()
