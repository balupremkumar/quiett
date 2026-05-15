"""
Entry point. Wires all modules together, then hands the main thread to pystray.

Thread map
----------
main thread   → tray.run() (pystray requirement)
worker thread → preview._tk_main() (persistent hidden Tk root)
worker thread → _load_model() (one-shot, exits after model is ready)
worker thread → _run_transcription() (spawned per recording)
hook thread   → hotkey / keyboard library (managed by keyboard library)
audio thread  → audio._callback() (managed by sounddevice)
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


def _load_config() -> dict:
    with open("config.json") as f:
        return json.load(f)


def main() -> None:
    cfg = _load_config()

    inject.configure(restore_delay_ms=cfg["clipboard_restore_delay_ms"])
    preview.start()  # spin up tkinter worker thread

    # ------------------------------------------------------------------
    # Callbacks wired between audio → transcription → preview → inject
    # ------------------------------------------------------------------

    def _run_transcription(chunks: list, hwnd: int) -> None:
        try:
            text = transcribe.run(
                chunks,
                language=cfg["language"],
                min_seconds=cfg["min_record_seconds"],
                filler_words=cfg["filler_words"],
            )
        except Exception as exc:
            print(f"Transcription error: {exc}")
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

    audio.configure(on_stop=_on_audio_stop)

    hotkey.configure(
        on_start=_on_recording_start,
        on_stop=audio.stop,
        is_recording=audio.is_recording,
        is_ready=transcribe.is_ready,
    )
    hotkey.start()

    # ------------------------------------------------------------------
    # Model loading — background thread, flips tray to idle when done
    # ------------------------------------------------------------------

    def _load_model() -> None:
        print("Loading model...")
        transcribe.load(cfg["model"])
        tray.set_state("idle")
        print("Ready.")

    threading.Thread(target=_load_model, daemon=True).start()

    tray.configure(on_view_history=preview.show_history)
    print("Hold Ctrl+Alt to dictate. Right-click tray icon to quit.")
    tray.run()  # blocks until user clicks Quit


if __name__ == "__main__":
    main()
