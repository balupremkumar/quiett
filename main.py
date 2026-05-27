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
import profile
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

    profile.init()
    inject.configure(restore_delay_ms=_cfg["clipboard_restore_delay_ms"])
    preview.start()

    # ------------------------------------------------------------------
    # Callbacks wired between audio → transcription → preview → inject
    # ------------------------------------------------------------------

    def _run_transcription(chunks: list, hwnd: int) -> None:
        cfg = _get_cfg()
        try:
            text, confidence = transcribe.run(
                chunks,
                language=cfg["language"],
                min_seconds=cfg["min_record_seconds"],
                filler_words=cfg["filler_words"],
                vad_filter=cfg.get("vad_filter", False),
                profile_rules=profile.get_active_rules(),
            )
        except Exception as exc:
            msg = f"Transcription error: {exc}"
            print(msg)
            tray.notify("VoiceDictate", msg)
            tray.set_state("idle")
            preview.hide_badge()
            return
        tray.set_state("idle")
        preview.hide_badge()
        if text is None:
            return
        if text.strip():
            history.save(text.strip())
        preview.show(text, hwnd, empty=not text.strip(), confidence=confidence)

    def _on_audio_stop(chunks: list) -> None:
        hwnd = inject.capture_foreground()
        tray.set_state("processing")
        preview.show_badge("processing")
        threading.Thread(
            target=_run_transcription,
            args=(chunks, hwnd),
            daemon=True,
        ).start()

    def _on_recording_start() -> None:
        tray.set_state("recording")
        preview.show_badge("recording")
        audio.start()

    # ------------------------------------------------------------------
    # Module configuration
    # ------------------------------------------------------------------

    audio.configure(
        on_stop=_on_audio_stop,
        max_duration_seconds=_cfg.get("max_record_seconds", 120),
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
    # Model loading
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
    # Config hot-reload
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
                restore_delay_ms=fresh.get("clipboard_restore_delay_ms", 150)
            )
        except Exception:
            pass
        t = threading.Timer(_HOT_RELOAD_INTERVAL, _reload_config)
        t.daemon = True
        t.start()

    t0 = threading.Timer(_HOT_RELOAD_INTERVAL, _reload_config)
    t0.daemon = True
    t0.start()

    # ------------------------------------------------------------------
    # Tray
    # ------------------------------------------------------------------

    tray.configure(
        on_view_history=preview.show_history,
        on_toggle_pause=hotkey.set_paused,
        on_view_profile=preview.show_profile,
    )
    print("Hold Ctrl+Alt to dictate. Right-click tray icon to quit.")
    tray.run()


if __name__ == "__main__":
    main()
