"""Regenerate assets/ from the programmatic icon masters in tray.py.

Single source of truth is the drawing code; this just materialises files:
  assets/icon.ico            multi-res desktop/installer icon (coloured badge)
  assets/tray_<state>.png    monochrome tray glyphs at 64px (current taskbar theme)
  assets/logo.png            256px recording-overlay logo

Run:  .venv\\Scripts\\python.exe scripts\\make_icons.py
"""
import os
import sys

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJ)

import tray  # noqa: E402

ASSETS = os.path.join(_PROJ, "assets")
os.makedirs(ASSETS, exist_ok=True)

tray.export_ico(os.path.join(ASSETS, "icon.ico"))
print("wrote assets/icon.ico")

for state in tray._TOOLTIPS:
    p = os.path.join(ASSETS, f"tray_{state}.png")
    tray._make_icon(state, target_size=64).save(p)
    print(f"wrote assets/tray_{state}.png")

tray.make_logo(target_size=256).save(os.path.join(ASSETS, "logo.png"))
print("wrote assets/logo.png")
