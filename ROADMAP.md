# Voice Dictation — Product Roadmap

> Positioning: **a sellable, portfolio-grade voice-to-text layer for builders.**
> Differentiators: local-first privacy, universal injection, developer-aware vocabulary, sub-second latency.
> Inspired by competitive analysis of BridgeVoice and similar tools.

## Where we are (Phase 3 — shipped)

- ✅ Hold-to-record global hotkey (Ctrl+Alt default, rebindable from settings)
- ✅ Local Whisper via faster-whisper (small model default, GPU auto-detect)
- ✅ Universal text injection — RDP, VS Code/Electron, browser HTML inputs all work (SendInput + scan codes)
- ✅ Edit-before-paste preview panel with word-level confidence colouring
- ✅ Custom corrections + learned speech profile (SQLite)
- ✅ Whisper `initial_prompt` + custom vocabulary (developer dictionary)
- ✅ Spoken punctuation, number-to-digit, acronym collapse
- ✅ History (atomic writes), privacy pause toggle, settings UI, system tray
- ✅ Rotating logger
- ✅ DPI-aware, multi-mic, hot-reloadable config

## Phase 4 — Productisation (next 2–4 weeks)

**Goal:** make it shippable to non-technical buyers. Brand, polish, distribution.

### 4.1 Brand and positioning
- [ ] Name + tagline (current: "Voice Dictation". Candidates: pick something Brandable)
- [ ] Icon set — 16/32/48/64/128/256 px, light + dark
- [ ] Marketing one-pager: target = developers with RSI / fast prototypers / non-English speakers
- [ ] Landing page (single static page, Cloudflare Pages): hero, 3 features, demo gif, pricing, install button
- [ ] Demo recording (60 s screen capture: prompt-to-Cursor flow)

### 4.2 Packaging and distribution
- [ ] PyInstaller `--onefile --windowed` build → single `.exe`
- [ ] Bundle CT2 binaries + Whisper small model in installer (or download-on-first-run)
- [ ] Inno Setup installer (`.exe` installer with Start Menu shortcut, autostart option)
- [ ] Code sign certificate (DigiCert / SSL.com, ~$300/yr) — avoids SmartScreen warning
- [ ] Auto-update channel via simple version-check JSON on GitHub Releases
- [ ] License key gate (offline-validatable JWT) for paid tiers — can use Polar.sh, LemonSqueezy, or Stripe + custom

### 4.3 First-run onboarding wizard
- [ ] Welcome screen → pick hotkey → pick mic → pick model size (with RAM warning)
- [ ] Sample dictation → verify paste works → success
- [ ] Skip if `~/.voice-dictate/onboarded.flag` exists

### 4.4 Toggle mode alongside push-to-talk
- [ ] New hotkey behavior option: `push-to-talk` (current) vs `toggle` (tap to start, tap to stop)
- [ ] Settings UI: radio button under Hotkey section
- [ ] Visual: badge shows "Recording" with stop button when in toggle mode

## Phase 5 — Cloud + multilingual (4–8 weeks)

**Goal:** match BridgeVoice's 99+ language pitch while preserving local-first default.

### 5.1 Pluggable transcription backend
- [ ] Define `TranscriptionBackend` interface (abstract over faster-whisper)
- [ ] Implementations:
  - `LocalWhisperBackend` (existing, default)
  - `GroqWhisperBackend` (cloud, Groq's whisper-large-v3-turbo — fast and cheap)
  - `OpenAIWhisperBackend` (cloud fallback)
  - `DeepgramBackend` (real-time streaming alternative)
- [ ] Per-recording backend selection (settings + tray submenu)
- [ ] Auto-failover: cloud fails → local; or vice versa
- [ ] BYOK (bring your own key) — stored in Windows Credential Manager via `keyring`

### 5.2 Language UX
- [ ] Auto-detect language (cloud backends support it natively)
- [ ] Quick language switch in tray submenu (top 10 languages)
- [ ] Per-app language override (e.g. always French in WeChat)

### 5.3 Privacy-first defaults
- [ ] Cloud mode requires explicit opt-in per session
- [ ] Always show cloud/local indicator on badge during recording
- [ ] "Send to cloud" badge in preview window
- [ ] No telemetry, no analytics — even for cloud users

## Phase 6 — Sub-second latency + streaming (6–12 weeks)

**Goal:** match the "<1s end-to-end" pitch.

### 6.1 Streaming transcription
- [ ] Feed audio to faster-whisper in 1–2s chunks while recording continues
- [ ] Live preview badge shows partial text under "Recording…"
- [ ] Final pass on full audio (more accurate) overrides partial when user releases
- [ ] Falls back to non-streaming if user disables (settings flag)

### 6.2 Latency wins
- [ ] Preload model at startup (already do) — measure cold vs warm latency, document
- [ ] Optional CTranslate2 `int8_float16` on GPU for faster decode
- [ ] Investigate whisper.cpp via `pywhispercpp` for CPU-only users (often faster than faster-whisper on CPU)
- [ ] Pre-allocate audio buffer to avoid GC during recording
- [ ] Benchmark suite: tests/benchmark.py measures cold/warm/streaming latency per model

### 6.3 Model picker UI
- [ ] Settings → Models section
- [ ] Tiny / Base / Small / Medium / Large-v3 / Large-v3-turbo
- [ ] Show: disk size, RAM estimate, latency benchmark, accuracy tier
- [ ] One-click download from HuggingFace with progress bar

## Phase 7 — Usage analytics for the user (4 weeks)

**Goal:** "track WPM and speaking time" — user-facing personal stats.

### 7.1 Local-only stats panel
- [ ] New tray menu: "Stats"
- [ ] Shows: total words, total dictation time, average WPM, hourly heatmap, top hour, longest streak
- [ ] Per-day chart (last 30 days, simple ASCII bar or Tk Canvas)
- [ ] Export CSV
- [ ] Privacy: data stays in `stats.db` (SQLite), never sent anywhere

### 7.2 Developer-aware insights
- [ ] Which terms get corrected most often → suggest adding to custom vocabulary
- [ ] Which apps get the most dictation → optimisation suggestions
- [ ] Confidence trend over time → "your speech profile is improving"

## Phase 8 — Power-user workflows (ongoing)

### 8.1 Dictation editing commands
- [ ] "scratch that" → delete last sentence
- [ ] "delete word" → delete previous word
- [ ] "capitalize next" → next word starts uppercase
- [ ] "code mode on" → suppress punctuation conversion, keep raw tokens
- [ ] Configurable phrases in settings

### 8.2 Per-app contexts
- [ ] Detect foreground app, load app-specific initial_prompt + corrections
- [ ] Built-in profiles: Cursor / VS Code (code context), Slack (informal), Outlook (professional), Terminal (commands)
- [ ] User can edit profiles in settings

### 8.3 AI-assisted post-processing (optional, opt-in)
- [ ] Pipe transcription through local Llama (Ollama) for grammar cleanup, tone shift, summarisation
- [ ] "Make this professional" / "translate to French" / "expand into a draft email"
- [ ] Activated by a hotkey modifier or spoken trigger ("polish that")

## Phase 9 — Cross-platform (long-term, optional)

**Reality check:** Python+Tkinter is Windows-tuned. Real cross-platform either means:
- (a) Keep Python core, swap UI for Qt/PyQt6 (medium lift) + per-OS injection layer
- (b) Rewrite in Rust+Tauri 2.0 (large lift, matches BridgeVoice positioning)

If pursued: **(b) is the consultancy-grade move.** Rust+Tauri = small binary, native UI, easy code-signing, true cross-platform. Keep Python prototype as v1.

Defer this decision until v1 has 50+ paying users — validate demand before rewrite.

## Phase 10 — Monetisation

### Pricing tiers
| Tier | Price | Features |
|---|---|---|
| Free | $0 | Local Whisper, all on-device features, watermark in history export |
| Pro | $9/mo or $79/yr | Cloud backends (BYOK), streaming, app profiles, no watermark |
| Team | $19/user/mo | Shared vocabulary, team profiles, SSO (future), priority support |
| Lifetime | $199 one-time | Pro features forever |

### Distribution channels
- [ ] ProductHunt launch (after Phase 4 ships)
- [ ] r/programming, r/developertools, HN Show
- [ ] Targeted: r/ergonomics, r/RSI (accessibility angle)
- [ ] LinkedIn: NZ MVP network, agency contacts
- [ ] Consultancy angle: case study showing custom-trained vocab improving accuracy 30%

### Consultancy positioning
This is **proof of capability** for client work:
- Real-time audio pipelines
- Native Windows integration (Win32, scan codes, SendInput)
- ML inference deployment (GGUF, CT2, GPU)
- Privacy-first architecture
- Polished desktop UX in Python
Use the public landing page + GitHub repo as "things I have shipped" portfolio when pitching custom voice/AI desktop work.

## Competitive matrix (for the landing page)

| Feature | Voice Dictation | BridgeVoice | Talon | Whispering | Wispr Flow |
|---|---|---|---|---|---|
| 100% offline option | ✅ | ✅ | ✅ | ✅ | ❌ |
| Cloud Whisper option | Phase 5 | ✅ | ❌ | ✅ | ✅ |
| Universal injection | ✅ | ✅ | ✅ | ⚠️ | ✅ |
| Developer dictionary | ✅ | ✅ | ✅ | ❌ | ❌ |
| RDP support | ✅ (fixed) | ❌ | ❌ | ❌ | ❌ |
| Word-level confidence | ✅ | ❌ | ❌ | ❌ | ❌ |
| Streaming | Phase 6 | ❌ | ❌ | ❌ | ✅ |
| Spoken punctuation | ✅ | ✅ | ✅ | ⚠️ | ✅ |
| Cross-platform | Windows | Mac/Win/Linux | Mac/Win/Linux | Mac/Win/Linux | Mac/Win |
| Price | Free + Pro $9 | $12/mo | $15/mo | Free | $12/mo |

Our wedge: **RDP + word-level confidence + first-class developer dictionary**, all offline, at half the price.

## Priority order if shipping next session

1. **Brand + name + icon** (1–2 hr) — needed before any public anything
2. **PyInstaller exe + Inno installer** (1 day) — turns this from "Python project" to "downloadable app"
3. **First-run onboarding** (4 hr) — every new user dies without this
4. **Toggle mode** (2 hr) — addresses BridgeVoice's "push-to-talk & toggle" line
5. **Groq cloud backend** (1 day) — biggest single feature for landing page (99+ languages)
6. **Landing page + demo gif** (1 day) — needed to sell

That's roughly 1 week of focused work to v1 launch.
