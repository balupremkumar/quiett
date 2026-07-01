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
import sys
import threading

import win32api
import win32event
import winerror

import agent
import api_server
import audio
import chime
import dashboard
import history
import hotkey
import inject
import llm_client
import preview
import profile
import reformat
import taskflow
import tray
import transcribe
from logger import log, error as log_error

_cfg: dict = {}
_cfg_lock = threading.Lock()
_task_session: bool = False   # True when Ctrl+Shift+Alt was held at recording start
_agent_session: bool = False  # True when Ctrl+Shift+C triggered this recording

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
    "agent_hotkey":                "ctrl+shift+c",
    "agent_mode_enabled":          False,
    "agent_command_mode_enabled":  False,
    "agent_trigger_phrases":       ["hey computer", "computer run", "computer open"],
    "api_server_enabled":          True,
    "api_server_port":             8090,
    "lmstudio_model":              "qwen2.5-1.5b-instruct",
    "taskflow_enabled":            True,
    "taskflow_trigger_phrases":    ["add this to TaskFlow", "add to my tasks", "add a task"],
    "taskflow_trailing_trigger_phrases": ["add that to TaskFlow", "add that to my tasks",
                                           "add that as a task"],
    "taskflow_direct_capture_modifiers": ["ctrl", "shift", "alt"],
    "taskflow_readback_phrases":   ["what's on my to-do list", "what's on my list",
                                     "read my tasks", "what are my tasks"],
    "taskflow_default_project":    "",
    "taskflow_voice_confirm":      False,
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
    cfg["taskflow_enabled"] = bool(cfg.get("taskflow_enabled", True))
    if not isinstance(cfg.get("per_app_context"), dict):
        cfg["per_app_context"] = {}
    if cfg.get("hotkey_mode") not in ("hold", "toggle", "auto"):
        cfg["hotkey_mode"] = "hold"
    cfg["agent_mode_enabled"] = bool(cfg.get("agent_mode_enabled", False))
    cfg["agent_command_mode_enabled"] = bool(cfg.get("agent_command_mode_enabled", False))
    if not isinstance(cfg.get("agent_trigger_phrases"), list):
        cfg["agent_trigger_phrases"] = []
    cfg["api_server_enabled"] = bool(cfg.get("api_server_enabled", True))
    try:
        cfg["api_server_port"] = max(1024, min(65535, int(cfg.get("api_server_port", 8090))))
    except (TypeError, ValueError):
        cfg["api_server_port"] = 8090
    for key in ("taskflow_trigger_phrases", "taskflow_trailing_trigger_phrases",
                "taskflow_readback_phrases"):
        if not isinstance(cfg.get(key), list):
            cfg[key] = _CONFIG_DEFAULTS[key]
        else:
            cfg[key] = [p for p in cfg[key] if isinstance(p, str) and p.strip()]
    if not isinstance(cfg.get("taskflow_default_project"), str):
        cfg["taskflow_default_project"] = ""
    cfg["taskflow_voice_confirm"] = bool(cfg.get("taskflow_voice_confirm", False))
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
    _mutex = win32event.CreateMutex(None, False, "VoiceDictate_SingleInstance_Mutex")
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        ctypes.windll.user32.MessageBoxW(
            None, "VoiceDictate is already running (check the system tray).",
            "VoiceDictate", 0x40,  # MB_ICONINFORMATION
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
    # TaskFlow helpers — auto-create (embedded mid-utterance capture),
    # read-back, and mark-done. All run off the transcription worker thread
    # already, so they're free to block briefly on TaskFlow's API.
    # ------------------------------------------------------------------

    def _relaunch_taskflow_with_toast() -> None:
        threading.Thread(target=taskflow.ensure_running, daemon=True).start()

    def _taskflow_unreachable_toast() -> None:
        if taskflow.record_health_check(False):
            preview.show_toast(
                "TaskFlow seems to be down.", kind="warn",
                action_label="Relaunch", action_cb=_relaunch_taskflow_with_toast,
            )
        tray.set_taskflow_status(False)

    def _auto_create_task(task_content: str, cfg: dict) -> bool:
        """Create a task with no confirmation step — used for a trigger
        phrase found embedded mid-utterance, where stopping to click a
        confirm button would break the user's conversational flow. Returns
        False if TaskFlow is unreachable, so the caller can fold the raw
        task content back into the normally-pasted remainder instead of
        silently losing it.
        """
        spec = taskflow.build_task_spec(task_content, cfg.get("taskflow_default_project") or None)
        title = spec.get("title", "").strip()
        if not title:
            return True
        if taskflow.is_duplicate(title):
            log("main", f"taskflow: skipped duplicate task {title!r}")
            preview.show_toast(f"Already added recently: {title}", kind="info")
            return True
        if not taskflow.check_health():
            _taskflow_unreachable_toast()
            return False
        taskflow.record_health_check(True)
        tray.set_taskflow_status(True)
        created = taskflow.create_task_from_spec(spec)
        if created is None:
            preview.show_toast(f"Couldn't reach TaskFlow — couldn't add {title!r}.", kind="warn")
            return False

        task_id = created.get("id")
        chime.play_task_added()
        tray.increment_task_count()
        if not cfg.get("history_paused", False):
            history.save(title, source="taskflow")
        if cfg.get("taskflow_voice_confirm"):
            chime.speak(f"Added {title} to your to-do list")

        def _undo() -> None:
            if task_id and taskflow.delete_task(task_id):
                preview.show_toast(f"Removed: {title}", kind="info")

        preview.show_toast(
            f"Added to to-do list: {title}", kind="info",
            action_label="Undo" if task_id else "",
            action_cb=_undo if task_id else None,
        )
        return True

    def _handle_readback(cfg: dict) -> None:
        if not taskflow.check_health():
            _taskflow_unreachable_toast()
            preview.show_toast("TaskFlow isn't running — can't read your list.", kind="warn")
            return
        tray.set_taskflow_status(True)
        taskflow.record_health_check(True)
        tasks = taskflow.list_tasks(completed=False)
        summary = taskflow.format_task_list(tasks or [])
        preview.show_toast(summary, kind="info")
        if cfg.get("taskflow_voice_confirm"):
            chime.speak(summary)

    def _handle_complete(spoken_title: str, cfg: dict) -> None:
        if not taskflow.check_health():
            _taskflow_unreachable_toast()
            preview.show_toast("TaskFlow isn't running.", kind="warn")
            return
        tray.set_taskflow_status(True)
        taskflow.record_health_check(True)
        match = taskflow.find_open_task_by_title(spoken_title)
        if match is None:
            preview.show_toast(f"Couldn't find an open task matching “{spoken_title}”.",
                               kind="warn")
            return
        updated = taskflow.update_task(match["id"], completed=True)
        title = match.get("title", spoken_title)
        if updated is None:
            preview.show_toast(f"Couldn't mark {title!r} as done.", kind="warn")
            return
        preview.show_toast(f"Marked done: {title}", kind="info")
        if cfg.get("taskflow_voice_confirm"):
            chime.speak(f"Marked {title} as done")

    # ------------------------------------------------------------------
    # Callbacks wired between audio → transcription → preview → inject
    # ------------------------------------------------------------------

    def _run_transcription(chunks: list, hwnd: int, task_session: bool = False,
                           agent_session: bool = False) -> None:
        cfg = _get_cfg()

        # QW-5: per-app vocabulary/prompt override based on foreground exe
        exe_name = inject._get_exe_name(hwnd).lower() if hwnd else ""
        app_ctx   = cfg.get("per_app_context", {}).get(exe_name, {})
        effective_vocab   = app_ctx.get("custom_vocabulary") or cfg.get("custom_vocabulary") or None
        effective_prompt  = app_ctx.get("initial_prompt")    or cfg.get("initial_prompt")    or None
        effective_fillers = app_ctx.get("filler_words")      or cfg.get("filler_words", [])

        text, confidence, words = None, None, None
        try:
            text, confidence, words = transcribe.run(
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
        if text.strip() and not cfg.get("history_paused", False):
            history.save(text.strip())

        # Ctrl+Alt+C agent mode: classify + confirm gate via preview
        if agent_session and text.strip():
            tray.set_state("idle")
            preview.hide_badge()
            if not llm_client.is_available():
                preview.show_toast("LM Studio not running — agent mode requires local LLM.", kind="warn")
            else:
                preview.show_badge("reformatting")
                def _run_agent():
                    try:
                        actions = agent.interpret(text.strip())
                        preview.hide_badge()
                        known = [a for a in actions if a.get("action") != "unknown"]
                        if not known:
                            preview.show_toast(f"Didn't understand: {text.strip()}", kind="warn")
                            return

                        def _add_task_fn(title: str) -> bool:
                            if not taskflow.check_health():
                                return False
                            spec = taskflow.build_task_spec(title)
                            result = taskflow.create_task_from_spec(spec)
                            if result:
                                tray.increment_task_count()
                            return bool(result)

                        def _do_execute():
                            for action in known:
                                ok = agent.execute(
                                    action,
                                    inject_fn=lambda t, h: inject.inject_text(t, h or hwnd),
                                    add_task_fn=_add_task_fn,
                                )
                                if not ok:
                                    preview.show_toast(
                                        f"Failed: {agent.describe([action])}", kind="warn"
                                    )

                        if any(a.get("action") == "run_command" for a in known):
                            preview.show_agent_confirm(agent.describe(known), _do_execute)
                        else:
                            _do_execute()
                    except Exception as exc:
                        log_error("main", f"agent error: {exc}")
                        preview.hide_badge()
                        preview.show_toast("Agent error — check app.log.", kind="error")

                threading.Thread(target=_run_agent, daemon=True).start()
            return

        # Ctrl+Shift+Alt direct-capture mode: skip all phrase matching and route
        # the full transcript straight to the task confirm panel.
        if task_session and text.strip():
            preview.show(
                text, hwnd,
                empty=False,
                confidence=confidence,
                words=words,
                auto_dismiss=cfg.get("preview_auto_dismiss_seconds", 0.0),
                task_mode=True,
            )
            return

        # TaskFlow: read-back, mark-done, and add-task trigger handling.
        # Must run before the auto-paste threshold below, else a confident
        # "add this to TaskFlow X" would get silently pasted as raw text.
        task_extracted = False
        if cfg.get("taskflow_enabled", True) and text.strip():
            stripped_text = text.strip()

            if taskflow.match_trigger(stripped_text, cfg.get("taskflow_readback_phrases", [])):
                threading.Thread(target=_handle_readback, args=(cfg,), daemon=True).start()
                return

            complete_title = taskflow.match_complete_command(stripped_text)
            if complete_title:
                threading.Thread(target=_handle_complete, args=(complete_title, cfg), daemon=True).start()
                return

            task_texts, remainder = taskflow.extract_tasks(
                stripped_text,
                cfg.get("taskflow_trigger_phrases", []),
                cfg.get("taskflow_trailing_trigger_phrases", []),
            )
            if task_texts:
                whole_utterance = len(task_texts) == 1 and not remainder.strip()
                if whole_utterance:
                    # Sole content of the recording — keep the deliberate
                    # manual confirm step (review before it's created).
                    preview.show(
                        task_texts[0], hwnd,
                        empty=not task_texts[0].strip(),
                        confidence=confidence,
                        words=None,
                        auto_dismiss=cfg.get("preview_auto_dismiss_seconds", 0.0),
                        task_mode=True,
                    )
                    return
                # Embedded mid-utterance: auto-create immediately (no
                # blocking confirm — that would break conversational flow).
                # Anything TaskFlow couldn't take gets folded back into the
                # remainder so it's never silently lost.
                leftover = []
                for task_text in task_texts:
                    if not _auto_create_task(task_text, cfg):
                        leftover.append(task_text)
                remainder = (remainder + " " + " ".join(leftover)).strip() if leftover else remainder
                if not remainder.strip():
                    return
                text = remainder
                task_extracted = True

        threshold = cfg.get("auto_paste_threshold", 0.0)
        if (threshold > 0.0 and confidence is not None
                and confidence >= threshold and text.strip()):
            inject.inject_text(text.strip(), hwnd)
            return

        preview.show(
            text, hwnd,
            empty=not text.strip(),
            confidence=confidence,
            words=None if task_extracted else words,
            auto_dismiss=cfg.get("preview_auto_dismiss_seconds", 0.0),
        )

    def _on_audio_stop(chunks: list) -> None:
        hotkey.set_external_recording(False)  # release hold-mode suppression
        hwnd = inject.capture_foreground()
        task_mode  = _task_session
        agent_mode = _agent_session
        tray.set_state("processing")
        preview.show_badge("processing")
        threading.Thread(
            target=_run_transcription,
            args=(chunks, hwnd, task_mode, agent_mode),
            daemon=True,
        ).start()

    def _on_recording_start(agent: bool = False) -> None:
        global _task_session, _agent_session
        _agent_session = agent
        _task_session = bool(
            not agent
            and _get_cfg().get("taskflow_direct_capture_modifiers")
            and (win32api.GetAsyncKeyState(0x10) & 0x8000)  # VK_SHIFT
        )
        if agent:
            hotkey.set_external_recording(True)
        preview.close_current_preview()
        edge = "#f97316" if agent else ("#22c55e" if _task_session else "#3b82f6")
        preview.flash_screen_edge(edge)
        tray.set_state("recording")
        badge = "recording_task" if _task_session else "recording"
        preview.show_badge(badge)
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
        vad_silence_mode=_cfg.get("vad_silence_mode", False),
        vad_aggressiveness=_cfg.get("vad_aggressiveness", 2),
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
        hotkey_mode=_cfg.get("hotkey_mode", "hold"),
    )
    hotkey.start()

    # Ctrl+Shift+C — agent/command mode
    import keyboard as _kb
    _agent_hk = _cfg.get("agent_hotkey", "ctrl+shift+c")
    if _cfg.get("agent_mode_enabled", False):
        try:
            # Build the set of key names to watch for release
            _agent_hk_keys: set[str] = set()
            for _part in _agent_hk.lower().split("+"):
                _part = _part.strip()
                if _part == "ctrl":
                    _agent_hk_keys.update(["ctrl", "left ctrl", "right ctrl"])
                elif _part == "shift":
                    _agent_hk_keys.update(["shift", "left shift", "right shift"])
                elif _part == "alt":
                    _agent_hk_keys.update(["alt", "left alt", "right alt"])
                else:
                    _agent_hk_keys.add(_part)

            def _agent_key_up(event):
                if (event.event_type == "up"
                        and event.name in _agent_hk_keys
                        and _agent_session
                        and audio.is_recording()):
                    audio.stop()

            _kb.add_hotkey(
                _agent_hk,
                lambda: _on_recording_start(agent=True) if transcribe.is_ready() and not audio.is_recording() else None,
                suppress=False,
            )
            _kb.hook(_agent_key_up)
            log("main", f"agent hotkey registered: {_agent_hk}")
        except Exception as exc:
            log_error("main", f"agent hotkey failed: {exc}")

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

    def _load_reformat() -> None:
        m = _cfg.get("lmstudio_model", "qwen2.5-1.5b-instruct")
        agent.set_model(m)
        reformat.load(model=m)

    if _cfg.get("agent_command_mode_enabled", False):
        agent.set_model(_cfg.get("lmstudio_model", "qwen2.5-1.5b-instruct"))
        threading.Thread(target=_load_reformat, daemon=True).start()

    # ------------------------------------------------------------------
    # TaskFlow auto-launch — non-blocking, no-ops if never installed
    # ------------------------------------------------------------------

    def _ensure_taskflow() -> None:
        if not _get_cfg().get("taskflow_enabled", True):
            return
        taskflow.ensure_running()
        tray.set_taskflow_status(taskflow.check_health())

    threading.Thread(target=_ensure_taskflow, daemon=True).start()

    # ------------------------------------------------------------------
    # HTTP API server (Phase 2)
    # ------------------------------------------------------------------

    if _cfg.get("api_server_enabled", True):
        def _api_create_task(title: str, project=None) -> dict | None:
            spec = taskflow.build_task_spec(title, project)
            if not taskflow.check_health():
                return None
            return taskflow.create_task_from_spec(spec)

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
            create_task=_api_create_task,
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

    def _restart_lmstudio():
        log("main", "user requested lmstudio restart")
        threading.Thread(target=_load_reformat, daemon=True).start()

    health.configure(
        toast_fn=preview.show_toast,
        restart_whisper_fn=_restart_whisper,
        restart_lmstudio_fn=_restart_lmstudio,
        vibe_enabled_fn=lambda: _get_cfg().get("agent_command_mode_enabled", False),
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
                            "taskflow_enabled", "taskflow_trigger_phrases",
                            "taskflow_trailing_trigger_phrases", "taskflow_readback_phrases",
                            "taskflow_default_project", "taskflow_voice_confirm",
                            "taskflow_direct_capture_modifiers"):
                    _cfg[key] = validated[key]
            inject.configure(
                restore_delay_ms=validated["clipboard_restore_delay_ms"],
                per_app_paste=validated.get("per_app_paste", {}),
                electron_paste_method=validated.get("electron_paste_method", "ctrl_v"),
                paste_mode=validated.get("paste_mode", "auto"),
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
            # Sync agent_command_mode_enabled into tray menu
            new_agent_cmd = validated.get("agent_command_mode_enabled", False)
            if new_agent_cmd != _cfg.get("agent_command_mode_enabled"):
                with _cfg_lock:
                    _cfg["agent_command_mode_enabled"] = new_agent_cmd
                tray.set_agent_command_mode(new_agent_cmd)
                if new_agent_cmd and not reformat.is_ready():
                    threading.Thread(target=_load_reformat, daemon=True).start()
                elif not new_agent_cmd and reformat.is_ready():
                    threading.Thread(target=reformat.unload, daemon=True).start()
            # Sync paste_mode state into tray menu
            new_paste_mode = validated.get("paste_mode", "auto")
            if new_paste_mode != _cfg.get("paste_mode"):
                with _cfg_lock:
                    _cfg["paste_mode"] = new_paste_mode
                tray.set_clipboard_only(new_paste_mode == "clipboard_only")
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

    def _on_toggle_agent_command_mode(enabled: bool) -> None:
        global _cfg
        with _cfg_lock:
            _cfg["agent_command_mode_enabled"] = enabled
        if enabled and not reformat.is_ready():
            agent.set_model(_get_cfg().get("lmstudio_model", "qwen2.5-1.5b-instruct"))
            threading.Thread(target=_load_reformat, daemon=True).start()
        elif not enabled and reformat.is_ready():
            threading.Thread(target=reformat.unload, daemon=True).start()
        try:
            with open("config.json") as f:
                raw = json.load(f)
            raw["agent_command_mode_enabled"] = enabled
            with open("config.json", "w") as f:
                json.dump(raw, f, indent=2)
        except Exception:
            pass
        tray.set_agent_command_mode(enabled)

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

    tray.configure(
        on_view_history=lambda: dashboard.open("history"),
        on_toggle_pause=hotkey.set_paused,
        on_view_profile=lambda: dashboard.open("home"),
        on_open_settings=lambda: dashboard.open("settings"),
        on_relaunch_taskflow=lambda: threading.Thread(target=taskflow.ensure_running, daemon=True).start(),
        on_toggle_clipboard_only=_on_toggle_clipboard_only,
        clipboard_only=_cfg.get("paste_mode", "auto") == "clipboard_only",
        on_toggle_agent_command_mode=_on_toggle_agent_command_mode,
        agent_command_mode=_cfg.get("agent_command_mode_enabled", False),
    )
    print("Hold Ctrl+Alt to dictate. Right-click tray icon to quit.")
    tray.run()


if __name__ == "__main__":
    main()
