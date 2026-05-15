# Voice Dictation — Project Instructions

## What this is
Hold Ctrl+Alt to record, release to transcribe via Whisper small model. Floating preview panel
appears near cursor — edit if needed, then Enter/Insert to paste into any focused app.
Fully offline, Windows only.

## Stack
- Python + venv
- faster-whisper (Whisper small, ~244 MB, auto-downloads to ~/.cache/huggingface)
- sounddevice (audio capture) | keyboard (global hotkey hook) | pyperclip (clipboard)
- pystray (system tray) | Pillow (icons) | pywin32 | Tkinter (preview panel)

## Key files
main.py | audio.py | transcribe.py | preview.py (Tkinter) | inject.py | hotkey.py | tray.py
history.py | config.json | create_shortcut.ps1

## Key design decisions
- Tkinter floating panel for edit-before-paste (prevents bad transcriptions landing silently)
- Clipboard injection (more reliable than keyboard simulation across all Windows apps)
- Clipboard save/restore after paste (150ms delay)
- Filler word removal: ["um", "uh", "you know", "like"] — configurable in config.json
- Admin rights required if global hotkeys not detected (low-level keyboard hook)

## Hotkey
Hold Ctrl+Alt → record (high beep). Release → transcribe (low beep) → preview → Enter/Insert to paste.

## Context — read at session start
Read C:\AI\Wiki\Balu-Wiki\wiki\technical\voice-dictation.md for architecture and open questions.

## Codebase navigation (Graphify)
3-layer query rule:
1. Query graphify-out/graph.json first — use /graphify query "your question"
2. Read C:\AI\Wiki\Balu-Wiki\wiki\technical\voice-dictation.md for decisions/context
3. Only read raw source files when editing or when layers 1-2 don't have the answer

Rebuild after structural changes: /graphify . --update
