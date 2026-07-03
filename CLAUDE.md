# Voice Dictation — Project Instructions

## Session protocol
At session start, read STATE.md first and trust it; do not re-explore the repo or git history unless STATE.md is missing or contradicts what you find.
Check D:\Dev\ai\handovers\ for any *-to-voice-dictation.md newer than STATE.md and read it.
A Stop hook keeps STATE.md current; when it fires, edit STATE.md in place, tersely.
Ideation lists ("give me 25 improvements") go into BACKLOG.md; read it before generating new ideas, and tick or cull items there.
When work here affects TaskFlow (to-do-app), write D:\Dev\ai\handovers\voice-dictation-to-to-do-app.md instead of printing a handover in chat.

## What this is
Hold Ctrl+Alt to record, release to transcribe, floating preview panel appears near cursor — edit if needed, then Enter/Insert to paste into any focused app.
Hold Ctrl+Shift+Alt instead to capture the whole utterance as a TaskFlow task.
Fully offline, Windows only.

## Stack
- Python + .venv
- whisper.cpp server (third_party/whisper.cpp, Vulkan build, large-v3-turbo-q5_0 model) — NOT faster-whisper
- LM Studio + Qwen2.5-1.5B-Instruct for agent command mode (lazy-loaded only when the toggle is on)
- sounddevice (audio capture) | keyboard (global hotkey hook) | pyperclip (clipboard)
- pystray (system tray) | Pillow (icons) | pywin32 | Tkinter (preview panel)

## Key files
main.py (wiring) | audio.py | transcribe.py | preview.py | inject.py | hotkey.py | tray.py
agent.py (command mode) | llm_client.py | taskflow.py | api_server.py | dashboard.py
history.py | config.json | run.bat / launch.vbs | app.log

## Key design decisions
- Tkinter floating panel for edit-before-paste (prevents bad transcriptions landing silently)
- Clipboard injection (more reliable than keyboard simulation across all Windows apps)
- Clipboard save/restore after paste
- Local LLMs load only on explicit toggle — never hold VRAM idle
- Admin rights required if global hotkeys not detected (low-level keyboard hook)

## Restarting the app
Use the restart-app skill (.claude/skills/restart-app) — it kills stale processes, relaunches from the correct directory, and confirms the new PID.
Never claim a fix works until the app was restarted this way and the changed flow was exercised.
