#!/usr/bin/env python3
"""
Voice dictation — hold Ctrl+Alt to record, release to transcribe and paste.
"""

import queue
import threading
import time

import keyboard
import numpy as np
import pyperclip
import sounddevice as sd
import tkinter as tk
import win32api
import win32gui
from faster_whisper import WhisperModel

SAMPLE_RATE = 16000
MIN_DURATION = 0.5  # seconds; shorter recordings are discarded

model = None
model_ready = False
recording = False
audio_chunks = []
stream = None
ctrl_held = False
alt_held = False
preview_queue = queue.Queue()
_record_lock = threading.Lock()


def _load_model():
    global model, model_ready
    print("Loading model...")
    model = WhisperModel("small.en", device="cpu", compute_type="int8")
    model_ready = True
    print("Ready.")


def _audio_callback(indata, frames, time_info, status):
    if recording:
        audio_chunks.append(indata.copy())


def _start_recording():
    global recording, audio_chunks, stream
    with _record_lock:
        if recording:
            return
        recording = True
    audio_chunks = []
    stream = sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="float32",
        callback=_audio_callback,
    )
    stream.start()


def _stop_recording():
    global recording, stream
    with _record_lock:
        if not recording:
            return
        recording = False
    # Capture target window immediately — before tkinter can steal focus
    hwnd = win32gui.GetForegroundWindow()
    s = stream
    stream = None
    if s:
        s.stop()   # blocks until pending callbacks finish
        s.close()
    chunks = audio_chunks[:]
    threading.Thread(target=_transcribe, args=(chunks, hwnd), daemon=True).start()


def _transcribe(chunks, hwnd):
    if not chunks:
        return
    audio = np.concatenate(chunks, axis=0).flatten()
    if len(audio) / SAMPLE_RATE < MIN_DURATION:
        return
    try:
        segments, _ = model.transcribe(audio, language="en")
        text = " ".join(seg.text for seg in segments).strip()
    except Exception as exc:
        print(f"Transcription error: {exc}")
        return
    if text:
        preview_queue.put({"text": text, "hwnd": hwnd})


def _on_key(event):
    global ctrl_held, alt_held

    name = event.name or ""
    is_ctrl = name in ("ctrl", "left ctrl", "right ctrl")
    is_alt = name in ("alt", "left alt", "right alt")

    if event.event_type == keyboard.KEY_DOWN:
        if is_ctrl:
            ctrl_held = True
        if is_alt:
            alt_held = True
    else:
        if is_ctrl:
            ctrl_held = False
        if is_alt:
            alt_held = False

    both = ctrl_held and alt_held
    if both and not recording:
        if not model_ready:
            print("Model still loading, please wait")
            return
        _start_recording()
    elif not both and recording:
        _stop_recording()


def _inject_text(text, hwnd):
    original = pyperclip.paste()
    pyperclip.copy(text)
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    time.sleep(0.08)  # allow focus switch before sending keystrokes
    keyboard.send("ctrl+v")

    def _restore():
        time.sleep(0.15)
        pyperclip.copy(original)

    threading.Thread(target=_restore, daemon=True).start()


def _show_preview(text, hwnd):
    root = tk.Tk()
    root.title("Dictation")
    root.attributes("-topmost", True)
    root.resizable(False, False)

    try:
        cx, cy = win32api.GetCursorPos()
    except Exception:
        cx, cy = 200, 200

    w, h = 440, 105
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    x = min(cx + 12, sw - w)
    y = min(cy + 12, sh - h)
    root.geometry(f"{w}x{h}+{x}+{y}")

    frame = tk.Frame(root, padx=8, pady=8)
    frame.pack(fill=tk.BOTH, expand=True)

    var = tk.StringVar(value=text)
    entry = tk.Entry(frame, textvariable=var, font=("Segoe UI", 11))
    entry.pack(fill=tk.X, pady=(0, 6))
    entry.select_range(0, tk.END)
    entry.icursor(tk.END)
    entry.focus_set()

    btns = tk.Frame(frame)
    btns.pack()

    def on_insert():
        result = var.get()
        root.destroy()
        _inject_text(result, hwnd)

    def on_cancel():
        root.destroy()

    tk.Button(btns, text="Insert", command=on_insert, width=10).pack(side=tk.LEFT, padx=4)
    tk.Button(btns, text="Cancel", command=on_cancel, width=10).pack(side=tk.LEFT, padx=4)

    root.bind("<Return>", lambda _: on_insert())
    root.bind("<Escape>", lambda _: on_cancel())
    root.protocol("WM_DELETE_WINDOW", on_cancel)

    root.mainloop()


def main():
    threading.Thread(target=_load_model, daemon=True).start()
    keyboard.hook(_on_key)
    print("Hold Ctrl+Alt to dictate. Ctrl+C to quit.")
    try:
        while True:
            try:
                item = preview_queue.get_nowait()
                _show_preview(item["text"], item["hwnd"])
            except queue.Empty:
                time.sleep(0.05)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
