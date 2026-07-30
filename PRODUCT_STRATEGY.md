# Product Strategy — from working tool to sellable product

Written 2026-07-30 from a 3-agent research sweep.
Dossiers: research\voice-dictation-competitors-2026.md, research\voice-dictation-feature-gaps-2026\DOSSIER.md, research\voice-dictation-pricing-2026.md (all under D:\Dev\ai\research\).
Companion docs: PRODUCTION_PLAN.md (engineering path P1-P11, still valid), NAMING.md (shortlist, pick pending), BACKLOG.md (items 1-200).
This doc owns positioning, pricing, feature tiers, the showcase build, and the release roadmap.

## 1. Positioning

**The gap (confirmed by research): nobody pairs genuinely fully-offline processing with premium-grade design.**
Wispr Flow (~$2B valuation talks) owns polish but is cloud-only and does not compete on privacy.
Everyone who owns "private/local" (VoiceInk, Voicetypr, OpenWhispr, Handy, Resonant, Amical, Spokenly) looks indie and utilitarian.
Superwhisper's Windows build is its weakest platform.
The open-source cluster (Handy, 20k+ stars) makes basic local transcription free forever, so price is not a lever; polish and unclaimed features are.

**Unclaimed combination: dictation + cloned-voice read-aloud + study mode.**
No competitor found pairs these.
Speechify's new Windows app (Mar 2026) is the only one combining dictation and TTS at scale, and it is a reading-first brand, not a dictation-first one.

**Positioning statement:**
The premium voice tool for Windows that never sends a word to the cloud.
Dictate anywhere at the speed of speech, and have anything read back in your own cloned voice.
Cloud AI is an option you can turn on, never a requirement.

**Three provable claims to lead with (never vague superlatives):**
1. "Nothing leaves your machine" — provable, demo with Wi-Fi off.
2. Speed: measure real latency on the RX 9070 XT build and quote it exactly (e.g. "words on screen 0.9s after you stop talking").
3. "Your voice reads back to you" — the demo moment no competitor has.

## 2. Name and brand

NAMING.md recommendation stands: **PrivateType** (clean collision check, privatetype.com buyable on BrandBucket), runner-up Quietype.
GATE: Balu's pick. Blocks logo, wordmark, installer strings, website copy.
Brand direction once picked: "quiet luxury" editorial look (research finding: the premium tells are restraint, type quality, and precise claims, not AI gradients).
One accent colour, Segoe UI Variable Display wordmark, mic-to-caret glyph per PRODUCTION_PLAN Part 3.
Logo/icon deliverables: master SVG, multi-res ICO, tray state variants, installer banner (P2 pipeline already exists via make_icons.py).

## 3. Pricing (research-backed recommendation)

Balu's brief asked for a monthly subscription.
The evidence says a subscription on purely local features draws backlash (HN "Show HN: Whispering" thread, Indie Hackers debate) because there is no recurring cost to justify it, and free local competitors exist.
The honest, proven pattern (Superwhisper, CleanShot X) is hybrid:

| Tier | Price | Unlocks |
|---|---|---|
| Free | $0 | Unlimited local dictation, full accuracy pipeline (vocab, corrections, history) |
| Pro | US$39 one-time, 1 year of updates | Cloned-voice read-aloud, Study Mode, voice profiles, priority/large models, meeting mode when built |
| Cloud AI (the subscription) | ~US$4-5/mo add-on | Cloud LLM cleanup, tone-per-app rewriting, voice edit commands, future sync. Optional, never gates anything bought |

The subscription exists, but it is attached to the features with genuine ongoing cost.
If Balu wants pure subscription anyway, the fallback is Pro at US$6-8/mo with a permanent-fallback licence (Jetbrains model); flagged as higher-risk against the $0 local floor.
GATE: Balu's ruling → brain/rulings.md.
Merchant of record: Polar.sh (NZ payout confirmed) or Lemon Squeezy; both have 2026 payout-delay complaints, treat as operational risk, keep records exportable, revisit Paddle at scale.

## 4. Product feature map

### Tier A — real and shipped (the working core)
Hold-to-talk local dictation, edit-before-paste panel, streaming partials, clipboard injection with save/restore, history (search/pin/export/replay), custom vocabulary + corrections + filler removal, incognito + redaction, per-app target indicator, diagnostics, cloned-voice read-aloud, study mode, tray, themed dashboard.

### Tier B — build for real before launch (feasible local, subscription-grade value)
1. **Auto-learning dictionary** (gap #5, Willow's headline): promote repeated user corrections into vocabulary automatically. Extends the existing corrections pipeline; BACKLOG 41.
2. **AI cleanup, local-first** (gap #1): optional local small-LLM pass for punctuation/filler/paragraphing with per-app presets, same feature upgraded by the cloud tier when enabled. NOTE: the 2026-07-25 "no LLM in the app" ruling was about scope bloat (agent mode, TaskFlow); a cleanup pass in service of dictation is a different case, needs Balu to reopen consciously. Subprocess + lazy load + idle reap, same pattern as TTS.
3. **Onboarding wizard** (PRODUCTION_PLAN 2.5): first-run flow, mic pick, hotkey demo, model download, guided first dictation. The single biggest sellable-gap.
4. **Usage stats page** (BACKLOG 40): words, WPM, time saved, top apps. Cheap, strong showcase and retention surface.

### Tier C — UI-built, guts later (showcase surfaces for the website)
Design and build the full UI for these in the dashboard, clearly marked "coming soon" in-app, real on the website roadmap:
1. **Meeting mode**: long-form transcription with speaker labels and summary (proven fully-local by shipping OSS tools; guts are a v1.x project).
2. **Voice edit commands** ("delete that", "rewrite shorter") — settings + preview UI.
3. **Multilingual + translate-while-dictating** — language picker UI; whisper.cpp already has translate task, verify limits.
4. **Cloud AI settings page**: the on/off switch, provider/key management, per-app tone presets, privacy explainer ("off by default, here is exactly what leaves your machine when on"). This page IS the positioning story in UI form.
5. **Snippets/templates by voice** (was culled 2026-07-25 as a built feature; returns as showcase UI only unless Balu re-rules).

### Explicitly out (rulings stand)
Agent command mode, TaskFlow, fitness divert, general personal-assistant expansion.

## 5. Premium UI redesign (the "super premium" pass)

Scope: the dashboard is the product's face; the panel and tray are the daily surfaces.
Execute with the design suite in order (ux-psychology → reference-teardown → ux-patterns → flow-benchmark on onboarding → ui-states → design-critique).
Reference set for teardown: Wispr Flow (category best), Raycast/Linear (system discipline), CleanShot X (single-panel settings).

1. **Design tokens first**: one shared token sheet (accent from brand, 2 neutrals per theme, radius, shadow, 8px grid, single type scale) applied to BOTH the dashboard CSS and preview.py constants (BACKLOG 33/34/50). This kills the cheap-vs-premium drift.
2. **Dashboard restructure**: Home (status + stats), Dictation, Voice (read-aloud/study), Dictionary, History, Cloud AI (showcase), Meeting mode (showcase), Diagnostics, About/licence. Settings search (BACKLOG 27), hotkey-recorder control (28), appearance page (31).
3. **Panel micro-polish**: acrylic backdrop (BACKLOG 14), pre-warmed window <100ms (52), compact-to-expanded modes (56), confidence heatmap (59).
4. **New logo/icon set** through the P2 pipeline once the name lands.
5. **Design-critique pass + screenshot set** at the end; the same screenshots feed the website.

## 6. Website showcase (kove.nz)

Read D:\Dev\ai\handovers\ portfolio brief before building (portfolio-handover memory).
Product page structure, applying the five premium moves from research:
1. Hero: before/after transcript framing (raw speech → clean text), not a feature list. Real latency number under it.
2. 60-second demo video: dictation into VS Code/Outlook, then Ctrl+Shift+S cloned-voice read-back — the unclaimed wow moment. Reuse the Flightdeck reel engine (deterministic ?at= seek) if a scripted reel beats screen capture.
3. "Nothing leaves your machine" section: airplane-mode demo clip, exact data-flow diagram, the Cloud AI page shown with its off-by-default switch.
4. Feature grid from the Tier A+B set, roadmap strip from Tier C ("coming: meeting mode, voice commands, translate").
5. Pricing table (section 3), FAQ (privacy, GPU requirements, Win11-only), quantified claims only.
De-ai-pass on all copy before publish.

## 7. Roadmap

**v0.5 "Product-ready" (this phase):** name + brand + logo, design-token UI redesign, onboarding wizard, stats page, auto-learning dictionary, Tier C showcase UIs, screenshots/demo captured.
**v0.9 "Distributable":** PyInstaller onedir, Inno installer, model-download-on-first-run, licence audit + NOTICE + EULA/privacy, code signing, licence-key gate + checkout (PRODUCTION_PLAN P6-P10).
**v1.0 "Launch":** website page live, Show HN + Product Hunt + dictation-reviewer outreach (the three researched channels), free tier public.
**v1.x:** meeting mode guts, voice edit commands (local), multilingual, per-app tone presets local.
**v2.0 "The subscription turns on":** cloud AI cleanup tier live (BYO-key free mode + hosted paid mode), possible mobile companion/sync exploration, team dictionary if B2B interest shows.

## 8. Gates — ruled 2026-07-30 (in brain/rulings.md)

1. Name: stays VoiceDictate for now; naming question still open, brand work proceeds glyph-only.
2. Pricing: deferred; ruled before the v0.9 licence-gate work. Hybrid remains the recommendation.
3. Local LLM cleanup: APPROVED, cleanup only, off by default, TTS subprocess pattern. Agent/TaskFlow stay dead.
4. Showcase set: all four approved (meeting mode, cloud AI page, voice edit commands, multilingual + translate). Snippets showcase not approved, leave out.
