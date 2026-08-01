# Production Plan — from working tool to sellable product

Written 2026-07-10.
Supersedes ROADMAP.md Phase 4 detail (note: ROADMAP 4.2 still says "CT2 binaries", stale — the engine is whisper.cpp Vulkan now).
Execution order at the bottom is the resume point.

## Part 1 — Breakdown analysis (as-is)

### Architecture
- Engine: whisper.cpp server (Vulkan, large-v3-turbo-q5_0) as a child process, HTTP transcription. Solid, shippable, MIT licence.
- Capture: sounddevice; hotkeys via `keyboard` lib low-level hook (needs admin in some setups — installer implication).
- Injection: clipboard paste with save/restore (inject.py, 38KB — battle-tested incl. RDP sticky-modifier fix).
- ~~Agent mode: LM Studio + Qwen2.5-1.5B~~, ~~TaskFlow integration~~ and ~~Local FitnessPal divert~~ — all DELETED from the product 2026-07-25 (de-bloat ruling). No LLM ships with, or is required by, this app any more, which removes the LM Studio redistribution blocker outright.
- Read-aloud: qwentts.cpp server (Vulkan, Qwen3-TTS), MIT code and Apache 2.0 weights, subprocess with idle reaper. Shippable; see STUDY_MODE_PLAN.md for where narration is heading.

### UI surfaces (the polish targets)
1. **Preview panel** (preview.py, 97KB Tkinter): themed, Segoe UI Variable, toasts, badges, edge-flash. Biggest file, monolithic.
2. **Recording badge + toasts** (in preview.py): functional, near-production.
3. **Tray** (tray.py): programmatic dot icon with pulse. Icon is placeholder-quality — replace with brand icon.
4. **Dashboard/settings** (dashboard.py, 47KB pywebview/Edge): the right tech for production UI; this is where design investment pays off most.
5. **First-run experience: none.** App assumes a configured dev machine. This is the single biggest gap to sellable.

### What is already production-grade
Transcription quality pipeline (corrections, custom vocabulary, speech profile), clipboard injection robustness, health checks, history, read-aloud with study mode, multi-res icon export plumbing (tray.export_ico), create_shortcut.ps1. (Reformat and snippets were removed 2026-07-25.)

### What is not
No installer, no onboarding, no brand, placeholder icon, personal integrations baked in, config.json hand-edited paths, models/ path assumptions, no crash reporting, no update channel, no licence gate, GPL risk unchecked (see Part 5).

## Part 2 — UI production polish (per surface)

Apply the design-suite skills in phase order when executing (ux-psychology → reference-teardown/ux-patterns → flow-benchmark for onboarding → ui-states → design-critique).
Reference products to tear down: Wispr Flow, SuperWhisper (mac), Windows Voice Access — all solve "ambient mic UI" already.

### 2.1 Preview panel
- Keep Tkinter (rewrite is not justified); tighten to a single design-token block: one accent, 2 neutrals per theme, one radius, one shadow treatment via layered frames.
- States pass (ui-states): empty transcription, very long text, transcription error, whisper server down, mid-edit resize. Each needs a designed state, not a default.
- Micro-polish list: consistent 8px spacing grid, fade-in ≤120ms, Escape/Enter affordances visible (keycap hints in footer), width clamp with wraplength tied to it.

### 2.2 Recording badge
- Replace text "0:00" emphasis with waveform-lite level meter (audio.py already has RMS); this is the "it's alive" moment demo videos sell on.
- Brand logo in badge already wired (_build_logo_variants) — swaps in for free once the brand icon lands.

### 2.3 Tray
- New brand icon (Part 3) at 16/20/24/32px hand-checked, not just LANCZOS downsample; 16px is what users see 99% of the time.
- Menu: group into Dictation / Modes / Tools / App with separators; add "Open Settings" as default double-click action.

### 2.4 Dashboard (main investment)
- Treat as the product's face: settings, history, stats, onboarding all live here.
- Design token sheet in one CSS block; light+dark; Segoe UI Variable to match the panel.
- Add pages: Onboarding wizard (2.5), About/licence, Update check.

### 2.5 First-run onboarding wizard (in dashboard)
- Steps: welcome → hotkey choice (push-to-talk vs toggle) → mic pick with live level → model download with progress (574MB) → test dictation into a built-in textbox → success + "try it anywhere".
- Flow-benchmark this against Wispr Flow's onboarding before building.
- Skip flag: `%APPDATA%\<AppName>\onboarded.flag`.

## Part 3 — Brand + custom icons

### 3.1 Naming (blocks everything downstream — decide first)
- Candidates to evaluate: VoxKey, Whispr is taken, TalkType is taken (check all against trademark + domain).
- Ruling needed from Balu; park in brain/rulings.md once decided.

### 3.2 Icon design brief
- Concept: a solid rounded-square app tile (Windows 11 style) with a stylised mic-to-cursor glyph — mic silhouette whose stem becomes a text caret. Reads at 16px.
- Deliverables: master SVG; ICO with 16/20/24/32/48/64/128/256 embedded; tray state variants (idle/recording/transcribing/paused/error) as tint+glyph-dot changes, not shape changes; installer banner (164×314 BMP for Inno) and wizard image (55×58).
- Production path: design the master as SVG by hand (code-drawn, reviewable), render via Pillow/cairosvg script in scripts/make_icons.py, replacing the current programmatic dot in tray.py with loaded assets + programmatic tinting for states.
- Design-critique pass on the 16px render before accepting.

### 3.3 Visual identity minimum
- One accent colour (current blue #3b82f6 is fine, verify contrast in light theme), wordmark = name set in Segoe UI Variable Display semibold, no more identity than that for v1.

## Part 4 — Desktop icon + installer + distribution

### 4.1 Build pipeline
- PyInstaller **onedir** (not onefile: slow start, more AV false-positives) → `dist/<AppName>/`.
- Bundle: whisper-server.exe + Vulkan deps, NOT the model (574MB — download on first run with checksum + resume).
- Exclusions audit: strip tests, recordings/, profile.db, history.json.
- Smoke script: launch exe on a VM without Python, run one dictation.

### 4.2 Installer (Inno Setup)
- Per-user install to `%LOCALAPPDATA%\Programs\<AppName>` (no admin needed for install).
- Options page: desktop shortcut, autostart (HKCU Run key, replaces launch.vbs), Start Menu entry.
- Hotkey hook may need elevation on some machines: detect at runtime and offer "restart as admin" toast, don't force admin install.
- Uninstaller must remove Run key and offer to keep/delete user data.

### 4.3 Trust + updates
- Code signing cert (~US$300/yr, SSL.com OV or Azure Trusted Signing ~US$10/mo) — without it SmartScreen kills conversion.
- Update check: static JSON on GitHub Releases/Cloudflare, compare semver, toast "Update available" → download installer. No silent auto-update in v1.
- Crash reporting: opt-in, local log bundle the user can email; no telemetry by default (privacy IS the product).

### 4.4 Licence gate
- Offline-validatable signed licence key (Ed25519 signature over email+tier+expiry), sold via LemonSqueezy or Polar.sh (they handle GST/VAT — matters for NZ).
- Free tier: full dictation, watermark-free; Paid: cloned-voice read-aloud and study mode, profiles, priority models. Keeps piracy pressure low. (Was "agent mode"; that feature no longer exists.)

## Part 5 — Legal/commercial blockers (check before any sale)

1. ~~**`keyboard` lib is MIT — but verify; `pystray` LGPL**~~ Confirmed 2026-08-01 via full audit (LICENSES.md): `keyboard` MIT, `pystray` LGPLv3 (dynamic linking OK, needs an attribution/NOTICE entry, documented). No GPL/AGPL anywhere in the dependency tree.
2. ~~**LM Studio cannot be redistributed or required.**~~ Resolved 2026-07-25, re-confirmed 2026-08-01 with grep evidence: nothing in the app uses LM Studio now. Blocker closed.
3. Whisper large-v3-turbo weights MIT, whisper.cpp MIT — confirmed 2026-08-01 (LICENSE file on disk). Qwen3-TTS engine (qwentts.cpp) and weights believed MIT/Apache-2.0 but **not verifiable from files on disk** (no LICENSE/model card vendored locally) — open item, pull both before ship.
4. EULA + privacy policy — drafted 2026-08-01 as EULA-DRAFT.md (NOT LEGAL ADVICE, Balu + lawyer review required before any sale).
5. Third-party licence NOTICE file generated into the installer — still TODO, blocked on item 3's two open verifications; full table ready in LICENSES.md once closed.

## Part 6 — Execution order (resume point)

Each step is one session-sized chunk; tick as done.

- [ ] **P1. Name decision** (Balu) + domain check → rulings.md.
- [ ] **P2. Icon set**: master SVG + scripts/make_icons.py + tray.py loads assets; design-critique the 16px. (No name needed if glyph-only.)
- [ ] **P3. Feature-flag personal bits**: voice-profile / cloned-voice reference behind a config flag defaulting off in "product" mode. (TaskFlow, agent mode and the Fitness Pal divert were deleted outright 2026-07-25, so they need no flag.)
- [ ] **P4. Dashboard design-token pass** + light theme fix-ups (design suite, full order).
- [ ] **P5. Onboarding wizard** in dashboard incl. model downloader.
- [ ] **P6. PyInstaller onedir build** + smoke on clean VM.
- [ ] **P7. Inno Setup installer** + uninstall correctness.
- [ ] **P8. Licence audit + NOTICE + EULA/privacy page.** Audit drafted 2026-08-01: LICENSES.md (full dependency table, verdicts) and EULA-DRAFT.md (NOT LEGAL ADVICE) written. No GPL/AGPL found; pystray LGPLv3 needs an attribution entry only, not a blocker. LM Studio blocker confirmed closed (grep evidence, no functional references remain). Two paperwork gaps open: qwentts.cpp engine licence and Qwen3-TTS weight licence are believed MIT/Apache-2.0 but not verifiable from files currently on disk (no LICENSE/model card shipped locally) — pull both before ship. NOTICE.txt generation still TODO (P7 scope, once the two gaps close). **Balu review of both docs pending.**
- [ ] **P9. Signing cert + SmartScreen test.**
- [ ] **P10. Licence key gate + LemonSqueezy checkout.**
- [ ] **P11. Landing page + 60s demo capture** (growth agent).

P2 can start immediately next session; P1 only gates the wordmark/installer strings.
