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
import reformat
import tray
import transcribe
from logger import log, error as log_error

_cfg: dict = {}
_cfg_lock = threading.Lock()

_HOT_RELOAD_INTERVAL = 30  # seconds


_CONFIG_DEFAULTS = {
    "hotkey":                      "ctrl+alt",
    "model":                       "small",
    "language":                    "en",
    "min_record_seconds":          0.5,
    "max_record_seconds":          120.0,
    "filler_words":                [],
    "clipboard_restore_delay_ms":  150,
    "vad_filter":                  False,
    "corrections":                 {},
    "silence_auto_stop_seconds":   3.0,
    "preview_position":            "cursor",
    "preview_auto_dismiss_seconds": 0.0,
    "auto_paste_threshold":        0.0,
    "initial_prompt":              "",
    "custom_vocabulary":           [],
    "input_device":                None,
    "history_paused":              False,
    "silence_threshold":           0.01,
    "per_app_paste":               {},
    "vibe_mode":                   False,
    "vibe_mode_backend":           "api",
}

_VALID_POSITIONS = {"cursor", "top-right", "bottom-right", "top-left", "bottom-left", "center"}


def _load_config() -> dict:
    with open("config.json") as f:
        return json.load(f)


def _validate_config(raw: dict) -> dict:
    cfg = {**_CONFIG_DEFAULTS, **raw}
    # Type clamps
    try:
        cfg["min_record_seconds"] = max(0.1, float(cfg["min_record_seconds"]))
    except (TypeError, ValueError):
        cfg["min_record_seconds"] = 0.5
    try:
        cfg["max_record_seconds"] = max(5.0, min(300.0, float(cfg["max_record_seconds"])))
    except (TypeError, ValueError):
        cfg["max_record_seconds"] = 120.0
    try:
        cfg["clipboard_restore_delay_ms"] = max(50, int(cfg["clipboard_restore_delay_ms"]))
    except (TypeError, ValueError):
        cfg["clipboard_restore_delay_ms"] = 150
    try:
        cfg["silence_auto_stop_seconds"] = max(0.0, float(cfg["silence_auto_stop_seconds"]))
    except (TypeError, ValueError):
        cfg["silence_auto_stop_seconds"] = 3.0
    try:
        cfg["preview_auto_dismiss_seconds"] = max(0.0, float(cfg["preview_auto_dismiss_seconds"]))
    except (TypeError, ValueError):
        cfg["preview_auto_dismiss_seconds"] = 0.0
    try:
        cfg["auto_paste_threshold"] = max(0.0, min(1.0, float(cfg["auto_paste_threshold"])))
    except (TypeError, ValueError):
        cfg["auto_paste_threshold"] = 0.0
    if not isinstance(cfg["filler_words"], list):
        cfg["filler_words"] = []
    if not isinstance(cfg["corrections"], dict):
        cfg["corrections"] = {}
    if not isinstance(cfg.get("custom_vocabulary"), list):
        cfg["custom_vocabulary"] = []
    if not isinstance(cfg.get("initial_prompt"), str):
        cfg["initial_prompt"] = ""
    cfg["history_paused"] = bool(cfg.get("history_paused", False))
    try:
        cfg["silence_threshold"] = max(0.001, min(0.5, float(cfg.get("silence_threshold", 0.01))))
    except (TypeError, ValueError):
        cfg["silence_threshold"] = 0.01
    if cfg["preview_position"] not in _VALID_POSITIONS:
        cfg["preview_position"] = "cursor"
    return cfg


def _get_cfg() -> dict:
    with _cfg_lock:
        return dict(_cfg)


def main() -> None:
    global _cfg
    _cfg = _validate_config(_load_config())
    _cfg["_active_hotkey"] = _cfg.get("hotkey", "ctrl+alt")

    # DPI awareness: per-monitor V2 for correct sizing on multi-DPI setups
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    profile.init()
    inject.configure(
        restore_delay_ms=_cfg["clipboard_restore_delay_ms"],
        per_app_paste=_cfg.get("per_app_paste", {}),
    )
    preview.configure_position(_cfg["preview_position"])
    preview.start()

    # ------------------------------------------------------------------
    # Callbacks wired between audio → transcription → preview → inject
    # ------------------------------------------------------------------

    def _run_transcription(chunks: list, hwnd: int) -> None:
        cfg = _get_cfg()
        text, confidence, words = None, None, None
        try:
            text, confidence, words = transcribe.run(
                chunks,
                language=cfg["language"],
                min_seconds=cfg["min_record_seconds"],
                filler_words=cfg["filler_words"],
                vad_filter=cfg.get("vad_filter", False),
                profile_rules=profile.get_active_rules(),
                corrections=cfg.get("corrections", {}),
                initial_prompt=cfg.get("initial_prompt") or None,
                custom_vocabulary=cfg.get("custom_vocabulary") or None,
            )
        except Exception as exc:
            msg = f"Transcription error: {exc}"
            log_error("main", msg)
            print(msg)
            tray.notify("VoiceDictate", msg)
        finally:
            tray.set_state("idle")
            preview.hide_badge()
        if text is None:
            return
        if text.strip() and not cfg.get("history_paused", False):
            history.save(text.strip())
        threshold = cfg.get("auto_paste_threshold", 0.0)
        if (threshold > 0.0 and confidence is not None
                and confidence >= threshold and text.strip()):
            inject.inject_text(text.strip(), hwnd)
            return

        # Vibe mode: reformat raw Whisper text into a structured coding prompt.
        # raw_text preserved so the preview Raw toggle can show the original.
        raw_text = text
        if cfg.get("vibe_mode") and text.strip() and reformat.is_ready():
            preview.show_badge("reformatting")
            try:
                text = reformat.run(text)
            except Exception as exc:
                log_error("main", f"reformat error: {exc}")
            finally:
                preview.hide_badge()

        preview.show(
            text, hwnd,
            empty=not text.strip(),
            confidence=confidence,
            words=words,
            auto_dismiss=cfg.get("preview_auto_dismiss_seconds", 0.0),
            raw=raw_text if cfg.get("vibe_mode") and raw_text != text else None,
        )

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
        preview.close_current_preview()
        tray.set_state("recording")
        preview.show_badge("recording")
        audio.start()

    # ------------------------------------------------------------------
    # Module configuration
    # ------------------------------------------------------------------

    audio.configure(
        on_stop=_on_audio_stop,
        max_duration_seconds=_cfg.get("max_record_seconds", 120),
        silence_timeout_seconds=_cfg.get("silence_auto_stop_seconds", 3.0),
        silence_threshold=_cfg.get("silence_threshold", 0.01),
        input_device=_cfg.get("input_device"),
    )

    def _on_too_short() -> None:
        tray.set_state("idle")
        preview.show_badge("too_short")
        threading.Timer(1.5, preview.hide_badge).start()

    def _on_not_ready() -> None:
        preview.show_badge("not_ready")
        threading.Timer(2.0, preview.hide_badge).start()

    def _safe_start_recording() -> None:
        if transcribe.is_ready() and not audio.is_recording():
            _on_recording_start()

    preview.set_rerecord_callback(_safe_start_recording)

    hotkey.configure(
        on_start=_on_recording_start,
        on_stop=audio.stop,
        on_cancel=audio.cancel,
        on_too_short=_on_too_short,
        on_not_ready=_on_not_ready,
        is_recording=audio.is_recording,
        is_ready=transcribe.is_ready,
        keys=_cfg.get("hotkey", "ctrl+alt"),
    )
    hotkey.start()

    # ------------------------------------------------------------------
    # Model loading
    # ------------------------------------------------------------------

    def _load_model() -> None:
        log("main", "loading model")
        print("Loading model...")
        backoff = 2
        for attempt in range(3):
            try:
                transcribe.load(_cfg["model"])
                tray.set_state("idle")
                print(f"Ready (device={transcribe.device_used()}).")
                log("main", f"model ready device={transcribe.device_used()}")
                return
            except Exception as exc:
                msg = f"Model load attempt {attempt + 1} failed: {exc}"
                print(msg)
                log_error("main", msg)
                if attempt < 2:
                    import time as _t
                    _t.sleep(backoff)
                    backoff *= 2
        tray.set_state("idle")
        tray.notify("VoiceDictate", "Model load failed after 3 attempts. Check app.log.")

    threading.Thread(target=_load_model, daemon=True).start()

    def _load_reformat() -> None:
        reformat.load(backend=_cfg.get("vibe_mode_backend", "api"))

    threading.Thread(target=_load_reformat, daemon=True).start()

    # ------------------------------------------------------------------
    # Config hot-reload
    # ------------------------------------------------------------------

    def _reload_config() -> None:
        global _cfg
        try:
            fresh = _load_config()
            with _cfg_lock:
                validated = _validate_config(fresh)
            with _cfg_lock:
                for key in ("language", "filler_words", "min_record_seconds",
                            "clipboard_restore_delay_ms", "max_record_seconds",
                            "vad_filter", "corrections", "silence_auto_stop_seconds",
                            "preview_position", "preview_auto_dismiss_seconds",
                            "auto_paste_threshold", "initial_prompt",
                            "custom_vocabulary", "input_device"):
                    _cfg[key] = validated[key]
            inject.configure(
                restore_delay_ms=validated["clipboard_restore_delay_ms"],
                per_app_paste=validated.get("per_app_paste", {}),
            )
            preview.configure_position(validated["preview_position"])
            audio.configure(
                on_stop=_on_audio_stop,
                max_duration_seconds=validated["max_record_seconds"],
                silence_timeout_seconds=validated["silence_auto_stop_seconds"],
                silence_threshold=validated.get("silence_threshold", 0.01),
                input_device=validated.get("input_device"),
            )
            # Hot-reload hotkey if changed
            new_keys = fresh.get("hotkey", "ctrl+alt")
            if new_keys != _cfg.get("_active_hotkey"):
                hotkey.rebind(new_keys)
                with _cfg_lock:
                    _cfg["_active_hotkey"] = new_keys
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
        on_open_settings=preview.show_settings,
    )
    print("Hold Ctrl+Alt to dictate. Right-click tray icon to quit.")
    tray.run()


if __name__ == "__main__":
    main()
