"""Tests for the QUIETT_UI_PLAN P4/P6 config-gated helpers in preview.py.
These are plain functions (read config.json, no Tk widgets involved), safe
to unit test without a live Tk root.
"""
import json

import pytest

import preview
import theme


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"

    def _write(data: dict) -> None:
        path.write_text(json.dumps(data), encoding="utf-8")

    monkeypatch.setattr(preview, "_CONFIG_FILE", str(path))
    return _write


class TestLearnFromEditsGate:
    def test_default_true_when_key_absent(self, config_file):
        config_file({})
        assert preview._learn_from_edits_enabled() is True

    def test_explicit_false(self, config_file):
        config_file({"learn_from_edits": False})
        assert preview._learn_from_edits_enabled() is False

    def test_explicit_true(self, config_file):
        config_file({"learn_from_edits": True})
        assert preview._learn_from_edits_enabled() is True

    def test_missing_file_defaults_true(self, tmp_path, monkeypatch):
        monkeypatch.setattr(preview, "_CONFIG_FILE", str(tmp_path / "nope.json"))
        assert preview._learn_from_edits_enabled() is True


class TestPanelAcrylicGate:
    def test_default_true_when_key_absent(self, config_file):
        config_file({})
        assert preview._panel_acrylic_enabled() is True

    def test_explicit_false(self, config_file):
        config_file({"panel_acrylic": False})
        assert preview._panel_acrylic_enabled() is False


class TestTargetProbeGate:
    """PASTE_UX_PLAN section 7: target_probe: false reverts to pre-plan
    behaviour, so the ring stops claiming to know what the focused element is."""

    def test_default_true_when_key_absent(self, config_file):
        config_file({})
        assert preview._target_probe_enabled() is True

    def test_explicit_false(self, config_file):
        config_file({"target_probe": False})
        assert preview._target_probe_enabled() is False

    def test_missing_file_defaults_true(self, tmp_path, monkeypatch):
        monkeypatch.setattr(preview, "_CONFIG_FILE", str(tmp_path / "nope.json"))
        assert preview._target_probe_enabled() is True


class TestTargetRingGate:
    def test_default_true_when_key_absent(self, config_file):
        config_file({})
        assert preview._target_ring_enabled() is True

    def test_explicit_false(self, config_file):
        config_file({"target_ring": False})
        assert preview._target_ring_enabled() is False


class TestThemeFlattening:
    """The palette preview.py hands out must trace straight back to
    theme.py's Tk-flat tokens — no second hand-maintained palette."""

    def test_dark_palette_matches_theme_tokens(self):
        t = preview._THEMES["dark"]
        assert t["BG"] == theme.TK_DARK["bg"]
        assert t["FG"] == theme.TK_DARK["text"]
        assert t["BLUE"] == theme.TK_DARK["ion"]
        assert t["REC"] == theme.TK_DARK["rec"]
        assert t["PAUSE"] == theme.TK_DARK["pause"]
        assert t["BORDER"] == theme.TK_DARK["line"]

    def test_light_palette_matches_theme_tokens(self):
        t = preview._THEMES["light"]
        assert t["BG"] == theme.TK_LIGHT["bg"]
        assert t["BLUE"] == theme.TK_LIGHT["ion"]
        assert t["REC"] == theme.TK_LIGHT["rec"]

    def test_no_hardcoded_green_anywhere_in_palette(self):
        # theme.py's own rule: "no green anywhere: success is ion" — the
        # accent/task tokens must be the ion hue, not a separate green.
        for name in ("dark", "light"):
            t = preview._THEMES[name]
            assert t["TASK"] == t["BLUE"]


class TestBadgeCfgThemeAware:
    def test_recording_accent_is_theme_rec(self):
        preview._apply_palette("dark")
        cfg = preview._badge_cfg("recording")
        assert cfg["accent"] == preview._REC

    def test_processing_accent_is_theme_pause(self):
        preview._apply_palette("dark")
        cfg = preview._badge_cfg("processing")
        assert cfg["accent"] == preview._PAUSE

    def test_unknown_state_falls_back_to_processing(self):
        assert preview._badge_cfg("bogus") == preview._badge_cfg("processing")

    def test_switching_theme_updates_the_accent(self):
        preview._apply_palette("dark")
        dark_rec = preview._badge_cfg("recording")["accent"]
        preview._apply_palette("light")
        light_rec = preview._badge_cfg("recording")["accent"]
        assert dark_rec != light_rec
        assert light_rec == theme.TK_LIGHT["rec"]
        preview._apply_palette("dark")  # leave global state as found
