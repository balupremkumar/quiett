"""Standalone test — verifies LM Studio + Qwen2.5-0.5B reformatter.

Run from the project root:
    python test_reformat.py [--backend lmstudio|api|rules]
"""
import sys
import time
import argparse

# Allow running from project root without installing as a package
sys.path.insert(0, r"C:\AI\projects\Voice Dictation")
import reformat

SAMPLES = [
    ("simple",       "I want to add a dark mode toggle to the settings panel"),
    ("filler",       "um basically can you fix the bug where um the clipboard isn't restored after paste"),
    ("multi-step",   "I need to refactor the audio module so it handles silence detection better and also add logging"),
    ("technical",    "let's create a test for the LM Studio integration you know to verify it works correctly"),
    ("imperative",   "I'm going to update inject.py so SendInput uses scan codes instead of keybd_event"),
    ("complex",      "essentially I want you to add a vibe mode toggle to the tray icon right-click menu and make sure it persists to config.json"),
]

def run_test(backend: str, model: str) -> None:
    print(f"\nLoading backend: {backend!r}, model: {model!r}")
    t0 = time.perf_counter()
    reformat.load(backend=backend, model=model)
    print(f"Ready in {time.perf_counter() - t0:.1f}s  (backend={reformat._backend!r})\n")
    print(f"{'—' * 72}")

    pass_count = 0
    for label, raw in SAMPLES:
        t1 = time.perf_counter()
        result = reformat.run(raw)
        elapsed = time.perf_counter() - t1
        changed = result.strip() != raw.strip()
        status = "OK" if changed else "SAME"
        print(f"[{label:<12}] {status}  ({elapsed:.2f}s)")
        print(f"  RAW:    {raw}")
        print(f"  RESULT: {result}")
        print()
        if changed:
            pass_count += 1

    print(f"{'—' * 72}")
    print(f"Reformatted {pass_count}/{len(SAMPLES)} samples.\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="lmstudio", choices=["lmstudio", "api", "rules"])
    ap.add_argument("--model",   default="qwen/qwen3-8b")
    args = ap.parse_args()
    run_test(args.backend, args.model)
