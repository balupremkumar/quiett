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

**Not solved: the top-of-screen line.** Balu identified it as the full-width 3px bar, which is `_show_edge_flash`. That is a failure fallback only, and all three of its callers write an ERROR first, yet `app.log` holds zero ERROR lines across five days. Nothing in the current code accounts for it. It now logs its own call stack on every fire, so the next occurrence names itself.

524 tests green.

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
