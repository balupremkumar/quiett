"""Quiett design tokens — the single source of truth for both UI stacks.

The dashboard (pywebview/Chromium) consumes CSS custom properties via
css_vars(); the Tk surfaces (preview.py) and the tray consume the flat,
pre-composited hex values in TK — Tk has no alpha compositing, so every
translucent token is flattened against the void here, once.

Rule: no other module defines a colour. If a surface needs a new token,
it is added here first, in both themes.
"""

# ---------------------------------------------------------------- dark (flagship)
DARK = {
    # grounds
    "void":     "#04050A",
    "glass":    "rgba(255,255,255,.028)",
    "glass2":   "rgba(255,255,255,.05)",
    "hair":     "rgba(255,255,255,.07)",
    "hair2":    "rgba(255,255,255,.14)",
    # ink
    "text":     "#F2F5FA",
    "mid":      "#8A94A8",
    "dim":      "#566074",
    # accent + states (no green anywhere: success is ion)
    "ion":      "#7DE8FF",
    "ion_deep": "#3F8CFF",
    "ion_soft": "rgba(125,232,255,.10)",
    "rec":      "#FF6B5E",
    "pause":    "#FFB86B",
    # aurora ground layers (dashboard body background)
    "aurora": ("radial-gradient(900px 600px at 12% -8%, rgba(63,110,255,.16), transparent 55%),"
               "radial-gradient(1100px 700px at 88% 12%, rgba(0,190,255,.09), transparent 55%),"
               "radial-gradient(900px 900px at 50% 118%, rgba(140,80,255,.09), transparent 60%)"),
}

# ---------------------------------------------------------------- light (derived, not inverted)
LIGHT = {
    "void":     "#EDF0F6",
    "glass":    "rgba(255,255,255,.66)",
    "glass2":   "rgba(255,255,255,.9)",
    "hair":     "rgba(24,44,74,.10)",
    "hair2":    "rgba(24,44,74,.20)",
    "text":     "#17202C",
    "mid":      "#54637A",
    "dim":      "#7C8AA0",
    "ion":      "#0E8FDD",   # darkened for 4.5:1 on light ground
    "ion_deep": "#2563EB",
    "ion_soft": "rgba(14,143,221,.10)",
    "rec":      "#E23A4E",
    "pause":    "#B4720A",
    "aurora": ("radial-gradient(900px 600px at 12% -8%, rgba(37,99,235,.08), transparent 55%),"
               "radial-gradient(1100px 700px at 88% 12%, rgba(14,143,221,.06), transparent 55%)"),
}

# ---------------------------------------------------------------- Tk flats (pre-composited on void)
# glass over void, hand-flattened; Tk gets solid hex only.
TK_DARK = {
    "bg":       "#0B0D13",   # glass on void
    "surface":  "#10141C",   # glass2 on void
    "elevated": "#161B25",
    "line":     "#1A2130",   # hair flattened
    "line2":    "#242D3F",
    "text":     "#F2F5FA",
    "mid":      "#8A94A8",
    "dim":      "#566074",
    "ion":      "#7DE8FF",
    "ion_deep": "#3F8CFF",
    "rec":      "#FF6B5E",
    "pause":    "#FFB86B",
}
TK_LIGHT = {
    "bg":       "#F2F4F9",
    "surface":  "#FFFFFF",
    "elevated": "#FFFFFF",
    "line":     "#DDE3EC",
    "line2":    "#C6CFDC",
    "text":     "#17202C",
    "mid":      "#54637A",
    "dim":      "#7C8AA0",
    "ion":      "#0E8FDD",
    "ion_deep": "#2563EB",
    "rec":      "#E23A4E",
    "pause":    "#B4720A",
}

# type + geometry, shared
FONT_UI = ("Segoe UI Variable Text", "Segoe UI")
FONT_DISPLAY = ("Segoe UI Variable Display", "Segoe UI")
FONT_MONO = ("Cascadia Mono", "Consolas")
RADIUS = {"ctl": 8, "card": 12, "win": 16}
GRID = 8
TYPE_SCALE = [12, 13, 15, 18, 24, 34]

CSS_FONT_UI = "'Segoe UI Variable Text','Segoe UI',system-ui,sans-serif"
CSS_FONT_DISPLAY = "'Segoe UI Variable Display','Segoe UI',system-ui,sans-serif"
CSS_FONT_MONO = "'Cascadia Mono',Consolas,monospace"


def css_vars(theme: dict) -> str:
    """Emit the theme as CSS custom properties (no selector wrapper)."""
    out = []
    for k, v in theme.items():
        if k == "aurora":
            continue
        out.append(f"--{k.replace('_', '-')}:{v};")
    out.append(f"--font:{CSS_FONT_UI};")
    out.append(f"--font-d:{CSS_FONT_DISPLAY};")
    out.append(f"--mono:{CSS_FONT_MONO};")
    out.append(f"--r-ctl:{RADIUS['ctl']}px;--r-card:{RADIUS['card']}px;--r-win:{RADIUS['win']}px;")
    return "".join(out)
