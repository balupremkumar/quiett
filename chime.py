"""
Soft synthesized chimes — one consistent family for every audible cue in the
app (BACKLOG item 45): record-start, record-stop, and error. All three share
the same soft-sine-bell synthesis (`_bell`) and only differ by
interval/register, so they read as one voice rather than three unrelated beeps.

Generates the WAV files on first import (cached on disk) and plays them
asynchronously via winsound — never blocks the calling thread. Respects the
`sound_volume` setting (0-100, instant-apply; 0 = mute) read fresh from
config.json on every play, same self-contained-read pattern preview.py uses
for `animations`.
"""
from __future__ import annotations

import json
import os
import struct
import tempfile
import threading
import wave

import numpy as np
import winsound

_SAMPLE_RATE = 44100
_CONFIG_FILE = "config.json"
_CACHE_DIR = os.path.join(tempfile.gettempdir(), "voicedictate_chimes")
os.makedirs(_CACHE_DIR, exist_ok=True)

_START_PATH = os.path.join(_CACHE_DIR, "start.wav")
_STOP_PATH  = os.path.join(_CACHE_DIR, "stop.wav")
_ERROR_PATH = os.path.join(_CACHE_DIR, "error.wav")


def _bell(freqs: list[tuple[float, float]], duration: float = 0.22) -> np.ndarray:
    """Sum of decaying sines at the given (freq_hz, amplitude) pairs."""
    n = int(_SAMPLE_RATE * duration)
    t = np.linspace(0.0, duration, n, endpoint=False)
    # Soft attack (10 ms), exponential decay
    attack = np.clip(t / 0.010, 0.0, 1.0)
    decay = np.exp(-t * 6.0)
    env = attack * decay

    sig = np.zeros(n, dtype=np.float32)
    for f, a in freqs:
        sig += a * np.sin(2.0 * np.pi * f * t)
    sig *= env
    sig /= max(1.0, np.max(np.abs(sig))) * 1.05  # leave headroom
    return sig.astype(np.float32) * 0.55


def _write_wav(path: str, samples: np.ndarray) -> None:
    pcm = np.clip(samples * 32767.0, -32768, 32767).astype(np.int16)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(_SAMPLE_RATE)
        wf.writeframes(pcm.tobytes())


def _ensure_chimes() -> None:
    if not os.path.exists(_START_PATH):
        # Rising bell — E5 + A5 (perfect-fifth-ish stack)
        _write_wav(_START_PATH, _bell([(659.25, 0.55), (880.0, 0.40), (1318.5, 0.20)],
                                      duration=0.20))
    if not os.path.exists(_STOP_PATH):
        # Falling bell — A4 + E4
        _write_wav(_STOP_PATH, _bell([(440.0, 0.55), (329.6, 0.45), (660.0, 0.15)],
                                     duration=0.24))
    if not os.path.exists(_ERROR_PATH):
        # Descending minor third (F4-D4), same soft-bell family but pitched
        # lower and falling — distinct from the stop bell (A4-E4, a fifth,
        # brighter register) so an error never reads as a routine stop.
        _write_wav(_ERROR_PATH, _bell([(349.23, 0.55), (293.66, 0.45)],
                                      duration=0.26))


_ensure_chimes()


def _volume_pct() -> int:
    """0-100 gain from the `sound_volume` setting, snapped to 5% buckets so
    repeated plays reuse a cached scaled file instead of re-encoding every
    time. Missing/invalid config = 100 (today's loudness, unchanged)."""
    try:
        with open(_CONFIG_FILE, encoding="utf-8") as f:
            v = int(json.load(f).get("sound_volume", 100))
    except Exception:
        v = 100
    v = max(0, min(100, v))
    return round(v / 5) * 5


def _scaled_path(base_path: str, pct: int) -> str:
    """Cached, volume-scaled copy of `base_path` at `pct`% gain. Built once
    per (sound, bucket) pair on first use, on the calling (already
    background) thread so playback never blocks."""
    if pct >= 100:
        return base_path
    root, ext = os.path.splitext(base_path)
    scaled_path = f"{root}_{pct}{ext}"
    if not os.path.exists(scaled_path):
        with wave.open(base_path, "rb") as wf:
            raw = wf.readframes(wf.getnframes())
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32767.0
        _write_wav(scaled_path, samples * (pct / 100.0))
    return scaled_path


def _play(path: str) -> None:
    def _run():
        try:
            pct = _volume_pct()
            if pct <= 0:
                return  # muted — sound_volume: 0
            target = _scaled_path(path, pct)
            winsound.PlaySound(target, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()


def play_start() -> None:
    _play(_START_PATH)


def play_stop() -> None:
    _play(_STOP_PATH)


def play_error() -> None:
    _play(_ERROR_PATH)
