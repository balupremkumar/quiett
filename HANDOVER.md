# Voice Dictation — Handover (UPDATED 2026-05-30)

Status: **all planned improvements applied** in working tree. Nothing committed yet.

## Shipped this session (uncommitted — run `git diff` to review)

### transcribe.py — full rewrite
- GPU auto-detect (CUDA float16 → CPU int8 fallback)
- `initial_prompt` + `custom_vocabulary` support
- `word_timestamps=True`; `run()` returns `(text, confidence, words)`
- Postprocessing: spoken punctuation, number-to-digit, acronym collapse, fixed filler-word boundary regex
- Load retry handled in main; CUDA→CPU auto-fallback inside `load()`

### inject.py — full rewrite (paste reliability)
- `SendInput` + scan codes (replaces legacy `keybd_event`) — fixes RDP, VS Code/Electron, browser HTML inputs
- Class-based settle delays: 400ms for `TscShellContainer`/`RAIL_WINDOW` (RDP), 300ms for `Chrome_WidgetWin` (Electron/Chrome/Edge), 150ms default
- Aggressive modifier flush (Ctrl/Alt/Shift/Win, both sides)
- `SwitchToThisWindow` alongside `SetForegroundWindow`
- Focus-loss detection → WM_PASTE fallback + keystroke retry
- Clipboard restore validates content before restoring (won't clobber user changes)
- Full logging into `app.log`

### audio.py
- `input_device` parameter (None = system default)
- `list_input_devices()` helper for UI
- `silence_threshold` now configurable (was hardcoded 0.01)

### hotkey.py
- `rebind(keys)` for hot-reloadable hotkey changes
- Try/except around `on_start()` resets `_held` if recording fails to start (desync recovery)
- Logger wired

### history.py
- Atomic write via tmp + fsync + `os.replace`

### logger.py (new)
- Rotating `app.log` (512KB × 2)

### main.py
- DPI awareness (`SetProcessDpiAwareness(2)` per-monitor V2)
- Model load retry (3× exponential backoff, CUDA→CPU fallback inside transcribe.load)
- New config fields wired: `initial_prompt`, `custom_vocabulary`, `input_device`, `history_paused`, `silence_threshold`
- Hot-reload extended to all new fields + hotkey rebinding
- `history_paused` short-circuits `history.save`
- Words passed through to preview

### preview.py
- `show()` accepts `words=` param
- Word-level confidence highlighting: words with prob <0.4 tinted red, <0.7 tinted amber, in the editable Text widget
- Settings UI extended with:
  - Hotkey combo field (hot-reloads on save)
  - Initial prompt textarea
  - Custom vocabulary textarea (one per line)
  - Mic dropdown (populated from `audio.list_input_devices()`)
  - Silence threshold field
  - "Pause history" checkbox under Privacy section
- Settings window grew to 520×780 to fit new fields

### config.json
- New defaults: `initial_prompt: ""`, `custom_vocabulary: []`, `input_device: null`, `history_paused: false`, `silence_threshold: 0.01`

## How to test (golden path)

```
cd "C:\AI\projects\Voice Dictation"
python main.py
```

1. Hotkey: Hold Ctrl+Alt, say "test one two three period new line second line", release → preview shows "Test 1 2 3.\nSecond line"
2. Paste reliability: try VS Code, an HTML input, an RDP session (mstsc) → all three should accept the paste
3. Settings UI: right-click tray → Settings → verify new sections (Hotkey, Whisper biasing, Microphone, Privacy, Silence threshold)
4. Hotkey rebind: change to `ctrl+shift` in settings → Save → no restart needed
5. Word confidence: low-quality audio should show red/amber tinted words in preview
6. Mic switch: select different device → Save → next recording uses that device
7. Pause history: enable → record → confirm history.json doesn't grow
8. Check `app.log` for traces of each above

## Not done / intentionally deferred

- **Streaming partial transcription** — bigger lift, requires audio chunking and live UI updates. Defer to future session.
- **PyInstaller exe bundling** — defer until features stabilize.
- **First-run onboarding wizard** — current settings UI is good enough as a first-run experience; user can opt in later.
- **Encrypted history (DPAPI)** — "pause history" toggle covers the immediate privacy concern.
- **Model size auto-pick by RAM** — defer; user knows their machine.
- **Cloud sync** — out of scope per original brief (offline-first).
- **Dictation editing commands** ("scratch that", "delete word") — defer; preview-edit is fast enough for now.

## Known gotchas

- **faster-whisper CUDA**: requires CUDA 12 runtime DLLs (`cudart64_12.dll`) on PATH. Falls back to CPU automatically.
- **Hotkey rebind from settings**: takes effect within 30s (hot-reload interval). If user wants instant, restart.
- **Word-confidence highlighting** uses literal text search; if the same word appears multiple times with different confidences, only the first match is tinted. Acceptable trade-off.
- **`_words_to_digits`** is conservative — single-token "two cats" won't convert. By design (avoids over-conversion).
- **DPI awareness**: applied at startup. If the user moves the window across monitors with different scaling, Tk may not re-scale perfectly. Per-monitor V2 is best-effort.

## File-by-file diff size summary

| File | Status |
|---|---|
| main.py | extensively modified (DPI, retry, new fields, hot-reload, words passthrough) |
| transcribe.py | rewritten |
| inject.py | rewritten (SendInput + scan codes) |
| audio.py | new param + helper |
| hotkey.py | rebind + recovery |
| preview.py | settings UI expanded + word-conf tags + show() signature |
| history.py | atomic write |
| logger.py | new file |
| config.json | new defaults |

Memory entry: `C:\Users\balup\.claude\projects\C--AI-projects-Voice-Dictation\memory\handover-2026-05-30.md`
