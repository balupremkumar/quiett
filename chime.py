"""
Soft synthesized chimes for recording start/stop.

Generates two short WAV files on first import (cached on disk) and plays them
asynchronously via winsound. Replaces the harsh winsound.Beep tones.
"""
from __future__ import annotations

import os
import struct
import tempfile
import threading
import wave

import numpy as np
import winsound

_SAMPLE_RATE = 44100
_CACHE_DIR = os.path.join(tempfile.gettempdir(), "voicedictate_chimes")
os.makedirs(_CACHE_DIR, exist_ok=True)

_START_PATH = os.path.join(_CACHE_DIR, "start.wav")
_STOP_PATH  = os.path.join(_CACHE_DIR, "stop.wav")
_TASK_PATH  = os.path.join(_CACHE_DIR, "task.wav")


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
    if not os.path.exists(_TASK_PATH):
        # Bright ascending triad (C5-E5-G5) — distinct from the record/stop
        # bells so "added to to-do list" reads as its own, separate event.
        _write_wav(_TASK_PATH, _bell([(523.25, 0.5), (659.25, 0.45), (783.99, 0.35)],
                                     duration=0.26))


_ensure_chimes()


def _play(path: str) -> None:
    def _run():
        try:
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()


def play_start() -> None:
    _play(_START_PATH)


def play_stop() -> None:
    _play(_STOP_PATH)


def play_task_added() -> None:
    _play(_TASK_PATH)


def speak(text: str) -> None:
    """Optional SAPI text-to-speech confirmation (off by default — see
    taskflow_voice_confirm in config.json). Runs on its own thread since
    ISpVoice.Speak() blocks synchronously by default."""
    def _run():
        try:
            import win32com.client
            voice = win32com.client.Dispatch("SAPI.SpVoice")
            voice.Speak(text)
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()
