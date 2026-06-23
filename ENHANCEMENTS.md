# Voice Dictation — Accuracy & Reliability Enhancement Report

> Goal: make dictation **clear, precise, and self-learning** for a New Zealand–accented
> speaker with natural stutters/false-starts. Private tool — not for sale. This report
> analyses real usage history (2026-06-23, full day), pins root causes in code, and lists
> 25 candidate enhancements to pick from.

---

## Part 1 — What the history actually shows (data-driven)

Analysed all 35 dictations from `history.json` (2026-06-23). The "weird words" are **not random** —
they're a handful of repeating, fixable patterns. Ranked by frequency/impact:

### 1.1 The `'SA` / `'MA` mangling — THE biggest source of garbage  ⚠️ root cause found
Real examples from your history:
- "there'SA quite a few", "there'SA lot of AI training", "there'SA new version"
- "that'SA worst case scenario", "It'SA must", "It'SA massive app", "It'SA power app"
- "I'MA product owner"

**Root cause:** `transcribe.py:373` `_collapse_acronyms()`. Its regex collapses any run of single
letters into an uppercase acronym (so "A W S" → "AWS"). But it also matches the `s a` inside
"it's a" and the `m a` inside "I'm a", because the apostrophe creates a word boundary. So:
- "it's a" → `it'` + `SA` → **it'SA**
- "I'm a" → `I'` + `MA` → **I'MA**
- "there's a" → **there'SA**

This single bug is responsible for the **majority** of the "random letters" you're seeing. Tiny fix,
huge win (only collapse runs that are already uppercase / known acronyms).

### 1.2 Over-aggressive number conversion ("one" → "1" everywhere)
- "no 1 does" (no one), "next 1, next page" (next one), "the 1 who did it" (the one),
  "1 of my colleagues" (one of my colleagues), "1 page" (one page)

**Root cause:** `transcribe.py:392` `_words_to_digits()` converts the *pronoun* "one" to a digit.
"one"/"two" as words should stay words unless they're clearly quantities.

### 1.3 Decimal & apostrophe spacing
- "Opus 4. 8" (should be "4.8") — `_cleanup_whitespace_around_punct` (`transcribe.py:367`) inserts a
  space after a period even between digits.
- "that 's going to", "there 's more insight", "it 's not" — stray space before `'s`.

### 1.4 Stutters & false-starts transcribed verbatim
Whisper faithfully writes every repetition; nothing collapses them:
- "in in the previous chats", "read the read the history", "look through look through",
  "he he's not sure", "and and refined", "pitch pitch me", "do, do that", "talk, talk more",
  "the cover Copilot studio" (false start)

You asked specifically for this: the app should recognise stutters and clean them.

### 1.5 NZ-accent "uptalk" → spurious question marks
You said question marks appear when you're not asking a question. This is **High Rising Terminal**
(the NZ/Aussie habit of ending statements on a rising tone). Whisper reads the rising intonation as
interrogative and appends `?` to plain statements. Needs a guard that only keeps `?` when the
sentence is structurally a question (starts with who/what/where/when/why/how/do/does/can/is/are…).

### 1.6 Domain words consistently mis-heard (fixable with vocabulary + corrections)
- "co-part studio" → **Copilot Studio**
- "Selvin district" → **Selwyn** (NZ place name)
- "the SAP" → **the app**
- "DAI'd" → **DRY'd** (don't-repeat-yourself, coding context)
- "KSASI" → garbled ("kiss-ass-y")
- Also seen and worth seeding: Power Platform, PowerFX, SharePoint, Azure AI Foundry, Waikato,
  agentic, ALM, CI/CD, Copilot, product owner, Cove / kove.nz

### 1.7 "AI" hallucination
- "because I have AI'm New Zealand" (…I'm from New Zealand), "make sure it's DAI'd"

Whisper is biasing toward "AI" (likely because it's so frequent in your speech / any prompt). Worth
watching when we tune the initial_prompt.

---

## Part 2 — The ongoing paste bug (VS Code / Remote Desktop)  ⚠️ root cause found

**Symptom:** pressing Insert/Enter to paste doesn't land in VS Code or Remote Desktop; you have to
Ctrl+C then manually Ctrl+V / right-click.

**Root cause:** commit `a589048` ("Type text via SendInput KEYEVENTF_UNICODE — replace synthetic
Ctrl+V") switched the paste method to **synthetic Unicode keystroke typing** for everything except
terminals. In `inject.py:592` `_decide_method()`, both VS Code (`Chrome_WidgetWin`) and RDP
(`TscShellContainer`/`RAIL_WINDOW`) fall through to `return "type"`.

Unicode `SendInput` typing is **unreliable over RDP** (synthetic KEYEVENTF_UNICODE events frequently
don't propagate into the remote session) and is often swallowed by Electron under focus races —
which is exactly why your manual clipboard Ctrl+V works but the app's typing doesn't.

**Fix direction:** route RDP (and likely Electron/VS Code) back to **clipboard + Ctrl+V** — the
method that already works for you manually and already exists in the codebase for terminals — plus
add paste-verification with auto-fallback. (Items #21–#23 below.)

---

## Part 3 — 25 candidate enhancements (pick which to action)

Tags: **[BUG]** = fixes a defect · **[ACC]** = accuracy · **[LEARN]** = personalisation/learning ·
**[UX]** = workflow · **[INFRA]** = robustness. Effort: S/M/L.

### A. Post-processing correctness (fixes for the patterns above)
1. **[BUG][ACC] Fix `'SA`/`'MA` mangling** — only collapse uppercase/known-acronym runs so
   "it's a", "I'm a", "there's a", "that's a" stop breaking. **S. Highest impact.**
2. **[BUG][ACC] Stop converting pronoun "one"/"two" to digits** — keep "no one", "next one",
   "the one who" as words; only digit-ise real quantities. **S.**
3. **[BUG][ACC] Fix decimal & apostrophe spacing** — "4.8" not "4. 8"; "that's" not "that 's". **S.**
4. **[ACC] Stutter / false-start collapse** — detect and remove immediate word/short-phrase
   repetitions ("in in", "he he", "look through look through", "pitch pitch"). **M.**
5. **[ACC] NZ uptalk guard** — only keep `?` when the sentence is structurally interrogative;
   otherwise make it `.` Kills spurious question marks. **M.**
6. **[ACC] Auto-capitalisation & sentence segmentation pass** — proper sentence boundaries, capital
   "I", basic proper-noun casing. **M.**
7. **[ACC] Smarter filler removal** — broaden + context-aware ("kind of", "sort of", "I mean",
   "blah blah", trailing "so.", "et cetera" noise) without nuking meaning. **M.**

### B. Personalisation & learning ("learn my faults / mannerisms")
8. **[LEARN] Personal vocabulary pack** — seed `custom_vocabulary` with your real terms (Copilot
   Studio, Power Platform, PowerFX, SharePoint, Azure AI Foundry, Waikato, Selwyn, Cove/kove.nz,
   agentic, ALM, CI/CD, product owner). **S.**
9. **[LEARN] Known-error correction dictionary** — seed `corrections` with confirmed mis-hears
   (Selvin→Selwyn, co-part→Copilot, DAI'd→DRY'd, "the SAP"→"the app" in context). **S.**
10. **[LEARN] Learn-from-edits loop** — when you edit the preview before pasting, diff raw vs final
    and *propose* a new correction/vocab rule. Over time it adapts to your speech. **M–L.**
11. **[LEARN] NZ-accent initial_prompt biasing** — prime Whisper with NZ spelling and your typical
    topics/style so it leans the right way (and stops over-hearing "AI"). **S.**
12. **[LEARN] Per-app context profiles** — code context for VS Code (keep raw, no number-convert),
    prose context for email/Claude (full cleanup). Auto-selected by foreground app. **M.**
13. **[LEARN] Accuracy review log + stats** — local log of raw → edited → pasted text plus per-day
    WPM/edit-rate, so we can *measure* whether each change actually improves quality. **M.**

### C. LLM (vibe-mode) cleanup
14. **[ACC] "Clean my speech" reformat prompt** — tune the LLM to strip stutters/fillers/false-starts
    and tidy grammar **while preserving your voice and every point** (you flagged over-explaining
    and "keep my tone"). **M.**
15. **[BUG] Close the LM Studio model-load gap** — ensure `qwen3-8b` is actually loaded (query
    `/v1/models`) instead of silently falling back to regex. **S–M.** (Known issue in memory.)
16. **[UX] "Reformatted vs raw/rules" indicator** — a badge so you know whether the LLM cleaned it
    or it silently fell back to regex. **S.**
17. **[ACC] Glossary-aware reformat** — pass your vocabulary/corrections into the LLM so it fixes
    domain terms during cleanup too. **S.**
18. **[ACC] Larger-model / decode tuning** — beam size, temperature-fallback, and best-of tuning on
    `large-v3-turbo` (or test full `large-v3`) for raw accuracy. **M.**

### D. Capture & workflow
19. **[UX] Toggle / hands-free mode** — tap to start, tap to stop, for long brain-dumps (your 300-word
    monologue) instead of holding Ctrl+Alt for 90 s. **M.**
20. **[UX] Streaming / chunked transcription** — partial text while you talk; also removes the 60 s
    inference-timeout risk on long recordings. **L.**
21. **[ACC] Voice editing commands** — "scratch that", "delete that", "new line", "cap that". **M.**

### E. Paste / injection (the ongoing bug)
22. **[BUG] Fix direct paste into VS Code & RDP** — route Electron/RDP to clipboard+Ctrl+V (the
    method that works for you manually) instead of unreliable Unicode typing. **M. Critical.**
23. **[BUG][INFRA] Paste verification + auto-fallback** — confirm the text landed; if not, fall back
    to clipboard paste / wire up the existing-but-unused `_try_wm_paste`. **M.**
24. **[UX] Per-app paste-method setting + remembers what worked** — UI for `per_app_paste`, and the
    app remembers the last successful method per application. **M.**

### F. Robustness (from the code audit)
25. **[INFRA] Stability fixes bundle** — unbounded `_ready.wait()` hang if the model fails to load;
    swallowed hot-reload errors; Settings-save clobbering concurrent config writes; MicMeter opening
    the mic the moment Settings opens; Settings/History permanently locked out if a window build
    throws. **M.**

---

## Recommended first batch (fastest path to "feels dramatically better")
The four small post-processing fixes + the two big bug fixes give the largest perceived jump:
**#1, #2, #3** (kills most garbage, ~1 hr total), **#22** (paste finally works in VS Code/RDP),
plus **#8 + #9** (your vocabulary/corrections). Then **#4, #5, #14** for stutters/uptalk/voice.

> Tell me which numbers to action and I'll implement them, then we test together.

---

## Part 4 — Launcher & icon fixes (root cause found)

Both reported problems come from **one bug**: the venv folder is `.venv` (with a dot), but the
launch scripts point at `venv` (no dot).

- **L1 — No-console launch.** `launch.vbs:5` already uses `pythonw.exe` with a hidden window
  (correct approach), but points at `venv\Scripts\pythonw.exe` → fails → you fall back to `run.bat`,
  which runs `python main.py` and leaves a CMD window open. **Fix:** `venv` → `.venv`, launch via
  `pythonw` (no console; system-tray icon only, app sits in background). **S.**
- **L2 — Desktop icon/shortcut.** `create_shortcut.ps1:5` points at `venv\Scripts\python.exe` →
  can't generate `icon.ico` or a working shortcut. `icon.ico` already exists (multi-res). **Fix:**
  `venv` → `.venv`, regenerate the Desktop shortcut pointing at `launch.vbs` with `icon.ico`. **S.**
- **L3 (optional) — Taskbar presence.** Tray icon satisfies "shows it's running." If you also want a
  pinned **taskbar** button with the app icon, set an `AppUserModelID` + a minimal taskbar-visible
  window. Optional. **M.**

---

## Part 5 — Second 25 enhancements (#26–#50)

Distinct from #1–#25 (which were mostly accuracy post-processing, learning, and paste). This batch
is functionality, workflow, audio quality, management UI, and reliability.

### G. Output modes & workflow
26. **[UX] Output format modes** — per-recording choice: raw / clean prose / bullet points / email /
    prompt. Selectable via tray or a modifier key. **M.**
27. **[UX][BUG-adjacent] Clipboard-only mode + toggle** — copy transcription to clipboard instead of
    auto-pasting (a reliable fallback for the apps where paste fights you; it's what you do manually
    now). **S.**
28. **[UX] Re-transcribe last recording** — keep the last audio buffer; re-run with a different
    model/settings without re-speaking. **M.**
29. **[UX] Undo last paste** — restore the previous clipboard / re-open the last preview. **M.**
30. **[ACC] Multi-language auto-detect** — NZ-English default, quick-switch + auto-detect for other
    languages. **M.**

### H. Audio capture quality (input-side, complements the text-side fixes)
31. **[ACC] Auto-gain normalisation** — level the mic signal before transcription so quiet/variable
    speech decodes more reliably. **M.**
32. **[ACC] Noise suppression** — RNNoise/WebRTC denoise on the captured audio for cleaner input
    (helps accuracy in noisy rooms). **M–L.**
33. **[ACC] Auto-trim leading/trailing silence** — cut dead air that triggers Whisper hallucinations
    ("Thank you." / "you" artefacts on near-silent clips). **S–M.**
34. **[UX] Mic profiles** — saved profiles (headset vs laptop) with per-profile gain/device. **M.**

### I. Smart formatting (new domains, not the bug-fixes in batch 1)
35. **[ACC] Smart numbers & units** — sensible dates, times, currency ($, %), phone numbers. **M.**
36. **[ACC] Code mode** — when target is VS Code/terminal, keep symbols/case raw, suppress
    number-word conversion and prose punctuation. **M.**
37. **[UX] Spoken formatting commands** — "all caps", "title case", "bullet point", "quote that". **M.**

### J. App & system integration
38. **[UX] Auto-start on Windows login** — toggle in settings (Startup shortcut / registry). **S.**
39. **[INFRA] Single-instance lock** — prevent duplicate app instances (named mutex). **S.**
40. **[UX] App allow/block list** — don't fire the hotkey in chosen apps (games, full-screen). **M.**
41. **[LEARN] Per-app mode auto-select** — raw vs vibe vs code mode chosen by foreground app. **M.**

### K. Management & visibility UI
42. **[UX] Dictionary manager UI** — add/edit/remove vocabulary & corrections in-app (no raw JSON). **M.**
43. **[UX] History tools** — search, export (CSV/txt), copy-from-history, delete entries. **M.**
44. **[INFRA] Diagnostics panel** — live status of whisper-server / LM Studio / Vulkan GPU / mic +
    latency, with one-click restart. **M.**
45. **[UX] Live theme + feedback customisation** — apply theme without restart; chime volume on/off;
    waveform/overlay position options. **M.**

### L. Reliability & resources
46. **[INFRA] Backend watchdog** — auto-restart whisper-server (and reload the LM model) if it dies
    mid-session. Extends the existing `health.py` monitor. **M.**
47. **[INFRA] Idle model unload** — free VRAM after inactivity; reload on demand. **M.**
48. **[UX] Latency + session stats** — show transcription time per recording; per-session WPM and
    edit-rate. **M.**
49. **[UX] First-run setup check** — verify `whisper-server.exe`, model file, mic, and hotkey on
    startup; clear guidance if anything is missing. **M.**
50. **[LEARN] "Report a bad transcription" button** — one click snapshots audio + raw + final text to
    a tuning folder, so we can improve accuracy from real failures later. **S–M.**

---

## Master pick-list summary

- **#1–#25** — accuracy post-processing, personalisation/learning, LLM cleanup, capture/workflow,
  paste fixes, robustness (Part 3).
- **L1–L3** — launcher / desktop icon / no-console (Part 4).
- **#26–#50** — output modes, audio quality, smart formatting, system integration, management UI,
  reliability (Part 5).

Execution spec for Sonnet: see **`SONNET_BUILD.md`**.
