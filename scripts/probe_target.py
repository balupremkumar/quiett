"""Diagnose what targetprobe.py sees for whatever window you click into.

Counts down so you can click into a field, then prints the resolved Target,
the raw UI Automation properties behind it, the verify signal, and timings.

Run:  .venv\\Scripts\\python.exe scripts\\probe_target.py
      .venv\\Scripts\\python.exe scripts\\probe_target.py 8 --repeat 3
      .venv\\Scripts\\python.exe scripts\\probe_target.py --no-warm   (cold cost)
"""
import os
import sys
import time

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJ)

import targetprobe  # noqa: E402

_VERDICT_MARK = {
    targetprobe.EDITABLE:     "[+]",
    targetprobe.NOT_EDITABLE: "[x]",
    targetprobe.UNKNOWN:      "[?]",
}


def _row(key: str, value) -> None:
    print(f"  {key:<14} {value}")


def _countdown(seconds: int) -> None:
    if seconds <= 0:
        return
    print(f"Click into the field you want to test. Probing in {seconds}s")
    for left in range(seconds, 0, -1):
        sys.stdout.write(f"\r  {left}... ")
        sys.stdout.flush()
        time.sleep(1)
    sys.stdout.write("\r        \r")
    sys.stdout.flush()


def _window_block(hwnd: int) -> None:
    print("Foreground window")
    _row("hwnd", f"{hwnd} (0x{hwnd:08X})" if hwnd else "0 (none)")
    _row("class", targetprobe._class_name(hwnd) or "-")
    _row("exe", targetprobe._exe_for_hwnd(hwnd) or "-")
    _row("title", (targetprobe._window_title(hwnd) or "-")[:70])
    print()


def _target_block(t, elapsed_ms: float, budget_ms: int) -> None:
    mark = _VERDICT_MARK.get(t.verdict, "[?]")
    print(f"Target   {mark} {t.verdict}")
    _row("kind", t.kind)
    _row("label", t.label or "-")
    if t.rect:
        l, top, r, b = t.rect
        _row("rect", f"({l}, {top}) to ({r}, {b})   {r - l}x{b - top}px")
    else:
        _row("rect", "-")
    _row("source", t.source)
    _row("probe cost", f"{elapsed_ms:.0f}ms  (budget {budget_ms}ms)")
    print()


def _uia_block(hwnd: int) -> None:
    print("Raw UI Automation")
    if not targetprobe.available():
        _row("state", "UIA not loaded (warm-up skipped or failed)")
        print()
        return
    t0 = time.perf_counter()
    props = targetprobe._run_with_deadline(lambda: targetprobe._uia_props(hwnd), 2.0)
    cost = (time.perf_counter() - t0) * 1000
    if not props:
        _row("state", "no focused element / call failed")
        print()
        return
    ct = props["control_type"]
    _row("control_type", f"{ct} ({targetprobe._CT_NAMES.get(ct, 'unknown')})")
    _row("name", (props["name"] or "-")[:60])
    _row("focusable", props["focusable"])
    _row("enabled", props["enabled"])
    _row("is_password", props["is_password"])
    _row("text_pattern", props["text_pattern"])
    _row("value_pattern", props["value_pattern"])
    _row("readonly", props["readonly"])
    _row("exe / pid", f"{props['exe'] or '-'} / {props['pid']}")
    _row("classes", ", ".join(props["classes"]) or "-")
    _row("trusted UIA", f"{props['trusted']}   (gates any confident NOT_EDITABLE)")
    _row("read cost", f"{cost:.0f}ms")
    print()


def _verify_block(hwnd: int) -> None:
    print("Verify signal")
    t0 = time.perf_counter()
    tok = targetprobe.verify_token(hwnd)
    cost = (time.perf_counter() - t0) * 1000
    if tok is None:
        _row("token", "none  (post-insert verification will report None)")
    else:
        _row("token", f"{tok.kind} = {tok.value}")
        _row("runtime_id", str(tok.runtime_id))
    _row("cost", f"{cost:.0f}ms")
    if tok is not None:
        t0 = time.perf_counter()
        landed = targetprobe.verify_landed(hwnd, tok)
        _row("re-read now", f"{landed}  (False expected, nothing was inserted) "
                            f"{(time.perf_counter() - t0) * 1000:.0f}ms")
    print()


def main(argv) -> int:
    seconds = 5
    repeat = 1
    warm = True
    args = list(argv)
    if "--no-warm" in args:
        warm = False
        args.remove("--no-warm")
    if "--repeat" in args:
        i = args.index("--repeat")
        repeat = max(1, int(args[i + 1]))
        del args[i:i + 2]
    if args and args[0].isdigit():
        seconds = int(args[0])

    print("Quiett target probe")
    print("-" * 60)
    if warm:
        t0 = time.perf_counter()
        targetprobe.warm_up()
        for _ in range(200):          # warm_up is async, wait up to 10s for it
            if targetprobe.available():
                break
            time.sleep(0.05)
        print(f"UIA warm-up: {'ready' if targetprobe.available() else 'FAILED'} "
              f"in {(time.perf_counter() - t0) * 1000:.0f}ms (one off, at startup)")
    else:
        print("UIA warm-up: skipped, first probe pays the cold cost")
    print()

    _countdown(seconds)

    for i in range(repeat):
        if repeat > 1:
            print(f"===== probe {i + 1} of {repeat} =====")
        hwnd = targetprobe.foreground_window()
        _window_block(hwnd)
        t0 = time.perf_counter()
        target = targetprobe.probe(hwnd)
        elapsed = (time.perf_counter() - t0) * 1000
        _target_block(target, elapsed, 250)
        _uia_block(hwnd)
        _verify_block(hwnd)
        if target.verdict == targetprobe.UNKNOWN:
            print("UNKNOWN means no opinion: the insert is still attempted.")
        elif target.verdict == targetprobe.NOT_EDITABLE:
            print("NOT_EDITABLE is confident: the insert would be held back.")
        else:
            print("EDITABLE is confident: insert and expect it to land.")
        if i + 1 < repeat:
            print()
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
