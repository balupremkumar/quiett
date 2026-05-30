import threading
import time
import winsound

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000

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
              input_device=None) -> None:
    global _on_stop, _max_duration, _silence_timeout, _silence_threshold, _input_device
    _on_stop           = on_stop
    _max_duration      = max_duration_seconds
    _silence_timeout   = silence_timeout_seconds
    _silence_threshold = silence_threshold
    _input_device      = input_device


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

    threading.Thread(target=winsound.Beep, args=(1000, 150), daemon=True).start()


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

    threading.Thread(target=winsound.Beep, args=(500, 150), daemon=True).start()
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


def _callback(indata, frames, time_info, status) -> None:
    global _had_voice, _last_voice_t, _silence_triggered
    if not _recording_event.is_set() or _session_chunks is None:
        return
    _session_chunks.append(indata.copy())

    # Silence auto-stop: only activates once voice has been detected at least once
    if _silence_timeout > 0:
        rms = float(np.sqrt(np.mean(indata ** 2)))
        if rms > _silence_threshold:
            _had_voice         = True
            _last_voice_t      = time.time()
            _silence_triggered = False
        elif _had_voice and not _silence_triggered and (time.time() - _last_voice_t) > _silence_timeout:
            _silence_triggered = True
            threading.Thread(target=stop, daemon=True).start()
