# Backlog — voice-dictation

Ideas land here instead of being regenerated in chat.
Tick when done, cull what no longer matters, add a date when adding items.

- [ ] (2026-07-03) Voice profile / cloning: multi-sample build, local TTS (Qwen3 TTS feasibility), narrate mode.
- [ ] (2026-07-03) Personal-assistant expansion of agent mode (open tab, go to site in current browser) — scoped design was approved 2026-07-01, verify what landed.
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

7. [ ] Live smooth waveform in the pill while recording; current badge bars are flat un-antialiased Canvas rectangles (preview.py:754-761). (H/M) — superwhisper, VoiceInk.
8. [ ] Hover-to-reveal stop/cancel controls on the compact pill; idle state stays minimal. (M/S) — superwhisper.
9. [ ] Raw-vs-cleaned transcript toggle in the preview panel before accepting. (M/M) — superwhisper Voice/AI toggle.
10. [ ] Confirmation prompt before processing very long recordings (>30s) to catch accidental holds. (L-M/S) — superwhisper.
11. [ ] Separate status zone in the recording bar ("processing…", "done") from the transcript text region. (M/S) — Windows Voice Access layout.
12. [ ] Selectable listening-state animations (pulse, ink-flow, etc.) as a personalisation touch. (L/M) — VoiceInk's nine animations.
13. [ ] Context-capture badge confirming clipboard/selection was grabbed as context. (L/S) — superwhisper Super Mode.
14. [ ] Acrylic backdrop on the popup via `DWMWA_SYSTEMBACKDROP_TYPE` = `DWMSBT_TRANSIENTWINDOW` (documented Win11 route, not the fragile Win10 accent API). (M/M) — MS Learn system backdrops.
15. [ ] Proper DWM drop shadow via `DwmExtendFrameIntoClientArea`, not legacy `CS_DROPSHADOW`. (M/S) — Cyotek/DWM docs.
16. [x] (2026-07-10) `DWMWA_WINDOW_CORNER_PREFERENCE` in winfx.apply_rounded_region, region mask kept as Win10 fallback (with DPI-scaled radius). Turned out to be the full fix, not just interim — see item 1.
17. [ ] True gradient in the badge; today it is six stacked colour bands simulating one (preview.py:676). (L/S) — audit.
18. [ ] Slide+fade entrance 150-250ms; today it is alpha-fade only, no positional motion (winfx.py:84-92). (M/M) — motion guides.
19. [ ] Adjustable pause-tolerance / wait-time slider so slow speakers aren't cut off. (M/M) — Windows 11 Voice Typing 2026.
20. [ ] Evaluate popup stack migration: raw Win32 layered window (full control, L effort) vs PySide6/QML (GPU-composited 60fps, M) vs pywebview frameless (Chromium AA free, but cold-start latency + no native rounded corners bug #834). (H/L) — rendering agent 8-10.

### Tray + desktop icons

21. [x] (2026-07-10) Monochrome theme-aware tray icon (SystemUsesLightTheme), state as colour dot, pulse moved to the dot; coloured badge kept for the desktop .ico.
22. [ ] Left-click = single default action, right-click = full menu; don't overload one button. (M/S) — Win11 tray convention.
23. [ ] Restructure the tray menu (currently ~13 items flat, tray.py:394-409) into grouped sections with the rare actions in a submenu. (M/S) — audit + tray conventions.
24. [ ] Desktop/installer icon redesign around one bold mic-to-caret glyph with subtle depth at 256px (ties to PRODUCTION_PLAN P2). (H/M) — MS/Apple icon guidelines.
25. [ ] Verify exported ICO bytes; Pillow has a known 255x255-clamp footgun on the 256px frame. (L/S) — Pillow #2264.
26. [ ] Re-render the recording pulse frames against the new icon set (pulse machinery already exists, tray.py:113). (L/S) — audit.

### Settings / dashboard

27. [ ] Settings search bar that filters and highlights matching controls across sections. (H/M) — Raycast Settings v2.
28. [ ] Hotkey-recorder control ("press a key combination…") with live chord display, replacing any dropdown/text binding. (H/M) — PowerToys Keyboard Manager.
29. [x] (2026-07-10) Instant-apply settings (debounced 450ms, "Saved" flash), Save button removed.
30. [ ] Per-section reset-to-defaults, scoped so users can undo just hotkeys or just model settings. (M/S) — Windows settings guidelines.
31. [ ] Expose appearance settings (accent, popup size, position with visual picker, animation style); today every colour, font, and dimension is a hardcoded constant (preview.py:67-118). (M/M) — audit.
32. [ ] One panel per concern, no nested menus (the ShareX failure mode vs CleanShot X). (H/M) — CleanShot comparisons.
33. [ ] Single type scale and 8px spacing grid shared across the Tk popup and the web dashboard; inconsistent spacing across windows is the biggest cheap-vs-premium tell. (H/M) — Raycast/Linear design-system analyses.
34. [ ] Shared design tokens between preview.py constants and the dashboard CSS custom properties so the two stacks can't drift. (M/M) — audit (two independent hardcoded palettes today).

### History

35. [ ] Consolidate the two history views (legacy Tk viewer in preview.py vs dashboard History page) into one. (M/M) — audit.
36. [ ] Filter-as-you-type search over history, no separate search screen. (M/S) — superwhisper sidebar.
37. [ ] Re-transcribe from history (right-click "process again") for after model/dictionary upgrades. (M/S) — superwhisper, Wispr Flow.
38. [ ] Replay audio from history entries; the audio files are already stored in recordings/ (history.py:12). (M/S) — MacWhisper + audit.
39. [ ] Export entries as Markdown/DOCX/SRT/VTT. (L-M/M) — MacWhisper.
40. [ ] Usage/stats tab: words dictated, WPM, top target apps; strong retention hook. (M/M) — Wispr Flow "Your Usage".

### Dictionary and snippets

41. [ ] Usage-ranked dictionary with starred/pinned terms getting transcription priority, auto-populated from user corrections. (H/M) — Wispr Flow.
42. [ ] Voice-triggered snippets: say a trigger phrase, paste a predefined block. (M/M) — Wispr Flow cues.

### Onboarding / first-run

43. [ ] 3-4 step skippable first-run: mic permission → hotkey demo with live feedback → one guided sample dictation → done. (H/M) — Arc onboarding pattern.
44. [ ] Pre-frame the admin/UAC request in plain language ("needs admin to catch your hotkey globally") before the OS prompt fires. (M/S) — 1Password pattern.

### Cross-cutting polish

45. [ ] Sound design pass: consistent start/stop/success/error cues with a volume setting (chime.py exists but is ad hoc). (M/S) — voice-app teardown + audit.
46. [ ] Rework or retire the screen-edge flash (preview.py:176) once the popup entrance animation carries the "hotkey registered" signal. (L/S) — audit.
47. [ ] Global motion policy: 100-400ms caps, state-confirming only, honour Windows reduce-motion. (M/S) — microinteraction guides.
48. [ ] Designed error states on every surface: mic missing, whisper server down, too-short recording, LM Studio absent. (H/M) — ui-states discipline.
49. [ ] Designed empty states for History and Dictionary pages (first-run look matters for a sellable product). (M/S) — ui-states discipline.
50. [ ] One premium accent applied uniformly; today the dashboard hardcodes `--acc:#60cdff` while Tk surfaces have their own palette, and it should follow the brand decision in PRODUCTION_PLAN P1. (M/S) — audit.

## Premium UI/UX enhancements, second batch (2026-07-10)

Items 51-100, generated inline against the first sweep's research and codebase audit; deduped against 1-50.

### Popup / preview panel, advanced

51. [ ] Streaming partial transcript in the recording pill — words appear while you speak (whisper.cpp streaming mode), the single biggest "it's alive" premium signal. (H/L)
52. [ ] Pre-warmed hidden popup window reused across dictations (create once, show/hide) so the panel appears <100ms after release. (H/M)
53. [ ] Auto-dismiss countdown as a subtle progress ring on the panel instead of an invisible timer. (M/S)
54. [ ] Drag-to-reposition the panel, position remembered per monitor. (M/M)
55. [ ] Pin button on the panel to suspend auto-dismiss for long edits. (M/S)
56. [ ] Compact-to-expanded panel modes (one-line pill vs multi-line editor) with animated resize. (M/M)
57. [ ] Word/char count and speaking-pace metadata in the panel footer. (L/S)
58. [x] (2026-07-10) Target app shown in the panel header ("→ VS Code", "→ TaskFlow" in task mode) via inject._get_exe_name; app icon (not just name) still open.
59. [ ] Per-word confidence heatmap toggle using the word confidences we already have (subtle underline shades, not colour-only). (M/M)
60. [ ] Keyboard shortcuts rendered as key chips on the panel buttons (Enter, Esc, Ctrl+R). (M/S)
61. [ ] N-best alternatives picker: arrow through whisper's alternative transcriptions for ambiguous utterances. (M/L)
62. [ ] Ghost preview: translucent caret-anchored hint of the text about to paste. (L/M)

### Window chrome / Windows integration

63. [x] (2026-07-10) Titlebar synced to app theme (DWMWA_USE_IMMERSIVE_DARK_MODE on shown + live on toggle), verified by screenshot.
64. [ ] Remember dashboard size/position; play nice with Win11 snap layouts. (L/S)
65. [ ] Taskbar jump list: Recent dictations / Settings / Pause (pywin32). (L/M)
66. [ ] Native Windows toasts for background events (task captured while in another app), quiet-hours aware. (M/M)
67. [ ] Fullscreen/game detection: suppress the popup, confirm via edge flash + sound, park text on the clipboard. (H/M)

### Tray, beyond items 21-26

68. [ ] Recent-dictations submenu in the tray, click to re-copy. (M/S)
69. [ ] Timed pause: "15 min / 1 hr / until restart" instead of a bare toggle. (M/S)
70. [ ] Mic device quick-picker in the tray with a follow-Windows-default toggle. (M/M)
71. [ ] Queued-items badge overlay on the tray icon (tasks captured while away). (L/M)

### Settings, beyond items 27-34

72. [ ] Diagnostics page: live mic level meter, whisper server status and latency, GPU device, hotkey hook health. (H/M)
73. [ ] "Test your mic" wizard with a sample transcription and feedback. (M/M)
74. [ ] Model picker (tiny to large-v3) with plain-language speed/accuracy tradeoffs and VRAM cost shown. (H/M)
75. [ ] Per-app profiles: different formatting/paste behaviour per target app (code-friendly in VS Code, prose in Word). (H/L)
76. [ ] Import/export settings + dictionary as one file for backup/migration. (M/S)
77. [ ] About page: version, changelog, licences, update check — sellable-product must-have. (M/S)
78. [x] (2026-07-10) Autostart toggle in Settings → System via schtasks ONLOGON /RL HIGHEST (plain-task fallback when not elevated).
79. [ ] UI scale / font-size setting. (M/M)
80. [ ] Full keyboard navigation with visible focus rings across the dashboard. (M/S)

### History, beyond items 35-40

81. [ ] Day-grouping headers (Today / Yesterday / This week). (M/S)
82. [ ] Pin/favourite entries that survive the 100-entry cap. (M/S)
83. [ ] Source filter chips: dictation / TaskFlow / agent. (M/S)
84. [ ] Bulk select for delete/export. (M/S)
85. [ ] Search-term highlighting in history results. (L/S)
86. [ ] Privacy mode: incognito dictation (no history/audio), optional pattern redaction (emails, numbers). (M/M)
87. [ ] Retention policy: keep N days/entries, auto-purge recordings. (M/S)
88. [ ] Mini waveform thumbnail per entry with inline play for the stored audio. (L/M)

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
