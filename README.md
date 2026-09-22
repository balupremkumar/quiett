# Quiett

Quiett is an offline hold-to-talk dictation app for Windows. Hold Ctrl+Alt, speak, release, and the transcript lands in a floating preview panel next to your cursor for a quick edit before it's inserted into whatever window had focus. It also reads text back in a cloned voice on demand. Everything runs locally on the GPU, whisper.cpp for transcription and qwentts.cpp for speech, so nothing you say leaves the machine. The owner dictates every prompt to his AI coding agents through this app, daily.

## Screenshot

No screenshot or demo clip is checked into this repo yet. A full walkthrough video is at [kove.nz/quiett-demo](https://kove.nz/quiett-demo).

## Features

- Hold-to-talk dictation (Ctrl+Alt) via a local whisper.cpp server (large-v3-turbo, GPU via Vulkan), with a floating preview panel to edit before insert
- Read-aloud (Ctrl+Shift+S) speaks the current selection in a cloned voice via a local qwentts.cpp server (GPU via Vulkan); tray Study Mode switches the same hotkey to a slower teacher delivery
- Rule-based transcript cleanup (filler words, punctuation, vocabulary rejoin) before insert
- Per-app insert routing: clipboard paste, keystroke typing, or a terminal-specific path, chosen by what each target application actually accepts
- Clipboard always holds the last dictation; a recovery panel opens only if a send genuinely failed
- Tray icon with live state, scrollable dictation history, and a settings dashboard
- RDP-aware modifier handling, with a manual unstick hotkey (Ctrl+Alt+Shift+U) for stuck modifiers

## Status

Daily driver. Dictation in, read-aloud out is the ruled scope; nothing else is planned. See [STATE.md](STATE.md) for the current session log and [BACKLOG.md](BACKLOG.md) for planned work.

## Tests

526 tests (pytest), covering transcription, insert routing and recovery, the preview panel, hotkeys, history, the dashboard API, target-window probing, TTS, and Win32 window effects.

## Run from source

Requires Python and Windows (hooks and window handling use Win32 APIs). whisper.cpp's `whisper-server.exe` and a ggml model (default large-v3-turbo) must be in place under `third_party/` and `models/`, and a qwentts.cpp build with its GGUF models under `third_party/qwentts.cpp/`. None of these are pip-installable or downloaded automatically.

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
.venv\Scripts\python main.py
```

`pytest` runs the test suite. `create_shortcut.ps1` (run once, right-click, Run with PowerShell) sets up a no-console Desktop shortcut via `launch.vbs` and `pythonw.exe`.

## Architecture

- Transcription runs on a local whisper.cpp server (`whisper-server.exe`, ggml large-v3-turbo model, GPU via Vulkan), started on demand
- Read-aloud runs on a local qwentts.cpp server (`tts-server.exe`, GGUF models, GPU via Vulkan), which starts on first use and unloads when idle
- A global low-level keyboard hook (`hotkey.py`, via the `keyboard` library) detects the Ctrl+Alt hold and drives record start/stop
- `transcribe.py` applies rule-based cleanup to the raw whisper.cpp output: filler removal, punctuation, vocabulary rejoin
- `inject.py` picks an insert path per target window, clipboard paste, keystroke typing, or a terminal-specific path, based on what was measured to actually land in that application
- `dashboard.py` serves a local settings and history UI over `api_server.py`; `preview.py` renders the floating editable transcript panel

## How it was built

The code in this repo is written by AI coding agents, directed and reviewed by Balu Premkumar. He owns the architecture decisions, the code review, and every merge. He also dictates every prompt he sends those agents through this app, so it is in daily use against its own development.

## Licence

MIT, see [LICENSE](LICENSE). Bundled third-party components and their licences are listed in [LICENSES.md](LICENSES.md).

More on this project: [kove.nz/work-voice-dictation](https://kove.nz/work-voice-dictation), demo at [kove.nz/quiett-demo](https://kove.nz/quiett-demo).
