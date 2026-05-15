# VoiceDictate

Hold **Ctrl+Alt** to record, release to transcribe. A floating preview panel appears near your cursor — edit if needed, then press **Enter** or click **Insert** to paste into the previously focused application.

## Setup

```
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

On first run the Whisper `small` model (~244 MB) is downloaded automatically to `~/.cache/huggingface`.

## Run (terminal)

```
venv\Scripts\python main.py
```

## Desktop shortcut (run once)

Right-click `create_shortcut.ps1` → **Run with PowerShell**. This places a **VoiceDictate** shortcut on your Desktop that launches the app with no console window via `launch.vbs` + `pythonw.exe`.

Double-click the shortcut to start — a mic icon appears in your system tray.

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
