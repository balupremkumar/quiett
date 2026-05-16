import threading
import winsound

import sounddevice as sd

SAMPLE_RATE = 16000

_recording = False
_session_chunks = None  # new list per session — avoids start/stop race
_stream = None
_on_stop = None
_timer = None
_max_duration = 60.0
_lock = threading.Lock()


def configure(on_stop, max_duration_seconds: float = 60.0) -> None:
    global _on_stop, _max_duration
    _on_stop = on_stop
    _max_duration = max_duration_seconds


def is_recording() -> bool:
    return _recording


def start() -> None:
    global _recording, _session_chunks, _stream, _timer
    with _lock:
        if _recording:
            return
        _recording = True
        _session_chunks = []
    _stream = sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="float32",
        callback=_callback,
    )
    _stream.start()
    _timer = threading.Timer(_max_duration, stop)
    _timer.daemon = True
    _timer.start()
    threading.Thread(target=winsound.Beep, args=(1000, 150), daemon=True).start()


def stop() -> None:
    global _recording, _stream, _timer
    with _lock:
        if not _recording:
            return
        _recording = False
        captured = _session_chunks  # snapshot reference inside lock
        t = _timer
        _timer = None
    if t:
        t.cancel()
    s = _stream
    _stream = None
    if s:
        s.stop()  # blocks until in-flight callbacks finish
        s.close()
    threading.Thread(target=winsound.Beep, args=(500, 150), daemon=True).start()
    if _on_stop and captured is not None:
        _on_stop(captured)


def _callback(indata, frames, time_info, status) -> None:
    if _recording and _session_chunks is not None:
        _session_chunks.append(indata.copy())
