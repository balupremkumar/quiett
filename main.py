"""
Entry point. Wires all modules together, then hands the main thread to pystray.

Thread map
----------
main thread   → tray.run() (pystray requirement)
worker thread → preview._tk_main() (persistent hidden Tk root)
worker thread → _load_model() (one-shot, exits after model is ready)
worker thread → _run_transcription() (spawned per recording)
worker thread → _reload_config() (periodic, every 30 s)
hook thread   → hotkey / keyboard library (managed by keyboard library)
audio thread  → audio._callback() (managed by sounddevice)

Hot-reloadable config fields (take effect on next recording):
  language, filler_words, min_record_seconds, clipboard_restore_delay_ms,
  max_record_seconds, vad_filter
Not hot-reloadable (require restart): model, hotkey
"""

import json
import threading

import audio
import history
import hotkey
import inject
import preview
import tray
import transcribe

_cfg: dict = {}
_cfg_lock = threading.Lock()

_HOT_RELOAD_INTERVAL = 30  # seconds


def _load_config() -> dict:
    with open("config.json") as f:
        return json.load(f)


def _get_cfg() -> dict:
    with _cfg_lock:
        return dict(_cfg)


def main() -> None:
    global _cfg
    _cfg = _load_config()

    inject.configure(restore_delay_ms=_cfg["clipboard_restore_delay_ms"])
    preview.start()  # spin up tkinter worker thread

    # ------------------------------------------------------------------
    # Callbacks wired between audio → transcription → preview → inject
    # ------------------------------------------------------------------

    def _run_transcription(chunks: list, hwnd: int) -> None:
        cfg = _get_cfg()
        try:
            text = transcribe.run(
                chunks,
                language=cfg["language"],
                min_seconds=cfg["min_record_seconds"],
                filler_words=cfg["filler_words"],
                vad_filter=cfg.get("vad_filter", False),
            )
        except Exception as exc:
            msg = f"Transcription error: {exc}"
            print(msg)
            tray.notify("VoiceDictate", msg)
            tray.set_state("idle")
            return
        tray.set_state("idle")
        if text is None:
            return  # recording was too short — discard silently
        if text.strip():
            history.save(text.strip())
        preview.show(text, hwnd, empty=not text.strip())

    def _on_audio_stop(chunks: list) -> None:
        # Called synchronously from the hotkey thread the instant the user
        # releases the hotkey — capture the target window before anything moves.
        hwnd = inject.capture_foreground()
        tray.set_state("processing")
        threading.Thread(
            target=_run_transcription,
            args=(chunks, hwnd),
            daemon=True,
        ).start()

    def _on_recording_start() -> None:
        tray.set_state("recording")
        audio.start()

    # ------------------------------------------------------------------
    # Module configuration
    # ------------------------------------------------------------------

    audio.configure(
        on_stop=_on_audio_stop,
        max_duration_seconds=_cfg.get("max_record_seconds", 60),
    )

    hotkey.configure(
        on_start=_on_recording_start,
        on_stop=audio.stop,
        is_recording=audio.is_recording,
        is_ready=transcribe.is_ready,
        keys=_cfg.get("hotkey", "ctrl+alt"),
    )
    hotkey.start()

    # ------------------------------------------------------------------
    # Model loading — background thread, flips tray to idle when done
    # ------------------------------------------------------------------

    def _load_model() -> None:
        print("Loading model...")
        try:
            transcribe.load(_cfg["model"])
            tray.set_state("idle")
            print("Ready.")
        except Exception as exc:
            msg = f"Model load failed: {exc}"
            print(msg)
            tray.set_state("idle")
            tray.notify("VoiceDictate", msg)

    threading.Thread(target=_load_model, daemon=True).start()

    # ------------------------------------------------------------------
    # Config hot-reload — polls every 30 s; reloads safe fields only
    # ------------------------------------------------------------------

    def _reload_config() -> None:
        global _cfg
        try:
            fresh = _load_config()
            with _cfg_lock:
                for key in ("language", "filler_words", "min_record_seconds",
                            "clipboard_restore_delay_ms", "max_record_seconds",
                            "vad_filter"):
                    if key in fresh:
                        _cfg[key] = fresh[key]
            inject.configure(
                restore_delay_ms=_cfg.get("clipboard_restore_delay_ms", 150)
            )
        except Exception:
            pass  # keep running with last good config
        threading.Timer(_HOT_RELOAD_INTERVAL, _reload_config).start()

    threading.Timer(_HOT_RELOAD_INTERVAL, _reload_config).start()

    # ------------------------------------------------------------------
    # Tray
    # ------------------------------------------------------------------

    tray.configure(
        on_view_history=preview.show_history,
        on_toggle_pause=hotkey.set_paused,
    )
    print("Hold Ctrl+Alt to dictate. Right-click tray icon to quit.")
    tray.run()  # blocks until user clicks Quit


if __name__ == "__main__":
    main()
