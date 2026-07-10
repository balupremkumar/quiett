# UI polish build — plan (2026-07-10)

Scope confirmed by Balu: ROI rows 1-11 (backlog items 2, 16, 58, 29, 78, 63, 1, 3, 4, 5, 21).
No selling-facing work (no onboarding, installer art, stats, streaming, model picker).
Implementation follows this plan; restart-app + screenshot verification at each milestone.

## Architecture decisions

The editable preview panel stays Tkinter (it needs the Text widget); its edges get native Win11 DWM rounded corners (compositor-antialiased), not a region mask.
The badge/pill (the "speech bubble" with the waveform) becomes a per-pixel-alpha layered window: Pillow renders each frame RGBA at 4x, LANCZOS downsample, pushed via UpdateLayeredWindow.
Backlog item 20 (full stack migration) stays open; this is the surgical path.
Theme becomes refresh-at-open: preview.py's palette constants turn into a refreshable module state re-resolved by main.py's existing 30s `_reload_config` thread plus a check on every window open.
`theme` gains a "system" value that follows the `AppsUseLightTheme` registry key.

## Milestones and order

### M1 — Rendering foundation (items 2, 16, 63)
1. DPI awareness v2: `SetProcessDpiAwarenessContext(-4)` at the top of main.py and dashboard.py `__main__`, with `shcore.SetProcessDpiAwareness(2)` fallback.
   Add `_scale()` helper in preview.py; scale geometry constants (_BADGE_W/H, _LOGO_SIZE, paddings, region radius) by `GetDpiForSystem()/96`; set Tk font scaling via `tk scaling`.
2. winfx.apply_rounded_region: try `DwmSetWindowAttribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE=33, DWMWCP_ROUND=2)` first (Win11), keep SetWindowRgn as Win10 fallback.
3. Titlebar sync: `DwmSetWindowAttribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE=20, dark)` on the dashboard window after shown (hwnd via FindWindow on title).
Verify: restart, screenshot dashboard + trigger a toast; corners must be smooth at 100% and 150% DPI.

### M2 — Behaviour quick wins (items 29, 78, 58, 3)
4. Instant-apply settings: every settings input saves on change (debounced 400ms) with a "Saved" flash; Save button removed.
5. Autostart toggle in Settings: `DashboardAPI.set_autostart(bool)` creates/deletes a `schtasks` logon task (`/RL HIGHEST`) running launch.vbs.
6. Target-app indicator: panel header shows "→ <app name>" resolved from the stored hwnd (exe basename via win32process; window title fallback).
7. Status dot: small state-coloured dot on the panel and badge using tray's state colours (single source: a shared STATE_COLOURS dict in tray.py imported by preview.py).
Verify: restart, change a setting (confirm persisted in config.json), dictate into two different apps.

### M3 — Live theme (item 4)
8. preview.py palette: `_apply_theme(name)` reassigns module colour globals; `refresh_theme()` re-resolves from config + registry, called by main.py `_reload_config` and before each window open.
9. `theme: system` option in dashboard select; dashboard resolves via `prefers-color-scheme` listener; Tk side reads AppsUseLightTheme.
10. Tray icons rebuilt on theme change (tray.refresh_theme()).
Verify: flip Windows theme with app running; popup, tray, dashboard all follow without restart.

### M4 — Icons (items 5, 21)
11. scripts/make_icons.py: master mic-to-caret glyph drawn in Pillow at 1024px; exports assets/icon.ico (16-256), assets/tray_{light,dark}_{state}.png sets.
12. tray.py: monochrome outlined glyph per Win11 convention, theme-aware (taskbar theme registry), state shown as a small coloured dot overlay; loads from assets/, falls back to programmatic drawing.
Verify: tray icon crisp at 100%/150%, correct in both Windows themes, states distinguishable.

### M5 — Per-pixel-alpha badge (item 1) — DESCOPED 2026-07-10
Outcome verified instead: the M1 DWM corner preference already gives compositor-antialiased badge edges (screenshot at 3x zoom over a black background, no stair-stepping).
The UpdateLayeredWindow rewrite adds soft shadows and sub-pixel alpha but no visible fix on Win11, at real regression risk (positioning, fade, dynamic partial-text growth).
Deferred into the popup-stack decision (BACKLOG item 20); Win10 still falls back to the hard region mask, which matters only if the product build targets Win10.

## Risks

DPI awareness changes physical sizes on scaled displays — M1 must be visually verified before anything else lands on top.
UpdateLayeredWindow bypasses Tk drawing entirely — that's why the badge (display-only) converts and the editable panel does not.
schtasks needs elevation to create /RL HIGHEST tasks — app already runs elevated for hooks; degrade to a plain logon task if not.
