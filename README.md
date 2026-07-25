# VoiceDictate

Hold **Ctrl+Alt** to record, release to transcribe. A floating preview panel appears near your cursor — edit if needed, then press **Enter** or click **Insert** to paste into the previously focused application.

## Setup

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Transcription runs on a local **whisper.cpp** server (`whisper-server.exe`, GPU via Vulkan)
using a ggml model in `models/` (default `large-v3-turbo`). The server binary lives in
`third_party/` and the model `.bin` in `models/` — these are not pip-installable and are not
downloaded automatically; they must be present before first run.

Read-aloud (Ctrl+Shift+S speaks the highlighted text in your cloned voice) runs on a local
**qwentts.cpp** server (`tts-server.exe`, GPU via Vulkan) with its GGUF models under
`third_party/qwentts.cpp/`. It starts on first use and unloads itself when idle.

## Run (terminal)

```
.venv\Scripts\python main.py
```

## Run with no console window / Desktop shortcut (run once)

Right-click `create_shortcut.ps1` → **Run with PowerShell**. This places a **VoiceDictate** shortcut
on your Desktop (with the app icon) that launches with **no console window** via `launch.vbs` +
`pythonw.exe`. `run.bat` also launches without a persistent console.

Double-click the shortcut to start — the app runs in the background and shows a mic icon in your
system tray.

## Administrator note

The `keyboard` library installs a global low-level keyboard hook. If hotkeys are not detected, run as Administrator:

```
# Right-click terminal → "Run as administrator"
venv\Scripts\python main.py
```

## Usage

| Action | Behaviour |
|---|---|
| Hold Ctrl+Alt | Start recording (high beep) |
| Release Ctrl+Alt | Stop and transcribe (low beep) |
| Enter / Insert | Paste into previously focused window |
| Esc / Cancel | Dismiss preview, do nothing |
| Append checkbox | Prepend a space and append to existing content |
| Tray → View History | Open scrollable history of past dictations |
| Tray → Quit | Exit app |

Recordings shorter than 0.5 seconds are silently discarded.  
History is stored in `history.json` (last 100 entries).
