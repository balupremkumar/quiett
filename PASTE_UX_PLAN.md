# Paste UX plan — stop losing dictations

Written 2026-08-22.
Supersedes nothing; RDP_FIX_PLAN.md rounds 1-4 stay as history, section 6 here is round 5.
Scope: insert reliability, insert failure UX, target visibility, RDP modifier latching. No new features beyond those.

## 0. What is actually wrong

Four separate defects, found from `app.log` and the code, not from guessing.

**D1 — silent failure reported as success.**
`inject.py:1068 inject_text()` returns `INSERTED` whenever `SendInput` accepts the events.
`_send_unicode_text` returning 1257 and `_send_keystroke` returning a non-zero count both mean only "Windows queued these events", never "the target consumed them".
If no editable element had focus, the characters go nowhere.
Then `_restore()` (inject.py:1221) puts the previous clipboard back 150-450ms later, so the dictation is destroyed.
No toast fires, because the code believes it succeeded.
This is the primary cause of "I'm missing the inserts all the time".

**D2 — notifications land on a monitor you are not looking at.**
`preview.py:609-611` computes toast position from `winfo_screenwidth()` / `winfo_screenheight()`, which under Tk on Windows is the **primary** display, not the virtual desktop.
`config.json` carries `panel_position` entries for `\\.\DISPLAY1` and `\\.\DISPLAY2`, so this is a multi-monitor machine.
Every toast, including the clipboard-fallback notice and the "Insert again" retry, is drawn bottom-right of DISPLAY1 whatever screen the user is on.
A full-screen RDP or maximised window on that display also covers it, since the toast is `-topmost` but so is a full-screen client.

**D3 — no way to tell where the text will go.**
Nothing in the app shows the resolved target before or during recording.
The user finds out only afterwards, by whether text appeared.
A terminal showing a prompt line looks identical whether or not that pane holds keyboard focus.

**D4 — RDP modifier latch, still live.**
Every hotkey release since the last restart logs `hotkey ended — RDP never took foreground within 4s, no flush needed`.
The 4s post-hotkey watcher in `_flush_hotkey_modifiers` (inject.py:434) is therefore never firing in practice, yet Ctrl still latches in the remote session and turns scrolling into zooming.
Client confirmed as mstsc, which `_is_rdp` (inject.py:727) already matches, so the miss is the **window**, not the detection: the latch survives past 4s, or happens on a path with no watcher at all.

## 1. Design principle

Ordered by how much it is trusted, because each layer fails on some app:

1. **Never lose the text.** Works everywhere, no detection required. This alone removes the worst symptom.
2. **Tell the user, on the screen they are looking at.** Works everywhere.
3. **Give a one-action recovery.** Works everywhere.
4. **Predict the target before inserting.** Best effort. Good on Chrome, Electron, WinUI, standard Win32; blind on Tk, canvas apps, and anything inside an RDP session.
5. **Verify after inserting.** Best effort, same coverage as 4.

Layers 4 and 5 are never allowed to block an insert on their own uncertainty.
A `NOT_EDITABLE` verdict only stops the injection when the probe is confident; `UNKNOWN` always attempts the insert.
This is the rule that keeps the change from making working apps worse.

## 2. Never lose the text (P1)

**Clipboard retention becomes conditional.**
`_restore()` currently runs unconditionally on a timer.
It may only run when the insert is **confirmed** landed (section 5) or the target probe returned a confident `EDITABLE`.
On `UNKNOWN` with no confirmation, and on every failure path, the dictated text stays on the clipboard and the previous contents are dropped.
This matches what Wispr Flow does and is the single highest-value change in this plan.

**A stash that outlives the clipboard.**
New module `stash.py`: holds the last dictation, its intended target hwnd, timestamp, and a `consumed` flag.
Populated on every transcription, cleared when an insert is confirmed or the user dismisses it.
Survives the clipboard being overwritten by something else, which the clipboard alone does not.
History already persists transcripts (`history.py`), so the stash is in-memory only and deliberately holds exactly one item.

**Tray reflects it.**
While a stash item is unconsumed the tray icon carries a dot and the menu's first item becomes `Place last dictation` with the first ~40 characters inline.

## 3. Notifications you actually see (P1)

**Monitor-correct positioning.**
New helper in `winfx.py`: `work_area_for_point(x, y)` and `work_area_for_window(hwnd)` over `MonitorFromPoint` / `MonitorFromWindow` + `GetMonitorInfoW`, returning the DPI-correct work area.
`_show_anchored_toast` anchors to the work area of the **target window**, falling back to the work area under the **cursor**, and only then to the primary display.
`_toast_offset` becomes per-monitor so stacking still works.
`_show_edge_flash` gets the same treatment; it is currently primary-only too.

**Escalation for a lost insert.**
A failed or unconfirmed insert no longer settles for a corner toast.
It reopens the preview panel at the cursor with the text, a `Place it` button and a `Dismiss` button.
The preview panel already positions at the cursor (`preview_position: "cursor"`), so it is on the right screen by construction, and it restores the edit-before-paste affordance at the moment it is most useful.
The corner toast stays for informational notices only.

**Audible cue on a real miss.**
`chime.play_error()` currently fires only for `kind="error"`.
Insert failure is raised to that class so a missed insert is heard, not just drawn.

## 4. Place mode — arm, click, insert (P1)

The requested "hold a key, pick a field, paste" flow, built as arm-then-click.
Chosen over hold-and-release deliberately: holding a modifier over RDP is the same mechanism that latches Ctrl remotely, so the fix must not depend on it.

New hotkey, default `ctrl+alt+v`, config key `place_hotkey`.

- Press once with a stash item present: enter armed state.
  An overlay near the cursor shows `ARMED`, the first line of the parked text, `Click the field to insert`, `Esc to cancel`.
  A target ring (section 5) follows focus as the user clicks around.
- The next click into any window resolves the newly focused element, waits for focus to settle, inserts there, then disarms.
- `Esc`, a second press of the hotkey, or 30 seconds of inactivity disarms.
- Pressing it with no stash item shows `Nothing to place yet`.

Implementation note: arming installs a low-level mouse hook via the existing `keyboard` library's companion or a `SetWindowsHookEx` WH_MOUSE_LL in `hotkey.py`, used only while armed and removed on disarm, so there is no permanent cost.

## 5. Knowing and showing the target (P2)

**New module `targetprobe.py`.**
One entry point, `probe(hwnd) -> Target`, returning `verdict` (`EDITABLE` / `NOT_EDITABLE` / `UNKNOWN`), `rect`, `label`, and `kind`.
Runs on a worker thread with a hard 250ms budget; a timeout yields `UNKNOWN`.
Layers, first confident answer wins:

1. `GetGUIThreadInfo` on the foreground thread. `hwndCaret` non-zero with a non-empty `rcCaret` is a real Win32 caret: confident `EDITABLE`, and `rcCaret` gives an exact position.
2. UI Automation `GetFocusedElement` via `comtypes` (already a dependency through pywebview, no new package). Read `ControlType`, `IsKeyboardFocusable`, `IsEnabled`, `IsPassword`, `IsTextPatternAvailable`, `IsValuePatternAvailable`, `ValueIsReadOnly`, `BoundingRectangle`.
   Edit / Document / ComboBox with an editable text or value pattern is confident `EDITABLE`.
   A Pane or Document that is read-only and not keyboard-focusable, **in a process known to expose UIA properly** (chrome/msedge/electron/WinUI/standard Win32), is confident `NOT_EDITABLE`.
   Measured on this machine: UIA one-off init 463ms, `GetFocusedElement` plus all properties 5-32ms. Init happens once at startup on a background thread so it never sits in the insert path.
3. Terminal classes from the existing `_TERMINAL_CLASSES` list: `EDITABLE`, rect = window rect.
4. RDP window: `UNKNOWN` by definition, with `label = "Remote session — Quiett cannot see the field"`. Honest rather than wrong.
5. Anything else, including apps with no accessibility layer such as Tk: `UNKNOWN`.

Verified during planning: with Chrome focused on a non-input pane the probe returns control type 50033, not keyboard-focusable, no text or value pattern, read-only — the exact `NOT_EDITABLE` shape.
A Tk `Text` widget returns the same shape despite being editable, which is precisely why "known good UIA provider" gates the confident negative.

**Target ring.**
New click-through overlay in `preview.py`: a 2px rounded outline on the probed rect, `WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW`, topmost, accent-coloured, drawn on the target's own monitor.
Shown while recording and while place mode is armed.
Accent when `EDITABLE`, amber when `NOT_EDITABLE` or `UNKNOWN`, so the user knows *before speaking* that the insert is at risk.
Falls back to outlining the window with an app-name label when no element rect is available.
Honours the existing `animations` config flag.

**Target label in the badge.**
The recording badge gains a single line: `→ Claude · message box`, from the probe's `label`.

**Post-insert verification.**
Where the probe returned a UIA element with a text or value pattern, capture a cheap length signal before the insert and compare after, on a 300ms budget.
A confirmed delta clears the stash and permits clipboard restore.

**Escalation policy (corrected 2026-08-22 after chunk A).**
The first draft of this section said the escalation UI fires whenever an insert is unconfirmed.
That is wrong, and chunk A proved it: verification legitimately returns "no signal" for terminals, RDP sessions, and every app with no accessibility layer, so escalating on unconfirmed would put a failure notice on most successful inserts and train the user to ignore it.
Silence is not the same as failure.
The correct table:

| Verify result | Pre-flight | Clipboard | Stash | User-visible |
|---|---|---|---|---|
| True, landed | any | restore previous | cleared | nothing |
| False, did not land | any | retain dictation | armed | recovery panel, error chime |
| None, no signal | EDITABLE or UNKNOWN | retain dictation | armed | tray dot only, no toast |
| n/a, insert refused | NOT_EDITABLE | retain dictation | armed | recovery panel, error chime |

Retention plus the tray dot is the silent safety net and costs the user nothing.
The loud path is reserved for positive evidence of failure.

## 6. RDP round 5 (P2)

Not another blind flush variant. Three changes, each independently useful.

**Foreground-change flush replaces the 4s window.**
Install a persistent `SetWinEventHook(EVENT_SYSTEM_FOREGROUND)`.
Whenever an RDP window *gains* the foreground, force-flush modifier key-ups into it after a 50ms settle.
Key-ups only, never downs — the 2026-08-12 tap experiment is recorded as harmful in `_flush_all_modifiers` and stays reverted.
Unmatched key-ups are no-ops everywhere, so this is safe to run on every foreground change.
This covers the case the current 4s watcher provably misses.

**A manual escape hatch that cannot be defeated by detection.**
Hotkey `ctrl+alt+shift+u`, config key `unstick_hotkey`, plus a tray item `Unstick modifiers`.
Force-flushes every modifier key-up into whatever currently holds focus and toasts `Modifiers released`.
This works even if every detection heuristic in the app is wrong, which is the point.

**Diagnosis for the next occurrence.**
The `RDP never took foreground` log line currently proves nothing because it does not say what *was* in the foreground.
It gains the foreground exe, class and title, so the next report identifies the client instead of requiring another guessing round.

**Documented client-side setting.**
`README.md` gains a line: in mstsc, Local Resources → Keyboard → *Apply Windows key combinations: On this computer* removes a documented class of modifier desync, because RDP sends modifiers in a separate packet that can arrive after the keystroke it modifies.

## 7. Config additions

```
"place_hotkey": "ctrl+alt+v",
"unstick_hotkey": "ctrl+alt+shift+u",
"target_ring": true,
"clipboard_retain_on_unconfirmed": true,
"target_probe": true
```

Every one defaults to the new behaviour and can be turned off.
`target_probe: false` disables sections 5 and the verification in 2, reverting to today's behaviour, so there is a clean escape if the probe misbehaves on some app.

## 8. Work split

Five chunks. P1 is the whole answer to "I'm missing the inserts"; P2 is the answer to "I can't tell where it will go".

| # | Chunk | Files | Priority |
|---|---|---|---|
| A | Clipboard retention + `stash.py` + tray state | `inject.py`, `stash.py`, `tray.py`, `main.py` | P1 |
| B | Monitor-correct toasts + edge flash + escalation panel | `winfx.py`, `preview.py`, `main.py` | P1 |
| C | Place mode: hotkey, armed overlay, mouse hook, insert | `hotkey.py`, `preview.py`, `main.py` | P1 |
| D | `targetprobe.py`, target ring, badge label, post-verify | `targetprobe.py`, `preview.py`, `inject.py`, `main.py` | P2 |
| E | RDP round 5: foreground hook, unstick hotkey, logging, README | `inject.py`, `hotkey.py`, `tray.py`, `README.md` | P2 |

A and B are independent and land first.
C depends on A (needs the stash).
D is independent of A-C but B's escalation reads its verdict, so D lands after B.
E is fully independent.

## 9. Verification

Unit tests in `tests/` for the pure logic: probe verdict resolution from synthetic property sets, monitor work-area maths, stash lifecycle, retention decision table.

Then a real end-to-end pass through the `restart-app` skill, exercising each of: Notepad, Windows Terminal, Chrome address bar, Chrome page body with **no** field focused, VS Code, an mstsc session, and the place-mode flow across two monitors.
The Chrome-with-no-field case is the one that currently loses text silently and is the acceptance test for this whole plan.
Nothing is reported as done until that pass is run and its output cited.

## 10. Out of scope

Renaming the project folder and repo to `quiett` is a separate task in the same session, done after the app work is landed and pushed.
No changes to transcription, TTS, study mode, the dashboard, or the API server.
