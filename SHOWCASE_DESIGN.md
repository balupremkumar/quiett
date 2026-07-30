# Showcase design brief — VoiceDictate premium mockup + reel

Written 2026-07-30. Feeds kove-site\voicedictate-demo\ (mock + reel) and the work-voice-dictation.html refresh.
Live app untouched; this is a standalone showcase.
Teardown done from knowledge of Wispr Flow, Raycast, Linear/CleanShot X (no current screenshots pulled).

## Teardown

| Reference | Eye path | Primary action | States | Trust signals | Steal |
|---|---|---|---|---|---|
| Wispr Flow | greeting/stat → recent notes → nav | hotkey education ("just talk") | onboarding-led, stats home | WPM/streak numbers, logos | stats-as-home; persistent hotkey education; bottom mic status |
| Raycast | content → keycap chips → rail | search/command | settings searchable, dense-but-airy | keyboard-first affordances | keycap chips; 1px hairlines; low-sat accent; settings discipline |
| Linear | headline type → nav → detail | one per screen | calm loading, subtle motion | typography quality itself | type-led hierarchy; 8px grid; 120-200ms state-confirming motion only |

Convention extracted: left rail (icons + labels), one accent, neutral dark surfaces with hairline borders, keycap chips for every shortcut, stats on home, persistent status footer, motion only to confirm state.

## Deliberate deviations (job stated)

1. Privacy is a first-class UI element: "Local only" status chip in the shell footer and a Cloud AI page whose hero is an OFF switch + data-flow diagram. Job: the positioning story must be visible in a 5s scan, no competitor does this.
2. Study Mode gets its own identity moment (segmented playback with visible pause markers). Job: the unclaimed differentiator has to be seen, not read about.

## Tokens (product brand, not Deep Cove; harmonious when embedded)

- Surfaces: bg #0A0C10, surface #10141B, elevated #161C26, hairline rgba(148,178,215,.10), strong rgba(148,178,215,.2)
- Text: #E9EEF6 / muted #97A3B6 / faint #6C7A8F
- Accent: #4CC2FF, gradient 125deg #7BD8FF → #4CC2FF → #3F8CFF (sparingly: primary CTA, active nav, waveform)
- States: recording #FF5C6A, success #3DDC97, warn #FFC24B (study mode borrows warn as its warm identity)
- Type: 'Segoe UI Variable Display'/'Segoe UI' first (Windows authenticity), Inter webfont fallback; JetBrains Mono for numbers/paths
- Scale: 12/13/15/18/24/34; 8px grid; radius 8 controls / 12 cards / 16 windows; layered soft shadows; light theme variant included
- Motion: 120-200ms ease-out, state-confirming only, respects prefers-reduced-motion

## Screen inventory (mock, one primary action each)

Shell: Win11-style titlebar, left rail (Home, Dictation, Voice, Dictionary, History, Cloud AI ·soon, Meeting Mode ·soon, Diagnostics, Settings), footer status "● Whisper ready · GPU · Local only".
1. Home — headline stat (words this week), WPM sparkline, time saved, streak, recent dictations, hotkey education card (primary).
2. Dictation — hotkey recorder chips, mic picker with live level, silence slider, insert behaviour, incognito + redaction.
3. Voice — cloned voice card, speed radios, Study Mode toggle + visible pause plan (primary = play sample).
4. Dictionary — vocabulary chips, corrections table, auto-learn toggle + "learned this week" feed.
5. History — search, day groups, rows (target app, pin, play, export), empty state designed.
6. Cloud AI (soon) — OFF switch hero, data-flow diagram, tone presets, BYO key. States: off (default) and on-preview.
7. Meeting Mode (soon) — speaker-labelled transcript, summary + actions panel.
8. Diagnostics — probes, latency numbers (real ones from the app: 0.87 RTF, ~145ms first audio).
Floating: recording badge (waveform, timer, streaming partial) + preview panel (clean text, keycap chips, WPM footer, countdown bar, pin, "→ VS Code" chip).

## Reel (deterministic t-engine, flightdeck reel.js pattern, ?at= seek, REEL_DONE)

~52s: open card → dictate into editor (hold chips, waveform, partials, panel, insert) → second app (email, target chip) → read-aloud (Ctrl+Shift+S, playback) → study mode (segmented, pause ticks) → dashboard tour (stats, dictionary learn, history) → privacy close ("0 bytes leave your machine", coming-soon strip, end card).

## Honesty locks (PORTFOLIO-HANDOFF)

In development, daily-driven by one user, no installer/pricing claims. Coming-soon features labelled as roadmap. Remove stale command-mode/LLM-rewrite claims from work-voice-dictation.html (features deleted 2026-07-25).
