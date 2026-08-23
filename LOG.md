# LOG — Quiett

Session history, appended each session. Never read at session start; [[projects/active/quiett/STATE|STATE]] is the dashboard.

Vault: [[HOME]] | [[PORTFOLIO|Portfolio]] | [[projects/active/quiett/STATE|State]] | [[projects/active/quiett/BACKLOG|Backlog]]

## 2026-08-23 — the place key was pressing itself; terminals get Shift+Insert

Balu's report: the numpad `.` "freaking out", one press showing up multiple times, weird Delete behaviour, and an intermittent line at the top of the screen at recording start.

**Root cause, proven from `app.log` and `history.json`, not guessed.** `inject.py` types via SendInput `KEYEVENTF_UNICODE`, which carries the UTF-16 code unit in `wScan`. `ord("S") == 83`, which is the numpad `.` scan code with no extended flag, so the reserved-key hook matched the app's own typing. It swallowed the S out of the text being inserted, so verification found a mismatch and refused to consume the stash, and it re-fired place mode, which typed the same text again. Self-feeding: 09:57 to 09:58 shows one press produce 14 inserts of the same 902 characters, spaced ~1.1s apart, which is the duration of one inject rather than any typematic rate. Both looping transcripts contained exactly one capital S; the ones that inserted cleanly contained none. Every reservable key had the same hole: 78='N', 74='J', 55='7', 82='R', 69='E'. A test asserted injected events *should* match, so it shipped green.

Fixed in `d3af1e2`: reject `LLKHF_INJECTED`, and fire on the leading edge of a press only. `KBDLLHOOKSTRUCT` has no previous-key-state field, unlike an ordinary `WM_KEYDOWN`, so a held key repeats with no keyup between and the 300ms time debounce was setting the repeat interval rather than stopping it.

**Then the reserved key was removed entirely.** Research across twelve products found no shipped dictation tool reserves an unmodified global key; all ten that insert text auto-insert on release into the HWND captured at hotkey-down. Wispr Flow's hotkey validator refuses single keys outright. Dossier: [[research/2026-08-23-dictation-insert-patterns|insert patterns]]. Balu chose the panel-Enter confirm, which needs no global key at all because the panel already has focus. `hotkey.py` lost 300 lines and gained a comment saying why, so it does not come back.

**The terminal fix, which is the part that matters day to day.** 80-90% of Balu's inserts go into a terminal. Terminals were getting Ctrl+Shift+V, which legacy conhost has no binding for, which is why right-click was the only paste that worked. Now Shift+Insert, which is bound in conhost, Windows Terminal, mintty, ConEmu and Alacritty alike. Verified end to end against a live conhost by reading the console screen buffer back: the marker landed. The control run with the old method typed a literal `^V` into the console. `VK_INSERT` had to be given `KEYEVENTF_EXTENDED` explicitly, because `MapVirtualKey` returns scan 0x52 and scan 0x52 without the extended bit is numpad 0, so it would have typed a literal "0" with NumLock on. The same scan-code shadowing as the original bug.

Tauri windows also routed to clipboard Ctrl+V. Flightdeck's `Tauri Window` class matched no rule and fell through to character typing, failing six inserts in a row before one verified.

**Then the doubled dictations.** Two of Balu's messages arrived with the same 137-character block verbatim, back to back. Not him repeating himself. `verify_landed` graded "same signal before and after" as a confident NO, but a WebView2 host hands UIA a focused element whose value reads "" whichever way the paste went, so every Flightdeck insert compared 0 to 0 and came back failed. Six in a row between 12:08 and 13:34, all `method=ctrl_v`. A false negative is worse than no verdict: it fires the recovery panel, which invites the user to place the same text again, which is exactly what doubled them. Fixed in `1b4db7c`: no possible delta means no signal, not a negative verdict, which is the rule `_read_signal` already applied to a capped text read. An unchanged NON-zero length is still a real failure. Confirmed in Balu's own use at 13:46 and 13:56, with the new diagnostic naming the cause outright: `verify: no readable signal either side (kind=value), claiming nothing`. Messages after that arrived once.

Two lessons compounded here, worth keeping together. Routing Tauri to Ctrl+V was correct but invisible, because the verifier was reporting failure regardless of what the paste did. And the day's two worst bugs were both the same shape: a signal that could not distinguish "no information" from "definitely not", graded as if it could.

**Not solved: the top-of-screen line.** Balu identified it as the full-width 3px bar, which is `_show_edge_flash`. That is a failure fallback only, and all three of its callers write an ERROR first, yet `app.log` holds zero ERROR lines across five days. Nothing in the current code accounts for it. It now logs its own call stack on every fire, so the next occurrence names itself.

528 tests green.

## 2026-08-22 — insert reliability overhaul, rename to Quiett

Balu's report: inserts going missing constantly, the "insert failed" notice rarely appearing, the initial clipboard copy notice never appearing, RDP still behaving as though Ctrl were held.

**Diagnosis came from `app.log` and the code, not from guessing.** Four separate defects:

- `inject_text()` returned `INSERTED` whenever `SendInput` accepted the events, which only means Windows queued them. With no editable element focused the characters went nowhere, then the restore thread put the old clipboard back 150-450ms later and the dictation was destroyed. No notice, because the app believed it had succeeded. This was the real cause of the missing inserts.
- Toasts positioned from `winfo_screenwidth()`, which under Tk on Windows is the primary monitor. On a two-screen setup every failure notice drew on DISPLAY1 regardless of where Balu was.
- Nothing showed where the text would go before it went.
- The RDP flush watcher logged `RDP never took foreground within 4s` on every single hotkey release, i.e. it had never fired once.

**Built** by five Sonnet agents on disjoint file sets, integrated and verified in the main thread. `stash.py`, `targetprobe.py`, conditional clipboard retention, monitor-correct notices, a click-through target ring, a recovery panel, place mode, RDP round 5. 506 tests at that point.

**Two bugs found by verification, not review.** The pre-flight probe ran before `_force_foreground()`, so it described whatever was in front rather than the target. And `winfx`'s `HMONITOR` restype defaulted to `c_int`, truncating the handle on 64-bit, which would have made every `GetMonitorInfoW` fail silently while looking fixed.

**One plan correction mid-build.** The first draft escalated the failure UI on every unconfirmed insert. Wrong: verification returns no signal for terminals, RDP and anything without an accessibility layer, so it would have warned on most successful inserts. Retention plus a tray dot became the silent net, with the loud path reserved for positive evidence of failure.

**Renamed** the folder and the GitHub repo to `quiett`. The folder could not be renamed directly because the session's shell was parked inside it, which pins a directory on Windows; moving the 70 children into a new folder achieved the same result. The empty original is left for next session.

**Place key reworked** after Balu rejected the `shift+alt+z` chord as too much for constant use. Now one unmodified reserved key, default numpad `.`. That key shares base scan code 83 with the main Delete key and differs only by the extended flag, so the gate matches on that with a regression test. `keyboard`'s own `suppress=True` was rejected because it switches the shared hook to blocking mode for every keystroke on the machine.

**Then Balu reported the Delete key sticking and freezing**, and both causes were ours: a thread created inside the hook proc (a low-level keyboard hook blocks all input until it returns, and Windows removes one that overruns 300ms), and an unconditionally swallowed key-up leaving the focused app believing the key was held. Fixed in `412433a`.

**Blank desktop icon** was also ours: the rename repointed the shortcuts' target and working directory but not `IconLocation`.

**What could not be verified.** A protected fullscreen game held the foreground for most of the session. Measured directly: no keyboard hook of any kind receives events in that state, and focus cannot be moved off the game, so the ring, overlay, DISPLAY2 toasts and RDP all remain untested. Two E2E harnesses produced misleading red results before the cause was found. Balu confirmed the numpad `.` key working in real use.

Commits: `5b85fec`, `a9ff082`, `7151d19`, `55b7fbd`, `412433a`. 559 tests green, main pushed.

## 2026-08-23 — insert rebuilt on measured evidence

Balu reported he could not insert into Flightdeck at all and was down to right-click paste, and that the orange recovery panel kept firing on non-terminal fields.
Reproduced end to end by injecting into live windows and reading the result back off a screenshot rather than trusting a status string.

The measured matrix (`ctrl_v` / `type` / `shift_insert`): Notepad exact / MANGLED / -; conhost exact / exact / nothing; Chrome textarea exact / exact / -; Flightdeck xterm.js NOTHING / exact / nothing.

Four separate bugs.
`_set_focus_on_child` destroyed DOM focus inside a WebView2 host, so every keystroke after it went nowhere; the same run with the step removed landed.
Flightdeck's terminal has no Ctrl+V paste binding at all, so `a3339a5` routing Tauri to Ctrl+V killed every insert there. Scan-code, virtual-key, both, and `keybd_event` all pasted nothing.
Windows 11 Notepad silently corrupts typed text ("FIXED notepad ok" arrived as "FIXED kkkkkkkkkk") at every batch size and pace tried; that had been shipping quietly for as long as plain Win32 targets were typed into.
The verifier produced 14 confident false "NOT landed" verdicts, which is what fired the recovery panel repeatedly.

Fixes: routing is per-app override, then RDP `ctrl_v`, then terminals and `Tauri Window` `type`, else `ctrl_v`. The focus-child step is gone.
The clipboard now always holds the last dictation, set before anything is sent, never restored. Balu's ruling, and it replaces the whole retain/restore machinery.
The verifier is advisory: it can promote to INSERTED, never demote. The recovery panel opens on `FAILED` only. A stuck modifier is flushed and the insert proceeds instead of aborting.

Note for the next session: a fullscreen MortalShell2 was reclaiming the foreground mid-test and poisoned the first Notepad run. Check the foreground before trusting a red result, per the standing rule.

526 tests green. App restarted and all four targets re-verified by screenshot after the change.
