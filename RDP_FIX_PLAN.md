# RDP stuck-modifier + flaky-paste fix plan (2026-08-12)

Architect: Fable. Implementation: backend (Sonnet). Bug re-reported by Balu 2026-08-12 despite the 2026-07-14 fix (a34ee1e, adb6c45).

## Residual defects

D1. Race: `inject._flush_hotkey_modifiers` (inject.py:375) and preview's `_activate_when_released` (preview.py:2034) both wake on "all modifiers physically up".
If the preview steals focus first, the flush thread sees TkTopLevel as foreground, `_is_rdp` is False, and it returns silently — no flush, no log.
Remote session keeps the stuck modifier; app.log shows the "force-flushed" line only sometimes, matching Balu's intermittency.

D2. One-shot: the flush checks foreground at one instant and never retries when the RDP window regains focus moments later.

D3. Shift source: Ctrl+Shift+S (TTS) exits without flushing on `_on_tts_hotkey` early returns (is_speaking → stop; is_recording) in main.py:634-646, and on the `get_selected_text` abort path (inject.py:940-946).

D4. Paste flakiness: for RDP targets `inject_text` sends Ctrl+V ~30ms after `_clipboard_set_text`, but rdpclip propagates the format list asynchronously and serves data via delayed rendering.
The `_restore` thread restores the old clipboard after at most `restore_delay_ms * 3` (450ms); a remote data request landing after that pastes the OLD text, or the paste no-ops.

## Fixes

F1 (D1): new `inject.flush_rdp_if_foreground()` — if current foreground is RDP, sleep 50ms then `_flush_all_modifiers(force=True)` and log.
preview.py `_activate_when_released` calls it after the held-check passes and BEFORE `inject.activate_window`, so the flush is guaranteed to run while mstsc still owns focus.

F2 (D2): rework `_flush_hotkey_modifiers` into a bounded watcher: after `_wait_modifiers_released(2000)`, poll the foreground every 100ms for up to 4s; first time it is RDP, force-flush once and exit.
Log both outcomes ("force-flushed" vs "watcher expired, RDP never foreground") so app.log always tells the story.

F3 (D3): call `inject.flush_hotkey_modifiers_async()` unconditionally at the top of `_on_tts_hotkey` (main.py:634); it is async, waits for physical release, and no-ops when RDP never gets focus.
Also flush in preview panel cancel (Esc/close) path so "edit then dismiss then click back into RDP" is covered.

F4 (D4): RDP-aware clipboard timing in `inject_text`:
after `_clipboard_set_text` for RDP targets sleep `rdp_clipboard_settle_ms` (config, default 250) before the Ctrl+V keystroke;
for RDP targets the `_restore` deadline becomes `rdp_clipboard_restore_delay_ms` (config, default 3000) instead of `restore_delay_ms * 3`.
Wire both keys through `inject.configure(...)` from main.py the same way `restore_delay_ms` flows, with config defaults + validation + hot-reload parity.

F5: add `msrdcw.exe` (Windows App) to `_is_rdp`; log a line whenever a flush decision skips (observability for the next report).

## Verification

Unit tests for the watcher, the TTS-exit flush wiring, and the RDP settle/restore timing (mock win32 as existing tests do).
Full suite green, then restart via the restart-app skill and exercise a local dictation.
In-RDP verify checklist for Balu: dictate over the RDP window and immediately shift-click + ctrl-click + type numbers in the remote; Ctrl+Shift+S over remote text twice (second press while speaking); paste a long dictation into a remote editor twice in a row.
