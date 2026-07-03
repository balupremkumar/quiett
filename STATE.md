# STATE — voice-dictation

Seeded 2026-07-03 from the workflow review; verify against reality on first resume, then keep current via the Stop hook.

## Current state
Hold-to-talk dictation: Ctrl+Alt records, whisper.cpp server (large-v3-turbo-q5_0, Vulkan build under third_party/whisper.cpp) transcribes, preview panel, clipboard injection.
TaskFlow integration: trigger phrases on Ctrl+Alt, whole-utterance task capture on Ctrl+Shift+Alt.
Agent command mode (opt-in toggle): LM Studio + Qwen2.5-1.5B-Instruct classifies spoken commands (open app, search, type); model lazy-loads only when the mode is on.
Vibe mode removed 2026-07-01 (de-bloat decision).
Voice profile / cloning work started 2026-07-02: recording samples feature added, profile build in progress.

## Last session changes
Sticky modifier key fix attempted for the remote-desktop scenario (2026-07-02 session); needs verification.

## Next steps
- [ ] Verify the sticky modifier fix survives a real remote-desktop session.
- [ ] Continue the voice profile / cloning build (multi-sample support, local TTS architecture).
- [ ] Confirm the double-enter insert behaviour is intended or fix it.

## Open bugs
Sticky Ctrl/Shift/Alt after paste when remote desktop + VS Code are involved (reported twice, 2026-07-02); fix unverified.

## Key files
main.py (wiring), hotkey.py, inject.py, preview.py, agent.py, llm_client.py, taskflow.py, api_server.py, config.json.
Launch: run.bat / launch.vbs; log: app.log.
