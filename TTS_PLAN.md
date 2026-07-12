# TTS Build-Out Plan — speak back in Balu's voice

Written 2026-07-12.
Status: awaiting Balu's go on P0.
Prerequisite state: voice_profile/ built (10 curated samples, best score 1.0, manifest.json engine-agnostic).

## The idea

The app gains a voice: select text anywhere in Windows, press a hotkey, hear it read back in Balu's cloned voice.
Fully offline like everything else; the voice never leaves the machine.
The existing voice profile is the reference audio a zero-shot cloning engine needs.

Primary flow (v1): highlight text in any app → Ctrl+Shift+S → app grabs the selection → synthesises with the cloned voice → plays through speakers. Press again to stop.
(Ctrl+Alt+anything is off the table for this: Ctrl+Alt is the record hold and would start a recording first.)
Later flows (v2+): play back a pending transcription from the preview panel before inserting (proofread by ear), history entries re-spoken in the cloned voice, spoken confirmations for agent/TaskFlow mode.

## What exists vs what is missing

Exists: sample collection (recordings/, 200-wav rolling), curation (voiceprofile.py, manifest with transcripts), audio playback plumbing (winsound/sounddevice), clipboard save/restore machinery (inject.py), lazy-load pattern for heavy models (agent mode), tray/settings/dashboard surfaces.
Missing: everything on the synthesis side. No engine, no tts.py, no speak hotkey, no playback pipeline, no UI.

## Hard constraints

1. **AMD GPU (RX 9070 XT, 16 GB).** No CUDA. Engine must run via ROCm-on-Windows PyTorch, DirectML/ONNX Runtime, Vulkan (GGUF-style), or acceptably fast on CPU. This is the single biggest risk and is what P0 exists to settle.
2. **No idle VRAM.** Same rule as agent mode: the TTS model loads on first use (or explicit toggle) and unloads after an idle window. whisper-server already holds VRAM; LM Studio loads on toggle.
3. **Offline only.** No cloud TTS fallback.
4. **Licence must survive productisation.** PRODUCTION_PLAN.md already tracks licence blockers (LM Studio). Engine licence must allow redistribution; avoid CPML (XTTS v2) and CC-BY-NC (F5-TTS) for anything shipped.

## Engine shortlist (verify at P0 — landscape moves monthly)

| Engine | Licence | Cloning | Notes |
|---|---|---|---|
| Chatterbox / Multilingual v3 (Resemble) | MIT | zero-shot, ~10 s ref | Quality leader (beat ElevenLabs in blind A/B); watermarks output; PyTorch — AMD path must be proven |
| Qwen3-TTS (Alibaba, 2026-01) | Apache 2.0 | zero-shot, ~3 s ref | Fully permissive; Qwen family already in the stack via LM Studio; check for GGUF/llama.cpp support → would ride the existing Vulkan stack |
| OpenVoice / OmniVoice | MIT | zero-shot | Fallbacks if the two above fail on AMD |
| GPT-SoVITS | MIT | few-shot fine-tune | Highest similarity ceiling; heavy setup; the 10-sample set is its training data. v2 quality path, not v1 |
| Kokoro-82M | Apache 2.0 | none | Not a candidate for cloning; only relevant as a fast generic-voice fallback |

Sources checked 2026-07-12: localaimaster.com engine tests, promptquorum licence comparison, findskill.ai Chatterbox benchmark.

## Phases

### P0 — Engine spike (decision gate, no product code)
**RUN 2026-07-12. Technical verdict: qwentts.cpp wins decisively. Awaiting Balu's similarity listening session to ratify.**

Results (all with voice_profile sample_01 as reference, same 5 sentences, RX 9070 XT):

| Route | Steady-state RTF | Time to first audio | Notes |
|---|---|---|---|
| Chatterbox, CPU | ~5 | 18-35 s/sentence | dead for interactive |
| Chatterbox, PyTorch ROCm | ~3.5 | 9-10 s/sentence | ROCm 7.13 alpha on RDNA4 Windows too immature |
| Qwen3-TTS 1.7B, PyTorch ROCm | 13-38 | 69-120 s/sentence | worst of all |
| **qwentts.cpp 1.7B Q8_0, Vulkan** | **0.87** | **~145 ms warm** | faster than real time; MIT engine, Apache 2.0 weights |

qwentts.cpp (github.com/ServeurpersoCom/qwentts.cpp, C++/GGML port of Qwen3-TTS): built with `-DGGML_VULKAN=ON` using the whisper.cpp short-dir procedure; binaries in spikes/tts_p0/bin/, GGUFs (2.3 GB total) in spikes/tts_p0/models/.
`tts-server` smoke-tested: POST /v1/voices registers the cloned voice from sample_01 (2.8 s, in-RAM), POST /v1/audio/speech synthesises in it, `response_format: "pcm"` streams s16le 24 kHz as generated — the P1 architecture is exactly the whisper-server pattern (lazy subprocess, kill to free VRAM, ~2.3 GB while resident).
Listening sets for Balu: spikes/tts_p0/out/qwentts_cpp/ (the candidate), out/chatterbox/ and out/qwen3tts/ (comparisons — same sentences).
PyTorch/ROCm routes are dead ends today; revisit only if a future need can't ride Vulkan.
Watch item: profile.py at project root shadows stdlib `profile`, breaking in-process torch imports — moot if P1 stays subprocess-only (it should).

### P1 — tts.py engine wrapper (shape settled by P0)
Subprocess pattern, mirroring transcribe.py's whisper-server management: launch tts-server (from third_party/qwentts.cpp/bin, port config key) on first use or explicit toggle, register the voice from voice_profile via POST /v1/voices, kill the process on idle timeout / toggle-off to free the ~2.3 GB VRAM.
Speak = POST /v1/audio/speech with `response_format: "pcm"`, stream chunks straight into a sounddevice OutputStream — playback starts at first chunk (~150 ms warm) and generation outruns playback (RTF 0.87), so no buffering logic needed.
`stop()` = abort the HTTP read + stop the stream.
No sentence chunking needed (streaming makes it moot). No PyTorch, no new Python deps beyond what's installed.
Config keys: `tts_enabled` (default false), `tts_port`, `tts_output_device`, `tts_unload_idle_seconds`.
Move binaries + GGUFs under third_party/qwentts.cpp/ (gitignored like whisper.cpp).
Unit tests with a fake server.

### P2 — Speak-selection flow (the feature)
New hotkey Ctrl+Shift+S (config `tts_hotkey`) registered alongside the others.
Selection grab: save clipboard → synthetic Ctrl+C → read clipboard → restore (inject.py already owns save/restore and modifier-flush; reuse, don't duplicate).
Empty selection → toast "Nothing selected", no model load.
Same hotkey or Esc while speaking = stop.
Badge state while synthesising/speaking (reuse the processing badge pattern), tray state sync.
E2E verify: select text in Notepad, VS Code, browser; speak; stop mid-playback; clipboard intact afterwards.

### P3 — UI surfaces
Tray: "Speak selection" item, TTS on/off toggle (mirrors agent-mode toggle semantics: off = model unloaded).
Dashboard Settings: TTS section (enable, hotkey display, speed, output device, idle-unload).
Dashboard Voice page (new): profile status from manifest (sample count, best score, built_at), Rebuild button, per-sample play/score list, "test my voice" input box.
ui-states pass on the new page (empty = no profile yet, loading = model loading, error = engine failed, ideal).
Remember dashboard _HTML escaping rule (memory: dashboard-html-escaping) and node --check before done.

### P4 — Integration extras (each small, ship independently)
Preview panel: play button to hear the pending transcription before inserting.
History: "speak" action next to the existing raw-audio replay.
Agent mode: spoken confirmations ("opened Spotify") behind a config flag, off by default.
Auto-recuration: voiceprofile.rebuild() automatically after every N new recordings (config `voice_profile_rebuild_every`), log the score trend.

### P5 — Productisation tie-in
Add engine + model weights to PRODUCTION_PLAN.md licence table.
Installer implications: model download on first run vs bundled (weights are 0.5-2 GB).
If Chatterbox: its audio watermark is a feature to disclose, not hide.

## Open questions for Balu
1. P0 needs ~an hour of his ear for similarity judging — schedule it.
2. Acceptable latency to first audio: is ~2-3 s fine for v1?
3. GPT-SoVITS fine-tune (higher similarity, much heavier) only if zero-shot disappoints — agree to defer that decision to after P0.
4. Speaker output vs configurable device (headset) default.
