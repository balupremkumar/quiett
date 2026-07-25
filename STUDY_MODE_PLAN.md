# Study Mode — narration built for reading to learn, not reading to notify

Status: P1 and P2 BUILT 2026-07-25. P3 and P4 remain plan only.
Written 2026-07-25, after the pace-drift fix (chunked synthesis + `tts_speed`) and the removal of TaskFlow, agent command mode and the fitness divert.

Balu's ruling on scope, 2026-07-25: a tray toggle, not a second hotkey. Same selection, same Ctrl+Shift+S, same flow; the toggle changes the delivery. Study rate configurable, starting at 0.95x.
As built: `narration.py` owns segmentation, pauses and per-segment rate for both modes, `tts.py` synthesises and stretches segment by segment, the tray carries a Study Mode checkbox, and the dashboard Settings page has a Read-aloud section (enable, speed, study mode, study speed, study pause length). Config keys: `study_mode`, `study_speed`, `study_pause_scale`.

## The ask

Highlight a block of text, press the read-aloud hotkey, and have the voice narrate it the way a good tutor reads to a student: slower, with pauses that land where meaning breaks, not where the model happened to run out of breath.
Ordinary read-aloud (a quick reply, a paragraph you are checking) should stay as it is now.

## Where we are today

The current path is `inject.get_selected_text()` → `tts.speak(text)` → chunked synthesis at `tts_max_chunk_chars` (120) → optional WSOLA stretch at `tts_speed` → sounddevice.
It already gives a steady pace and a global speed control, and pauses between chunks are a fixed 0.25s floor plus whatever tail the render had.

That is flat narration. It reads a heading the same as a sentence, a bulleted list the same as prose, and it gives a full stop the same pause as a comma-spliced clause boundary. For study listening, those distinctions are most of the value.

## Design principles

1. **Structure drives prosody, not a model.** The pauses come from parsing the text (punctuation, line breaks, list markers, headings), not from asking an LLM to annotate it. Deterministic, offline, no VRAM, no latency, debuggable when it sounds wrong.
2. **Study Mode is a mode, not a new setting soup.** One toggle picks a coherent set of behaviours. Individual knobs exist underneath for tuning, but the user should never have to assemble the mode themselves.
3. **Never change the words.** The narrator may re-time and re-group text; it must not rewrite it. What you hear must match what you highlighted, or trust in it as a study tool is gone.
4. **Everything degrades to today's behaviour.** If parsing produces nothing useful, the result is the current flat read at the configured speed.

## P1 — Prosody engine (the core of it)

A `narration.py` module that turns raw selected text into a list of `(text_to_speak, pause_after_seconds, speed_multiplier)` segments. `tts.speak()` consumes that list instead of a flat chunk list.

Segmentation and pause budget (starting values, all tunable):

| Boundary | Detected by | Pause after |
|---|---|---|
| Comma, semicolon, colon | punctuation inside a sentence | 0.15s |
| Sentence end | `.!?` plus closing quote/bracket | 0.45s |
| Clause break in a long sentence | existing `_chunk_text` split point | 0.20s |
| Line break inside a paragraph | single newline | 0.35s |
| Paragraph break | blank line | 0.90s |
| List item | leading `-`, `*`, `•`, `1.`, `a)` | 0.55s, and 0.30s extra before the first item |
| Heading | short line, no terminal punctuation, followed by a blank line or a longer line | 1.00s after, 0.60s before |

Rate shaping on top of the global `tts_speed`:

- Headings read at 0.95x of the current speed, so they sit slightly apart from body text.
- The first sentence of a paragraph reads at 0.97x — a small settle-in, the thing human readers do without noticing.
- Sentences over ~40 words read at 0.95x; dense sentences are where a listener falls behind.
- Anything in quotes reads at 0.97x with 0.2s of extra pause either side, so quoted material is audibly quoted.

Everything above is one multiplier applied to the existing WSOLA stretch, so it costs nothing new at runtime (the stretcher already runs at RTF 0.008).

## P2 — Study Mode toggle and defaults

- Config: `study_mode` (bool, default false), plus `study_speed` (default 0.85) and `study_pause_scale` (default 1.0, multiplies every pause in the table above).
- Tray: a **Study Mode** checkbox next to Read-aloud Speed. When on, read-aloud uses `study_speed` and the full prosody engine; when off, `tts_speed` and the current flat behaviour.
- The tray speed picker adjusts whichever speed is live, so one control does not silently edit the other mode's setting.

Ruling to make before building: whether Study Mode should be a separate hotkey (Ctrl+Shift+D, say) instead of a mode toggle, so a single selection can be read either way without visiting the tray. A second hotkey is more direct but adds another chord to remember. Recommendation: mode toggle first, hotkey later only if the toggle proves annoying in real use.

## P3 — Playback control (the part that makes it usable for study)

Reading to learn means rewinding. Today the only control is stop.

- Ctrl+Shift+S while speaking stops (already true). Add: pressing it twice quickly restarts the current segment.
- A small transport panel near the cursor while narrating: play/pause, back one sentence, forward one sentence, current position (sentence n of m), and the live speed. Reuses the existing Tk floating panel and the preview panel's drag/pin patterns.
- Pause and resume must be sample-accurate, which means the consumer loop needs to hold its position in the current segment rather than tearing down the stream. Straightforward with the current queue design.
- Because synthesis runs a chunk at a time and each chunk is one or two sentences, "back one sentence" is a re-synthesis of a short chunk, roughly 2-3s of work. Cache the last N rendered segments as PCM so back/forward inside recently read material is instant.

## P4 — Study session extras (only if P1-P3 land well)

- **Terms list.** Before narrating, scan the selection for words in the user's custom vocabulary and, at the end, read a short "terms in this passage" summary. No LLM: it is a set intersection.
- **Repeat-on-demand.** A hotkey that re-reads the last paragraph, for when attention drifts. Cheap given the P3 segment cache.
- **Session log.** What was narrated, when, and how long, written to the existing history store with a `source="narration"` tag so the dashboard can show study time. Respects incognito exactly as dictation does.
- **Skip code blocks.** Text that looks like code (indented, brace-heavy, high symbol density) is skipped with a spoken "code block, skipped" marker rather than read character by character.

## What this plan deliberately excludes

- No LLM-based prosody, sentiment reading, or emphasis prediction. It would need VRAM, add latency, and would sometimes be confidently wrong about which word to stress.
- No SSML. The qwentts.cpp server takes plain text; prosody here is timing and rate, applied client-side.
- No summarisation or rewriting of the source text (see principle 3).
- No multi-voice or per-speaker rendering.

## Effort and sequencing

P1 is the substantial piece: a parser, a pause table, and a change to how `tts.speak()` consumes work. P2 is small. P3 is a UI build plus a genuine change to the playback loop, so it is the risky one. P4 items are independent and can be picked off individually.

Suggested order: P1 → listen to a real study passage and tune the pause table by ear → P2 → use it for a week → P3 only if the lack of rewind is what actually gets in the way.

## Open questions for Balu

1. RULED 2026-07-25: toggle, not a second hotkey. Built that way.
2. STILL OPEN: what are you actually studying, and in what app? If it is PDFs or a browser, selection grabbing may need per-app handling that the current `get_selected_text` does not do. This is the one that could force more work.
3. STILL OPEN: is a spoken heading announcement wanted ("Heading: Retrieval augmented generation"), or just the timing change that is built now?
4. STILL OPEN: how long are the passages, typically? A 10-minute read changes the priority of P3 rewind controls sharply.

## Tuning by ear

Every pause and rate is a named constant in `narration.py`, and `study_pause_scale` scales the lot from Settings without touching code. If the delivery feels wrong after real use, change those numbers first before adding any new mechanism.
