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

import atexit
import ctypes
import json
import os
import sys
import threading
import time
from datetime import datetime

import win32api
import win32event
import winerror

import api_server
import audio
import dashboard
import history
import hotkey
import inject
import preview
import profile
import tray
import transcribe
import tts
import voiceprofile
from logger import log, error as log_error


def _enable_dpi_awareness() -> None:
    """Per-monitor-v2 DPI awareness. Must run before any window (Tk, pystray)
    exists; without it Windows bitmap-stretches every surface on scaled
    displays, which blurs text and compounds edge aliasing."""
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_ssize_t(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor v1
        except Exception:
            pass


_enable_dpi_awareness()

_cfg: dict = {}
_cfg_lock = threading.Lock()

_UNDO_PHRASES = {"scratch that", "undo that", "undo last insert"}

# Foreground window captured at hotkey-down, used as the paste target when the
# one at hotkey-up is unusable (our own badge/panel had focus).
_recording_target: dict = {"hwnd": 0}

_HOT_RELOAD_INTERVAL = 30  # seconds


_CONFIG_DEFAULTS = {
    "hotkey":                      "ctrl+alt",
    "model":                       "large-v3-turbo",
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
    "per_app_context":             {},
    "electron_paste_method":       "ctrl_v",
    "paste_mode":                  "auto",
    "hotkey_mode":                 "hold",
    "api_server_enabled":          True,
    "api_server_port":             8090,
    "live_preview_enabled":        True,
    "retain_audio":                True,
    "retain_audio_max_files":      200,
    "retain_audio_min_seconds":    3.0,
    "voice_profile_max_samples":   10,
    "badge_animation":             "waveform",
    "incognito":                   False,
    "redact_patterns":             [],
    "tts_enabled":                 False,   # cloned-voice playback — OFF by default, loads nothing until used
    "tts_hotkey":                  "ctrl+shift+s",  # NOT ctrl+alt+<x>: ctrl+alt is the record hold
    "tts_reference":               "",      # voice_profile filename pinned by ear; "" = manifest best
    "tts_port":                    8092,
    "tts_speed":                   1.0,     # playback rate; <1 slows the voice down, pitch unchanged
    "tts_max_chunk_chars":         120,     # synthesis chunk size; larger = the talker rushes, 0 = off
    "tts_unload_idle_seconds":     300,     # kill tts-server after this idle; 0 = never
    "study_mode":                  False,   # read-aloud narrates with structural pauses instead of flat
    "study_speed":                 0.95,    # ...and at its own rate, so normal read-aloud keeps tts_speed
    "study_pause_scale":           1.0,     # multiplies every study pause, for tuning delivery by ear
}

_RECORDINGS_DIR = "recordings"


def _save_recording(chunks: list, cfg: dict) -> str | None:
    """Keep the raw audio of this dictation as a WAV in recordings/.

    Builds the voice-sample dataset for future voice cloning and enables
    re-transcribing past dictations. Recordings shorter than
    retain_audio_min_seconds are skipped (too short to be useful samples);
    the directory is pruned to the newest retain_audio_max_files.
    Returns the saved filename, or None.
    """
    if not cfg.get("retain_audio", True):
        return None
    try:
        duration = sum(len(c) for c in chunks) / audio.SAMPLE_RATE
        if duration < float(cfg.get("retain_audio_min_seconds", 3.0)):
            return None
        os.makedirs(_RECORDINGS_DIR, exist_ok=True)
        fname = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3] + ".wav"
        with open(os.path.join(_RECORDINGS_DIR, fname), "wb") as f:
            f.write(transcribe._chunks_to_wav_bytes(chunks))
        cap = max(0, int(cfg.get("retain_audio_max_files", 200)))
        if cap:
            files = sorted(f for f in os.listdir(_RECORDINGS_DIR)
                           if f.endswith(".wav"))
            for old in files[:-cap]:
                try:
                    os.remove(os.path.join(_RECORDINGS_DIR, old))
                except OSError:
                    pass
        return fname
    except Exception as exc:
        log_error("main", f"retain audio failed: {exc}")
        return None

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
    if cfg.get("badge_animation") not in ("waveform", "pulse", "bars"):
        cfg["badge_animation"] = "waveform"
    if not isinstance(cfg.get("per_app_context"), dict):
        cfg["per_app_context"] = {}
    if cfg.get("hotkey_mode") not in ("hold", "toggle", "auto"):
        cfg["hotkey_mode"] = "hold"
    cfg["api_server_enabled"] = bool(cfg.get("api_server_enabled", True))
    try:
        cfg["api_server_port"] = max(1024, min(65535, int(cfg.get("api_server_port", 8090))))
    except (TypeError, ValueError):
        cfg["api_server_port"] = 8090
    cfg["live_preview_enabled"] = bool(cfg.get("live_preview_enabled", True))
    cfg["retain_audio"] = bool(cfg.get("retain_audio", True))
    try:
        cfg["retain_audio_max_files"] = max(0, int(cfg.get("retain_audio_max_files", 200)))
    except (TypeError, ValueError):
        cfg["retain_audio_max_files"] = 200
    try:
        cfg["retain_audio_min_seconds"] = max(0.0, float(cfg.get("retain_audio_min_seconds", 3.0)))
    except (TypeError, ValueError):
        cfg["retain_audio_min_seconds"] = 3.0
    try:
        cfg["voice_profile_max_samples"] = max(1, int(cfg.get("voice_profile_max_samples", 10)))
    except (TypeError, ValueError):
        cfg["voice_profile_max_samples"] = 10
    try:
        cfg["tts_speed"] = max(0.5, min(2.0, float(cfg.get("tts_speed", 1.0))))
    except (TypeError, ValueError):
        cfg["tts_speed"] = 1.0
    try:
        cfg["tts_max_chunk_chars"] = max(0, int(cfg.get("tts_max_chunk_chars", 120)))
    except (TypeError, ValueError):
        cfg["tts_max_chunk_chars"] = 120
    cfg["study_mode"] = bool(cfg.get("study_mode", False))
    try:
        cfg["study_speed"] = max(0.5, min(2.0, float(cfg.get("study_speed", 0.95))))
    except (TypeError, ValueError):
        cfg["study_speed"] = 0.95
    try:
        cfg["study_pause_scale"] = max(0.0, min(3.0, float(cfg.get("study_pause_scale", 1.0))))
    except (TypeError, ValueError):
        cfg["study_pause_scale"] = 1.0
    cfg["incognito"] = bool(cfg.get("incognito", False))
    if not isinstance(cfg.get("redact_patterns"), list):
        cfg["redact_patterns"] = []
    else:
        cfg["redact_patterns"] = [p for p in cfg["redact_patterns"] if isinstance(p, str) and p.strip()]
    return cfg


def _get_cfg() -> dict:
    with _cfg_lock:
        return dict(_cfg)


def main() -> None:
    global _cfg
    _cfg = _validate_config(_load_config())
    _cfg["_active_hotkey"] = _cfg.get("hotkey", "ctrl+alt")

    # Single-instance guard: a second launch (e.g. double-clicking the desktop
    # shortcut again) would race the first for the hotkey hook, port 8089, and
    # config.json writes. Bail out with a clear message instead.
    _mutex = win32event.CreateMutex(None, False, "Quiett_SingleInstance_Mutex")
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        ctypes.windll.user32.MessageBoxW(
            None, "Quiett is already running (check the system tray).",
            "Quiett", 0x40,  # MB_ICONINFORMATION
        )
        sys.exit(0)

    # DPI awareness: per-monitor V2 for correct sizing on multi-DPI setups
    try:
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
        electron_paste_method=_cfg.get("electron_paste_method", "ctrl_v"),
        paste_mode=_cfg.get("paste_mode", "auto"),
    )
    inject.set_paste_failure_callback(
        lambda msg: preview.show_toast(msg, kind="warn")
    )
    inject.set_paste_info_callback(
        lambda msg: preview.show_toast(msg, kind="info")
    )
    preview.configure_position(_cfg["preview_position"])
    preview.start()

    # First-run check: surface missing prerequisites clearly instead of letting
    # them fail silently or only show up as a buried log line.
    def _check_first_run() -> None:
        problems = transcribe.check_prerequisites(_cfg["model"])
        if not audio.list_input_devices():
            problems.append("no microphone detected")
        for problem in problems:
            log_error("main", f"first-run check: {problem}")
            preview.show_toast(f"Setup issue: {problem}", kind="error")

    _check_first_run()

    # ------------------------------------------------------------------
    # Callbacks wired between audio → transcription → preview → inject
    # ------------------------------------------------------------------

    def _run_transcription(chunks: list, hwnd: int, duration: float = 0.0) -> None:
        cfg = _get_cfg()

        # QW-5: per-app vocabulary/prompt override based on foreground exe
        exe_name = inject._get_exe_name(hwnd).lower() if hwnd else ""
        app_ctx   = cfg.get("per_app_context", {}).get(exe_name, {})
        effective_vocab   = app_ctx.get("custom_vocabulary") or cfg.get("custom_vocabulary") or None
        effective_prompt  = app_ctx.get("initial_prompt")    or cfg.get("initial_prompt")    or None
        effective_fillers = app_ctx.get("filler_words")      or cfg.get("filler_words", [])

        text, confidence, words, raw_text = None, None, None, None
        try:
            text, confidence, words, raw_text = transcribe.run(
                chunks,
                language=cfg["language"],
                min_seconds=cfg["min_record_seconds"],
                filler_words=effective_fillers,
                vad_filter=cfg.get("vad_filter", False),
                profile_rules=profile.get_active_rules(),
                corrections=cfg.get("corrections", {}),
                initial_prompt=effective_prompt,
                custom_vocabulary=effective_vocab,
            )
        except Exception as exc:
            msg = f"Transcription error: {exc}"
            log_error("main", msg)
            print(msg)
            preview.show_toast(msg, kind="error")
        finally:
            tray.set_state("idle")
            preview.hide_badge()
        if text is None:
            return

        lowered = text.strip().lower().rstrip(" .!?,")

        # Spoken undo: "scratch that" as the whole utterance undoes the last
        # insert instead of pasting the phrase. Checked before history save so
        # command utterances don't pollute history.
        if lowered in _UNDO_PHRASES:
            ok = inject.undo_last()
            preview.show_toast("Undid last insert" if ok else "Nothing to undo",
                               kind="info")
            return

        # Incognito (item 86): single gate for both the transcript and the
        # raw audio — nothing from this dictation reaches disk.
        incognito = cfg.get("incognito", False)

        def _persist() -> None:
            """Write the WAV and the history entry, then tell an open dashboard
            to refresh. Deliberately off the critical path: a 100-second
            dictation is a ~3 MB WAV plus an fsync, and doing that before the
            panel appears was a visible stall between speaking and seeing the
            text."""
            audio_file = _save_recording(chunks, cfg) if text.strip() and not incognito else None
            if text.strip() and not cfg.get("history_paused", False) and not incognito:
                history.save(text.strip(), audio=audio_file)
                dashboard.notify_change("history")

        # Per-app auto-paste ("auto_paste": true in per_app_context) skips the
        # preview entirely for trusted apps; the global confidence threshold
        # still applies everywhere else.
        threshold = cfg.get("auto_paste_threshold", 0.0)
        app_auto = bool(app_ctx.get("auto_paste"))
        if text.strip() and (app_auto or (threshold > 0.0 and confidence is not None
                                          and confidence >= threshold)):
            inject.inject_text(text.strip(), hwnd)
            threading.Thread(target=_persist, daemon=True).start()
            return

        preview.show(
            text, hwnd,
            empty=not text.strip(),
            confidence=confidence,
            words=words,
            auto_dismiss=cfg.get("preview_auto_dismiss_seconds", 0.0),
            duration=duration,
            raw=raw_text,
            incognito=incognito,
        )
        threading.Thread(target=_persist, daemon=True).start()

    def _on_audio_stop(chunks: list) -> None:
        hotkey.set_external_recording(False)  # release hold-mode suppression
        inject.flush_hotkey_modifiers_async()  # un-stick modifiers in a focused RDP session
        # Target resolution, best evidence first: whoever is in front now, else
        # whoever was in front when the hotkey went down. The second case is
        # real — our own badge or a leftover preview panel holding focus at
        # release used to mean "no paste target" and the dictation only ever
        # reached the clipboard (app.log 2026-07-27 18:46:17).
        hwnd = inject.capture_foreground()
        if not hwnd:
            fallback = _recording_target.get("hwnd", 0)
            if inject.is_usable_target(fallback):
                hwnd = fallback
                log("main", f"paste target fell back to the hotkey-down window hwnd={hwnd}")
        duration = (sum(len(c) for c in chunks) / audio.SAMPLE_RATE) if chunks else 0.0

        tray.set_state("processing")
        preview.show_badge("processing")
        threading.Thread(
            target=_run_transcription,
            args=(chunks, hwnd, duration),
            daemon=True,
        ).start()

    _PARTIAL_MIN_START_SECONDS = 1.0   # first partial fires once this much audio exists
    _PARTIAL_MIN_NEW_SECONDS   = 0.7   # ...then again once this much *new* audio has landed
    _PARTIAL_MAX_NEW_SECONDS   = 3.0   # cadence ceiling once the server is struggling
    _PARTIAL_SLOW_THRESHOLD    = 1.5   # seconds — a partial slower than this backs off the cadence
    _PARTIAL_POLL_SECONDS      = 0.15

    def _partial_worker() -> None:
        """Live partial transcription while recording — feeds the badge.

        Paced so a partial inference only starts once _PARTIAL_MIN_NEW_SECONDS
        of new audio has landed since the last one. The call itself blocks this
        loop (single thread), so a slow whisper-server response naturally delays
        the next check instead of piling up a second request — no separate
        in-flight guard needed.

        If a partial consistently takes longer than _PARTIAL_SLOW_THRESHOLD, the
        new-audio gate backs off (up to _PARTIAL_MAX_NEW_SECONDS) so the live
        line never starves the eventual final transcription of server time; it
        tightens back up once responses are fast again.
        """
        last_dur = 0.0
        new_audio_gate = _PARTIAL_MIN_NEW_SECONDS
        while audio.is_recording():
            time.sleep(_PARTIAL_POLL_SECONDS)
            chunks = audio.get_chunks_snapshot()
            dur = sum(len(c) for c in chunks) / audio.SAMPLE_RATE
            if dur < _PARTIAL_MIN_START_SECONDS or dur - last_dur < new_audio_gate:
                continue
            if not audio.is_recording():
                break
            t0 = time.time()
            txt = transcribe.run_partial(chunks, _get_cfg().get("language", "en"))
            latency = time.time() - t0
            last_dur = dur
            if latency > _PARTIAL_SLOW_THRESHOLD:
                new_audio_gate = min(_PARTIAL_MAX_NEW_SECONDS, new_audio_gate * 1.5)
            elif latency < _PARTIAL_SLOW_THRESHOLD * 0.5:
                new_audio_gate = max(_PARTIAL_MIN_NEW_SECONDS, new_audio_gate * 0.85)
            if txt and audio.is_recording():
                preview.set_partial_text(txt)

    def _on_recording_start() -> None:
        # Capture before anything of ours can take focus (closing the panel,
        # raising the badge) — at hotkey-down the app the user is dictating
        # into is by definition in front.
        _recording_target["hwnd"] = inject.capture_foreground(quiet=True)
        preview.close_current_preview()
        # No unconditional edge flash here — the badge's own entrance animation
        # is the "hotkey registered" signal; flash_screen_edge is now only a
        # fallback fired from preview.py if the badge itself fails to build
        # (item 46 — previously this double-fired alongside the badge).
        tray.set_state("recording")
        preview.show_badge("recording")
        try:
            audio.start()
        except Exception as exc:
            # Designed error state (BACKLOG item 48a) — most commonly no input
            # device connected. Never leave the "recording" badge hanging.
            log_error("main", f"audio.start failed — no microphone? {exc}")
            hotkey.set_external_recording(False)
            tray.set_state("idle")
            preview.show_badge("mic_error")
            threading.Timer(2.5, preview.hide_badge).start()
            return
        if _get_cfg().get("live_preview_enabled", True):
            threading.Thread(target=_partial_worker, daemon=True).start()

    # ------------------------------------------------------------------
    # Module configuration
    # ------------------------------------------------------------------

    audio.configure(
        on_stop=_on_audio_stop,
        max_duration_seconds=_cfg.get("max_record_seconds", 120),
        silence_timeout_seconds=_cfg.get("silence_auto_stop_seconds", 3.0),
        silence_threshold=_cfg.get("silence_threshold", 0.01),
        input_device=_cfg.get("input_device"),
        vad_silence_mode=_cfg.get("vad_silence_mode", False),
        vad_aggressiveness=_cfg.get("vad_aggressiveness", 2),
    )

    def _on_too_short() -> None:
        tray.set_state("idle")
        preview.show_badge("too_short")
        threading.Timer(1.5, preview.hide_badge).start()

    def _on_cancel() -> None:
        audio.cancel()
        inject.flush_hotkey_modifiers_async()  # cancelled recordings never reach inject

    def _on_not_ready() -> None:
        # Distinguish "still loading" (benign, resolves itself) from a
        # permanent load failure (BACKLOG item 48b) — the latter needs a red,
        # actionable error state, not an indefinite "please wait" that never
        # comes true.
        if transcribe.load_error():
            preview.show_badge("model_error")
            threading.Timer(3.0, preview.hide_badge).start()
        else:
            preview.show_badge("not_ready")
            threading.Timer(2.0, preview.hide_badge).start()

    def _safe_start_recording() -> None:
        if transcribe.is_ready() and not audio.is_recording():
            _on_recording_start()

    preview.set_rerecord_callback(_safe_start_recording)

    hotkey.configure(
        on_start=_on_recording_start,
        on_stop=audio.stop,
        on_cancel=_on_cancel,
        on_too_short=_on_too_short,
        on_not_ready=_on_not_ready,
        is_recording=audio.is_recording,
        is_ready=transcribe.is_ready,
        keys=_cfg.get("hotkey", "ctrl+alt"),
        hotkey_mode=_cfg.get("hotkey_mode", "hold"),
    )
    hotkey.start()

    import keyboard as _kb

    # Speak-selection hotkey — read the highlighted text aloud in the cloned
    # voice (TTS_PLAN P2). Registered unconditionally so the tts_enabled toggle
    # applies instantly, but a disabled toggle costs nothing: no server, no
    # VRAM, just a toast pointing at Settings.
    tts.set_cfg_getter(_get_cfg)
    _tts_hk = _cfg.get("tts_hotkey", "ctrl+shift+s")
    if _tts_hk:
        def _tts_worker() -> None:
            text = inject.get_selected_text()
            if not text.strip():
                log("main", "tts: hotkey fired but no selection was grabbed")
                preview.show_toast("Nothing selected to speak.")
                return
            tray.set_state("processing")
            try:
                tts.speak(text)
            except Exception as exc:
                log_error("main", f"tts speak failed: {exc}")
                preview.show_toast(f"Voice playback failed: {exc}", kind="error")
            finally:
                tray.set_state("idle")

        def _on_tts_hotkey() -> None:
            if tts.is_speaking():
                log("main", "tts: hotkey pressed while speaking — stopping")
                tts.stop()
                return
            if audio.is_recording():
                log("main", "tts: hotkey ignored, recording in progress")
                return
            if not _get_cfg().get("tts_enabled", False):
                log("main", "tts: hotkey pressed but tts_enabled is off")
                preview.show_toast("Voice playback is off. Enable it in Settings to use this hotkey.")
                return
            threading.Thread(target=_tts_worker, daemon=True).start()

        try:
            _kb.add_hotkey(_tts_hk, _on_tts_hotkey, suppress=False)
            log("main", f"tts hotkey registered: {_tts_hk}")
        except Exception as exc:
            log_error("main", f"tts hotkey failed: {exc}")

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
        preview.show_toast("Model load failed after 3 attempts. Check app.log.",
                           kind="error")

    threading.Thread(target=_load_model, daemon=True).start()
    atexit.register(transcribe.shutdown)
    atexit.register(tts.shutdown)

    # ------------------------------------------------------------------
    # HTTP API server (Phase 2)
    # ------------------------------------------------------------------

    if _cfg.get("api_server_enabled", True):
        def _api_patch_config(data: dict) -> None:
            try:
                with open("config.json") as f:
                    raw = json.load(f)
                raw.update(data)
                with open("config.json", "w") as f:
                    json.dump(raw, f, indent=2)
            except Exception as exc:
                log_error("main", f"api patch_config: {exc}")

        api_server.configure(
            get_config=_get_cfg,
            get_history=history.load,
            trigger_dictate=lambda: _on_recording_start() if transcribe.is_ready() and not audio.is_recording() else None,
            patch_config=_api_patch_config,
            port=_cfg.get("api_server_port", 8090),
        )
        api_server.start()

    # ------------------------------------------------------------------
    # Health monitor — toasts a restart action when a backend goes down
    # ------------------------------------------------------------------

    import health

    def _restart_whisper():
        log("main", "user requested whisper restart")
        try:
            transcribe.shutdown()
        except Exception:
            pass
        threading.Thread(target=_load_model, daemon=True).start()

    health.configure(
        toast_fn=preview.show_toast,
        restart_whisper_fn=_restart_whisper,
    )
    health.start()

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
                            "custom_vocabulary", "input_device",
                            "live_preview_enabled", "per_app_context",
                            "retain_audio", "retain_audio_max_files",
                            "retain_audio_min_seconds", "redact_patterns",
                            "tts_speed", "tts_max_chunk_chars",
                            "study_speed", "study_pause_scale"):
                    _cfg[key] = validated[key]
            inject.configure(
                restore_delay_ms=validated["clipboard_restore_delay_ms"],
                per_app_paste=validated.get("per_app_paste", {}),
                electron_paste_method=validated.get("electron_paste_method", "ctrl_v"),
                paste_mode=validated.get("paste_mode", "auto"),
            )
            preview.configure_position(validated["preview_position"])
            preview.refresh_theme()
            tray.refresh_theme()
            audio.configure(
                on_stop=_on_audio_stop,
                max_duration_seconds=validated["max_record_seconds"],
                silence_timeout_seconds=validated["silence_auto_stop_seconds"],
                silence_threshold=validated.get("silence_threshold", 0.01),
                input_device=validated.get("input_device"),
                vad_silence_mode=validated.get("vad_silence_mode", False),
                vad_aggressiveness=validated.get("vad_aggressiveness", 2),
            )
            # Hot-reload hotkey if changed
            new_keys = fresh.get("hotkey", "ctrl+alt")
            if new_keys != _cfg.get("_active_hotkey"):
                hotkey.rebind(new_keys)
                with _cfg_lock:
                    _cfg["_active_hotkey"] = new_keys
            # Sync paste_mode state into tray menu
            new_paste_mode = validated.get("paste_mode", "auto")
            if new_paste_mode != _cfg.get("paste_mode"):
                with _cfg_lock:
                    _cfg["paste_mode"] = new_paste_mode
                tray.set_clipboard_only(new_paste_mode == "clipboard_only")
            # Sync incognito state into tray menu (item 86 — toggleable from
            # either Settings or the tray, so either side may change it)
            new_incognito = validated.get("incognito", False)
            if new_incognito != _cfg.get("incognito"):
                with _cfg_lock:
                    _cfg["incognito"] = new_incognito
                tray.set_incognito(new_incognito)
            # Sync study_mode into the tray, and with it the speed the picker
            # shows: each mode carries its own rate.
            new_study = validated.get("study_mode", False)
            if new_study != _cfg.get("study_mode"):
                with _cfg_lock:
                    _cfg["study_mode"] = new_study
                tray.set_study_mode(new_study)
                tray.set_tts_speed(validated["study_speed"] if new_study
                                   else validated["tts_speed"])
        except Exception as exc:
            log_error("main", f"config hot-reload failed: {exc}")
        t = threading.Timer(_HOT_RELOAD_INTERVAL, _reload_config)
        t.daemon = True
        t.start()

    t0 = threading.Timer(_HOT_RELOAD_INTERVAL, _reload_config)
    t0.daemon = True
    t0.start()

    # ------------------------------------------------------------------
    # Tray
    # ------------------------------------------------------------------

    def _on_toggle_clipboard_only(enabled: bool) -> None:
        global _cfg
        new_mode = "clipboard_only" if enabled else "auto"
        with _cfg_lock:
            _cfg["paste_mode"] = new_mode
        inject.configure(
            restore_delay_ms=_cfg["clipboard_restore_delay_ms"],
            per_app_paste=_cfg.get("per_app_paste", {}),
            electron_paste_method=_cfg.get("electron_paste_method", "ctrl_v"),
            paste_mode=new_mode,
        )
        try:
            with open("config.json") as f:
                raw = json.load(f)
            raw["paste_mode"] = new_mode
            with open("config.json", "w") as f:
                json.dump(raw, f, indent=2)
        except Exception:
            pass

    def _on_toggle_incognito(enabled: bool) -> None:
        global _cfg
        with _cfg_lock:
            _cfg["incognito"] = enabled
        try:
            with open("config.json") as f:
                raw = json.load(f)
            raw["incognito"] = enabled
            with open("config.json", "w") as f:
                json.dump(raw, f, indent=2)
        except Exception:
            pass
        tray.set_incognito(enabled)

    def _write_config_key(key: str, value) -> None:
        try:
            with open("config.json") as f:
                raw = json.load(f)
            raw[key] = value
            with open("config.json", "w") as f:
                json.dump(raw, f, indent=2)
        except Exception:
            pass

    def _on_set_tts_speed(speed: float) -> None:
        """The tray picker edits whichever mode is live, so a study pace never
        overwrites the one picked for ordinary read-aloud."""
        global _cfg
        key = "study_speed" if _get_cfg().get("study_mode", False) else "tts_speed"
        with _cfg_lock:
            _cfg[key] = speed
        _write_config_key(key, speed)
        which = "Study mode" if key == "study_speed" else "Read-aloud"
        preview.show_toast(f"{which} speed: {speed:g}x")

    def _on_toggle_study_mode(enabled: bool) -> None:
        global _cfg
        with _cfg_lock:
            _cfg["study_mode"] = enabled
        _write_config_key("study_mode", enabled)
        cfg = _get_cfg()
        speed = cfg.get("study_speed", 0.95) if enabled else cfg.get("tts_speed", 1.0)
        tray.set_tts_speed(speed)
        preview.show_toast(
            f"Study mode on — read-aloud pauses on structure at {speed:g}x."
            if enabled else "Study mode off.")

    def _on_rebuild_voice_profile() -> None:
        def _worker() -> None:
            try:
                result = voiceprofile.rebuild(
                    max_samples=_get_cfg().get("voice_profile_max_samples", 10))
                preview.show_toast(f"Voice profile: {result['message']}", kind="info")
            except Exception as exc:
                log_error("main", f"voice profile rebuild failed: {exc}")
                preview.show_toast("Voice profile rebuild failed — check app.log.",
                                   kind="error")
        threading.Thread(target=_worker, daemon=True).start()

    tray.configure(
        on_view_history=lambda: dashboard.open_window("history"),
        on_toggle_pause=hotkey.set_paused,
        on_view_profile=lambda: dashboard.open_window("home"),
        on_open_settings=lambda: dashboard.open_window("settings"),
        on_open_dashboard=lambda: dashboard.open_window("home"),
        on_toggle_clipboard_only=_on_toggle_clipboard_only,
        clipboard_only=_cfg.get("paste_mode", "auto") == "clipboard_only",
        on_rebuild_voice_profile=_on_rebuild_voice_profile,
        on_toggle_incognito=_on_toggle_incognito,
        incognito=_cfg.get("incognito", False),
        on_toggle_study_mode=_on_toggle_study_mode,
        study_mode=_cfg.get("study_mode", False),
        on_set_tts_speed=_on_set_tts_speed,
        tts_speed=(_cfg.get("study_speed", 0.95) if _cfg.get("study_mode", False)
                   else _cfg.get("tts_speed", 1.0)),
    )
    print("Hold Ctrl+Alt to dictate. Right-click tray icon to quit.")
    tray.run()


if __name__ == "__main__":
    main()
