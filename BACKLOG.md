# Backlog — voice-dictation

Ideas land here instead of being regenerated in chat.
Tick when done, cull what no longer matters, add a date when adding items.

- [ ] (2026-07-03) Voice profile / cloning: multi-sample build, local TTS (Qwen3 TTS feasibility), narrate mode. TTS shipped; narration next, see STUDY_MODE_PLAN.md.
- [x] CULLED 2026-07-25: personal-assistant expansion of agent mode. Agent mode itself was deleted; do not reopen.
- [ ] (2026-07-03) Remaining FluidVoice gap-analysis items not yet built (tier 2 minus item 9).

## Premium UI/UX revamp — 4-agent research (2026-07-10)

Angles: dictation-app teardown (Wispr Flow, superwhisper, VoiceInk, MacWhisper, Voice Access), premium desktop patterns (Raycast, PowerToys, CleanShot, Arc, 1Password), Windows rendering tech, codebase audit.
Impact H/M/L, Effort S/M/L.

### Convergent findings (two agents reached the same conclusion independently)

1. [x] (2026-07-10) **CONVERGENT** Jagged speech-bubble edges fixed via DWM corner preference (compositor-AA'd, verified 3x zoom); full `UpdateLayeredWindow` rewrite deferred into item 20's stack decision — no visible Win11 gain. Win10 keeps the region-mask fallback.
2. [x] (2026-07-10) **CONVERGENT** Per-monitor DPI awareness v2 in main.py + dashboard.py, tk font scaling, badge constants scaled via _px().
3. [x] (2026-07-10) **CONVERGENT** State encoded in surfaces: status dot on the preview panel header; tray = monochrome glyph + state colour dot (idle bare, recording pulses).
4. [x] (2026-07-10) **CONVERGENT** Live theme sync: refresh_theme() on window open + 30s config reload; `theme: system` follows AppsUseLightTheme (Tk + dashboard prefers-color-scheme); tray follows SystemUsesLightTheme.
5. [x] (2026-07-10) **CONVERGENT** Icon pipeline: scripts/make_icons.py materialises assets/ (multi-res icon.ico, tray PNG set, logo) from the programmatic masters in tray.py.
6. [ ] **CONVERGENT** Motion quality needs the surface off plain Tk: Tk `-alpha` fades tear well below 60fps; premium motion is 100-400ms, state-confirming only, honouring reduce-motion. (M/M) — microinteraction guides + Tk tearing reports.

### Recording pop-up / speech bubble

7. [x] (2026-07-11) True continuous waveform: 28-sample level history upsampled bicubic, mirrored filled polygon at 3x supersample, LANCZOS downscale, gradient composited via mask; same 50ms cadence. Needs Balu's eyes on a real dictation.
8. [x] (2026-07-11) Hover fades in ⏹ stop / ✕ cancel on the badge (calls audio.stop/cancel directly, same path as hotkey release); zero layout shift, 120ms debounce, recording states only.
9. [x] (2026-07-11) Toggle existed as dead vibe-mode code; revived and fixed 2 real bugs (edits clobbered on flip, correction-learning compared wrong side). transcribe.run() now returns raw_text; per-side edit stash; toggle hidden when raw == cleaned.
10. [x] (2026-07-11) Confirm gate before transcribing recordings >30s. REVERTED 2026-07-12 on Balu's request: he routinely dictates 1-2 minutes, the gate fired on real dictations. Always transcribe now; gate code removed from main.py and preview.py.
11. [x] (2026-07-11) Dedicated status row in the wave badge ("Recording…", "Processing…", "Cleaning up…") between waveform and partial transcript; panel header already carried the status dot. No "Done" state invented (would touch main.py flow).
12. [x] (2026-07-11) Three styles behind `badge_animation` (waveform default / pulse / bars), renderer-dict pattern for future styles, static frame when motion off, Settings select wired instant-apply.
13. [x] (2026-07-11) CLOSED as not-applicable: audited, nothing in the codebase captures clipboard/selection as context (per_app_context is vocab overrides; inject.py clipboard reads are save/restore only). Revisit only if a context feature is ever built.
14. [ ] Acrylic backdrop on the popup via `DWMWA_SYSTEMBACKDROP_TYPE` = `DWMSBT_TRANSIENTWINDOW` (documented Win11 route, not the fragile Win10 accent API). (M/M) — MS Learn system backdrops.
15. [x] (2026-07-11) DWM drop shadow via `DwmExtendFrameIntoClientArea` 1px bottom margin (winfx.py), Win11 corner-preference path only; Win10 region fallback untouched.
16. [x] (2026-07-10) `DWMWA_WINDOW_CORNER_PREFERENCE` in winfx.apply_rounded_region, region mask kept as Win10 fallback (with DPI-scaled radius). Turned out to be the full fix, not just interim — see item 1.
17. [x] (2026-07-11) Real vertical gradient via cached PIL column, single PhotoImage per frame (replaces six bands + per-bar rectangles); wave bar width/gap now DPI-scaled too.
18. [x] (2026-07-11) Slide+fade entrance (14px drift, 200ms, ease-out) on panel and both badge variants via _play_entrance; `animations:false` in config.json disables.
19. [x] (2026-07-11) silence_auto_stop_seconds exposed as a 0-10s slider ("Off" at 0), instant-apply, float-parse fix in collectSettings for range inputs.
20. [ ] Evaluate popup stack migration: raw Win32 layered window (full control, L effort) vs PySide6/QML (GPU-composited 60fps, M) vs pywebview frameless (Chromium AA free, but cold-start latency + no native rounded corners bug #834). (H/L) — rendering agent 8-10. RULED 2026-07-11: Win11-only product (ULW/Win10 fallback polish out of scope); decision deferred until Balu tests the batch-4 motion in real use.

### Tray + desktop icons

21. [x] (2026-07-10) Monochrome theme-aware tray icon (SystemUsesLightTheme), state as colour dot, pulse moved to the dot; coloured badge kept for the desktop .ico.
22. [x] (2026-07-11) Left-click opens the dashboard (hidden default=True menu item), right-click keeps the full menu.
23. [x] (2026-07-11) Tray menu grouped: state, common actions (History, Settings, Pause submenu), rare actions under "More", Quit; all prior actions reachable.
24. [ ] Desktop/installer icon redesign around one bold mic-to-caret glyph with subtle depth at 256px (ties to PRODUCTION_PLAN P2). (H/M) — MS/Apple icon guidelines.
25. [x] (2026-07-11) Verified clean: all 7 frames (16-256) full size on Pillow 12.2.0, no clamp; `make_icons.py --verify` added for regression.
26. [x] (2026-07-11) Verified already correct: pulse frames build from the current monochrome glyph via _rebuild_icons on theme refresh, in-memory only, nothing stale. No change.

### Settings / dashboard

27. [ ] Settings search bar that filters and highlights matching controls across sections. (H/M) — Raycast Settings v2.
28. [ ] Hotkey-recorder control ("press a key combination…") with live chord display, replacing any dropdown/text binding. (H/M) — PowerToys Keyboard Manager.
29. [x] (2026-07-10) Instant-apply settings (debounced 450ms, "Saved" flash), Save button removed.
30. [x] (2026-07-11) "Reset section" on 8 Settings sections, JS defaults mirror of main._CONFIG_DEFAULTS, one confirm, instant-apply.
31. [ ] Expose appearance settings (accent, popup size, position with visual picker, animation style); today every colour, font, and dimension is a hardcoded constant (preview.py:67-118). (M/M) — audit.
32. [ ] One panel per concern, no nested menus (the ShareX failure mode vs CleanShot X). (H/M) — CleanShot comparisons.
33. [ ] Single type scale and 8px spacing grid shared across the Tk popup and the web dashboard; inconsistent spacing across windows is the biggest cheap-vs-premium tell. (H/M) — Raycast/Linear design-system analyses.
34. [ ] Shared design tokens between preview.py constants and the dashboard CSS custom properties so the two stacks can't drift. (M/M) — audit (two independent hardcoded palettes today).

### History

35. [x] (2026-07-11) Consolidate the two history views: dashboard History page is canonical, tray already routed to it (main.py:1063); removed the legacy Tk viewer and its dead helpers from preview.py (242 lines).
36. [x] (2026-07-11) Filter-as-you-type history search (150ms debounce, matches text + target app, empty-result message), verified by screenshot.
37. [ ] Re-transcribe from history (right-click "process again") for after model/dictionary upgrades. (M/S) — superwhisper, Wispr Flow.
38. [x] (2026-07-11) Replay audio from history entries via winsound (SND_FILENAME|SND_ASYNC), play/stop toggle per row, button hidden when there's no audio or the file's gone.
39. [x] (2026-07-11) Export entries as Markdown/plain text/SRT/VTT with a format picker; SRT/VTT use sequential 2s placeholder cues, disclaimed in the file header. DOCX dropped (no dependency allowed).
40. [ ] Usage/stats tab: words dictated, WPM, top target apps; strong retention hook. (M/M) — Wispr Flow "Your Usage".

### Dictionary and snippets

41. [ ] Usage-ranked dictionary with starred/pinned terms getting transcription priority, auto-populated from user corrections. (H/M) — Wispr Flow.
42. [x] CULLED 2026-07-25: voice-triggered snippets were built, then removed in the de-bloat pass. Do not rebuild without a fresh ruling.

### Onboarding / first-run

43. [ ] 3-4 step skippable first-run: mic permission → hotkey demo with live feedback → one guided sample dictation → done. (H/M) — Arc onboarding pattern.
44. [ ] Pre-frame the admin/UAC request in plain language ("needs admin to catch your hotkey globally") before the OS prompt fires. (M/S) — 1Password pattern.

### Cross-cutting polish

45. [x] (2026-07-11) Error chime added to the existing bell family (F4→D4), wired to error toasts + error badges only; `sound_volume` 0-100 setting (slider, instant-apply), 0 = mute, cached scaled WAVs, non-blocking; speak() follows the same knob.
46. [x] (2026-07-11) Edge flash was double-firing alongside the badge every recording; now fires only as fallback when badge or preview construction fails.
47. [x] (2026-07-11) winfx.reduce_motion() (SPI_GETCLIENTAREAANIMATION) + 100-400ms clamp inside all shared animation primitives; dashboard honours prefers-reduced-motion; Animations toggle in Settings; countdown bar drops to discrete steps when motion is off.
48. [x] (2026-07-11) mic_error/model_error red badges with plain next-step copy (also fixed a real bug: failed InputStream start left is_recording() stuck True with a frozen badge); whisper-down + too-short already met the bar; LM Studio copy upgraded; sidebar status dot wired live (Ready / Whisper down / LM Studio down, 15s poll).
49. [x] (2026-07-11) emptyState() helper + SVG glyphs: History never-used vs filter-empty kept distinct; Dictionary corrections + vocabulary each get icon, one-line what, one-line how-to. Home "Recent Activity" left plain (out of scope, flagged).
50. [ ] One premium accent applied uniformly; today the dashboard hardcodes `--acc:#60cdff` while Tk surfaces have their own palette, and it should follow the brand decision in PRODUCTION_PLAN P1. (M/S) — audit.

## Premium UI/UX enhancements, second batch (2026-07-10)

Items 51-100, generated inline against the first sweep's research and codebase audit; deduped against 1-50.

### Popup / preview panel, advanced

51. [x] (2026-07-11) Live partial mechanism upgraded to feel streaming: tighter cadence (1.0s/0.7s) with latency-adaptive backoff, 20s tail window so long holds stop degrading, LCP word-diff renders stable words committed (_FG2) vs shifting tail muted (_FG3), pixel-wrapped 1→2 lines then ellipsis. Needs Balu's eyes on a real dictation.
52. [ ] Pre-warmed hidden popup window reused across dictations (create once, show/hide) so the panel appears <100ms after release. (H/M)
53. [x] (2026-07-11) Auto-dismiss countdown as a thin depleting accent bar along the panel's bottom edge; restarts on typing/focus/hover. (Bar, not ring — Canvas arcs jank in Tk.) Also fixed a pre-existing stale-after()-callback bug on close.
54. [x] (2026-07-11) Drag panel by header, saved per monitor device name to config.json `panel_position`, clamped to work area on restore. Note: a saved drag overrides `_preview_position` mode unconditionally — flag if fixed mode should win.
55. [x] (2026-07-11) 📌 pin in the panel header suspends auto-dismiss preserving remaining time; unpin resumes; pinned state visibly distinct.
56. [ ] Compact-to-expanded panel modes (one-line pill vs multi-line editor) with animated resize. (M/M)
57. [x] (2026-07-11) Word/char count existed already; added WPM from utterance duration (plumbed duration through preview.show), fixed at panel open, counts stay live.
58. [x] (2026-07-10) Target app shown in the panel header ("→ VS Code", "→ TaskFlow" in task mode) via inject._get_exe_name; app icon (not just name) still open.
59. [ ] Per-word confidence heatmap toggle using the word confidences we already have (subtle underline shades, not colour-only). (M/M)
60. [x] (2026-07-11) Already implemented (preview.py:1296-1316 `_chip()`, theme-aware, ↵/Ctrl+↵/Esc/Ctrl+R/Shift+↵); ticked on audit, no change needed.
61. [ ] N-best alternatives picker: arrow through whisper's alternative transcriptions for ambiguous utterances. (M/L)
62. [ ] Ghost preview: translucent caret-anchored hint of the text about to paste. (L/M)

### Window chrome / Windows integration

63. [x] (2026-07-10) Titlebar synced to app theme (DWMWA_USE_IMMERSIVE_DARK_MODE on shown + live on toggle), verified by screenshot.
64. [x] (2026-07-11) Dashboard geometry persisted to config.json `dashboard_window` (600ms debounce, clamped to visible screens); save+restore verified E2E (moved to 150,120 → reopened at 150,120).
65. [ ] Taskbar jump list: Recent dictations / Settings / Pause (pywin32). (L/M)
66. [ ] Native Windows toasts for background events (task captured while in another app), quiet-hours aware. (M/M)
67. [ ] Fullscreen/game detection: suppress the popup, confirm via edge flash + sound, park text on the clipboard. (H/M)

### Tray, beyond items 21-26

68. [x] (2026-07-11) Recent Dictations tray submenu: top 5, 40-char truncation, reads history fresh at menu build, click copies + tray notify, disabled empty state.
69. [x] (2026-07-11) Pause submenu: 15 min / 1 hour / until restart / Resume, threading.Timer auto-resume, remaining minutes in tray tooltip.
70. [x] (2026-07-11) More → Microphone submenu: radio-checked input devices + System default, writes `input_device`, existing hot-reload applies it from the next recording.
71. [ ] Queued-items badge overlay on the tray icon (tasks captured while away). (L/M)

### Settings, beyond items 27-34

72. [x] (2026-07-11) Diagnostics page + GET /diagnostics + 5s mic probe (409 during real recordings, auto-stop). Transport via pywebview bridge, NOT CORS (first pass added Access-Control-Allow-Origin:* which would have exposed /history to any local webpage — caught in review, removed). API + probe verified E2E, page screenshot-verified light theme.
73. [ ] "Test your mic" wizard with a sample transcription and feedback. (M/M)
74. [ ] Model picker (tiny to large-v3) with plain-language speed/accuracy tradeoffs and VRAM cost shown. (H/M)
75. [ ] Per-app profiles: different formatting/paste behaviour per target app (code-friendly in VS Code, prose in Word). (H/L)
76. [x] (2026-07-11) Export/import via file dialog (Downloads fallback), validates known keys, excludes dashboard_window, designed error toasts, never raises on bad files.
77. [x] (2026-07-11) About page: VERSION "0.1.0" (new constant — confirm scheme), description, licence line, whisper.cpp/LM Studio credits, hardcoded changelog, disabled update button (offline). Verified by screenshot.
78. [x] (2026-07-10) Autostart toggle in Settings → System via schtasks ONLOGON /RL HIGHEST (plain-task fallback when not elevated).
79. [x] (2026-07-11) dashboard_scale select 90-125% via documentElement zoom, live + persisted; dashboard only (Tk is DPI-handled).
80. [x] (2026-07-11) :focus-visible accent ring, toggles as role=switch with Enter/Space + aria-checked sync, focus-within reveals row actions, aria-labels on icon-only buttons, Esc exits select mode. Covers the dashboard half of item 97.

### History, beyond items 35-40

81. [x] (2026-07-11) Day-grouping headers (Today / Yesterday / weekday / date), derived from the filtered list so they collapse with search. Verified by screenshot.
82. [x] (2026-07-11) Star toggle per entry, `pinned` field in history.py, cap eviction skips pinned, "Pinned" group above day groups; backwards-compatible.
83. [x] (2026-07-11) All/Dictation/TaskFlow/Agent chips, AND-combined with text search, verified by screenshot. main.py now tags agent-session saves source="agent" (was untagged, chip would've been empty forever).
84. [x] (2026-07-11) Select mode with per-entry checkboxes, select all, delete (single confirm, warns on pinned), export via file dialog with Downloads fallback.
85. [x] (2026-07-11) Theme-aware `<mark>` highlight on matches; HTML-escaped per segment, no raw-text innerHTML injection.
86. [x] (2026-07-11) Privacy mode: incognito toggle in tray + Settings (config.json `incognito`, hot-reload, preview panel indicator), plus `redact_patterns` regex list applied at history-save time only.
87. [x] (2026-07-11) Settings → History: keep 50/100/250/500 entries + auto-delete recordings never/7/30/90 days, instant-apply; history.py enforces on save and purge-on-startup, pinned exempt, defaults match old behaviour. Smoke-tested in isolation.
88. [x] (2026-07-11) Mini waveform thumbnail per entry with stored audio, rendered server-side via PIL from the WAV's downsampled peaks, cached in-memory, lazy-loaded via IntersectionObserver so History with hundreds of entries doesn't render them all up front.

### Stats and delight

89. [ ] Animated count-up on stat cards plus a words-per-day sparkline. (M/S)
90. [ ] Streaks and personal records (longest dictation, fastest WPM). (L/S)
91. [ ] "Time saved vs typing" estimate — the headline retention metric Wispr Flow leads with. (M/S)

### Onboarding and product polish

92. [ ] Interactive hotkey practice step: detect the user's actual Ctrl+Alt hold and animate the pill live during onboarding. (H/M)
93. [ ] What's-new panel after updates. (L/S)
94. [ ] Contextual tips, max one per day ("Say 'scratch that' to undo"). (L/M)
95. [ ] In-app feedback button that bundles a log excerpt and system info. (M/S)

### Accessibility and system respect

96. [ ] Honour Windows high-contrast mode with a dedicated theme. (M/M)
97. [ ] Screen-reader (UIA) labels on popup and dashboard controls. (M/M)
98. [ ] Reduce-transparency setting honoured: disable acrylic when Windows says so. (L/S)
99. [ ] Deferred model load: boot instantly, load whisper on first hotkey hold with a "warming up" pill state. (M/M)
100. [ ] Idle resource respect: auto-unload whisper after N idle minutes, footprint shown in diagnostics. (M/M)

## Smoother, faster, more accurate — third batch (2026-07-27)

Scope guard: dictation in, read-aloud out. Nothing here reintroduces agent mode, TaskFlow or the fitness divert.
Grounded in the 2026-07-27 corpus pass (100 real dictations, 200 recordings) and the window/insert bugs found the same day.
Impact H/M/L, Effort S/M/L.

### Accuracy — dictionary and the model

101. [ ] Correction learning from panel edits: when the text is edited before inserting, diff it against the transcript and offer the substitution as a `corrections` entry. (H/M)
102. [ ] Dictionary page shows each correction's hit count and last-fired date, so dead entries can be culled. (M/S)
103. [ ] Bulk-import vocabulary from a project's own files (README, CLAUDE.md, config keys) to seed domain terms per app. (H/M)
104. [ ] Per-app initial_prompt already exists in `per_app_context`; expose it in Settings instead of hand-editing config.json. (M/S)
105. [ ] Warn when initial_prompt plus vocabulary exceeds Whisper's 224-token prompt budget — silently truncated today. (H/S)
106. [ ] Confidence-weighted correction suggestions: surface the lowest-probability words from the response as dictionary candidates. (H/M)
107. [ ] "Re-transcribe with corrections" on a history entry, using its stored WAV and the current dictionary. (H/M)
108. [ ] Batch re-transcribe all retained recordings after a dictionary change, and report what changed. (M/M)
109. [ ] Homophone pass for NZ place and company names (Christchurch, Selwyn, Harcourt, Kove) driven by the vocabulary, not one-off corrections. (M/M)
110. [ ] Flag likely mistranscriptions at dictation time: unknown capitalised tokens, mid-word splits, words with no dictionary entry. (H/M)
111. [ ] Keep the raw transcript in history alongside the cleaned one, so post-processing changes can be replayed over past dictations. (H/S)
112. [ ] Spoken spelling mode: "spell it, K O V E" inserts the letters as a word. (M/M)
113. [ ] Number formatting policy setting: prose ("one solution") vs data ("1 solution"), instead of hardcoded rules. (M/S)
114. [ ] Currency and unit handling: "twenty dollars fifty" to $20.50, "five k" to 5k. (M/M)
115. [ ] Recognise dictated file paths and keep them intact ("D drive dev AI projects"). (M/M)
116. [ ] Auto-capitalise known product names anywhere in the sentence, from the vocabulary list. (M/S) — single-word terms landed 2026-07-27; widen to multi-word terms.
117. [ ] Sentence-level confidence in the panel: dim any sentence below a threshold so it gets read before inserting. (H/M)
118. [ ] Language-model rescoring pass over the n-best whisper output using the vocabulary. (H/L)
119. [ ] Compare large-v3-turbo against large-v3 on the 200-recording corpus and record the WER difference before changing the default. (M/M)
120. [ ] Build a permanent regression corpus: 20 recordings plus their agreed correct text, run as a scored test. (H/M)
121. [ ] Track per-session accuracy: corrections applied per 1000 words, trending over time, on the Diagnostics page. (M/M)
122. [ ] Strip the trailing "..." and stray quote marks Whisper adds when a recording ends mid-breath. (M/S)
123. [ ] Handle mid-sentence self-corrections better ("go to the settings, no, the diagnostics page"). (M/M)
124. [ ] Configurable auto-punctuation strength: as-spoken, light, or full. (M/M)
125. [ ] Dictating into code contexts (VS Code, terminal) with punctuation and casing rules that suit code, not prose. (M/M)

### Speed and responsiveness

126. [ ] Stream the final transcription as it decodes rather than waiting for the whole response, so the panel fills progressively. (H/M)
127. [ ] Keep whisper-server warm with a periodic no-op so the first dictation after an idle hour isn't slower. (M/S)
128. [ ] Show time-to-text in the panel footer (release to panel), so regressions are visible in normal use. (M/S)
129. [ ] Pre-open the clipboard and resolve the paste target while transcription is still running. (M/S)
130. [ ] Reuse the partial-transcription buffer instead of re-encoding the whole recording to WAV at stop. (M/M)
131. [ ] Replace the fixed settle sleeps in inject.py with focus polling, cutting 150-400ms off every insert. (H/M)
132. [ ] Adaptive partial cadence driven by measured server latency rather than the current fixed backoff constants. (M/M)
133. [ ] Page in the model weights on first use so startup stops competing with the hotkey hook. (M/M)
134. [ ] Profile the Tk panel build; cache the fonts, gradients and images rebuilt on every show. (M/M)
135. [ ] Log the gap between hotkey release and the audio-stop callback; it is invisible today. (M/S)

### Insert reliability

136. [ ] Target picker in the panel: choose which window to insert into when the captured one is gone or wrong. (H/M)
137. [ ] Remember the last five insert targets and offer them as a fallback list. (M/M)
138. [ ] Retry a failed insert once automatically before falling back to the clipboard. (H/S)
139. [ ] Make the clipboard fallback loud: a persistent toast with a "Paste now" action that re-attempts the insert. (H/S)
140. [ ] Verify the insert landed by reading back the target's text length where the control allows it. (M/L)
141. [ ] Per-app insert method learning: record which method worked per exe and prefer it next time. (H/M)
142. [ ] Detect a UAC-elevated target before recording, not at insert time, while there is still time to switch windows. (M/S)
143. [ ] Queue an insert when the target is busy (modal open, app not responding) and retry when it settles. (M/M)
144. [ ] Per-app insert history so "scratch that" still works after switching windows. (M/M)
145. [ ] Confirm-before-insert for windows never dictated into before. (L/S)

### The dashboard window

146. [ ] Keyboard shortcut to summon the dashboard from anywhere, mirroring the tray click. (M/S)
147. [ ] Remember which page was open and restore it, instead of always honouring the launch argument. (M/S)
148. [ ] Live-refresh Home stats and Diagnostics the same way History now refreshes. (M/S)
149. [ ] Show a "new dictations since you opened this" pill instead of re-rendering under the user mid-scroll. (M/S)
150. [ ] Close the dashboard subprocess when the main app exits; today it outlives its parent. (H/S)
151. [ ] Restart the dashboard automatically if its process dies while the window was open. (L/M)
152. [ ] Undo for history delete, single and bulk, for ten seconds after the action. (H/S)
153. [ ] Cross-process file lock landed for history.json; config.json has the identical two-writer race. (H/S)
154. [ ] Optimistic UI on pin and delete: update the row immediately, roll back if the call fails. (M/S)
155. [ ] Virtualised history list so 500+ entries stay smooth. (M/M)
156. [ ] Full-text search across all retained recordings' transcripts, not just the capped history. (M/M)
157. [ ] Date-range filter and a calendar heatmap of dictation volume. (L/M)
158. [ ] Per-entry "insert into last app" action from history. (M/S)
159. [ ] Export the whole history as one file, not just the current selection. (M/S)
160. [ ] Warn before Clear All when pinned entries would be lost. (M/S)

### Preview panel

161. [ ] Show which app the text will land in, with its icon, and make it clickable to change target. (H/M)
162. [ ] Hold the panel for a beat after insert in an "inserted" state instead of vanishing. (M/S)
163. [ ] Word-level confidence shading in the panel body, using the probabilities already returned. (M/M)
164. [ ] Inline dictionary add: select a word in the panel, press a key, and it becomes a vocabulary entry. (H/M)
165. [ ] Panel remembers its size per monitor as well as its position. (L/S)
166. [ ] Append mode: hold the hotkey again within N seconds to add to the open panel instead of replacing it. (H/M)
167. [ ] Quick actions on the panel: insert as bullet list, as quote, as lowercase. (M/M)
168. [ ] Escape-to-dismiss should offer to keep the text on the clipboard rather than dropping it. (H/S)
169. [ ] Auto-dismiss countdown pauses on hover or keypress. (M/S)
170. [ ] Multi-monitor: place the panel on the target window's monitor, not the cursor's, when they differ. (M/S)

### Read-aloud and study mode

171. [ ] Transport controls while speaking: pause, resume, skip sentence, restart (STUDY_MODE_PLAN P3). (H/M)
172. [ ] Progress indicator showing position in the passage during a long read. (M/S)
173. [ ] Highlight the sentence being spoken in the source app where the API allows it. (L/L)
174. [ ] Read from the clipboard as well as the selection, for apps that block selection copying. (M/S)
175. [ ] Queue multiple selections to read back to back. (L/M)
176. [ ] Per-app read-aloud speed, so a PDF reads slower than a chat message. (L/S)
177. [ ] Warm the TTS server on selection, before the hotkey, when read-aloud is enabled. (M/M)
178. [ ] Cache synthesised audio by text hash so re-reading the same passage is instant. (M/M)
179. [ ] Study mode: pause length per structure type in Settings, not one global scale. (M/S)
180. [ ] Study mode: spell out acronyms and numbers on first occurrence. (M/M)

### Diagnostics, safety and packaging

181. [ ] Startup self-check reporting whisper-server, mic, hotkey hook and disk space in one toast if anything is wrong. (H/S)
182. [ ] Log rotation for app.log with a size cap; it grows unbounded today. (H/S)
183. [ ] Crash reporter: catch unhandled exceptions in every thread and write a diagnosable bundle. (H/M)
184. [ ] Watchdog that restarts whisper-server if inference fails twice in a row. (H/M)
185. [ ] Disk-space guard before retaining audio, with a clear message instead of a silent failure. (M/S)
186. [ ] Config schema validation with per-key error messages surfaced in Settings, not silently clamped. (M/M)
187. [ ] Back up config.json and history.json on every write, keeping the last five. (H/S)
188. [ ] Encrypt retained recordings and history at rest, given they hold work conversations. (M/L)
189. [ ] Redact patterns should apply to the panel and the clipboard too, not only what is written to history. (H/S)
190. [ ] Per-app incognito: never retain anything dictated into a named app. (M/S)
191. [ ] First-run wizard: mic pick, hotkey test, one practice dictation, vocabulary seeding. (H/M)
192. [ ] Installer and signed binary (PRODUCTION_PLAN P5-P11), blocked on the product name. (H/L)
193. [ ] Auto-update check with a manual apply step. (M/M)
194. [ ] Settings search box; the page is long enough now that scanning it is the slow part. (M/S)
195. [ ] Import and export the whole profile (config, dictionary, voice reference) as one file. (M/S)
196. [ ] Portable mode: keep all state next to the exe so it runs from a USB stick. (L/M)
197. [ ] Local-only usage stats shown as a weekly summary. (L/M)
198. [ ] Health page listing the last ten dictations with duration, inference time and confidence, for spotting drift. (M/S)
199. [ ] Document the hotkey conflicts VoiceDictate has with common apps, and detect them at startup. (M/M)
200. [ ] Prune the recordings directory by total size as well as file count; 200 long dictations is several GB. (H/S)
