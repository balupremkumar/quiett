# Voice Dictation — Project Instructions

## Session protocol
At session start, read STATE.md first and trust it; do not re-explore the repo or git history unless STATE.md is missing or contradicts what you find.
Check D:\Dev\ai\handovers\ for any *-to-voice-dictation.md newer than STATE.md and read it.
A Stop hook keeps STATE.md current; when it fires, edit STATE.md in place, tersely.
Ideation lists ("give me 25 improvements") go into BACKLOG.md; read it before generating new ideas, and tick or cull items there.

## What this is
Hold Ctrl+Alt to record, release to transcribe, floating preview panel appears near cursor — edit if needed, then Enter/Insert to paste into any focused app.
Ctrl+Shift+S reads the highlighted text aloud in the cloned voice. Tray → Study Mode switches that same hotkey to a slower, teacher-style delivery that pauses on sentences, paragraphs, lists and headings.
Fully offline, Windows only.
Scope ruled 2026-07-25: dictation in, read-aloud out, nothing else. TaskFlow capture, agent command mode and the Local FitnessPal divert were all removed; do not reintroduce them. No LLM runs in this app any more.

## Stack
- Python + .venv
- whisper.cpp server (third_party/whisper.cpp, Vulkan build, large-v3-turbo-q5_0 model) — NOT faster-whisper
- qwentts.cpp server (third_party/qwentts.cpp, Vulkan build, Qwen3-TTS) for cloned-voice read-aloud, started on first speak and reaped when idle
- sounddevice (audio capture) | keyboard (global hotkey hook) | pyperclip (clipboard)
- pystray (system tray) | Pillow (icons) | pywin32 | Tkinter (preview panel)

## Key files
main.py (wiring) | audio.py | transcribe.py | preview.py | inject.py | hotkey.py | tray.py
tts.py (read-aloud) | narration.py (segments, pauses, rate) | voiceprofile.py | api_server.py | dashboard.py
history.py | config.json | run.bat / launch.vbs | app.log

## Key design decisions
- Tkinter floating panel for edit-before-paste (prevents bad transcriptions landing silently)
- Clipboard injection (more reliable than keyboard simulation across all Windows apps)
- Clipboard save/restore after paste
- Read-aloud synthesises a chunk at a time: the talker accelerates through a long request, chunking holds one pace
- Model servers load lazily and get reaped when idle — never hold VRAM for a feature you are not using
- Admin rights required if global hotkeys not detected (low-level keyboard hook)

## Restarting the app
Use the restart-app skill (.claude/skills/restart-app) — it kills stale processes, relaunches from the correct directory, and confirms the new PID.
Never claim a fix works until the app was restarted this way and the changed flow was exercised.
