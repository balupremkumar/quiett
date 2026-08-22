# STATE — Quiett

Vault: [[HOME]] | [[PORTFOLIO|Portfolio]] | [[projects/active/quiett/BACKLOG|Backlog]] | [[projects/active/quiett/LOG|Log]]

Dashboard only. Session history lives in LOG.md and is never read at session start.
Renamed from voice-dictation on 2026-08-22; folder is `D:\Dev\ai\projects\active\quiett`, repo is `balupremkumar/quiett`.

## Current state

Hold-to-talk dictation: Ctrl+Alt records, whisper.cpp (large-v3-turbo-q5_0, Vulkan) transcribes, preview panel, then injection.
Read-aloud: Ctrl+Shift+S speaks the selection in the cloned voice (qwentts.cpp, Vulkan). Tray Study Mode switches the same hotkey to a slower teacher delivery.
Scope ruled 2026-07-25: dictation in, read-aloud out, nothing else. No LLM runs in this app.

Insert reliability overhaul shipped 2026-08-22. An insert is only reported as landed when it is verified; otherwise the dictation stays on the clipboard and parked in `stash.py` with a tray dot, so it is never destroyed. `targetprobe.py` decides whether a field can receive text (Win32 caret, then UI Automation) and a confident "no field" refuses the insert rather than typing into nothing. Notices now draw on the monitor the target is on. Design record: `PASTE_UX_PLAN.md`.

**Place key: DISABLED 2026-08-23** (`place_key: ""`). The reserved numpad `.` was pressing itself: `inject.py` types via SendInput `KEYEVENTF_UNICODE`, which puts the UTF-16 code unit in `wScan`, and `ord("S") == 83` is the numpad `.` scan code. Every capital S in a dictation was swallowed out of the insert (so verification failed and the stash stayed unconsumed) and re-fired place mode, which typed the same text again. One press, 14 inserts. Fixed in `d3af1e2` and the key left off pending the redesign below.
Place mode still reaches the tray `Place last dictation` item and the recovery panel.
Also on: `ctrl+shift+u` unstick modifiers, tray `Unstick modifiers`.

563 tests green. main in sync, `d3af1e2` not yet pushed.

## Next steps

- [ ] Delete the empty `D:\Dev\ai\projects\active\voice-dictation` folder. It survived the rename because a shell was parked inside it, which pins a directory on Windows.
- [ ] Confirm the Stop hook's `state_check.py` still fires after the rename; it may point at the old path.
- [ ] Decide the confirm-gesture redesign (research dossier: `D:\Deviesearch6-08-23-dictation-insert-patterns.md`). No shipped dictation tool reserves an unmodified global key; all 10 surveyed auto-insert on release into the HWND captured at hotkey-down. Options in preference order: confirm with Enter inside the already-focused preview panel (kills the global-key problem entirely), or `RegisterHotKey` + `MOD_NOREPEAT` on a chord (the OS de-duplicates auto-repeat, so a storm is impossible by construction). Do not re-enable the low-level hook for this.
- [ ] Settle the gap ledger item that needs hardware: does `RegisterHotKey` suppress the key from the foreground app? 20-line script, Notepad, see if a `.` appears.
- [ ] Hook watchdog. Windows silently removes a low-level hook that overruns `LowLevelHooksTimeout` and never tells the app. That is a stuck-key generator on the hold-to-record hook too, which is staying.
- [ ] Hands-on, needs no fullscreen game running: target ring and armed overlay render, are click-through, correct at 125% DPI on the second monitor; a toast lands on DISPLAY2; RDP behaviour.
- [ ] Five ambiguous mistranscriptions under Open bugs need Balu's ear before they become corrections.
- [ ] Balu to answer the 3 open questions at the end of `STUDY_MODE_PLAN.md` before Study Mode P3 (transport controls).
- [ ] PRODUCTION_PLAN P2: desktop icon design. Pipeline exists (`scripts/make_icons.py`). Accent discrepancy still unresolved: kove.nz showcase runs violet, the shipped app is cyan ion, no ruling covers it.
- [ ] BACKLOG 51 (streaming transcript) and 72 (diagnostics page) are in scope but not started.

## Open bugs

**RDP sticky Ctrl/Shift/Alt — the weakest area, rounds 1-5 all in.** Round 5 (2026-08-22) replaced the 4s post-hotkey watcher, which `app.log` proved had never fired once, with a persistent `EVENT_SYSTEM_FOREGROUND` hook. Key-ups only, never downs: the 2026-08-12 tap experiment made it dramatically worse and stays reverted. The hook only fires on foreground CHANGES, so if mstsc holds focus throughout and drops the key-up internally, nothing sees it and only the manual `ctrl+shift+u` helps. The decline log now names the foreground exe, class and title, so the next occurrence should identify the cause instead of costing another guessing round.

**The top-of-screen hairline at recording start is unexplained.** Balu identified it as the full-width 3px bar at the top edge, which is `_show_edge_flash` in `preview.py`. That is a FAILURE fallback only (item 46 removed the unconditional fire), and all three of its callers log an ERROR first, yet `app.log` holds zero ERROR lines across five days. So no known caller accounts for it. `_show_edge_flash` now logs its own call stack on every fire, so the next occurrence names itself instead of costing another guessing round.

**Keyboard hooks are dead while a protected fullscreen game is foreground.** Measured 2026-08-22, not a regression: a raw `WH_KEYBOARD_LL` hook with a valid handle and live pump received zero events, the `keyboard` library saw zero, and focus could not be moved off the game. Ctrl+Alt record and the numpad key are both inert in that state. Check the foreground window before treating hotkey or focus test failures as code bugs.

Ambiguous mistranscriptions left uncorrected pending Balu's ear: "the SAP will be tested", "you're going to get some wet errors", "the X-Men Eyes models that we have", "Heesh" as a whole utterance, "bench, rear squat".
The vocabulary rejoin capitalises an ordinary "share point" into "SharePoint". Accepted trade.
Titlebar follows the Windows theme, not the app theme. Small DWM call if wanted.
Theme switching still needs an app restart for the Tk surfaces (BACKLOG item 4).

## Key files

`main.py` (wiring) | `hotkey.py` | `inject.py` | `preview.py` | `stash.py` | `targetprobe.py` | `winfx.py` | `tray.py`
`tts.py` | `narration.py` | `history.py` | `api_server.py` | `dashboard.py` | `config.json`
Launch: `run.bat` / `launch.vbs`. Log: `app.log`. Restart: the `restart-app` skill, never by launching directly.
Diagnostic: `scripts/probe_target.py` prints what the target probe sees for the focused window.
