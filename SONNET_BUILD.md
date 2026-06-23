# Sonnet Build Sheet — Voice Dictation enhancements

**You are implementing pre-specified, pre-diagnosed changes. Do NOT re-audit the codebase.**
All root causes, file paths, and line numbers below were established by an Opus pass. Trust them;
verify only by reading the specific function you're about to edit. Rationale and the full
candidate list live in `ENHANCEMENTS.md` — read it only if a spec here is unclear.

## Rules of engagement (token efficiency)
1. Work **phase by phase, in order**. Commit after each phase so progress is never lost.
2. For each item: read only the named function(s), make the change, run the named test/check.
3. Do not refactor unrelated code. Do not "improve" things not in scope.
4. After Phase 1, run `.venv\Scripts\python -m pytest tests/test_postprocess_real.py -v` — all green
   before moving on.
5. If a spec is ambiguous or a test target looks wrong, STOP and leave a `# TODO(review):` note
   rather than guessing.
6. Commit message convention: `fix:`/`feat:` + short summary, end with the Co-Authored-By line.
7. Manual-test items (paste, launcher, UI) can't be unit-tested — implement to spec and list them in
   the final summary under "needs manual test by user."

---

## PHASE 0 — Hygiene (do first, trivial)
- **0.1** Delete `dictate.py` (dead legacy faster-whisper script, nothing imports it).
- **0.2** Remove `faster-whisper` from `requirements.txt` (live path uses the whisper.cpp server).
- **0.3** Update `tests/test_transcribe.py` if it mocks `faster_whisper` (drop/adjust that test).
- Commit: `chore: remove dead faster-whisper path`.

---

## PHASE 1 — Post-processing accuracy fixes (HIGHEST VALUE, all in `transcribe.py`)
Target tests: `tests/test_postprocess_real.py` (already written; currently RED on purpose).
The post-process chain is `_postprocess()` at `transcribe.py:310`.

- **1.1 (#1) Fix acronym mangling** — `_collapse_acronyms()` `transcribe.py:373`.
  Current regex collapses any run of single letters, so the `s a` in "it's a" → "SA"
  ("it'SA"), `m a` in "I'm a" → "I'MA", etc. **Fix:** only collapse a run when every letter is
  already **uppercase** (genuine spelled-out acronym like "A W S" / "I.B.M."), OR restrict matches
  so a letter immediately preceded by an apostrophe-contraction is never consumed. Simplest robust
  approach: change the per-letter class so the run must be uppercase letters
  (`[A-Z]`) — Whisper emits real acronyms capitalised. Verify `TestContractionMangling` passes
  including `test_real_acronyms_still_collapse`.

- **1.2 (#2) Pronoun numbers** — `_words_to_digits()` `transcribe.py:392`.
  Do not digit-ise a standalone "one"/"two" when it's a pronoun. Heuristic: skip conversion of a
  lone "one"/"two" when the previous non-space token is one of
  {no, the, this, that, which, every, any, each, only, that's, is, are, big} OR the next token is
  "of"/"who"/"that". Keep multi-word numbers ("twenty five") and clear quantities converting.
  Verify `TestPronounNumbers`.

- **1.3 (#3) Spacing** — `_cleanup_whitespace_around_punct()` `transcribe.py:365`.
  (a) Don't insert a space after a period that sits between two digits (decimal "4.8").
  Change the `([,.;:!?])(?=\S)` rule to not fire when the punct is `.` flanked by digits.
  (b) Add a rule removing a space before contraction suffixes:
  `re.sub(r"\s+'(s|t|re|ll|ve|m|d)\b", r"'\1", text, flags=re.I)` so "that 's" → "that's".
  Verify `TestSpacing`.

- **1.4 (#5) NZ uptalk guard** — new helper in `transcribe.py`, call it inside `_postprocess` AFTER
  punctuation is applied. For each sentence ending in `?`, downgrade to `.` UNLESS the sentence
  (first word, ignoring leading filler) starts with an interrogative trigger
  {who, what, where, when, why, how, which, whose, do, does, did, is, are, am, was, were, can,
  could, will, would, should, shall, may, might, have, has, had, "can't", "don't", "isn't", "didn't"}
  or contains a trailing tag like "right", "yeah", "isn't it", "does it". Keep it conservative.
  Verify `TestUptalkQuestionMarks`.

- Commit: `fix: post-processing accuracy (contractions, pronoun numbers, spacing, uptalk)`.

---

## PHASE 2 — Personalisation data (no logic, just seed config)
- **2.1 (#8) Vocabulary** — add to `config.json` `custom_vocabulary` (array): `Copilot Studio`,
  `Power Platform`, `PowerFX`, `SharePoint`, `Azure AI Foundry`, `Waikato`, `Selwyn`, `Cove`,
  `kove.nz`, `agentic`, `ALM`, `CI/CD`, `product owner`, `Power App`, `Power Automate`.
- **2.2 (#9) Corrections** — add to `config.json` `corrections` (object, whisper_out -> correct):
  `{"Selvin": "Selwyn", "co-part studio": "Copilot Studio", "co-part": "Copilot",
    "DAI'd": "DRY'd"}`. (Leave "the SAP"→"the app" out for now — too context-dependent; revisit
  with #10.)
- Note: `_apply_profile()` `transcribe.py:322` already applies `corrections` as word-boundary
  regex; confirm multi-word keys like "co-part studio" work (they should via `re.escape`).
- Commit: `feat: seed personal vocabulary and corrections`.

---

## PHASE 3 — Launcher, icon, no-console (root cause: `venv` should be `.venv`)
All three reported launcher problems are the missing dot in the venv path.
- **3.1 (L1)** `launch.vbs:5` — change `venv\Scripts\pythonw.exe` to `.venv\Scripts\pythonw.exe`.
  This gives no-console launch (pythonw, window style 0) with only the system-tray icon.
- **3.2 (L2)** `create_shortcut.ps1:5` — change `venv\Scripts\python.exe` to
  `.venv\Scripts\python.exe`. The script already generates `icon.ico` via `tray.export_ico` and
  sets `IconLocation` to it; fixing the path makes the Desktop shortcut + icon work.
- **3.3** Replace `run.bat` contents so it launches without a persistent console — use:
  `@echo off` then `start "" .venv\Scripts\pythonw.exe main.py` then `exit`. (Or just direct users
  to the Desktop shortcut.) Keep the `cd /d` to the project dir.
- **3.4** Update `README.md` Run/Setup section: venv is `.venv`, model is local ggml via
  whisper.cpp server (not HF auto-download of "small"), launch via the Desktop shortcut / `launch.vbs`
  (no console).
- Manual test by user: run shortcut → no CMD window, tray icon present, dictation works.
- Commit: `fix: launcher venv path, no-console launch, desktop icon`.

---

## PHASE 4 — The paste bug (VS Code / RDP) — #22, #23, #27
Root cause: commit `a589048` routed everything except terminals to Unicode `SendInput` typing;
`_decide_method()` `inject.py:592` returns `"type"` for VS Code (`Chrome_WidgetWin`) and RDP
(`TscShellContainer`/`RAIL_WINDOW`). Unicode typing is unreliable over RDP and Electron. The
clipboard+Ctrl+V path already exists and works (used for terminals).

- **4.1 (#22)** In `_decide_method()` `inject.py:592`: route RDP (`TscShellContainer`, `RAIL_WINDOW`)
  to `"ctrl_v"`. Add a config flag `electron_paste_method` (default `"ctrl_v"`) and route
  `Chrome_WidgetWin` to that flag's value so VS Code/Electron use clipboard paste too. Keep per-app
  `per_app_paste` override winning over all defaults.
- **4.2 (#23)** After a `"type"` paste, verify success cheaply (compare SendInput sent-count to
  expected, or detect focus loss) and on failure fall back to clipboard+Ctrl+V. Wire the existing
  but-unused `_try_wm_paste()` `inject.py:552` as a last resort.
- **4.3 (#27)** Add a "clipboard-only" mode: config `paste_mode: "auto" | "clipboard_only"`; when
  `clipboard_only`, copy to clipboard and show a toast instead of injecting. Add a tray toggle.
- Manual test by user: paste into VS Code and an RDP (mstsc) session via Insert/Enter — must land
  directly, no manual Ctrl+V.
- Commit: `fix: reliable paste into VS Code and RDP (clipboard route + verify/fallback)`.

---

## PHASE 5 — Robustness bundle (#25, #15, #16, #39, #46, #49)
- **5.1 (#25a)** `transcribe.run()` `_ready.wait()` `transcribe.py:246` — add a timeout (e.g. 30s);
  on timeout log + return `(None, None, None)` so a failed model load can't hang the worker thread
  forever.
- **5.2 (#25b)** `main.py` `_reload_config` `~main.py:372` — replace bare `except Exception: pass`
  with `log_error(...)` so bad config saves are visible.
- **5.3 (#25c)** Settings save `preview.py` `on_save ~1693` — re-read `config.json` immediately
  before writing and merge, instead of clobbering with the snapshot taken at window-open (prevents
  losing tray/hot-reload writes).
- **5.4 (#25d)** MicMeter — don't open a live mic stream the instant Settings opens
  (`preview.py:1484`); open it only when the user focuses the Audio page / interacts with the mic
  dropdown, and ensure `meter.stop()` always runs on window close.
- **5.5 (#25e)** Wrap `_open_settings`/`_open_history`/`_open_profile` calls in `_tick` with
  try/except that resets the corresponding `_*_open` flag on failure (prevents permanent lockout).
- **5.6 (#39) Single-instance lock** — in `main.py` startup, acquire a Windows named mutex; if
  already held, show a toast "already running" and exit.
- **5.7 (#15/#16) Vibe load-gap + indicator** — `reformat.load()` `reformat.py:220`: after probing
  the server, query `/v1/models` and confirm the configured chat model is loaded; if not, dispatch
  `lms load` and wait. In `_run_transcription` (`main.py`), track whether reformat used the model vs
  the regex fallback and pass a flag to `preview.show(...)` so the badge can show "LLM" vs "rules".
- **5.8 (#46) Watchdog** — extend `health.py` so a dead whisper-server is auto-restarted (call
  `transcribe.load()` again) rather than only toasting.
- **5.9 (#49) First-run check** — on startup verify `whisper-server.exe` and the model `.bin` exist
  and a mic is available; if not, show a clear toast/dialog instead of failing silently.
- Commit per logical sub-group (e.g. `fix: lifecycle robustness`, `feat: vibe load-gap + indicator`,
  `feat: backend watchdog + first-run check`).

---

## PHASE 6 — Higher-effort features (only if user greenlights; each is independent)
Implement on request, one per branch/commit. Specs are in `ENHANCEMENTS.md`:
- #4 stutter/false-start collapse  · #6 auto-capitalisation/segmentation · #7 smarter fillers
- #10 learn-from-edits loop · #12 per-app context profiles · #13 accuracy review log
- #14 voice-preserving LLM cleanup prompt · #17 glossary-aware reformat · #18 decode tuning
- #19 toggle/hands-free mode · #20 streaming · #21 voice editing commands
- #26 output format modes · #28 re-transcribe last · #29 undo paste · #30 multi-language
- #31 auto-gain · #32 noise suppression · #33 silence trim · #34 mic profiles
- #35 smart numbers/units · #36 code mode · #37 spoken formatting commands
- #38 auto-start · #40 app allow/block list · #41 per-app mode · #42 dictionary UI
- #43 history tools · #44 diagnostics panel · #45 live theme · #47 idle unload
- #48 latency/stats · #50 report-bad-transcription
- L3 taskbar AppUserModelID

**Note for the human:** Phases 0–5 are the high-value, well-bounded core (the bug fixes + the two
launcher fixes + personalisation + robustness). Phase 6 items are real features — do them in small
batches, not all at once, testing each. Recommended Sonnet run: **Phases 0–5 in order**, then pick
Phase-6 items as desired.

---

## Final deliverable from Sonnet
A short summary listing: tests passing, what was committed per phase, and a "needs manual test by
user" list (everything in Phases 3–5 that touches paste/launcher/UI).
