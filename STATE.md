# STATE — voice-dictation

Seeded 2026-07-03 from the workflow review; verify against reality on first resume, then keep current via the Stop hook.

## Current state
Hold-to-talk dictation: Ctrl+Alt records, whisper.cpp server (large-v3-turbo-q5_0, Vulkan build under third_party/whisper.cpp) transcribes, preview panel, clipboard injection.
TaskFlow integration: trigger phrases on Ctrl+Alt, whole-utterance task capture on Ctrl+Shift+Alt.
Agent command mode (opt-in toggle): LM Studio + Qwen2.5-1.5B-Instruct classifies spoken commands (open app, search, type); model lazy-loads only when the mode is on.
Vibe mode removed 2026-07-01 (de-bloat decision).
Voice profile / cloning work started 2026-07-02: recording samples feature added, profile build in progress.

## Last session changes
2026-07-10: wrote PRODUCTION_PLAN.md — full productisation breakdown (UI polish per surface, brand/icon spec, PyInstaller+Inno installer, licence blockers incl. LM Studio redistribution, execution order P1-P11). Session hit usage limit right after; no code changed.
2026-07-10 (later): 4-agent UI/UX research sweep — 50-item premium revamp list landed in BACKLOG.md (dated heading). Root cause of jagged speech bubble confirmed: CreateRoundRectRgn hard mask (winfx.py:31-32) + no DPI awareness. No code changed.
2026-07-10 (evening): light-mode rework, verified via dashboard screenshot. Fixed `def open` shadowing builtin in dashboard.py (broke ALL dashboard config reads/writes silently — renamed open_window, callers in main.py:974-977). Softened light palettes (dashboard CSS + preview.py _THEMES), fixed 6 hardcoded dark colours in preview.py, theme injected at dashboard launch (no dark flash). Hardened scripts/restart_app.ps1: relative-cmdline instances (stale 9:49am copy held mutex all day), pythonw3.13.exe children, CIM date crash.

## Next steps
- [ ] RESUME HERE: decide popup rendering stack (BACKLOG item 20: patched Tk layered window vs PySide6 vs pywebview) — gates most pop-up revamp items; then execute the convergent cluster (per-pixel-alpha panel, DPI awareness v2, DWM corner quick win).
- [ ] Execute PRODUCTION_PLAN.md P2 (icon set) — merge with BACKLOG items 5, 21, 24 (single-master ICO pipeline, theme-aware monochrome tray). P1 (product name) needs Balu's decision but doesn't block P2.
- [ ] Verify the sticky modifier fix survives a real remote-desktop session.
- [ ] Continue the voice profile / cloning build (multi-sample support, local TTS architecture).
- [ ] Confirm the double-enter insert behaviour is intended or fix it.

## Open bugs
Sticky Ctrl/Shift/Alt after paste when remote desktop + VS Code are involved (reported twice, 2026-07-02); fix unverified.
Titlebar follows Windows theme, not app theme (light app + dark OS = dark titlebar); small DWM call if wanted.
Theme switch still needs app restart for Tk surfaces (BACKLOG item 4).
2026-07-10 changes committed (light-mode fixes, restart script, plan/backlog docs); push to origin not done, ask Balu.

## Key files
main.py (wiring), hotkey.py, inject.py, preview.py, agent.py, llm_client.py, taskflow.py, api_server.py, config.json.
Launch: run.bat / launch.vbs; log: app.log.
