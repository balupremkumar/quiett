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
import stash
import tray
import transcribe
import tts
import voiceprofile
from logger import log, error as log_error, warn


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
    "rdp_clipboard_settle_ms":     250,   # rdpclip needs longer than local apps before Ctrl+V
    "rdp_clipboard_restore_delay_ms": 3000,  # ...and the remote may read the clipboard late
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
    "panel_acrylic":               True,    # Win11 acrylic backdrop on the panel/badge (QUIETT_UI_PLAN P4)
    "learn_from_edits":            True,    # auto-promote a correction to a rule after 3 repeats (P6)
    "dashboard_prewarm":           True,    # boot the dashboard hidden at startup so it opens instantly
    # PASTE_UX_PLAN section 7, insert reliability. All default to the new behaviour.
    "clipboard_retain_on_unconfirmed": True,  # keep the dictation on the clipboard when we cannot confirm it landed
    "target_probe":                True,    # probe the target before/after inserting; False = pre-plan behaviour
    "target_ring":                 True,    # outline the resolved target while recording
    # Place mode's everyday entry point: ONE key, no modifiers, reserved by
    # Quiett while it runs (hotkey.RESERVED_KEYS lists the names). "" disables.
    "place_key":                   "numpad_decimal",
    # Optional extra chord for place mode, off by default now the key above
    # exists. NEVER pick a ctrl+alt+<x> combo here: ctrl+alt is the record hold
    # and hotkey._on_key only tests that every modifier is down, so extra keys
    # are ignored and the combo starts a recording before it does its own job.
    "place_hotkey":                "",
    "unstick_hotkey":              "ctrl+shift+u",  # force-flush modifiers stuck in an RDP session
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
        cfg["rdp_clipboard_settle_ms"] = max(0, int(cfg["rdp_clipboard_settle_ms"]))
    except (TypeError, ValueError):
        cfg["rdp_clipboard_settle_ms"] = 250
    try:
        cfg["rdp_clipboard_restore_delay_ms"] = max(50, int(cfg["rdp_clipboard_restore_delay_ms"]))
    except (TypeError, ValueError):
        cfg["rdp_clipboard_restore_delay_ms"] = 3000
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
    cfg["panel_acrylic"] = bool(cfg.get("panel_acrylic", True))
    cfg["learn_from_edits"] = bool(cfg.get("learn_from_edits", True))
    cfg["dashboard_prewarm"] = bool(cfg.get("dashboard_prewarm", True))
    cfg["clipboard_retain_on_unconfirmed"] = bool(cfg.get("clipboard_retain_on_unconfirmed", True))
    cfg["target_probe"] = bool(cfg.get("target_probe", True))
    cfg["target_ring"] = bool(cfg.get("target_ring", True))
    for key, default in (("place_hotkey", ""),
                         ("unstick_hotkey", "ctrl+shift+u")):
        value = cfg.get(key)
        cfg[key] = value.strip().lower() if isinstance(value, str) and value.strip() else default
    # place_key names a physical key Quiett reserves outright, so an unknown
    # name cannot be honoured at all: fall back rather than silently leaving
    # place mode with no key. "" is a deliberate "no reserved key".
    raw_place_key = cfg.get("place_key", "numpad_decimal")
    place_key = raw_place_key.strip().lower() if isinstance(raw_place_key, str) else None
    if place_key is None or (place_key and place_key not in hotkey.RESERVED_KEYS):
        warn("main", f"place_key {raw_place_key!r} is not a key Quiett can reserve, "
                     "using numpad_decimal")
        place_key = "numpad_decimal"
    cfg["place_key"] = place_key
    return cfg


def _get_cfg() -> dict:
    with _cfg_lock:
        return dict(_cfg)


# ---------------------------------------------------------------------------
# Escalation after an insert (PASTE_UX_PLAN section 5, corrected 2026-08-22)
#
# Silence is not the same as failure. Verification legitimately returns "no
# signal" for terminals, RDP sessions and every app with no accessibility
# layer, so escalating on merely-unconfirmed would put a failure notice on
# most SUCCESSFUL inserts and train the user to ignore the one that matters.
# Clipboard retention plus the tray dot is the silent safety net; the loud
# path is reserved for positive evidence that the text did not land.
# ---------------------------------------------------------------------------

ESCALATE_NONE     = "none"       # nothing on screen beyond the tray dot
ESCALATE_RECOVERY = "recovery"   # panel at the cursor, error chime

# A pre-flight refusal is kept distinct from the other five clipboard
# fallbacks so the recovery panel can name the real reason.
_REFUSED_NOT_EDITABLE = inject.REFUSED_NOT_EDITABLE


def escalation_for(status: str, paste_mode: str = "auto") -> dict:
    """Pure decision table for a finished insert: no side effects, no config
    reads, everything it needs is an argument.

    consume_stash - is the parked copy safe to drop?
    ui            - which surface, if any, the user sees.
    reason        - honest wording for that surface.
    """
    if paste_mode == "clipboard_only":
        # Copying instead of inserting is the whole point of that mode, so
        # nothing here is a failure and nothing here consumed the stash.
        return {"consume_stash": False, "ui": ESCALATE_NONE, "reason": ""}
    if status == inject.INSERTED:
        return {"consume_stash": True, "ui": ESCALATE_NONE, "reason": ""}
    if status == inject.INSERTED_UNCONFIRMED:
        # No signal. Retained clipboard, armed stash, tray dot, no toast.
        return {"consume_stash": False, "ui": ESCALATE_NONE, "reason": ""}
    if status == _REFUSED_NOT_EDITABLE:
        return {"consume_stash": False, "ui": ESCALATE_RECOVERY,
                "reason": "No text field was focused, so nothing was typed. Click into "
                          "the field you want, then Place it."}
    if status == inject.FAILED:
        return {"consume_stash": False, "ui": ESCALATE_RECOVERY,
                "reason": "The text did not reach the field, and the clipboard copy "
                          "failed too. Click into the field you want, then Place it."}
    return {"consume_stash": False, "ui": ESCALATE_RECOVERY,
            "reason": "The text did not reach the field. It is on your clipboard. "
                      "Click into the field you want, then Place it."}


def _apply_escalation(text: str, decision: dict) -> None:
    """Show whatever the table asked for. Confirmed-landed and no-signal both
    show nothing, which is the point of the corrected policy."""
    if decision.get("ui") != ESCALATE_RECOVERY:
        return

    def _place() -> None:
        threading.Thread(target=insert_text_now, args=(text,), daemon=True).start()

    def _dismiss() -> None:
        stash.clear()

    preview.show_recovery_panel(text, decision.get("reason", ""), _place, _dismiss)


def escalate_insert(text: str, status: str) -> None:
    """Escalation entry point for an insert that ran somewhere else - the
    preview panel fires its own and consumes the stash there, so this applies
    only the user-visible half of the table."""
    _apply_escalation(text, escalation_for(status, _get_cfg().get("paste_mode", "auto")))


def _resolve_paste_target() -> int:
    """Best paste target right now, best evidence first: whoever is in front,
    else whoever was in front when the hotkey went down. The second case is
    real — our own badge or a leftover preview panel holding focus at release
    used to mean "no paste target" and the dictation only ever reached the
    clipboard (app.log 2026-07-27 18:46:17)."""
    hwnd = inject.capture_foreground()
    if not hwnd:
        fallback = _recording_target.get("hwnd", 0)
        if inject.is_usable_target(fallback):
            hwnd = fallback
            log("main", f"paste target fell back to the hotkey-down window hwnd={hwnd}")
    return hwnd


def insert_text_now(text: str, hwnd: int = 0, submit: bool = False,
                    escalate: bool = True) -> str:
    """Insert text into the target and escalate only on evidence it missed.

    hwnd=0 resolves the target at call time, which is what makes recovery
    work: the user focuses the field they wanted, presses "Place it", and the
    text goes there rather than back into whatever was in front before.

    escalate=False returns the status and shows nothing, for the one caller
    that has its own answer to a refusal (the place key, which arms instead).
    The stash half of the decision still applies either way.
    """
    # Not stripped: a panel insert with "append" on carries a leading space
    # that the retry has to preserve.
    if not text or not text.strip():
        return inject.INSERTED
    target = hwnd if hwnd else _resolve_paste_target()
    fn = inject.inject_text_and_submit if submit else inject.inject_text
    status = fn(text, target)
    decision = escalation_for(status, _get_cfg().get("paste_mode", "auto"))
    if decision["consume_stash"]:
        stash.mark_consumed()   # confirmed landed: nothing left to recover
    if escalate:
        _apply_escalation(text, decision)
    return status


def insert_last_dictation(text: str) -> None:
    """Tray "Insert Last Dictation" — give the menu a moment to close and focus
    to settle back on the user's window before resolving the target."""
    def _worker() -> None:
        time.sleep(0.25)
        insert_text_now(text)
    threading.Thread(target=_worker, daemon=True).start()


# ---------------------------------------------------------------------------
# Place mode - arm, click, insert (PASTE_UX_PLAN section 4)
#
# Tap to arm, never hold. Pressing and HOLDING a modifier is the exact
# mechanism that latches Ctrl inside an RDP session, so the recovery for a
# lost insert must not be built on it.
#
# The mouse hook lives only while armed (hotkey.capture_next_click) and the
# click is never swallowed: it has to reach the target app or the field the
# user picked never gets focus.
# ---------------------------------------------------------------------------

_PLACE_TIMEOUT_SECONDS = 30.0   # armed and forgotten disarms itself
_PLACE_SETTLE_SECONDS  = 0.25   # let the click land and focus settle before inserting

_place_lock = threading.Lock()
_place: dict = {"armed": False, "text": "", "timer": None}


def place_mode_armed() -> bool:
    with _place_lock:
        return bool(_place["armed"])


def toggle_place_mode() -> str:
    """The place hotkey. Returns "armed" | "disarmed" | "empty" for the log and
    the tests; what the user sees is the overlay or the toast."""
    if place_mode_armed():
        disarm_place_mode("hotkey pressed again")
        return "disarmed"

    # The default place hotkey (ctrl+alt+v) shares its prefix with the ctrl+alt
    # record hold, so the modifiers alone have already started a recording by
    # the time the V lands. Drop it: the user asked to place, not to dictate.
    _cancel_recording_for_place()

    item = stash.get() or {}
    text = item.get("text", "")
    if not stash.has_unconsumed() or not text.strip():
        preview.show_toast("Nothing to place yet")
        return "empty"
    _arm_place_mode(text)
    return "armed"


def place_key_pressed() -> str:
    """The reserved place key (the numpad "." by default). One key, no
    modifiers, because this is reached for constantly.

    Placing beats arming as the default outcome: by the time the user hits it
    they have already clicked into the field they want, so the honest reading
    of the press is "put it here". Arming, with the overlay and the pick-by-
    click, is the fallback for the one case where the pre-flight probe is
    CONFIDENT there is no field to place into. Anything less than confident
    still inserts, which is the rule that keeps working apps working.

    Returns "disarmed" | "empty" | "placed" | "armed" for the log and the tests.
    """
    if place_mode_armed():
        disarm_place_mode("place key pressed again")
        return "disarmed"

    item = stash.get() or {}
    text = item.get("text", "")
    if not stash.has_unconsumed() or not text.strip():
        preview.show_toast("Nothing to place yet")
        return "empty"

    # escalate=False: a refusal here is not a dead end, it is the cue to arm.
    # One press, one outcome, one notice - the recovery panel would be a second.
    status = insert_text_now(text, escalate=False)
    if status == _REFUSED_NOT_EDITABLE:
        log("main", "place key: no field had focus, arming place mode instead")
        _arm_place_mode(text)
        return "armed"
    escalate_insert(text, status)
    return "placed"


def _cancel_recording_for_place() -> None:
    if not audio.is_recording():
        return
    log("main", "place hotkey cancelled the recording its own modifiers started")
    try:
        audio.cancel()
        hotkey.set_external_recording(False)
        tray.set_state("idle")
        preview.hide_badge()
        preview.hide_target_ring()
    except Exception as exc:
        log_error("main", f"could not cancel the recording before arming place mode: {exc}")


def _arm_place_mode(text: str) -> None:
    timer = threading.Timer(_PLACE_TIMEOUT_SECONDS, lambda: disarm_place_mode("timed out"))
    timer.daemon = True
    with _place_lock:
        _place["armed"] = True
        _place["text"] = text
        _place["timer"] = timer
    timer.start()
    preview.show_place_overlay(text)
    # Esc has to be a global tap: the overlay is click-through and never takes
    # focus, so it can never receive a key event of its own. Registered only
    # while armed, so Esc is untouched the rest of the time.
    hotkey.register_tap("esc", lambda: disarm_place_mode("esc"))
    if not hotkey.capture_next_click(_on_place_click):
        # No hook, no place mode. Say so rather than leaving an overlay up
        # that will never do anything.
        log_error("main", "place mode could not install the mouse hook")
        disarm_place_mode("no mouse hook")
        preview.show_toast("Windows would not give Quiett a mouse hook. "
                           "Use the tray's Place last dictation instead.", kind="warn")
        return
    log("main", "place mode armed")


def disarm_place_mode(reason: str = "") -> None:
    """Idempotent: Esc, a second hotkey press, the insert and the timeout all
    land here, and several of them can race."""
    with _place_lock:
        if not _place["armed"]:
            return
        _place["armed"] = False
        _place["text"] = ""
        timer = _place["timer"]
        _place["timer"] = None
    if timer is not None:
        timer.cancel()
    hotkey.unregister_tap("esc")
    hotkey.cancel_click_capture()
    preview.hide_place_overlay()
    log("main", f"place mode disarmed ({reason or 'no reason given'})")


def _on_place_click() -> None:
    """The click that picked a target. Runs on hotkey.py's worker thread, after
    the click has been delivered to whatever the user clicked."""
    with _place_lock:
        if not _place["armed"]:
            return
        text = _place["text"]
    disarm_place_mode("placed")
    if not text.strip():
        return
    # The click is delivered but the app has not necessarily finished moving
    # focus into the control it hit, and inserting into a half-settled target
    # is how text lands somewhere the user did not choose.
    time.sleep(_PLACE_SETTLE_SECONDS)
    insert_text_now(text)   # hwnd=0: resolve the window the user just picked


def _wire_target_probe() -> None:
    """Connect targetprobe to inject, or leave inserts unverified.

    The pre-flight call is also where the verifier's pre-insert snapshot is
    taken. That is deliberate: it is the last moment before the text goes out,
    and probing the target twice would pay the cost twice for the same answer.

    A missing or broken probe is never fatal. Without it every insert grades as
    unconfirmed, which keeps the dictation on the clipboard and the tray dot
    lit, so the safety net still holds; only the silent-success case is lost.
    """
    if not _get_cfg().get("target_probe", True):
        log("main", "target probe disabled by config, inserts stay unverified")
        return
    try:
        import targetprobe
    except Exception as exc:
        log_error("main", f"targetprobe unavailable, inserts stay unverified: {exc}")
        return

    targetprobe.warm_up()   # 84-102ms of COM init, off the insert path
    # One insert runs at a time, so a single slot is enough and cannot leak.
    pending = {"hwnd": 0, "token": None}

    def _preflight(hwnd: int) -> str:
        verdict = targetprobe.probe(hwnd).verdict
        try:
            pending["hwnd"], pending["token"] = hwnd, targetprobe.verify_token(hwnd)
        except Exception:
            pending["hwnd"], pending["token"] = 0, None
        return verdict

    def _verify(hwnd: int, before_token: dict):
        token = pending["token"] if pending["hwnd"] == hwnd else None
        pending["hwnd"], pending["token"] = 0, None
        if token is None:
            return None
        return targetprobe.verify_landed(hwnd, token)

    inject.set_preflight_callback(_preflight)
    inject.set_verify_callback(_verify)
    log("main", "target probe wired, UIA readiness follows from targetprobe")


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
        rdp_clipboard_settle_ms=_cfg["rdp_clipboard_settle_ms"],
        rdp_clipboard_restore_delay_ms=_cfg["rdp_clipboard_restore_delay_ms"],
        clipboard_retain_on_unconfirmed=_cfg["clipboard_retain_on_unconfirmed"],
    )
    _wire_target_probe()
    inject.set_paste_failure_callback(
        lambda msg: preview.show_toast(msg, kind="warn")
    )
    inject.set_paste_info_callback(
        lambda msg: preview.show_toast(msg, kind="info")
    )
    # An insert fired from the preview panel that did not land comes back here
    # for the escalation table.
    preview.set_insert_failed_callback(escalate_insert)
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

        # Park it before anything can go wrong with the insert. The clipboard
        # is not a safe place for the only copy of a dictation, and history
        # says nothing about whether it ever landed (PASTE_UX_PLAN section 2).
        if text.strip():
            stash.put(text.strip(), hwnd)

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
            insert_text_now(text.strip(), hwnd)
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
        preview.hide_target_ring()             # recording over, nothing to outline
        inject.flush_hotkey_modifiers_async()  # un-stick modifiers in a focused RDP session
        hwnd = _resolve_paste_target()
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
        # Outline the target while recording (PASTE_UX_PLAN section 5), so
        # "is there a field to receive this" is answered before speaking
        # rather than after. Probes on its own worker; never blocks the start.
        preview.show_ring_for_window(_recording_target["hwnd"])
        try:
            audio.start()
        except Exception as exc:
            # Designed error state (BACKLOG item 48a) — most commonly no input
            # device connected. Never leave the "recording" badge hanging.
            log_error("main", f"audio.start failed — no microphone? {exc}")
            hotkey.set_external_recording(False)
            tray.set_state("idle")
            preview.hide_target_ring()
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
        preview.hide_target_ring()             # recording over, nothing to outline
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

    # Place mode (PASTE_UX_PLAN section 4). Optional extra chord, off by
    # default; its own registration path, so the hold-to-record hook above is
    # untouched.
    _place_hk = _cfg.get("place_hotkey", "")
    if _place_hk:
        hotkey.register_tap(_place_hk, toggle_place_mode)

    # ...and the everyday entry point: one reserved key, no modifiers, on its
    # own low-level hook so the key is swallowed before the focused app sees
    # it. The nav-cluster Delete shares scan code 83 with the numpad "." and is
    # told apart by the extended flag, so it keeps working (hotkey.py).
    _place_key = _cfg.get("place_key", "numpad_decimal")
    if _place_key:
        if hotkey.register_reserved_key(_place_key, place_key_pressed):
            _scan = hotkey.RESERVED_KEYS.get(_place_key, ("?",))[0]
            log("main", f"reserved place key: {_place_key} (scan {_scan})")
        else:
            log_error("main", f"place key {_place_key} could not be reserved; place mode "
                              "is only on the tray and the optional chord until restart")

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
            # Ctrl+Shift+S over an RDP window can leave Shift stuck in the remote
            # session, and every early return below exits without touching inject.
            inject.flush_hotkey_modifiers_async()
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

    # Manual modifier escape hatch (PASTE_UX_PLAN section 6). This is the one
    # RDP fix that cannot be defeated by a wrong detection heuristic, which is
    # exactly why it exists: rounds 1-4 all depended on correctly spotting the
    # RDP window, and the log shows that watcher never fired.
    _unstick_hk = _cfg.get("unstick_hotkey", "ctrl+shift+u")
    if _unstick_hk:
        def _on_unstick_hotkey() -> None:
            threading.Thread(
                target=lambda: preview.show_toast(inject.unstick_modifiers()),
                daemon=True).start()

        try:
            _kb.add_hotkey(_unstick_hk, _on_unstick_hotkey, suppress=False)
            log("main", f"unstick hotkey registered: {_unstick_hk}")
        except Exception as exc:
            log_error("main", f"unstick hotkey failed: {exc}")

    # Persistent RDP modifier flush (PASTE_UX_PLAN section 6). Replaces nothing:
    # the 4s post-hotkey watcher and preview's flush_rdp_if_foreground both stay,
    # because this hook only fires on foreground CHANGES and cannot see a session
    # that holds focus throughout.
    if inject.start_foreground_watch():
        atexit.register(inject.stop_foreground_watch)

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
    # The dashboard hides rather than exits when its window is closed, so it
    # has to be told to go when we do.
    atexit.register(dashboard.shutdown)

    # ------------------------------------------------------------------
    # Dashboard prewarm — boot it hidden once startup has settled, so the
    # first tray click only has to unhide a window that is already built.
    # ------------------------------------------------------------------

    if _cfg.get("dashboard_prewarm", True):
        _prewarm_timer = threading.Timer(6.0, dashboard.prewarm)
        _prewarm_timer.daemon = True
        _prewarm_timer.start()

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
                            "rdp_clipboard_settle_ms",
                            "rdp_clipboard_restore_delay_ms",
                            "vad_filter", "corrections", "silence_auto_stop_seconds",
                            "preview_position", "preview_auto_dismiss_seconds",
                            "auto_paste_threshold", "initial_prompt",
                            "custom_vocabulary", "input_device",
                            "live_preview_enabled", "per_app_context",
                            "retain_audio", "retain_audio_max_files",
                            "retain_audio_min_seconds", "redact_patterns",
                            "tts_speed", "tts_max_chunk_chars",
                            "study_speed", "study_pause_scale",
                            "panel_acrylic", "learn_from_edits",
                            "clipboard_retain_on_unconfirmed",
                            "target_probe", "target_ring"):
                    _cfg[key] = validated[key]
            inject.configure(
                restore_delay_ms=validated["clipboard_restore_delay_ms"],
                per_app_paste=validated.get("per_app_paste", {}),
                electron_paste_method=validated.get("electron_paste_method", "ctrl_v"),
                paste_mode=validated.get("paste_mode", "auto"),
                rdp_clipboard_settle_ms=validated["rdp_clipboard_settle_ms"],
                rdp_clipboard_restore_delay_ms=validated["rdp_clipboard_restore_delay_ms"],
                clipboard_retain_on_unconfirmed=validated["clipboard_retain_on_unconfirmed"],
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
            # Prewarm can be switched on mid-session; switching it off only
            # stops the next startup booting one, it never closes a dashboard
            # the user may be looking at.
            new_prewarm = validated.get("dashboard_prewarm", True)
            if new_prewarm != _cfg.get("dashboard_prewarm"):
                with _cfg_lock:
                    _cfg["dashboard_prewarm"] = new_prewarm
                if new_prewarm:
                    threading.Thread(target=dashboard.prewarm, daemon=True).start()
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
            rdp_clipboard_settle_ms=_cfg["rdp_clipboard_settle_ms"],
            rdp_clipboard_restore_delay_ms=_cfg["rdp_clipboard_restore_delay_ms"],
            clipboard_retain_on_unconfirmed=_cfg.get("clipboard_retain_on_unconfirmed", True),
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
        on_insert_last=insert_last_dictation,
        on_toggle_incognito=_on_toggle_incognito,
        incognito=_cfg.get("incognito", False),
        on_toggle_study_mode=_on_toggle_study_mode,
        study_mode=_cfg.get("study_mode", False),
        on_set_tts_speed=_on_set_tts_speed,
        tts_speed=(_cfg.get("study_speed", 0.95) if _cfg.get("study_mode", False)
                   else _cfg.get("tts_speed", 1.0)),
    )
    # Parked-dictation state drives the tray dot and its first menu item.
    stash.set_change_callback(tray.refresh_stash)
    print("Hold Ctrl+Alt to dictate. Right-click tray icon to quit.")
    tray.run()


if __name__ == "__main__":
    main()
