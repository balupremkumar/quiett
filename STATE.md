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
2026-07-11: UI polish build (ROI rows 1-11) committed as 9c7d2b2 after Balu's OK. Batch 2 built via two Sonnet frontend agents and verified: dashboard (36 history search, 81 day headers, 64 geometry persist — save+restore tested E2E via window move, 77 About page, new VERSION "0.1.0" constant, confirm scheme with Balu) and popup/tray (15 DWM shadow, 11 badge status row, 22 tray left-click default, 23 grouped menu, 69 timed pause; 60 key chips found already done). App restarted clean (PID 14916), About + History verified by screenshot in dark theme. Committed d4194a4. Batch 3 done and verified: history (82 pin, 83 chips, 84 bulk select, 85 highlight, 87 retention) + popup (10 long-recording gate, 17 real gradient, 46 edge flash fallback-only, 53 countdown bar, 55 pin button, 57 WPM footer). history.py diff eyeballed (clean, additive), source="agent" tag added at main.py:464, app restarted clean (PID 18844), History chips/Select UI screenshot-verified dark theme. Committed 044b653. Batch 4 done: tray (25 ICO verified clean + --verify flag, 26 pulse already coherent, 68 recent-dictations submenu, 70 mic quick-picker) + popup (7 continuous waveform, 8 hover stop/cancel, 18 slide+fade entrance with animations:false kill-switch, 54 drag reposition per monitor). App restarted clean (PID 3620), no log errors. config.json drift = dashboard_window live state, not a bug.
2026-07-10 (evening): light-mode rework, verified via dashboard screenshot. Fixed `def open` shadowing builtin in dashboard.py (broke ALL dashboard config reads/writes silently — renamed open_window, callers in main.py:974-977). Softened light palettes (dashboard CSS + preview.py _THEMES), fixed 6 hardcoded dark colours in preview.py, theme injected at dashboard launch (no dark flash). Hardened scripts/restart_app.ps1: relative-cmdline instances (stale 9:49am copy held mutex all day), pythonw3.13.exe children, CIM date crash.

## Next steps
- [ ] Balu hands-on checks, batches 2-4: dictate once (waveform, badge status row, hover stop/cancel, slide+fade, WPM footer, countdown bar + pin), drag the panel header and reopen, tray right-click (grouped menu, Recent Dictations, Pause submenu, More → Microphone), tray left-click opens dashboard, history chips/pin/bulk select, >30s recording confirm.
- [ ] Design call to confirm: saved drag position overrides `_preview_position` fixed modes unconditionally (BACKLOG 54 note).
- [ ] Confirm VERSION scheme ("0.1.0" was a judgment call) before it ships anywhere.
- [ ] 6 commits unpushed to origin; ask Balu about a push.
- [ ] BACKLOG item 20 (popup stack decision) now also owns the deferred ULW badge; only matters if product targets Win10 or wants soft shadows.
- [ ] PRODUCTION_PLAN.md P2 (desktop icon design) — pipeline exists now (make_icons.py); P1 product name still needs Balu.
- [ ] Verify the sticky modifier fix survives a real remote-desktop session.
- [ ] Continue the voice profile / cloning build (multi-sample support, local TTS architecture).
- [ ] Confirm the double-enter insert behaviour is intended or fix it.

## Open bugs
Sticky Ctrl/Shift/Alt after paste when remote desktop + VS Code are involved (reported twice, 2026-07-02); fix unverified.
Titlebar follows Windows theme, not app theme (light app + dark OS = dark titlebar); small DWM call if wanted.
Theme switch still needs app restart for Tk surfaces (BACKLOG item 4).
2026-07-10 changes committed in 4 commits, 3aa16a0..b033298 (light-mode fixes, restart script, config theme=dark via fixed save path, plan/backlog docs incl. items 51-100); push to origin not done, ask Balu.
Top-25 ROI ranking given in chat 2026-07-10 evening: quick wins first = DPI awareness, DWM corner pref, target-app indicator (58), instant-apply, autostart, titlebar sync (63).
2026-07-10 (night): built UI_POLISH_PLAN.md items (ROI rows 1-11, Balu-approved scope): DPI awareness v2, DWM rounded corners (fixed the jagged bubble — verified 3x zoom), titlebar theme sync, instant-apply settings (Save button gone), autostart toggle, target-app "→ VS Code" indicator + status dot on panel, live theme sync incl. theme:"system", monochrome theme-aware tray icon + scripts/make_icons.py → assets/. M5 ULW badge descoped (DWM already fixed edges; see plan + BACKLOG 1/20). App restarted + verified per milestone. Committed 2026-07-11 as 9c7d2b2.

## Key files
main.py (wiring), hotkey.py, inject.py, preview.py, agent.py, llm_client.py, taskflow.py, api_server.py, config.json.
Launch: run.bat / launch.vbs; log: app.log.
