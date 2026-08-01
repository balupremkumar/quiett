# Third-party licence audit — Quiett

Drafted 2026-08-01 for PRODUCTION_PLAN.md P8. Source: on-disk inspection only (venv metadata, `third_party/` files, requirements.txt, source imports). No web lookups were done — two items below need one before ship.

Verdict key: **OK** = ship as-is with attribution. **ATTRIBUTION** = OK to ship, must include the licence text in a NOTICE/about screen. **VERIFY** = cannot confirm from files on disk, do not ship until confirmed. **BLOCKER** = do not ship without a licence change or replacement.

## 1. Python runtime dependencies (requirements.txt, confirmed via `.venv` package metadata)

| Package | Version | Licence | Obligation | Verdict |
|---|---|---|---|---|
| sounddevice | 0.5.5 | MIT | Attribution | OK |
| numpy | 2.5.0 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 (multi-licensed, all permissive) | Attribution | OK |
| pyperclip | 1.11.0 | BSD | Attribution | OK |
| keyboard | 0.13.5 | MIT | Attribution | OK |
| pywin32 | 312 | PSF (Python Software Foundation License) | Attribution | OK |
| pystray | 0.19.5 | **LGPLv3** | Attribution + source offer (link to PyPI/GitHub) + keep the module replaceable in the build | **ATTRIBUTION — loudest flag on this list, see note below** |
| Pillow | 12.2.0 | MIT-CMU (historical MIT/X11-style, aka HPND) | Attribution | OK |
| pywebview | 6.2.1 | BSD-3-Clause | Attribution | OK |

**pystray LGPLv3 note:** this is the only copyleft licence in the dependency tree, and it's the one PRODUCTION_PLAN Part 5 flagged. Not GPL, not a blocker — LGPL permits linking (including Python import) into a proprietary app provided the LGPL component itself stays replaceable and its licence text is reproduced. Practical actions before ship: (a) include pystray's LGPLv3 text and copyright notice in the NOTICE file / About page, (b) build with PyInstaller **onedir** (already the plan, P6) so pystray ships as a separate module in `dist/<AppName>/`, not statically fused into one exe — this preserves the "user can replace the library" condition, (c) do not modify pystray's source. No code change needed, just the NOTICE entry.

## 2. Transitive packages actually imported at runtime (pywebview's Windows/EdgeChromium backend)

Confirmed present in `.venv` and required by pywebview on Windows; not separate requirements.txt entries but they do ship in any PyInstaller build.

| Package | Version | Licence | Verdict |
|---|---|---|---|
| pythonnet | 3.1.0 | MIT | OK |
| clr_loader | 0.3.1 | MIT | OK |
| comtypes | 1.4.16 | MIT | OK |
| bottle | 0.13.4 | MIT | OK |
| colorama | 0.4.6 | BSD | OK |
| proxy_tools | 0.1.0 | MIT | OK |

WebView2 itself is a Windows-provided runtime component (Microsoft), not bundled by this app — no separate licensing action.

## 3. Installed in `.venv` but NOT used in production code — exclude from the build

`pip freeze` shows a faster-whisper/ctranslate2 stack (faster-whisper, ctranslate2, av, onnxruntime, tokenizers, huggingface_hub, hf-xet) plus dev tooling (pytest, rich, typer, click, Flask, Jinja2, Werkzeug, blinker, itsdangerous). Grepped every production `.py` file (main.py, transcribe.py, tts.py, audio.py, inject.py, hotkey.py, preview.py, tray.py, dashboard.py, api_server.py, health.py, history.py, narration.py, voiceprofile.py, config handling) for `faster_whisper`, `ctranslate2`, `import av`, `onnxruntime` — no matches. These are leftovers from the pre-whisper.cpp faster-whisper era (ROADMAP.md already flags that stale reference) plus the pytest suite's own dependencies. Not a licence question since nothing ships, but flag for PRODUCTION_PLAN P6 (exclusions audit) — PyInstaller must not pull these in, and they should be pruned from the dev venv or split into a `requirements-dev.txt` at some point so `pip freeze` stops being misleading.

## 4. Bundled engines (`third_party/`)

| Component | Licence | Evidence on disk | Verdict |
|---|---|---|---|
| whisper.cpp (engine binary/source) | MIT | `third_party/whisper.cpp/LICENSE` (full MIT text, copyright "The ggml authors") | OK — vendor this LICENSE file into the NOTICE |
| whisper large-v3-turbo-q5_0.bin (model weights) | MIT (OpenAI Whisper upstream) | `third_party/whisper.cpp/models/README.md` links the weights to "upstream (openai/whisper)"; OpenAI's Whisper repo is published MIT. No copy of OpenAI's own LICENSE text is vendored locally. | OK to ship, but **fetch and vendor OpenAI's actual LICENSE text** for the NOTICE file before ship — currently relying on the well-known fact rather than a file on disk |
| qwentts.cpp (engine binaries: `qwen-tts.exe`, `tts-server.exe`, `qwen-codec.exe`, `ggml-*.dll`) | Believed MIT | **No LICENSE or README file shipped in `third_party/qwentts.cpp`** — only compiled `bin/` and `models/` subfolders exist, no source tree. The MIT claim comes from this project's own build notes (STATE.md 2026-07-12: "qwentts.cpp (C++ GGML port of Qwen3-TTS, MIT...)"), not from a file in this repo. | **VERIFY** — pull the LICENSE file from the qwentts.cpp upstream repo and vendor it here before any commercial ship |
| Qwen3-TTS weights (`qwen-talker-1.7b-base-Q8_0.gguf`, `qwen-tokenizer-12hz-Q8_0.gguf`) | Believed Apache-2.0 | **No model card or LICENSE shipped alongside the GGUFs.** Apache-2.0 claim is from project memory (matches Alibaba's usual Qwen3 release licence) and PRODUCTION_PLAN's own note, not a file on disk. | **VERIFY** — pull the Qwen3-TTS model card/LICENSE from Hugging Face before ship. If confirmed Apache-2.0: permissive, needs a NOTICE entry + unmodified copyright/attribution, not a blocker. |

Both VERIFY items are the same shape of risk: functionally fine (both engines have been treated as MIT/Apache-2.0 throughout this project's build history and nothing about their behaviour suggests otherwise), but there is currently no licence file on disk to point to if challenged. This is a paperwork gap, not a known problem — closeable in one session with two repo visits.

## 5. Fonts

Segoe UI Variable — Windows 11 system font, rendered via the OS (Tkinter/pywebview draw text through Windows' own font stack). Not vendored, not embedded in the installer. No licensing action needed as long as the installer never bundles the font file itself.

## 6. Icons / brand assets

`assets/icon.ico`, `logo.png`, `tray_idle.png`, `tray_loading.png`, `tray_processing.png`, `tray_recording.png` — original artwork produced for this project (hand-tuned mic-to-caret glyph set, shipped in the 2026-07-30 Quiett UI build). Owned outright by Kove / Balu Premkumar. No third-party licence involved. Verdict: OK.

## 7. Voice profile samples

`voice_profile/*.wav`, `manifest.json`, `tts_reference.{wav,txt}` — Balu's own voice recordings, used to clone the read-aloud voice. This is personal data, not third-party IP, so it isn't a licensing question — it's a privacy one, already covered by the product's core "nothing leaves the machine" design. Each customer who uses the paid cloned-voice feature records their own reference sample locally; Balu's own samples never ship to customers. No rights issue. Not included in LICENSES scope beyond this note.

## 8. GPL/AGPL check

**None found.** No GPL- or AGPL-licensed component exists anywhere in the runtime dependency tree, `third_party/`, or transitive imports checked above. The only copyleft licence present is pystray's LGPLv3 (section 1), which is compatible with a proprietary build under the conditions noted there.

## 9. PRODUCTION_PLAN Part 5 blockers — resolved status

1. **`keyboard` MIT, `pystray` LGPLv3 — both confirmed** via `.venv` package metadata (see section 1). `keyboard` closed, no obligation beyond attribution. `pystray` needs an attribution/NOTICE entry, not a blocker — see the note in section 1.
2. **LM Studio redistribution blocker — CONFIRMED RESOLVED.** Grepped every `.py` file in the repo for `lm_studio`/`lmstudio`/`LM Studio`; only two stale comments remain (`dashboard.py:756`, `transcribe.py:3`), both describing history, neither importing or launching anything. No process launch, no config key, no health check gated on LM Studio (health.py has no reference at all). STATE.md 2026-07-25 confirms `lmstudio_boot.py`, `fitness.py`, `agent.py`, `taskflow.py`, `llm_client.py` were all deleted outright. Nothing in the shipped app requires, bundles, or invokes LM Studio. **Blocker closed, evidence above.**
3. Whisper large-v3-turbo weights MIT — confirmed (section 4), matches PRODUCTION_PLAN's existing note. Qwen3-TTS weights "Apache-2.0, fine" — **downgraded to VERIFY** (section 4); the licence is believed correct but not independently confirmable from files currently on disk.
4. EULA — drafted, see `EULA-DRAFT.md`. Balu review pending.
5. Third-party NOTICE file for the installer — not yet generated. Once the two VERIFY items above are closed, collate: whisper.cpp LICENSE, OpenAI Whisper LICENSE, qwentts.cpp LICENSE, Qwen3-TTS LICENSE, and the MIT/BSD/PSF/LGPL texts for the packages in sections 1-2, into a single NOTICE.txt shipped in the installer (P7 scope).

## Summary

No GPL/AGPL exposure. No live blockers. Two paperwork gaps (qwentts.cpp engine licence, Qwen3-TTS weight licence — both believed fine, neither verified from a file on disk) to close before a paid release. One LGPL component (pystray) needs an attribution entry, not a code or licence change. LM Studio blocker is closed with evidence.
