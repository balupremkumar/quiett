"""
Modern pywebview dashboard for VoiceDictate.

Replaces the Tkinter Settings + History popup windows with a single
Edge-rendered window (Windows 11 native WebView2). The floating preview
panel (preview.py) is unchanged — it still appears near the cursor.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import date, datetime

# webview and history are only needed when running as __main__ (subprocess),
# but importing them at module level is harmless and keeps DashboardAPI clean.
try:
    import webview
    import history as hist
except ImportError:
    webview = None  # type: ignore
    hist = None     # type: ignore

_CONFIG_FILE = "config.json"

# Bumped by hand alongside CHANGELOG entries below (no CHANGELOG.md yet —
# app is pre-productisation per PRODUCTION_PLAN.md, hardcode until it exists).
VERSION = "0.1.0"
_CHANGELOG = [
    {"version": "0.1.0", "date": "2026-07-11", "notes": [
        "History search, day-grouping, and window-position memory added to dashboard.",
        "About page added.",
    ]},
]

# ── Subprocess launcher ────────────────────────────────────────────────────────
# pywebview requires the main thread, but pystray already owns it in main.py.
# Solution: launch the dashboard as a separate pythonw.exe subprocess so
# webview gets a clean main thread. Config/history are shared via the JSON files.

_proc: "subprocess.Popen | None" = None
_proc_lock = threading.Lock()
_AUTOSTART_TASK = "VoiceDictate"
_THIS_FILE = os.path.abspath(__file__)
_PROJECT_DIR = os.path.dirname(_THIS_FILE)


def open_window(page: str = "home") -> None:
    """Launch the dashboard subprocess, or ignore if already running.

    Must not be named `open`: a module-level `open` shadows the builtin for
    every function here and silently broke all config reads/writes."""
    global _proc
    with _proc_lock:
        if _proc is not None and _proc.poll() is None:
            return  # window is already open
        _proc = subprocess.Popen(
            [sys.executable, _THIS_FILE, page],
            cwd=_PROJECT_DIR,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )


# ── Config helpers ─────────────────────────────────────────────────────────────

def _read_cfg() -> dict:
    try:
        with open(_CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _merge_cfg(updates: dict) -> bool:
    try:
        current = _read_cfg()
        current.update(updates)
        tmp = _CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, _CONFIG_FILE)
        return True
    except Exception:
        return False


def _format_history_export(entries: list) -> str:
    lines = ["# VoiceDictate history export", ""]
    for e in entries:
        src = e.get("source") or "dictation"
        lines.append(f"## {e.get('timestamp', '')} — {src}")
        lines.append("")
        lines.append(e.get("text", ""))
        lines.append("")
    return "\n".join(lines)


def _day_group_label(d: "date", today: "date") -> str:
    """Today / Yesterday / weekday name (this calendar week) / '3 July 2026'."""
    diff = (today - d).days
    if diff == 0:
        return "Today"
    if diff == 1:
        return "Yesterday"
    if diff > 1 and d.isocalendar()[:2] == today.isocalendar()[:2]:
        return d.strftime("%A")
    return d.strftime("%d %B %Y").lstrip("0")


# ── Window geometry persistence ──────────────────────────────────────────────

_MIN_W, _MIN_H = 700, 500


def _load_window_geom() -> "dict | None":
    geom = _read_cfg().get("dashboard_window")
    if not isinstance(geom, dict):
        return None
    try:
        x, y = int(geom["x"]), int(geom["y"])
        w, h = max(_MIN_W, int(geom["w"])), max(_MIN_H, int(geom["h"]))
    except Exception:
        return None
    try:
        screens = webview.screens
        if screens:
            min_x = min(s.x for s in screens)
            min_y = min(s.y for s in screens)
            max_x = max(s.x + s.width for s in screens)
            max_y = max(s.y + s.height for s in screens)
            # Clamp so the title bar always stays reachable even if the
            # monitor that used to hold the window is now unplugged.
            x = min(max(x, min_x), max_x - 120)
            y = min(max(y, min_y), max_y - 80)
    except Exception:
        pass
    return {"x": x, "y": y, "w": w, "h": h}


def _save_window_geom(win) -> None:
    try:
        _merge_cfg({"dashboard_window": {
            "x": win.x, "y": win.y, "w": win.width, "h": win.height,
        }})
    except Exception:
        pass


def _apply_titlebar_theme(dark: bool) -> None:
    """Match the Win11 titlebar to the app theme (DWMWA_USE_IMMERSIVE_DARK_MODE);
    otherwise a light app under a dark Windows theme keeps a dark titlebar."""
    try:
        import ctypes
        from ctypes import wintypes

        user32, dwmapi = ctypes.windll.user32, ctypes.windll.dwmapi
        pid = os.getpid()
        val = ctypes.c_int(1 if dark else 0)

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _enum(hwnd, _):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid and user32.IsWindowVisible(hwnd):
                dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(val),
                                             ctypes.sizeof(val))
            return True

        user32.EnumWindows(_enum, 0)
    except Exception:
        pass


# ── Python → JS API ───────────────────────────────────────────────────────────

class DashboardAPI:
    """All methods callable from JS as window.pywebview.api.method(args)."""

    def get_stats(self) -> dict:
        entries = hist.load()
        today_str = date.today().isoformat()
        today_words = total_words = today_recs = 0
        for e in entries:
            wc = len(e.get("text", "").split())
            total_words += wc
            if e.get("timestamp", "").startswith(today_str):
                today_words += wc
                today_recs += 1
        cfg = _read_cfg()
        return {
            "today_words": today_words,
            "total_words": total_words,
            "today_recordings": today_recs,
            "total_recordings": len(entries),
            "correction_count": len(cfg.get("corrections", {})),
            "vocab_count": len(cfg.get("custom_vocabulary", [])),
        }

    def get_history(self, limit: int = 200) -> list:
        entries = hist.load()
        today = date.today()
        today_str = today.isoformat()
        result = []
        for i, e in enumerate(entries[:limit]):
            text = e.get("text", "")
            ts = e.get("timestamp", "")
            source = e.get("source", "")
            try:
                dt = datetime.fromisoformat(ts)
                ts_date = dt.date().isoformat()
                fmt = "%I:%M %p" if ts_date == today_str else "%d %b, %I:%M %p"
                label = dt.strftime(fmt).lstrip("0")
                day_label = _day_group_label(dt.date(), today)
            except Exception:
                label = ts
                ts_date = ""
                day_label = ""
            result.append({
                "index": i, "text": text,
                "words": len(text.split()),
                "label": label, "ts_date": ts_date, "source": source,
                "day_label": day_label, "pinned": bool(e.get("pinned")),
            })
        return result

    def delete_history_entry(self, idx: int) -> bool:
        try:
            entries = hist._load()
            if 0 <= idx < len(entries):
                entries.pop(idx)
                hist._write(entries)
                return True
        except Exception:
            pass
        return False

    def delete_history_entries(self, indices: list) -> int:
        try:
            entries = hist._load()
            drop = {int(i) for i in indices}
            keep = [e for i, e in enumerate(entries) if i not in drop]
            removed = len(entries) - len(keep)
            hist._write(keep)
            return removed
        except Exception:
            return 0

    def export_history_entries(self, indices: list) -> dict:
        try:
            entries = hist.load()
            chosen = [entries[i] for i in indices if 0 <= i < len(entries)]
            if not chosen:
                return {"ok": False, "error": "No entries to export."}
            chosen.sort(key=lambda e: e.get("timestamp", ""))
            content = _format_history_export(chosen)
            default_name = f"voicedictate-history-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"

            dialog_ok = True
            result = None
            try:
                win = webview.windows[0] if webview.windows else None
                if win is None:
                    raise RuntimeError("no window")
                result = win.create_file_dialog(
                    webview.FileDialog.SAVE, save_filename=default_name,
                    file_types=("Markdown (*.md)", "Text file (*.txt)"))
            except Exception:
                dialog_ok = False

            if not dialog_ok:
                downloads = os.path.join(os.path.expanduser("~"), "Downloads")
                os.makedirs(downloads, exist_ok=True)
                path = os.path.join(downloads, default_name)
            elif not result:
                return {"ok": False, "cancelled": True}
            else:
                path = result if isinstance(result, str) else result[0]

            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return {"ok": True, "path": path, "fallback": not dialog_ok}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def set_pinned(self, index: int, pinned: bool) -> bool:
        try:
            return hist.set_pinned(int(index), bool(pinned))
        except Exception:
            return False

    def clear_history(self) -> bool:
        try:
            hist.clear()
            return True
        except Exception:
            return False

    def get_dictionary(self) -> dict:
        cfg = _read_cfg()
        return {
            "corrections": cfg.get("corrections", {}),
            "vocabulary": cfg.get("custom_vocabulary", []),
        }

    def set_correction(self, original: str, replacement: str) -> bool:
        original = original.strip()
        replacement = replacement.strip()
        if not original or not replacement:
            return False
        cfg = _read_cfg()
        corr = dict(cfg.get("corrections", {}))
        corr[original] = replacement
        return _merge_cfg({"corrections": corr})

    def remove_correction(self, original: str) -> bool:
        cfg = _read_cfg()
        corr = dict(cfg.get("corrections", {}))
        if original in corr:
            del corr[original]
            return _merge_cfg({"corrections": corr})
        return False

    def add_vocabulary(self, word: str) -> bool:
        word = word.strip()
        if not word:
            return False
        cfg = _read_cfg()
        vocab = list(cfg.get("custom_vocabulary", []))
        if word not in vocab:
            vocab.append(word)
            return _merge_cfg({"custom_vocabulary": vocab})
        return True

    def remove_vocabulary(self, word: str) -> bool:
        cfg = _read_cfg()
        vocab = list(cfg.get("custom_vocabulary", []))
        if word in vocab:
            vocab.remove(word)
            return _merge_cfg({"custom_vocabulary": vocab})
        return False

    def get_config(self) -> dict:
        return _read_cfg()

    def set_titlebar_dark(self, dark: bool) -> bool:
        _apply_titlebar_theme(bool(dark))
        return True

    def get_autostart(self) -> bool:
        try:
            r = subprocess.run(
                ["schtasks", "/Query", "/TN", _AUTOSTART_TASK],
                capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            return r.returncode == 0
        except Exception:
            return False

    def set_autostart(self, enabled: bool) -> bool:
        try:
            if not enabled:
                r = subprocess.run(
                    ["schtasks", "/Delete", "/F", "/TN", _AUTOSTART_TASK],
                    capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                return True
            vbs = os.path.join(_PROJECT_DIR, "launch.vbs")
            base = ["schtasks", "/Create", "/F", "/TN", _AUTOSTART_TASK,
                    "/SC", "ONLOGON", "/TR", f'wscript.exe "{vbs}"']
            # /RL HIGHEST keeps global hotkeys working (app runs elevated);
            # fall back to a normal task when we aren't elevated ourselves
            r = subprocess.run(base + ["/RL", "HIGHEST"], capture_output=True,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            if r.returncode != 0:
                r = subprocess.run(base, capture_output=True,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
            return r.returncode == 0
        except Exception:
            return False

    def save_settings(self, data: dict) -> bool:
        ok = _merge_cfg(data)
        if ok and ("history_max_entries" in data or "recording_retention_days" in data):
            try:
                hist.purge()
            except Exception:
                pass
        return ok

    def get_audio_devices(self) -> list:
        try:
            import audio
            return [{"index": d["index"], "name": d["name"]}
                    for d in audio.list_input_devices()]
        except Exception:
            return []

    def get_about(self) -> dict:
        return {"version": VERSION, "changelog": _CHANGELOG}




# ── Embedded HTML template ─────────────────────────────────────────────────────

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>VoiceDictate</title>
<style>
/* ── Variables ── */
:root {
  --bg:#202020; --bg-sb:#181818; --surf:#2c2c2c; --surf2:#383838;
  --hov:#ffffff0d; --act:#ffffff18; --brd:#ffffff14; --brd2:#ffffff22;
  --txt:#ffffff; --txt2:rgba(255,255,255,.78); --txt3:rgba(255,255,255,.5);
  --acc:#60cdff; --acc-bg:rgba(96,205,255,.1); --acc-hov:rgba(96,205,255,.18);
  --danger:#f85149; --success:#3fb950;
  --shad:0 2px 14px rgba(0,0,0,.45);
  --r:8px; --r-sm:5px; --sbw:200px;
  --font:"Segoe UI Variable Text","Segoe UI",system-ui,sans-serif;
  --font-d:"Segoe UI Variable Display","Segoe UI",system-ui,sans-serif;
}
[data-theme=light]{
  --bg:#ececef; --bg-sb:#e4e4e8; --surf:#f8f8fa; --surf2:#efeff2;
  --hov:#0000000d; --act:#00000016; --brd:#00000026; --brd2:#00000044;
  --txt:#1c1c1c; --txt2:rgba(0,0,0,.78); --txt3:rgba(0,0,0,.55);
  --acc:#0067c0; --acc-bg:rgba(0,103,192,.08); --acc-hov:rgba(0,103,192,.14);
  --danger:#cf222e; --success:#2da44e;
  --shad:0 2px 10px rgba(0,0,0,.07);
}

*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100vh;overflow:hidden}
body{font-family:var(--font);background:var(--bg);color:var(--txt);-webkit-font-smoothing:antialiased}

/* ── Layout ── */
.app{display:flex;height:100vh}

/* ── Sidebar ── */
.sidebar{width:var(--sbw);background:var(--bg-sb);display:flex;flex-direction:column;
  padding:14px 8px;flex-shrink:0;border-right:1px solid var(--brd)}
.logo{display:flex;align-items:center;gap:9px;padding:6px 8px 22px;
  font-family:var(--font-d);font-size:14.5px;font-weight:600;color:var(--txt)}
.logo svg{width:20px;height:20px;color:var(--acc);flex-shrink:0}
.nav{flex:1;display:flex;flex-direction:column;gap:1px}
.nav-item{display:flex;align-items:center;gap:9px;padding:9px 11px;border-radius:var(--r-sm);
  cursor:pointer;font-size:13px;color:var(--txt2);border:none;background:transparent;
  width:100%;text-align:left;transition:background .1s,color .1s;font-family:var(--font)}
.nav-item:hover{background:var(--hov);color:var(--txt)}
.nav-item.active{background:var(--act);color:var(--txt);font-weight:500}
.nav-item svg{width:15px;height:15px;flex-shrink:0}
.sb-footer{margin-top:auto;padding:10px 4px 2px;display:flex;align-items:center;
  justify-content:space-between}
.sb-status{display:flex;align-items:center;gap:7px;font-size:11px;color:var(--txt3)}
.status-dot{width:6px;height:6px;border-radius:50%;background:var(--success)}
.theme-btn{width:28px;height:28px;border-radius:5px;border:none;background:transparent;
  cursor:pointer;color:var(--txt3);display:flex;align-items:center;justify-content:center;
  transition:background .1s,color .1s}
.theme-btn:hover{background:var(--hov);color:var(--txt)}
.theme-btn svg{width:14px;height:14px}

/* ── Main ── */
.main{flex:1;overflow:hidden;display:flex;flex-direction:column}
.page{display:none;flex-direction:column;height:100%;overflow-y:auto}
.page.active{display:flex;animation:fadeIn .15s ease}
@keyframes fadeIn{from{opacity:0;transform:translateY(3px)}to{opacity:1;transform:translateY(0)}}

/* ── Page header ── */
.ph{padding:26px 30px 18px;display:flex;align-items:flex-start;
  justify-content:space-between;flex-shrink:0}
.ph h1{font-family:var(--font-d);font-size:21px;font-weight:600;line-height:1.2}
.ph .sub{font-size:12px;color:var(--txt3);margin-top:2px}
.ph-actions{display:flex;gap:8px;align-items:center}

/* ── Stat cards ── */
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;
  padding:0 30px 22px;flex-shrink:0}
.stat-card{background:var(--surf);border:1px solid var(--brd);border-radius:var(--r);
  padding:14px 16px;box-shadow:var(--shad);transition:border-color .15s}
.stat-card:hover{border-color:var(--brd2)}
.stat-lbl{font-size:9.5px;font-weight:700;letter-spacing:.6px;color:var(--txt3);
  text-transform:uppercase;margin-bottom:8px;display:flex;align-items:center;gap:5px}
.stat-lbl svg{width:11px;height:11px}
.stat-val{font-family:var(--font-d);font-size:25px;font-weight:700;
  color:var(--txt);letter-spacing:-.5px}

/* ── Section title ── */
.sec-hdr{display:flex;align-items:center;justify-content:space-between;
  padding:0 30px 10px;flex-shrink:0}
.sec-hdr h2{font-size:13px;font-weight:600;color:var(--txt2)}
.link-btn{font-size:12px;color:var(--acc);background:none;border:none;cursor:pointer;
  padding:2px 6px;border-radius:4px;font-family:var(--font);transition:background .1s}
.link-btn:hover{background:var(--acc-bg)}

/* ── Activity / History list ── */
.list-wrap{padding:0 30px;flex:1;overflow-y:auto}
.day-hdr{font-size:10.5px;font-weight:700;letter-spacing:.5px;text-transform:uppercase;
  color:var(--txt3);padding:14px 12px 6px}
.day-hdr:first-child{padding-top:4px}
.list-item{padding:9px 12px;border-radius:var(--r-sm);cursor:default;
  transition:background .1s;position:relative;display:flex;flex-direction:column;
  gap:2px;border-bottom:1px solid var(--brd)}
.list-item:last-child{border-bottom:none}
.list-item:hover{background:var(--hov)}
.li-text{font-size:12.5px;color:var(--txt);overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;padding-right:70px;-webkit-user-select:text;user-select:text}
.li-meta{font-size:11px;color:var(--txt3);display:flex;align-items:center;gap:5px}
.src-badge{font-size:9px;padding:1px 5px;border-radius:8px;background:var(--acc-bg);
  color:var(--acc);font-weight:700;text-transform:uppercase}
.li-acts{position:absolute;right:10px;top:50%;transform:translateY(-50%);
  display:none;gap:4px}
.list-item:hover .li-acts{display:flex}
.ia{width:26px;height:26px;display:flex;align-items:center;justify-content:center;
  background:var(--surf2);border:1px solid var(--brd);border-radius:4px;
  cursor:pointer;color:var(--txt2);transition:all .1s}
.ia:hover{background:var(--hov);color:var(--txt)}
.ia.del:hover{background:rgba(248,81,73,.15);color:var(--danger);border-color:var(--danger)}
.ia svg{width:12px;height:12px}
.ia.pin.on{color:var(--acc)}
.empty{text-align:center;padding:48px 32px;color:var(--txt3);font-size:13px}
.list-item.pinned{background:var(--acc-bg)}
.pin-badge{display:inline-flex;color:var(--acc)}
.pin-badge svg{width:10px;height:10px}
mark{background:var(--acc-bg);color:inherit;border-radius:2px;padding:0 1px}

/* ── Search ── */
.search-wrap{padding:14px 30px 10px;flex-shrink:0;position:relative}
.search-icon{position:absolute;left:42px;top:50%;transform:translateY(-50%);
  width:14px;height:14px;color:var(--txt3);pointer-events:none}
.search-in{width:100%;padding:8px 12px 8px 34px;background:var(--surf);
  border:1px solid var(--brd);border-radius:var(--r-sm);color:var(--txt);
  font-family:var(--font);font-size:12.5px;outline:none}
.search-in:focus{border-color:var(--acc)}
.search-in::placeholder{color:var(--txt3)}

/* ── Filter chips ── */
.chip-row{display:flex;gap:6px;padding:0 30px 12px;flex-shrink:0;flex-wrap:wrap}
.chip{padding:5px 12px;border-radius:14px;border:1px solid var(--brd);background:var(--surf);
  color:var(--txt2);font-size:11.5px;font-weight:500;cursor:pointer;transition:all .12s;
  font-family:var(--font)}
.chip:hover{background:var(--hov);color:var(--txt)}
.chip.active{background:var(--acc-bg);border-color:var(--acc);color:var(--acc)}

/* ── Bulk select ── */
.bulk-bar{display:flex;align-items:center;gap:14px;padding:8px 30px;flex-shrink:0;
  background:var(--surf2);border-bottom:1px solid var(--brd);font-size:12px;color:var(--txt2)}
.bulk-all{display:flex;align-items:center;gap:6px;cursor:pointer}
.bulk-all input{accent-color:var(--acc);cursor:pointer}
.bulk-count{color:var(--txt3)}
.bulk-acts{margin-left:auto;display:flex;gap:8px}
.li-check{display:none;position:absolute;left:10px;top:50%;transform:translateY(-50%);
  accent-color:var(--acc);cursor:pointer}
.list-wrap.select-mode .li-check{display:block}
.list-wrap.select-mode .list-item{padding-left:34px}
.list-wrap.select-mode .li-acts{display:none !important}

/* ── Toast ── */
.dash-toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%) translateY(8px);
  background:var(--surf2);border:1px solid var(--brd);border-radius:var(--r-sm);
  padding:8px 16px;font-size:12px;color:var(--txt);box-shadow:var(--shad);opacity:0;
  pointer-events:none;transition:opacity .2s,transform .2s;z-index:50}
.dash-toast.show{opacity:1;transform:translateX(-50%) translateY(0)}

/* ── Dictionary page ── */
.dict-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;
  padding:0 30px 30px;flex:1;overflow-y:auto;align-content:start}
.dict-sec{background:var(--surf);border:1px solid var(--brd);border-radius:var(--r);
  padding:16px;box-shadow:var(--shad);display:flex;flex-direction:column;gap:10px}
.dict-sec-ttl{font-size:11px;font-weight:700;color:var(--txt3);text-transform:uppercase;
  letter-spacing:.5px}
.corr-item{display:flex;align-items:center;gap:6px;padding:5px 6px;border-radius:4px;
  transition:background .1s}
.corr-item:hover{background:var(--hov)}
.corr-from{color:var(--txt2);font-size:12px;flex:1;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.corr-arr{color:var(--txt3);font-size:11px;flex-shrink:0}
.corr-to{color:var(--txt);font-size:12px;font-weight:500;flex:1;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.corr-del{display:none;width:20px;height:20px;border-radius:3px;background:none;
  border:none;cursor:pointer;color:var(--txt3);align-items:center;justify-content:center;
  transition:all .1s;flex-shrink:0}
.corr-item:hover .corr-del{display:flex}
.corr-del:hover{color:var(--danger);background:rgba(248,81,73,.12)}
.corr-del svg{width:10px;height:10px}
.vocab-chips{display:flex;flex-wrap:wrap;gap:6px;min-height:20px}
.vchip{display:flex;align-items:center;gap:4px;padding:3px 8px 3px 10px;
  background:var(--acc-bg);border:1px solid rgba(96,205,255,.15);border-radius:12px;
  font-size:11.5px;color:var(--acc)}
[data-theme=light] .vchip{border-color:rgba(0,103,192,.2)}
.vdel{background:none;border:none;cursor:pointer;color:var(--acc);opacity:.6;
  padding:0;display:flex;align-items:center;transition:opacity .1s}
.vdel:hover{opacity:1}
.vdel svg{width:9px;height:9px}
.add-row{display:flex;gap:6px;margin-top:4px}
.add-row-2{display:grid;grid-template-columns:1fr 1fr auto;gap:6px;margin-top:4px}
.add-in{flex:1;padding:6px 9px;background:var(--surf2);border:1px solid var(--brd);
  border-radius:var(--r-sm);color:var(--txt);font-family:var(--font);
  font-size:12px;outline:none}
.add-in:focus{border-color:var(--acc)}
.btn{padding:6px 14px;border-radius:var(--r-sm);border:none;cursor:pointer;
  font-family:var(--font);font-size:12px;font-weight:500;transition:all .12s}
.btn-p{background:var(--acc);color:#000}
[data-theme=light] .btn-p{color:#fff}
.btn-p:hover{opacity:.85}
.btn-s{background:var(--surf2);color:var(--txt);border:1px solid var(--brd)}
.btn-s:hover{background:var(--act)}
.btn-danger{background:rgba(248,81,73,.12);color:var(--danger);
  border:1px solid rgba(248,81,73,.3)}
.btn-danger:hover{background:rgba(248,81,73,.22)}

/* ── Settings ── */
.settings-scroll{padding:0 30px 30px;flex:1;overflow-y:auto}
.s-sec{background:var(--surf);border:1px solid var(--brd);border-radius:var(--r);
  overflow:hidden;box-shadow:var(--shad);margin-bottom:16px}
.s-sec:last-child{margin-bottom:0}
.s-sec-ttl{padding:11px 16px;font-size:11px;font-weight:700;color:var(--txt3);
  text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid var(--brd);
  background:var(--surf2)}
.s-row{display:flex;align-items:center;padding:11px 16px;gap:14px;
  border-bottom:1px solid var(--brd);min-height:44px}
.s-row:last-child{border-bottom:none}
.s-row.col{flex-direction:column;align-items:flex-start;gap:7px}
.s-lbl{flex:1}
.s-lbl-t{font-size:13px;color:var(--txt);font-weight:500}
.s-lbl-s{font-size:11px;color:var(--txt3);margin-top:1px}
/* toggle */
.tog{width:38px;height:21px;background:var(--surf2);border:1px solid var(--brd);
  border-radius:11px;cursor:pointer;position:relative;transition:background .15s,border-color .15s;
  flex-shrink:0}
.tog.on{background:var(--acc);border-color:var(--acc)}
.tog-k{width:15px;height:15px;background:var(--txt3);border-radius:50%;
  position:absolute;top:2px;left:2px;transition:transform .15s,background .15s;
  box-shadow:0 1px 3px rgba(0,0,0,.3)}
.tog.on .tog-k{transform:translateX(17px);background:#000}
[data-theme=light] .tog.on .tog-k{background:#fff}
/* inputs */
.n-in,.t-in,.sel-in{padding:6px 9px;background:var(--surf2);border:1px solid var(--brd);
  border-radius:var(--r-sm);color:var(--txt);font-family:var(--font);
  font-size:12.5px;outline:none}
.n-in{width:78px;text-align:right}
.t-in{width:200px}
.t-in.wide{width:300px}
.sel-in{padding-right:26px;cursor:pointer;-webkit-appearance:none;appearance:none;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 20 20' fill='%23888'%3E%3Cpath fill-rule='evenodd' d='M5.293 7.293a1 1 0 011.414 0L10 10.586l3.293-3.293a1 1 0 111.414 1.414l-4 4a1 1 0 01-1.414 0l-4-4a1 1 0 010-1.414z' clip-rule='evenodd'/%3E%3C/svg%3E");
  background-repeat:no-repeat;background-position:right 7px center;background-size:13px}
.n-in:focus,.t-in:focus,.sel-in:focus,.add-in:focus{border-color:var(--acc)}
.ta-in{width:100%;padding:7px 9px;background:var(--surf2);border:1px solid var(--brd);
  border-radius:var(--r-sm);color:var(--txt);font-family:var(--font);
  font-size:12px;outline:none;resize:vertical;min-height:56px}
.ta-in:focus{border-color:var(--acc)}
select option{background:var(--surf2);color:var(--txt)}
.save-bar{padding:14px 30px;display:flex;justify-content:flex-end;gap:10px;
  align-items:center;border-top:1px solid var(--brd);flex-shrink:0;background:var(--bg)}
.save-ok{font-size:12px;color:var(--success);opacity:0;transition:opacity .3s}
.save-ok.show{opacity:1}

/* ── Scrollbar ── */
::-webkit-scrollbar{width:5px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--surf2);border-radius:3px}
::-webkit-scrollbar-thumb:hover{background:var(--brd2)}
</style>
</head>
<body>
<div class="app">

<!-- ── Sidebar ─────────────────────────────────────────────── -->
<aside class="sidebar">
  <div class="logo">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <rect x="9" y="2" width="6" height="12" rx="3"/>
      <path d="M5 10a7 7 0 0014 0"/><line x1="12" y1="17" x2="12" y2="22"/>
      <line x1="8" y1="22" x2="16" y2="22"/>
    </svg>
    VoiceDictate
  </div>
  <nav class="nav">
    <button class="nav-item active" data-page="home" onclick="navigateTo('home')">
      <svg viewBox="0 0 20 20" fill="currentColor"><path d="M10.707 2.293a1 1 0 00-1.414 0l-7 7a1 1 0 001.414 1.414L4 10.414V17a1 1 0 001 1h2a1 1 0 001-1v-2a1 1 0 011-1h2a1 1 0 011 1v2a1 1 0 001 1h2a1 1 0 001-1v-6.586l.293.293a1 1 0 001.414-1.414l-7-7z"/></svg>
      Home
    </button>
    <button class="nav-item" data-page="history" onclick="navigateTo('history')">
      <svg viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm1-12a1 1 0 10-2 0v4a1 1 0 00.293.707l2.828 2.829a1 1 0 101.415-1.415L11 9.586V6z" clip-rule="evenodd"/></svg>
      History
    </button>
    <button class="nav-item" data-page="dictionary" onclick="navigateTo('dictionary')">
      <svg viewBox="0 0 20 20" fill="currentColor"><path d="M9 4.804A7.968 7.968 0 005.5 4c-1.255 0-2.443.29-3.5.804v10A7.969 7.969 0 015.5 14c1.669 0 3.218.51 4.5 1.385A7.962 7.962 0 0114.5 14c1.255 0 2.443.29 3.5.804v-10A7.968 7.968 0 0014.5 4c-1.255 0-2.443.29-3.5.804V12a1 1 0 11-2 0V4.804z"/></svg>
      Dictionary
    </button>
    <button class="nav-item" data-page="settings" onclick="navigateTo('settings')">
      <svg viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M11.49 3.17c-.38-1.56-2.6-1.56-2.98 0a1.532 1.532 0 01-2.286.948c-1.372-.836-2.942.734-2.106 2.106.54.886.061 2.042-.947 2.287-1.561.379-1.561 2.6 0 2.978a1.532 1.532 0 01.947 2.287c-.836 1.372.734 2.942 2.106 2.106a1.532 1.532 0 012.287.947c.379 1.561 2.6 1.561 2.978 0a1.533 1.533 0 012.287-.947c1.372.836 2.942-.734 2.106-2.106a1.533 1.533 0 01.947-2.287c1.561-.379 1.561-2.6 0-2.978a1.532 1.532 0 01-.947-2.287c.836-1.372-.734-2.942-2.106-2.106a1.532 1.532 0 01-2.287-.947zM10 13a3 3 0 100-6 3 3 0 000 6z" clip-rule="evenodd"/></svg>
      Settings
    </button>
    <button class="nav-item" data-page="about" onclick="navigateTo('about')">
      <svg viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7-4a1 1 0 11-2 0 1 1 0 012 0zM9 9a1 1 0 000 2v3a1 1 0 001 1h1a1 1 0 100-2v-3a1 1 0 00-1-1H9z" clip-rule="evenodd"/></svg>
      About
    </button>
  </nav>
  <div class="sb-footer">
    <div class="sb-status">
      <div class="status-dot" id="statusDot"></div>
      <span id="statusTxt">Ready</span>
    </div>
    <button class="theme-btn" id="themeBtn" onclick="toggleTheme()" title="Toggle theme">
      <svg id="themeIcon" viewBox="0 0 20 20" fill="currentColor">
        <path d="M17.293 13.293A8 8 0 016.707 2.707a8.001 8.001 0 1010.586 10.586z"/>
      </svg>
    </button>
  </div>
</aside>

<!-- ── Main ─────────────────────────────────────────────────── -->
<main class="main">

<!-- Home -->
<div class="page active" id="page-home">
  <div class="ph">
    <div>
      <h1>Home</h1>
      <div class="sub">Your usage at a glance.</div>
    </div>
  </div>
  <div class="stats" id="statsGrid">
    <div class="stat-card">
      <div class="stat-lbl">
        <svg viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M4 4a2 2 0 012-2h4.586A2 2 0 0112 2.586L15.414 6A2 2 0 0116 7.414V16a2 2 0 01-2 2H6a2 2 0 01-2-2V4zm2 6a1 1 0 011-1h6a1 1 0 110 2H7a1 1 0 01-1-1zm1 3a1 1 0 100 2h6a1 1 0 100-2H7z" clip-rule="evenodd"/></svg>
        WORDS TODAY
      </div>
      <div class="stat-val" id="s-tw">—</div>
    </div>
    <div class="stat-card">
      <div class="stat-lbl">
        <svg viewBox="0 0 20 20" fill="currentColor"><path d="M2 11a1 1 0 011-1h2a1 1 0 011 1v5a1 1 0 01-1 1H3a1 1 0 01-1-1v-5zM8 7a1 1 0 011-1h2a1 1 0 011 1v9a1 1 0 01-1 1H9a1 1 0 01-1-1V7zM14 4a1 1 0 011-1h2a1 1 0 011 1v12a1 1 0 01-1 1h-2a1 1 0 01-1-1V4z"/></svg>
        TOTAL WORDS
      </div>
      <div class="stat-val" id="s-ttl">—</div>
    </div>
    <div class="stat-card">
      <div class="stat-lbl">
        <svg viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M7 4a3 3 0 016 0v4a3 3 0 11-6 0V4zm4 10.93A7.001 7.001 0 0017 8a1 1 0 10-2 0A5 5 0 015 8a1 1 0 00-2 0 7.001 7.001 0 006 6.93V17H6a1 1 0 100 2h8a1 1 0 100-2h-3v-2.07z" clip-rule="evenodd"/></svg>
        TODAY'S RECS
      </div>
      <div class="stat-val" id="s-tr">—</div>
    </div>
    <div class="stat-card">
      <div class="stat-lbl">
        <svg viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm1-12a1 1 0 10-2 0v4a1 1 0 00.293.707l2.828 2.829a1 1 0 101.415-1.415L11 9.586V6z" clip-rule="evenodd"/></svg>
        ALL TIME
      </div>
      <div class="stat-val" id="s-at">—</div>
    </div>
  </div>
  <div class="sec-hdr">
    <h2>Recent Activity</h2>
    <button class="link-btn" onclick="navigateTo('history')">View All</button>
  </div>
  <div class="list-wrap" id="activityList"><div class="empty">Loading…</div></div>
</div>

<!-- History -->
<div class="page" id="page-history">
  <div class="ph">
    <div><h1>History</h1><div class="sub">Your recent dictations.</div></div>
    <div class="ph-actions">
      <button class="btn btn-s" id="selectModeBtn" onclick="toggleSelectMode()">Select</button>
      <button class="btn btn-danger" onclick="confirmClear()">Clear All</button>
    </div>
  </div>
  <div class="search-wrap">
    <svg class="search-icon" viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M8 4a4 4 0 100 8 4 4 0 000-8zM2 8a6 6 0 1110.89 3.476l4.817 4.817a1 1 0 01-1.414 1.414l-4.816-4.816A6 6 0 012 8z" clip-rule="evenodd"/></svg>
    <input class="search-in" id="histSearch" placeholder="Search dictations or app…" oninput="onHistSearchInput(this.value)">
  </div>
  <div class="chip-row" id="sourceChips">
    <button class="chip active" data-src="all" onclick="setSourceFilter('all')">All</button>
    <button class="chip" data-src="" onclick="setSourceFilter('')">Dictation</button>
    <button class="chip" data-src="taskflow" onclick="setSourceFilter('taskflow')">TaskFlow</button>
    <button class="chip" data-src="agent" onclick="setSourceFilter('agent')">Agent</button>
  </div>
  <div class="bulk-bar" id="bulkBar" style="display:none">
    <label class="bulk-all"><input type="checkbox" id="selectAllChk" onchange="selectAllToggle(this.checked)"> Select all</label>
    <span class="bulk-count" id="bulkCount">0 selected</span>
    <div class="bulk-acts">
      <button class="btn btn-s" onclick="exportSelected()">Export</button>
      <button class="btn btn-danger" onclick="deleteSelected()">Delete</button>
    </div>
  </div>
  <div class="list-wrap" id="historyList"><div class="empty">Loading…</div></div>
</div>

<!-- Dictionary -->
<div class="page" id="page-dictionary">
  <div class="ph">
    <div><h1>Dictionary</h1><div class="sub">Corrections and custom vocabulary.</div></div>
  </div>
  <div class="dict-grid">
    <!-- Corrections -->
    <div class="dict-sec">
      <div class="dict-sec-ttl">Corrections</div>
      <div id="corrList"><div class="empty" style="padding:16px 0">Loading…</div></div>
      <div class="add-row-2" style="margin-top:8px">
        <input class="add-in" id="corrFrom" placeholder="Heard (e.g. Selvin)">
        <input class="add-in" id="corrTo" placeholder="Replace with (e.g. Selwyn)">
        <button class="btn btn-p" onclick="addCorrection()">Add</button>
      </div>
    </div>
    <!-- Vocabulary -->
    <div class="dict-sec">
      <div class="dict-sec-ttl">Custom Vocabulary</div>
      <div class="vocab-chips" id="vocabList"></div>
      <div class="add-row" style="margin-top:8px">
        <input class="add-in" id="vocabWord" placeholder="New word or phrase…" onkeydown="if(event.key==='Enter')addVocab()">
        <button class="btn btn-p" onclick="addVocab()">Add</button>
      </div>
    </div>
  </div>
</div>

<!-- Settings -->
<div class="page" id="page-settings">
  <div class="ph"><div><h1>Settings</h1><div class="sub">Configure your dictation preferences.</div></div></div>
  <div class="settings-scroll" id="settingsForm">

    <div class="s-sec">
      <div class="s-sec-ttl">Appearance</div>
      <div class="s-row">
        <div class="s-lbl"><div class="s-lbl-t">Theme</div><div class="s-lbl-s">Dark or light interface</div></div>
        <select class="sel-in" data-key="theme" onchange="applyThemeFromSelect(this.value)">
          <option value="dark">Dark</option>
          <option value="light">Light</option>
          <option value="system">Follow Windows</option>
        </select>
      </div>
      <div class="s-row">
        <div class="s-lbl"><div class="s-lbl-t">Preview Position</div><div class="s-lbl-s">Where the transcription panel appears</div></div>
        <select class="sel-in" data-key="preview_position">
          <option value="cursor">Near cursor</option>
          <option value="top-right">Top right</option>
          <option value="bottom-right">Bottom right</option>
          <option value="top-left">Top left</option>
          <option value="bottom-left">Bottom left</option>
          <option value="center">Centre</option>
        </select>
      </div>
      <div class="s-row">
        <div class="s-lbl"><div class="s-lbl-t">Auto-dismiss (seconds)</div><div class="s-lbl-s">0 = never auto-dismiss</div></div>
        <input class="n-in" type="number" data-key="preview_auto_dismiss_seconds" min="0" max="30" step="0.5">
      </div>
    </div>

    <div class="s-sec">
      <div class="s-sec-ttl">Audio</div>
      <div class="s-row">
        <div class="s-lbl"><div class="s-lbl-t">Input Device</div><div class="s-lbl-s">Microphone used for recording</div></div>
        <select class="sel-in" data-key="input_device" id="micSelect">
          <option value="">System default</option>
        </select>
      </div>
    </div>

    <div class="s-sec">
      <div class="s-sec-ttl">Recording</div>
      <div class="s-row">
        <div class="s-lbl"><div class="s-lbl-t">Min duration (s)</div><div class="s-lbl-s">Ignore recordings shorter than this</div></div>
        <input class="n-in" type="number" data-key="min_record_seconds" min="0.1" max="5" step="0.1">
      </div>
      <div class="s-row">
        <div class="s-lbl"><div class="s-lbl-t">Max duration (s)</div><div class="s-lbl-s">Auto-stop after this many seconds</div></div>
        <input class="n-in" type="number" data-key="max_record_seconds" min="5" max="300" step="5">
      </div>
      <div class="s-row">
        <div class="s-lbl"><div class="s-lbl-t">Silence auto-stop (s)</div><div class="s-lbl-s">Release hotkey automatically after silence. 0 = off</div></div>
        <input class="n-in" type="number" data-key="silence_auto_stop_seconds" min="0" max="10" step="0.5">
      </div>
      <div class="s-row">
        <div class="s-lbl"><div class="s-lbl-t">VAD filter</div><div class="s-lbl-s">Strip silence via voice activity detection</div></div>
        <div class="tog" data-key="vad_filter" onclick="togClick(this)"><div class="tog-k"></div></div>
      </div>
    </div>

    <div class="s-sec">
      <div class="s-sec-ttl">Transcription</div>
      <div class="s-row">
        <div class="s-lbl"><div class="s-lbl-t">Language</div><div class="s-lbl-s">ISO code, e.g. en, fr, de</div></div>
        <input class="t-in" type="text" data-key="language" maxlength="10">
      </div>
      <div class="s-row col">
        <div class="s-lbl"><div class="s-lbl-t">Filler words</div><div class="s-lbl-s">Comma-separated words to remove (e.g. um, uh, like)</div></div>
        <input class="t-in wide" type="text" data-key="filler_words" data-type="array">
      </div>
      <div class="s-row col">
        <div class="s-lbl"><div class="s-lbl-t">Initial prompt</div><div class="s-lbl-s">Primes Whisper with context (vocabulary, style)</div></div>
        <textarea class="ta-in" data-key="initial_prompt" rows="3"></textarea>
      </div>
    </div>

    <div class="s-sec">
      <div class="s-sec-ttl">Paste</div>
      <div class="s-row">
        <div class="s-lbl"><div class="s-lbl-t">Clipboard-only mode</div><div class="s-lbl-s">Copy to clipboard instead of auto-pasting</div></div>
        <div class="tog" data-key="_paste_clipboard_only" onclick="togClick(this)"><div class="tog-k"></div></div>
      </div>
      <div class="s-row">
        <div class="s-lbl"><div class="s-lbl-t">Clipboard restore delay (ms)</div><div class="s-lbl-s">How long before restoring your previous clipboard</div></div>
        <input class="n-in" type="number" data-key="clipboard_restore_delay_ms" min="50" max="1000" step="50" data-type="int">
      </div>
      <div class="s-row">
        <div class="s-lbl"><div class="s-lbl-t">Electron / VS Code paste</div><div class="s-lbl-s">Method for Electron apps</div></div>
        <select class="sel-in" data-key="electron_paste_method">
          <option value="ctrl_v">Clipboard + Ctrl+V</option>
          <option value="type">Unicode typing</option>
        </select>
      </div>
    </div>

    <div class="s-sec">
      <div class="s-sec-ttl">History</div>
      <div class="s-row">
        <div class="s-lbl"><div class="s-lbl-t">Keep history</div><div class="s-lbl-s">Older entries are trimmed past this count. Pinned entries are never trimmed.</div></div>
        <select class="sel-in" data-key="history_max_entries">
          <option value="50">50 entries</option>
          <option value="100" selected>100 entries</option>
          <option value="250">250 entries</option>
          <option value="500">500 entries</option>
        </select>
      </div>
      <div class="s-row">
        <div class="s-lbl"><div class="s-lbl-t">Auto-delete recordings</div><div class="s-lbl-s">Deletes saved audio older than this. Transcripts stay either way; pinned entries are exempt.</div></div>
        <select class="sel-in" data-key="recording_retention_days">
          <option value="0" selected>Never</option>
          <option value="7">After 7 days</option>
          <option value="30">After 30 days</option>
          <option value="90">After 90 days</option>
        </select>
      </div>
    </div>

    <div class="s-sec">
      <div class="s-sec-ttl">Agent Command Mode</div>
      <div class="s-row">
        <div class="s-lbl"><div class="s-lbl-t">LM Studio model</div><div class="s-lbl-s">Model identifier as shown in lms ls (e.g. qwen/qwen2.5-1.5b-instruct)</div></div>
        <input class="t-in wide" type="text" data-key="lmstudio_model">
      </div>
    </div>

    <div class="s-sec">
      <div class="s-sec-ttl">System</div>
      <div class="s-row">
        <div class="s-lbl"><div class="s-lbl-t">Start with Windows</div><div class="s-lbl-s">Launch hidden at logon via Task Scheduler</div></div>
        <div class="tog" id="autostartTog" onclick="autostartClick(this)"><div class="tog-k"></div></div>
      </div>
    </div>

    <div class="s-sec">
      <div class="s-sec-ttl">TaskFlow integration</div>
      <div class="s-row">
        <div class="s-lbl"><div class="s-lbl-t">Enable TaskFlow</div><div class="s-lbl-s">Voice triggers create tasks in TaskFlow</div></div>
        <div class="tog" data-key="taskflow_enabled" onclick="togClick(this)"><div class="tog-k"></div></div>
      </div>
      <div class="s-row">
        <div class="s-lbl"><div class="s-lbl-t">Voice confirmation</div><div class="s-lbl-s">Speak the task title back after creating it</div></div>
        <div class="tog" data-key="taskflow_voice_confirm" onclick="togClick(this)"><div class="tog-k"></div></div>
      </div>
      <div class="s-row">
        <div class="s-lbl"><div class="s-lbl-t">Default project</div><div class="s-lbl-s">Project name for new tasks (leave blank for none)</div></div>
        <input class="t-in" type="text" data-key="taskflow_default_project">
      </div>
    </div>

  </div>
  <div class="save-bar">
    <span class="save-ok" id="saveOk">✓ Saved</span>
    <button class="btn btn-s" onclick="reloadSettings()">Reload</button>
  </div>
</div>

<!-- About -->
<div class="page" id="page-about">
  <div class="ph"><div><h1>About</h1><div class="sub">Version, licence, and credits.</div></div></div>
  <div class="dict-grid" style="grid-template-columns:1fr">
    <div class="dict-sec">
      <div class="dict-sec-ttl">VoiceDictate</div>
      <div class="s-lbl-s" id="aboutVersion" style="font-size:12.5px">Version —</div>
      <div class="s-lbl-s" style="font-size:12.5px;line-height:1.5">
        Offline, hold-to-talk voice dictation for Windows. Records while you hold a hotkey,
        transcribes locally, and lets you review before it lands in whatever app has focus.
      </div>
      <div class="s-lbl-s" style="font-size:11.5px;margin-top:2px">
        Licence: private build, not for redistribution.
      </div>
    </div>
    <div class="dict-sec">
      <div class="dict-sec-ttl">Credits</div>
      <div class="s-lbl-s" style="font-size:12.5px;line-height:1.7">
        Speech recognition by <strong style="color:var(--txt2)">whisper.cpp</strong>
        (large-v3-turbo, Vulkan build) — ggml-org/whisper.cpp, MIT licence.<br>
        Agent command mode via <strong style="color:var(--txt2)">LM Studio</strong> +
        Qwen2.5-1.5B-Instruct.
      </div>
    </div>
    <div class="dict-sec">
      <div class="dict-sec-ttl">Changelog</div>
      <div id="changelogList" class="s-lbl-s" style="font-size:12.5px">Loading…</div>
      <button class="btn btn-s" disabled title="Offline app — no update check" style="opacity:.5;cursor:not-allowed;align-self:flex-start">Check for updates</button>
    </div>
  </div>
</div>

</main>
</div>

<script>
// ── State ──────────────────────────────────────────────────────────────────
let _histAll = [];
let _histTexts = [];  // parallel array — safe index-based access avoids JSON-in-onclick quoting bugs
let _homeTexts = [];
let _currentPage = 'home';
let _theme = 'dark';
let _INIT_PAGE = 'home';
let _histSourceFilter = 'all';
let _selectMode = false;
let _selectedIdx = new Set();

// ── Helpers ────────────────────────────────────────────────────────────────
function fmt(n) {
  if (n == null) return '—';
  if (n >= 1000000) return (n/1e6).toFixed(1).replace('.0','')+'M';
  if (n >= 10000) return (n/1000).toFixed(1).replace('.0','')+'K';
  return n.toLocaleString();
}

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function copyIcon() {
  return '<svg viewBox="0 0 20 20" fill="currentColor"><path d="M8 2a1 1 0 000 2h2a1 1 0 100-2H8z"/><path d="M3 5a2 2 0 012-2 3 3 0 003 3h2a3 3 0 003-3 2 2 0 012 2v6h-4.586l1.293-1.293a1 1 0 00-1.414-1.414l-3 3a1 1 0 000 1.414l3 3a1 1 0 001.414-1.414L10.414 13H15v3a2 2 0 01-2 2H5a2 2 0 01-2-2V5z"/></svg>';
}

function trashIcon() {
  return '<svg viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M9 2a1 1 0 00-.894.553L7.382 4H4a1 1 0 000 2v10a2 2 0 002 2h8a2 2 0 002-2V6a1 1 0 100-2h-3.382l-.724-1.447A1 1 0 0011 2H9zM7 8a1 1 0 012 0v6a1 1 0 11-2 0V8zm5-1a1 1 0 00-1 1v6a1 1 0 102 0V8a1 1 0 00-1-1z" clip-rule="evenodd"/></svg>';
}

function xIcon() {
  return '<svg viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clip-rule="evenodd"/></svg>';
}

function starIcon(filled) {
  return filled
    ? '<svg viewBox="0 0 20 20" fill="currentColor"><path d="M9.049 2.927c.3-.921 1.603-.921 1.902 0l1.286 3.958a1 1 0 00.95.69h4.162c.969 0 1.371 1.24.588 1.81l-3.368 2.446a1 1 0 00-.363 1.118l1.287 3.957c.3.922-.755 1.688-1.539 1.118l-3.367-2.446a1 1 0 00-1.176 0l-3.367 2.446c-.784.57-1.838-.196-1.539-1.118l1.286-3.957a1 1 0 00-.363-1.118L2.062 9.385c-.783-.57-.38-1.81.588-1.81h4.163a1 1 0 00.95-.69l1.286-3.958z"/></svg>'
    : '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"><path d="M9.049 2.927c.3-.921 1.603-.921 1.902 0l1.286 3.958a1 1 0 00.95.69h4.162c.969 0 1.371 1.24.588 1.81l-3.368 2.446a1 1 0 00-.363 1.118l1.287 3.957c.3.922-.755 1.688-1.539 1.118l-3.367-2.446a1 1 0 00-1.176 0l-3.367 2.446c-.784.57-1.838-.196-1.539-1.118l1.286-3.957a1 1 0 00-.363-1.118L2.062 9.385c-.783-.57-.38-1.81.588-1.81h4.163a1 1 0 00.95-.69l1.286-3.958z"/></svg>';
}

function highlightText(text, q) {
  if (!q) return esc(text);
  const lower = text.toLowerCase();
  const needle = q.toLowerCase();
  let out = '';
  let i = 0;
  while (true) {
    const pos = lower.indexOf(needle, i);
    if (pos === -1) { out += esc(text.slice(i)); break; }
    out += esc(text.slice(i, pos));
    out += '<mark>' + esc(text.slice(pos, pos + needle.length)) + '</mark>';
    i = pos + needle.length;
  }
  return out;
}

function showToast(msg) {
  let t = document.getElementById('dashToast');
  if (!t) {
    t = document.createElement('div');
    t.id = 'dashToast';
    t.className = 'dash-toast';
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(window._toastT);
  window._toastT = setTimeout(() => t.classList.remove('show'), 3200);
}

// ── Navigation ─────────────────────────────────────────────────────────────
function navigateTo(page) {
  if (_currentPage === page && document.getElementById('page-'+page).classList.contains('active')) {
    if (page === 'history') loadHistory();
    return;
  }
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById('page-'+page).classList.add('active');
  document.querySelector('.nav-item[data-page="'+page+'"]').classList.add('active');
  _currentPage = page;
  if (page === 'home') loadHome();
  else if (page === 'history') loadHistory();
  else if (page === 'dictionary') loadDictionary();
  else if (page === 'settings') loadSettings();
  else if (page === 'about') loadAbout();
}

// ── Theme ──────────────────────────────────────────────────────────────────
function resolveTheme(t) {
  return t === 'system'
    ? (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
    : t;
}

let _themePref = 'dark';

function applyTheme(t) {
  _themePref = t;
  t = resolveTheme(t);
  _theme = t;
  document.documentElement.setAttribute('data-theme', t);
  try { window.pywebview.api.set_titlebar_dark(t !== 'light'); } catch (e) {}
  const icon = document.getElementById('themeIcon');
  if (t === 'light') {
    icon.innerHTML = '<path fill-rule="evenodd" d="M10 2a1 1 0 011 1v1a1 1 0 11-2 0V3a1 1 0 011-1zm4 8a4 4 0 11-8 0 4 4 0 018 0zm-.464 4.95l.707.707a1 1 0 001.414-1.414l-.707-.707a1 1 0 00-1.414 1.414zm2.12-10.607a1 1 0 010 1.414l-.706.707a1 1 0 11-1.414-1.414l.707-.707a1 1 0 011.414 0zM17 11a1 1 0 100-2h-1a1 1 0 100 2h1zm-7 4a1 1 0 011 1v1a1 1 0 11-2 0v-1a1 1 0 011-1zM5.05 6.464A1 1 0 106.465 5.05l-.708-.707a1 1 0 00-1.414 1.414l.707.707zm1.414 8.486l-.707.707a1 1 0 01-1.414-1.414l.707-.707a1 1 0 011.414 1.414zM4 11a1 1 0 100-2H3a1 1 0 000 2h1z" clip-rule="evenodd"/>';
  } else {
    icon.innerHTML = '<path d="M17.293 13.293A8 8 0 016.707 2.707a8.001 8.001 0 1010.586 10.586z"/>';
  }
}

function applyThemeFromSelect(val) { applyTheme(val); }

function toggleTheme() {
  const next = _theme === 'dark' ? 'light' : 'dark';
  applyTheme(next);
  const sel = document.querySelector('select[data-key="theme"]');
  if (sel) sel.value = next;
  window.pywebview.api.save_settings({theme: next});
}

// ── Home page ──────────────────────────────────────────────────────────────
async function loadHome() {
  const [stats, history] = await Promise.all([
    window.pywebview.api.get_stats(),
    window.pywebview.api.get_history(10),
  ]);
  document.getElementById('s-tw').textContent  = fmt(stats.today_words);
  document.getElementById('s-ttl').textContent = fmt(stats.total_words);
  document.getElementById('s-tr').textContent  = fmt(stats.today_recordings);
  document.getElementById('s-at').textContent  = fmt(stats.total_recordings);

  const el = document.getElementById('activityList');
  if (!history.length) { el.innerHTML = '<div class="empty">No dictations yet.</div>'; return; }
  _homeTexts = history.map(e => e.text);
  el.innerHTML = history.map((e, i) => `
    <div class="list-item">
      <div class="li-text">${esc(e.text)}</div>
      <div class="li-meta">
        <span>${esc(e.label)}</span>
        <span>·</span><span>${e.words} words</span>
        ${e.source ? '<span class="src-badge">'+esc(e.source)+'</span>' : ''}
      </div>
      <div class="li-acts">
        <button class="ia" title="Copy" onclick="copyText(_homeTexts[${i}])">${copyIcon()}</button>
      </div>
    </div>`).join('');
}

// ── History page ──────────────────────────────────────────────────────────
let _histQuery = '';

async function loadHistory() {
  const el = document.getElementById('historyList');
  el.innerHTML = '<div class="empty">Loading…</div>';
  _histAll = await window.pywebview.api.get_history(500);
  renderHistory(filterEntries(_histAll, _histQuery, _histSourceFilter));
}

function filterEntries(items, q, srcFilter) {
  let out = items;
  if (srcFilter && srcFilter !== 'all') {
    out = out.filter(e => (e.source || '') === srcFilter);
  }
  if (q) {
    const needle = q.toLowerCase();
    out = out.filter(e =>
      e.text.toLowerCase().includes(needle) ||
      (e.source || '').toLowerCase().includes(needle));
  }
  return out;
}

function renderHistItem(e, i) {
  return `
    <div class="list-item${e.pinned ? ' pinned' : ''}" id="hi-${e.index}">
      <input type="checkbox" class="li-check" ${_selectedIdx.has(e.index) ? 'checked' : ''}
        onclick="toggleSelect(${e.index}, this.checked)">
      <div class="li-text">${highlightText(e.text, _histQuery)}</div>
      <div class="li-meta">
        <span>${esc(e.label)}</span>
        <span>·</span><span>${e.words} words</span>
        ${e.source ? '<span class="src-badge">'+esc(e.source)+'</span>' : ''}
        ${e.pinned ? '<span class="pin-badge" title="Pinned">'+starIcon(true)+'</span>' : ''}
      </div>
      <div class="li-acts">
        <button class="ia pin${e.pinned ? ' on' : ''}" title="${e.pinned ? 'Unpin' : 'Pin'}"
          onclick="togglePin(${e.index}, ${e.pinned ? 'false' : 'true'})">${starIcon(e.pinned)}</button>
        <button class="ia" title="Copy" onclick="copyText(_histTexts[${i}])">${copyIcon()}</button>
        <button class="ia del" title="Delete" onclick="deleteHistory(${e.index})">${trashIcon()}</button>
      </div>
    </div>`;
}

function renderHistory(items) {
  const el = document.getElementById('historyList');
  el.classList.toggle('select-mode', _selectMode);
  if (!items.length) {
    _histTexts = [];
    el.innerHTML = (_histQuery || (_histSourceFilter && _histSourceFilter !== 'all'))
      ? '<div class="empty">No dictations match your filters.</div>'
      : '<div class="empty">No dictations yet.</div>';
    updateBulkCount();
    return;
  }
  const pinned = items.filter(e => e.pinned);
  const rest = items.filter(e => !e.pinned);
  const ordered = pinned.concat(rest);
  _histTexts = ordered.map(e => e.text);
  let html = '';
  if (pinned.length) html += '<div class="day-hdr">Pinned</div>';
  let lastDay = null;
  ordered.forEach((e, i) => {
    if (!e.pinned && e.day_label && e.day_label !== lastDay) {
      lastDay = e.day_label;
      html += `<div class="day-hdr">${esc(e.day_label)}</div>`;
    }
    html += renderHistItem(e, i);
  });
  el.innerHTML = html;
  updateBulkCount();
}

let _histSearchT = null;
function onHistSearchInput(q) {
  clearTimeout(_histSearchT);
  _histSearchT = setTimeout(() => {
    _histQuery = q;
    renderHistory(filterEntries(_histAll, _histQuery, _histSourceFilter));
  }, 150);
}

function setSourceFilter(src) {
  _histSourceFilter = src;
  document.querySelectorAll('#sourceChips .chip').forEach(c => {
    c.classList.toggle('active', c.dataset.src === src);
  });
  renderHistory(filterEntries(_histAll, _histQuery, _histSourceFilter));
}

async function togglePin(idx, newVal) {
  const ok = await window.pywebview.api.set_pinned(idx, newVal);
  if (!ok) return;
  const e = _histAll.find(x => x.index === idx);
  if (e) e.pinned = newVal;
  renderHistory(filterEntries(_histAll, _histQuery, _histSourceFilter));
}

async function deleteHistory(idx) {
  await window.pywebview.api.delete_history_entry(idx);
  _histAll = _histAll.filter(e => e.index !== idx);
  _selectedIdx.delete(idx);
  renderHistory(filterEntries(_histAll, _histQuery, _histSourceFilter));
}

async function confirmClear() {
  if (!confirm('Clear all history? This cannot be undone.')) return;
  await window.pywebview.api.clear_history();
  _histAll = [];
  _selectedIdx.clear();
  renderHistory([]);
}

// ── Bulk select ──────────────────────────────────────────────────────────
function toggleSelectMode() {
  _selectMode = !_selectMode;
  document.getElementById('selectModeBtn').textContent = _selectMode ? 'Cancel' : 'Select';
  document.getElementById('bulkBar').style.display = _selectMode ? 'flex' : 'none';
  if (!_selectMode) _selectedIdx.clear();
  renderHistory(filterEntries(_histAll, _histQuery, _histSourceFilter));
}

function toggleSelect(idx, checked) {
  if (checked) _selectedIdx.add(idx); else _selectedIdx.delete(idx);
  updateBulkCount();
}

function updateBulkCount() {
  const el = document.getElementById('bulkCount');
  if (el) el.textContent = _selectedIdx.size + ' selected';
}

function selectAllToggle(checked) {
  const visible = filterEntries(_histAll, _histQuery, _histSourceFilter);
  visible.forEach(e => { if (checked) _selectedIdx.add(e.index); else _selectedIdx.delete(e.index); });
  renderHistory(visible);
}

async function deleteSelected() {
  if (!_selectedIdx.size) return;
  const items = _histAll.filter(e => _selectedIdx.has(e.index));
  const pinnedCount = items.filter(e => e.pinned).length;
  let msg = `Delete ${_selectedIdx.size} selected ${_selectedIdx.size === 1 ? 'entry' : 'entries'}? This cannot be undone.`;
  if (pinnedCount) msg = `${pinnedCount} of the selected entries are pinned. ` + msg;
  if (!confirm(msg)) return;
  const idxList = Array.from(_selectedIdx);
  await window.pywebview.api.delete_history_entries(idxList);
  _selectedIdx.clear();
  await loadHistory();
}

async function exportSelected() {
  if (!_selectedIdx.size) return;
  const idxList = Array.from(_selectedIdx);
  try {
    const res = await window.pywebview.api.export_history_entries(idxList);
    if (res && res.ok) {
      showToast(res.fallback ? 'Exported to ' + res.path : 'Exported to ' + res.path);
    } else if (res && res.cancelled) {
      // user cancelled the save dialog — no toast
    } else {
      showToast('Export failed' + (res && res.error ? ': ' + res.error : ''));
    }
  } catch (e) {
    showToast('Export failed: ' + e);
  }
}

function copyText(text) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.cssText = 'position:fixed;opacity:0;top:0;left:0';
  document.body.appendChild(ta);
  ta.select();
  document.execCommand('copy');
  document.body.removeChild(ta);
}

// ── Dictionary page ────────────────────────────────────────────────────────
async function loadDictionary() {
  const dict = await window.pywebview.api.get_dictionary();
  renderCorrections(dict.corrections);
  renderVocab(dict.vocabulary);
}

function renderCorrections(corr) {
  const el = document.getElementById('corrList');
  const entries = Object.entries(corr || {});
  if (!entries.length) { el.innerHTML = '<div class="empty" style="padding:10px 0;font-size:12px">No corrections yet.</div>'; return; }
  el.innerHTML = entries.map(([from, to]) => `
    <div class="corr-item">
      <span class="corr-from">${esc(from)}</span>
      <span class="corr-arr">→</span>
      <span class="corr-to">${esc(to)}</span>
      <button class="corr-del" title="Remove" onclick="removeCorrection(${JSON.stringify(from)})">${xIcon()}</button>
    </div>`).join('');
}

function renderVocab(vocab) {
  const el = document.getElementById('vocabList');
  if (!(vocab && vocab.length)) { el.innerHTML = '<div class="empty" style="font-size:12px;padding:4px 0">No vocabulary yet.</div>'; return; }
  el.innerHTML = vocab.map(w => `
    <div class="vchip">${esc(w)}
      <button class="vdel" title="Remove" onclick="removeVocab(${JSON.stringify(w)})">${xIcon()}</button>
    </div>`).join('');
}

async function addCorrection() {
  const from = document.getElementById('corrFrom').value.trim();
  const to = document.getElementById('corrTo').value.trim();
  if (!from || !to) return;
  await window.pywebview.api.set_correction(from, to);
  document.getElementById('corrFrom').value = '';
  document.getElementById('corrTo').value = '';
  loadDictionary();
}

async function removeCorrection(from) {
  await window.pywebview.api.remove_correction(from);
  loadDictionary();
}

async function addVocab() {
  const word = document.getElementById('vocabWord').value.trim();
  if (!word) return;
  await window.pywebview.api.add_vocabulary(word);
  document.getElementById('vocabWord').value = '';
  loadDictionary();
}

async function removeVocab(word) {
  await window.pywebview.api.remove_vocabulary(word);
  loadDictionary();
}

// ── Settings page ──────────────────────────────────────────────────────────
async function loadSettings() {
  const [cfg, devices] = await Promise.all([
    window.pywebview.api.get_config(),
    window.pywebview.api.get_audio_devices(),
  ]);

  // Populate mic dropdown
  const micSel = document.getElementById('micSelect');
  micSel.innerHTML = '<option value="">System default</option>';
  devices.forEach(d => {
    const opt = document.createElement('option');
    opt.value = d.index;
    opt.textContent = d.name;
    if (cfg.input_device === d.index) opt.selected = true;
    micSel.appendChild(opt);
  });

  // Text / number inputs
  document.querySelectorAll('[data-key]').forEach(el => {
    const key = el.dataset.key;
    if (key === '_paste_clipboard_only') return; // handled below
    if (el.classList.contains('tog')) return;    // handled below
    const val = cfg[key];
    if (val == null) return;
    if (el.dataset.type === 'array') {
      el.value = Array.isArray(val) ? val.join(', ') : val;
    } else {
      el.value = val;
    }
  });

  // Toggles
  document.querySelectorAll('.tog[data-key]').forEach(tog => {
    const key = tog.dataset.key;
    let val = key === '_paste_clipboard_only' ? (cfg.paste_mode === 'clipboard_only') : cfg[key];
    tog.classList.toggle('on', !!val);
  });

  // Theme select
  const themeSel = document.querySelector('select[data-key="theme"]');
  if (themeSel) themeSel.value = cfg.theme || 'dark';

  // Autostart state lives in Task Scheduler, not config
  try {
    const on = await window.pywebview.api.get_autostart();
    document.getElementById('autostartTog').classList.toggle('on', !!on);
  } catch (e) {}
}

async function reloadSettings() { await loadSettings(); }

// ── About page ─────────────────────────────────────────────────────────────
async function loadAbout() {
  const about = await window.pywebview.api.get_about();
  document.getElementById('aboutVersion').textContent = 'Version ' + about.version;
  const el = document.getElementById('changelogList');
  if (!about.changelog || !about.changelog.length) {
    el.innerHTML = '<div class="empty" style="padding:8px 0">No changelog entries yet.</div>';
    return;
  }
  el.innerHTML = about.changelog.map(c => `
    <div style="margin-bottom:10px">
      <div style="color:var(--txt);font-weight:600">v${esc(c.version)} <span style="color:var(--txt3);font-weight:400">— ${esc(c.date)}</span></div>
      <ul style="margin:4px 0 0 16px;padding:0">
        ${c.notes.map(n => `<li style="margin-bottom:2px">${esc(n)}</li>`).join('')}
      </ul>
    </div>`).join('');
}

function togClick(el) { el.classList.toggle('on'); autoSave(); }

// ── Instant apply — settings persist on change, no Save button ─────────────
let _autoSaveT = null;
function autoSave() {
  clearTimeout(_autoSaveT);
  _autoSaveT = setTimeout(async () => {
    try {
      await window.pywebview.api.save_settings(collectSettings());
      flashSaved();
    } catch (e) { console.error('autosave failed:', e); }
  }, 450);
}

function flashSaved() {
  const ok = document.getElementById('saveOk');
  ok.classList.add('show');
  clearTimeout(window._okT);
  window._okT = setTimeout(() => ok.classList.remove('show'), 1600);
}

async function autostartClick(el) {
  el.classList.toggle('on');
  try {
    await window.pywebview.api.set_autostart(el.classList.contains('on'));
    flashSaved();
  } catch (e) {
    el.classList.toggle('on');  // revert on failure
  }
}

function collectSettings() {
  const data = {};
  document.querySelectorAll('[data-key]').forEach(el => {
    const key = el.dataset.key;
    if (el.classList.contains('tog')) {
      const isOn = el.classList.contains('on');
      if (key === '_paste_clipboard_only') {
        data['paste_mode'] = isOn ? 'clipboard_only' : 'auto';
      } else {
        data[key] = isOn;
      }
      return;
    }
    if (el.tagName === 'SELECT') {
      if (key === 'input_device') {
        data[key] = el.value === '' ? null : parseInt(el.value);
      } else {
        data[key] = el.value;
      }
      return;
    }
    if (el.dataset.type === 'array') {
      data[key] = el.value.split(',').map(s => s.trim()).filter(Boolean);
      return;
    }
    if (el.dataset.type === 'int') { data[key] = parseInt(el.value) || 0; return; }
    if (el.type === 'number') { data[key] = parseFloat(el.value) || 0; return; }
    data[key] = el.value;
  });
  return data;
}


// ── Init ───────────────────────────────────────────────────────────────────
window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
  if (_themePref === 'system') applyTheme('system');
});

window.addEventListener('pywebviewready', async function () {
  try {
    const cfg = await window.pywebview.api.get_config();
    applyTheme(cfg.theme || 'dark');
    const sf = document.getElementById('settingsForm');
    sf.addEventListener('input', autoSave);
    sf.addEventListener('change', autoSave);
    if (_INIT_PAGE === 'home') {
      await loadHome();
    } else {
      navigateTo(_INIT_PAGE);
    }
  } catch (e) {
    console.error('init error:', e);
  }
});
</script>
</body>
</html>"""


# ── Subprocess entrypoint ──────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        import ctypes as _ct
        _ct.windll.user32.SetProcessDpiAwarenessContext(_ct.c_ssize_t(-4))
    except Exception:
        pass
    try:
        hist.purge()
    except Exception:
        pass
    _page = sys.argv[1] if len(sys.argv) > 1 else "home"
    _theme_pref = _read_cfg().get("theme", "dark")
    if _theme_pref == "system":
        try:
            import winreg
            with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as _k:
                _theme = "light" if winreg.QueryValueEx(_k, "AppsUseLightTheme")[0] else "dark"
        except Exception:
            _theme = "dark"
    else:
        _theme = _theme_pref
    # Inject the initial page before pywebviewready fires — avoids the race
    # between window.shown (too early) and the JS api being available.
    _html = _HTML.replace("let _INIT_PAGE = 'home';", f"let _INIT_PAGE = '{_page}';")
    # Same trick for the theme — without it light users get a dark first paint
    # and a dark window background behind every resize.
    _html = _html.replace("let _theme = 'dark';", f"let _theme = '{_theme}';")
    _html = _html.replace('<html lang="en">', f'<html lang="en" data-theme="{_theme}">')
    _geom = _load_window_geom()
    _win_kwargs = dict(
        title="VoiceDictate",
        html=_html,
        js_api=DashboardAPI(),
        min_size=(_MIN_W, _MIN_H),
        background_color="#ececef" if _theme == "light" else "#202020",
    )
    if _geom:
        _win_kwargs.update(x=_geom["x"], y=_geom["y"], width=_geom["w"], height=_geom["h"])
    else:
        _win_kwargs.update(width=980, height=660)
    _w = webview.create_window(**_win_kwargs)
    _w.events.shown += lambda: _apply_titlebar_theme(_theme != "light")
    # Debounced geometry save on move/resize — avoids hammering config.json
    # while the user is mid-drag.
    _geom_timer: "threading.Timer | None" = None
    _geom_timer_lock = threading.Lock()

    def _schedule_geom_save(*_args):
        global _geom_timer
        with _geom_timer_lock:
            if _geom_timer is not None:
                _geom_timer.cancel()
            _geom_timer = threading.Timer(0.6, _save_window_geom, args=(_w,))
            _geom_timer.daemon = True
            _geom_timer.start()

    _w.events.moved += _schedule_geom_save
    _w.events.resized += _schedule_geom_save
    webview.start(debug=False, gui="edgechromium")
