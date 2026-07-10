"""Regenerate assets/ from the programmatic icon masters in tray.py.

Single source of truth is the drawing code; this just materialises files:
  assets/icon.ico            multi-res desktop/installer icon (coloured badge)
  assets/tray_<state>.png    monochrome tray glyphs at 64px (current taskbar theme)
  assets/logo.png            256px recording-overlay logo

Run:  .venv\\Scripts\\python.exe scripts\\make_icons.py
      .venv\\Scripts\\python.exe scripts\\make_icons.py --verify   (check only, no regen)
"""
import os
import struct
import sys

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJ)

import tray  # noqa: E402

ASSETS = os.path.join(_PROJ, "assets")
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def verify_ico(path: str) -> bool:
    """Assert every expected frame is present at its full declared size.

    Two independent checks, since the historical Pillow footgun (#2264) was a
    256->255 clamp that could happen at either layer:
      1. Raw ICONDIR entries: the on-disk width/height byte for a 256px frame
         must be the 0x00 sentinel (0 means 256 per the ICO spec), not 255.
      2. Decoded pixels: PIL.Image.open + im.load() at each declared size must
         yield an image whose actual .size matches (catches decode-time clamps
         even if the header bytes are correct).
    """
    from PIL import Image

    with open(path, "rb") as f:
        data = f.read()
    _reserved, _itype, count = struct.unpack("<HHH", data[0:6])
    header_sizes = []
    off = 6
    for _ in range(count):
        w, h = struct.unpack("<BB", data[off:off + 2])
        header_sizes.append((w if w != 0 else 256, h if h != 0 else 256))
        off += 16

    ok = True
    expected = set(ICO_SIZES)
    missing = expected - set(header_sizes)
    if missing:
        print(f"FAIL: missing header entries for {sorted(missing)}")
        ok = False

    for size in ICO_SIZES:
        im = Image.open(path)
        im.size = size
        im.load()
        if im.size != size:
            print(f"FAIL: {size} frame decoded as {im.size} (clamp bug)")
            ok = False
        else:
            print(f"OK: {size[0]}x{size[1]} frame present and decodes at full size")
    return ok


if __name__ == "__main__" and "--verify" in sys.argv:
    ico_path = os.path.join(ASSETS, "icon.ico")
    passed = verify_ico(ico_path)
    print("VERIFY PASSED" if passed else "VERIFY FAILED")
    sys.exit(0 if passed else 1)

os.makedirs(ASSETS, exist_ok=True)

tray.export_ico(os.path.join(ASSETS, "icon.ico"))
print("wrote assets/icon.ico")

for state in tray._TOOLTIPS:
    p = os.path.join(ASSETS, f"tray_{state}.png")
    tray._make_icon(state, target_size=64).save(p)
    print(f"wrote assets/tray_{state}.png")

tray.make_logo(target_size=256).save(os.path.join(ASSETS, "logo.png"))
print("wrote assets/logo.png")

print("run with --verify to assert exported ICO frame sizes")
