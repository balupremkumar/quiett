# STATE — Quiett

Vault: [[HOME]] | [[PORTFOLIO|Portfolio]] | [[projects/active/quiett/BACKLOG|Backlog]] | [[projects/active/quiett/LOG|Log]]

Dashboard only. Session history lives in LOG.md and is never read at session start.
Renamed from voice-dictation on 2026-08-22; folder is `D:\Dev\ai\projects\active\quiett`, repo is `balupremkumar/quiett`.

## Current state

Hold-to-talk dictation: Ctrl+Alt records, whisper.cpp (large-v3-turbo-q5_0, Vulkan) transcribes, preview panel, then injection.
Read-aloud: Ctrl+Shift+S speaks the selection in the cloned voice (qwentts.cpp, Vulkan). Tray Study Mode switches the same hotkey to a slower teacher delivery.
Scope ruled 2026-07-25: dictation in, read-aloud out, nothing else. No LLM runs in this app.

Insert reliability overhaul shipped 2026-08-22, then largely superseded on 2026-08-23 (see below): verification no longer decides anything the user can see. `targetprobe.py` still decides whether a field can receive text (Win32 caret, then UI Automation) and a confident "no field" refuses the insert rather than typing into nothing. Notices draw on the monitor the target is on. Design record: `PASTE_UX_PLAN.md`, now out of date on the verification and clipboard sections.

**The reserved place key is REMOVED** (2026-08-23). It was pressing itself: `inject.py` types via SendInput `KEYEVENTF_UNICODE`, which puts the UTF-16 code unit in `wScan`, and `ord("S") == 83` is the numpad `.` scan code. Every capital S in a dictation was swallowed out of the insert, so verification failed and the stash stayed unconsumed, then re-fired place mode, which typed the same text again. One press, 14 inserts. Do not reinstate an unmodified global key: no shipped dictation tool has one ([[research/2026-08-23-dictation-insert-patterns|dossier]]). The confirm gesture is Enter or Insert inside the preview panel, which already has focus.
Place mode keeps the tray `Place last dictation` item, the recovery panel, the optional `place_hotkey` chord and click-to-place.

**Insert rebuilt around measured evidence, not verdicts** (2026-08-23).
Balu could not insert into Flightdeck at all and was down to right-click paste; it turned out not to be a Flightdeck problem alone.
Every claim below was checked by injecting into a live window and reading the result back off a screenshot.

Measured matrix (`ctrl_v` / `type` / `shift_insert`):
Notepad (Win11 WinUI) exact / MANGLED / -.
conhost (cmd.exe) exact / exact / nothing.
Chrome textarea exact / exact / -.
Flightdeck xterm.js (Tauri) NOTHING / exact / nothing.

Four bugs came out of it.
`_set_focus_on_child` (AttachThreadInput + SetFocus on `Chrome_RenderWidgetHostHWND`) DESTROYED DOM focus inside a WebView2 host: typing with that step landed nothing, the identical run without it landed. Removed.
Flightdeck's terminal has no Ctrl+V paste binding at all, so routing Tauri to Ctrl+V on 2026-08-23 (`a3339a5`) killed every insert there. Scan-code events, virtual-key events, both together and `keybd_event` all pasted nothing; only typing lands.
Windows 11 Notepad SILENTLY CORRUPTS typed text: "FIXED notepad ok" arrived as "FIXED kkkkkkkkkk". Its WinUI/TSF stack resolves each VK_PACKET against the current async key state, so a fast burst collapses to the last character. Every batch size and pace tried (32/8/4/1 chars per SendInput, 0-5ms apart) mangled it. That bug had been shipping quietly for as long as plain Win32 targets were typed into.
The verifier produced 14 confident "NOT landed" verdicts on inserts that had landed, which is what fired the orange recovery panel repeatedly.

Routing is now: per-app override, then RDP -> `ctrl_v`, then terminals and `Tauri Window` -> `type`, everything else -> `ctrl_v`.
`Shift+Insert` and `Ctrl+Shift+V` are gone from the routing; neither works in conhost (the latter types a literal `^V`).

**The clipboard always holds the last dictation** (2026-08-23). Ruled by Balu, and it replaces the whole save/restore/retain machinery.
Text goes on the clipboard BEFORE anything is sent, on every path, and the previous clipboard never comes back. `should_restore_clipboard` returns False unconditionally.
The verifier is advisory now: it can promote a status to INSERTED, never demote one. No verdict puts anything on screen.
The recovery panel opens on `FAILED` only, meaning nothing was sent AND the clipboard copy failed too, so the panel holds the only copy. A refused send is a toast plus the tray dot.
A modifier still held after 1.5s is flushed and the insert proceeds, instead of aborting into the recovery panel.
An insert that was actually sent consumes the stash, confirmed or not, so the tray dot means something again.

**Doubled dictations fixed** (2026-08-23, `1b4db7c`). Two of Balu's messages arrived with the same 137-char block verbatim twice. `verify_landed` graded "same signal before and after" as a confident NO, but a WebView2 host hands UIA an element whose value reads "" whichever way the paste went, so every Flightdeck insert compared 0 to 0 and was reported failed. That fired the recovery panel, Balu placed the text again, and it landed twice. No possible delta now means no signal, not a negative verdict; an unchanged NON-zero length is still a real failure. Every non-yes verdict logs the numbers and signal kind it saw.

Also on: `ctrl+shift+u` unstick modifiers, tray `Unstick modifiers`.

526 tests green. Not yet pushed.

## Next steps

- [ ] Delete the empty `D:\Dev\ai\projects\active\voice-dictation` folder. It survived the rename because a shell was parked inside it, which pins a directory on Windows.
- [ ] Confirm the Stop hook's `state_check.py` still fires after the rename; it may point at the old path.
- [ ] **Voice-test the new routing.** The matrix was proven by injecting into live windows, but not yet by actually dictating. Dictate into Flightdeck's terminal, a plain terminal, Notepad and a browser field; each should land once with no orange panel, and the transcript should still be on the clipboard afterwards.
- [ ] **Flightdeck should bind Ctrl+V to paste in its terminal.** It answers no paste keystroke at all today, which is why right-click was the only way. Quiett works around it by typing, but every other tool that pastes into that terminal will hit the same wall.
- [ ] Windows 11 Notepad's typed-text corruption is worked around, not fixed. If a future target needs typing AND uses WinUI/TSF, the same collapse will appear; the only lever left is a pace far too slow for a real dictation.
- [ ] Hook watchdog. Windows silently removes a low-level hook that overruns `LowLevelHooksTimeout` and never tells the app. The reserved-key hook is gone but the hold-to-record path still uses the `keyboard` library's shared hook, so the failure mode survives.
- [ ] Consider Handy's tunable paste delay (60ms there). The clipboard-restore toggle is now moot: the restore is gone.
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
