"""
Voice profile builder — curates the best dictation recordings from
recordings/ into voice_profile/ as reference samples for voice cloning.

Multiple samples beat one: zero-shot TTS engines get the single best clip
(manifest "best"), multi-reference engines get the whole set, and a future
fine-tune (GPT-SoVITS style) uses the set as its training data.
manifest.json keeps the file → transcript mapping engine-agnostic.

Scoring is heuristic, no ML:
  - duration: sweet spot 6–20 s (zero-shot references), hard bounds 3–40 s
  - speech level: healthy average RMS, not whisper-quiet, not hot
  - clipping: samples near full scale penalised hard
  - silence: clips that are mostly pauses score low
"""

import json
import os
import shutil
import wave
from datetime import datetime

import numpy as np

import history
from logger import log, warn

RECORDINGS_DIR = "recordings"
PROFILE_DIR    = "voice_profile"
MANIFEST_FILE  = os.path.join(PROFILE_DIR, "manifest.json")

_HARD_MIN_S  = 3.0
_HARD_MAX_S  = 40.0
_IDEAL_MIN_S = 6.0
_IDEAL_MAX_S = 20.0

_SPEECH_RMS      = 0.010   # frame RMS above this counts as speech
_LEVEL_IDEAL_LO  = 0.030   # healthy average speech level band
_LEVEL_IDEAL_HI  = 0.200
_CLIP_THRESHOLD  = 0.985   # |sample| above this counts as clipped


def _read_wav(path: str) -> tuple:
    """Return (float32 mono array in [-1,1], sample_rate) or (None, 0)."""
    try:
        with wave.open(path, "rb") as w:
            if w.getnchannels() != 1 or w.getsampwidth() != 2:
                return None, 0
            sr = w.getframerate()
            raw = w.readframes(w.getnframes())
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        return data, sr
    except Exception:
        return None, 0


def score_clip(data: np.ndarray, sr: int) -> tuple[float, dict]:
    """Score a clip 0..1 for suitability as a voice-clone reference."""
    duration = len(data) / sr
    metrics = {"duration_s": round(duration, 2)}
    if duration < _HARD_MIN_S or duration > _HARD_MAX_S:
        return 0.0, metrics

    if _IDEAL_MIN_S <= duration <= _IDEAL_MAX_S:
        d_score = 1.0
    elif duration < _IDEAL_MIN_S:
        d_score = (duration - _HARD_MIN_S) / (_IDEAL_MIN_S - _HARD_MIN_S)
    else:
        d_score = max(0.2, 1.0 - (duration - _IDEAL_MAX_S) / (_HARD_MAX_S - _IDEAL_MAX_S))

    frame = max(1, int(sr * 0.03))
    n_frames = len(data) // frame
    if n_frames < 10:
        return 0.0, metrics
    framed = data[: n_frames * frame].reshape(n_frames, frame)
    rms = np.sqrt(np.mean(framed ** 2, axis=1))

    speech_mask = rms > _SPEECH_RMS
    silence_ratio = 1.0 - float(speech_mask.mean())
    metrics["silence_ratio"] = round(silence_ratio, 3)
    # up to 25% pauses is natural speech; beyond that penalise linearly
    sil_score = 1.0 - min(1.0, max(0.0, silence_ratio - 0.25) / 0.50)

    level = float(rms[speech_mask].mean()) if speech_mask.any() else 0.0
    metrics["speech_rms"] = round(level, 4)
    if _LEVEL_IDEAL_LO <= level <= _LEVEL_IDEAL_HI:
        lvl_score = 1.0
    elif level < _LEVEL_IDEAL_LO:
        lvl_score = level / _LEVEL_IDEAL_LO
    else:
        lvl_score = max(0.3, 1.0 - (level - _LEVEL_IDEAL_HI))

    clip_ratio = float((np.abs(data) > _CLIP_THRESHOLD).mean())
    metrics["clip_ratio"] = round(clip_ratio, 5)
    clip_score = 1.0 - min(1.0, clip_ratio * 200.0)

    score = (d_score * 0.30 + sil_score * 0.30 + lvl_score * 0.25
             + clip_score * 0.15)
    metrics["score"] = round(score, 3)
    return score, metrics


def _transcripts_by_audio() -> dict:
    """Map recordings filename → transcript text from history entries."""
    out = {}
    try:
        for entry in history.load():
            audio = entry.get("audio")
            if audio:
                out[audio] = entry.get("text", "").strip()
    except Exception:
        pass
    return out


def rebuild(max_samples: int = 10, min_score: float = 0.5) -> dict:
    """Re-curate voice_profile/ from recordings/. Returns a summary dict.

    Selected clips are copied as sample_NN.wav (best first); files from a
    previous build that are no longer selected are removed. manifest.json
    records source file, score, metrics, and transcript per sample.
    """
    if not os.path.isdir(RECORDINGS_DIR):
        return {"samples": 0, "message": "no recordings yet"}

    transcripts = _transcripts_by_audio()
    scored = []
    for fname in sorted(os.listdir(RECORDINGS_DIR)):
        if not fname.endswith(".wav"):
            continue
        path = os.path.join(RECORDINGS_DIR, fname)
        data, sr = _read_wav(path)
        if data is None:
            continue
        score, metrics = score_clip(data, sr)
        if score >= min_score:
            scored.append((score, fname, metrics))

    scored.sort(key=lambda t: t[0], reverse=True)
    selected = scored[: max(1, max_samples)] if scored else []

    if not selected:
        return {"samples": 0,
                "message": f"no recordings scored >= {min_score} yet — keep dictating"}

    os.makedirs(PROFILE_DIR, exist_ok=True)
    samples = []
    for i, (score, fname, metrics) in enumerate(selected, start=1):
        dest = f"sample_{i:02d}.wav"
        shutil.copy2(os.path.join(RECORDINGS_DIR, fname),
                     os.path.join(PROFILE_DIR, dest))
        samples.append({
            "file": dest,
            "source": fname,
            "transcript": transcripts.get(fname, ""),
            **metrics,
        })

    # Remove stale sample files beyond this build's count
    keep = {s["file"] for s in samples} | {os.path.basename(MANIFEST_FILE)}
    for f in os.listdir(PROFILE_DIR):
        if f not in keep:
            try:
                os.remove(os.path.join(PROFILE_DIR, f))
            except OSError:
                pass

    manifest = {
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "best": samples[0]["file"],
        "samples": samples,
    }
    with open(MANIFEST_FILE, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    log("voiceprofile", f"built {len(samples)} samples, "
                        f"best={samples[0]['source']} score={samples[0]['score']}")
    return {"samples": len(samples), "best": samples[0],
            "message": f"{len(samples)} samples curated, "
                       f"best score {samples[0]['score']}"}


def load_manifest() -> dict | None:
    """Current profile manifest, or None if never built."""
    try:
        with open(MANIFEST_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


if __name__ == "__main__":
    print(json.dumps(rebuild(), indent=2, ensure_ascii=False))
