# Quiett UI plan — bringing the showcase design into the real app, local only

Written 2026-07-30.
Source design: kove-site\quiett-demo\app.html ("ion glass" identity) + SHOWCASE_DESIGN.md tokens.
Scope ruling: LOCAL ONLY. No Cloud AI page, no Meeting Mode page, no licence/installer/telemetry work, no network calls, no web fonts. Those live in PRODUCTION_PLAN.md for later.
Companion refs: BACKLOG items are cited so they get ticked, not duplicated. Execution follows this doc; each phase is one session-sized chunk, verified via restart-app + screenshots before moving on.

## 0. Feasibility summary

| Surface | Stack | Fidelity to showcase | Why |
|---|---|---|---|
| Dashboard | pywebview / Edge WebView2 (Chromium) | ~95% | backdrop-filter, gradients, grain, glow all work natively |
| Preview panel + badge | Tkinter + DWM | ~70% | DWM gives acrylic + rounded corners + shadow; no CSS glow/blur inside Tk widgets |
| Tray | pystray + PIL masters | 100% | icon pipeline already exists (make_icons.py) |

Fonts, fully local: Segoe UI Variable (ships with Win11) for UI and display numerals, Cascadia Mono (ships with Win11) replacing the showcase's JetBrains Mono. Nothing downloaded, ever. The grain texture is an inline SVG data URI.

## 1. P1 — One token sheet, two stacks (BACKLOG 33, 34, 50)

The root cause of "cheap vs premium" drift is two hand-maintained palettes: dashboard CSS custom properties in dashboard.py's _HTML, and Tk constants at preview.py:67-118.

- New module `theme.py`: single source of truth. A dict of the ion-glass tokens (void #04050A, glass rgba steps, hair/hair2, text #F2F5FA / mid #8A94A8 / dim #566074, ion #7DE8FF, ion-deep #3F8CFF, rec #FF6B5E, pause #FFB86B, radius 8/12/16, type scale 12/13/15/18/24/34, 8px grid).
- dashboard.py renders its `:root{}` block FROM theme.py at page build (it already assembles _HTML in Python, so this is string substitution, not architecture).
- preview.py imports the same dict for its Tk colour constants. Tk needs flat hex, so theme.py precomputes the composited glass colours against the void background (Tk has no alpha compositing).
- tray.py state colours come from the same dict.
- Danger note from 2026-07-12: dashboard _HTML is a NON-RAW Python string; any `\n` inside generated JS must be escaped `\\n`. Keep the node --check gate in the verify step of every phase.

## 2. P2 — Dashboard shell restyle

Target: the showcase shell, minus Cloud AI and Meeting Mode entries.

- Aurora background (three radial gradients on the void) + grain overlay + glass window styling. WebView2 supports backdrop-filter; the dashboard is an opaque window, so "glass" is gradient-on-dark, which is what the showcase actually does for the window body.
- Rail: borderless, gradient-hairline divider, section labels (DICTATE / SYSTEM), active item = ion glow tick + brightened icon. Pages: Home (new, P3), Dictation, Voice (P6), Dictionary, History, Diagnostics, Settings.
- Titlebar: keep native window chrome (the dashboard is a normal OS window; do NOT fake window buttons), but add the brand row inside the page: glyph + QUIE TT wordmark + version tag, and the "LOCAL ONLY" seal chip. The seal is honest here: it can key off the same check Diagnostics uses (open-socket listing), showing sealed state from real data.
- Status strip ("voice line") along the dashboard bottom: breathing waveform SVG (rAF, respects the existing animations:false and OS reduce-motion paths), model + GPU text from health.py state, hotkey reminder with keycap chips.
- Controls restyle: hairline-capsule toggles, segmented pills, glow slider thumbs, keycap component. Instant-apply behaviour unchanged.
- Both themes: ion glass is the dark theme. Light theme = derived token set in theme.py (same hues, inverted neutrals, accent darkened for contrast, glow reduced); the dashboard already live-switches dark/light/system, keep that working. Verify contrast 4.5:1 body text in both.

## 3. P3 — Home page (new; BACKLOG 40 is the data half)

The showcase Home, powered by real data from history.json + speech profile:

- Hero numeral: words this week (sum over history), Segoe UI Variable Display Light at display size, delta vs prior week.
- Metric row: average WPM (already computed per dictation), ×-faster-than-typing (against the 40wpm constant), time reclaimed (words/(wpm typing) − words/(wpm actual)), dictation count.
- Rhythm strip: 14 hairline bars from words-per-day, today glowing. Plain divs, no chart library.
- Recent list: last 3 dictations with target-app glyph chips (inject._get_exe_name is already stored per entry).
- Education card: keycap hotkey reminder; button = "Open History". No simulate button in the real app (real dictations are one hotkey away; a fake one is showcase-only).
- Empty states designed (first-run: no history yet → the education card becomes the hero).

## 4. P4 — Panel + badge, the Tk reality (BACKLOG 14; parts of 31)

What transfers to Tk directly: token recolour (P1), the ion waveform gradient (the PIL gradient renderer from item 17 just takes new stops), status row copy, WPM footer, countdown bar colour, keycap hint chips, target-app chip.

What needs DWM:
- Acrylic backdrop: DWMWA_SYSTEMBACKDROP_TYPE = DWMSBT_TRANSIENTWINDOW on the panel + badge (BACKLOG 14, documented Win11 route). Fallback: current solid surface when the call fails.
- Rounded corners + shadow: already shipped (items 15/16).

What does NOT transfer inside Tk: CSS glow shadows, translucent hairline gradients. Do not fake them with images; the acrylic + palette carries the look.
- BACKLOG 20 (popup stack migration to pywebview/Win32) remains DEFERRED per the 2026-07-11 ruling. If Balu wants 100% showcase fidelity on the popup after seeing P4, that ruling gets reopened with this plan as input; it is not part of this scope.

## 5. P5 — Tray + icon set (PRODUCTION_PLAN P2, now unblocked by the name)

- Master glyph: the mic-to-caret mark from the showcase (mic capsule, arc, caret stem) in the ion gradient for the desktop .ico, monochrome for the tray per the theme-aware system already shipped (item 21).
- scripts/make_icons.py renders the set (16-256 ico, tray PNGs, logo for the badge via _build_logo_variants which is already wired).
- Hand-check the 16px render (design-critique pass) before accepting; --verify flag exists for regression.
- Tray tooltips already say Quiett; state dot colours move to theme.py tokens.

## 6. P6 — Voice page + Dictionary upgrades (local features, real guts)

- Voice page (promoted out of Settings): orb identity (CSS, dashboard-side), speed segmented control (writes tts_speed/study_speed exactly as the tray does), Study Mode toggle, and the pause-plan visual rendered from REAL narration.py output: run the segmenter on a fixed sample passage and draw actual segment widths and pause durations. The showcase faked this; the app doesn't have to.
- "Play sample" button: speaks the sample passage through the normal tts.py path (local, already lazy-loaded).
- Dictionary auto-learn (BACKLOG 41): promotion rule "same correction applied 3 times → auto-add to corrections" using the existing speech-profile correction tracking; toggle "Learn from my edits" (default on, config key learn_from_edits); "Learned this week" feed from promotion timestamps. This is the one new backend feature in the plan and it is pure local logic in the existing pipeline.

## 7. P7 — Diagnostics + close-out

- Diagnostics restyle to probe rows with ion ticks; keep the pywebview bridge transport (the 2026-07-11 CORS lesson).
- New probe, the trust feature: network isolation = enumerate this process tree's open sockets (psutil, already a dependency, or netstat fallback) and show "N connections open" with the expected loopback-only list (whisper-server 8089, API 8090, dashboard control 8093). This makes the "local only" seal a measurement, not a promise.
- Light-theme sweep of every new surface, screenshot both themes.
- Final design-critique pass against the showcase, side by side; fix list before calling the project done.
- Full suite green, restart-app, and one real dictation + one read-aloud exercised on the new UI.

## Explicitly out of scope (production track, PRODUCTION_PLAN.md owns these)

Cloud AI page and any cloud/LLM cleanup work, Meeting Mode (page and guts), onboarding wizard for strangers (P5 there), installer/licensing/signing (P6-P10), the kove.nz side, web fonts or any network fetch, telemetry of any kind.

## Order and sizing

P1 tokens (S) → P2 shell (M) → P3 home (M) → P4 panel/badge (S-M) → P5 icons (S) → P6 voice+dictionary (M) → P7 diagnostics+close (S).
Each phase: code → node --check on dashboard JS → pytest → restart-app skill → screenshot verify (PrintWindow pattern from scratchpad capture_dash.ps1 works when occluded) → tick here and in BACKLOG.
