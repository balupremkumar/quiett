"""Tests for lmstudio_boot.py. subprocess.run is monkeypatched; Thread is
replaced with a synchronous stand-in (start() runs target() immediately) so
assertions don't need to race a real daemon thread — same technique used in
test_inject.py for its clipboard-restore thread."""
import sys

sys.modules.pop("lmstudio_boot", None)

import lmstudio_boot


class _SyncThread:
    """Runs target(*args, **kwargs) synchronously on start(), instead of on
    a real thread — deterministic call-order assertions with no join()."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


class TestBoot:
    def test_boots_server_then_loads_model(self, monkeypatch, tmp_path):
        exe = tmp_path / "lms.exe"
        exe.write_text("")
        monkeypatch.setattr(lmstudio_boot, "LMS_EXE", str(exe))
        monkeypatch.setattr(lmstudio_boot.threading, "Thread", _SyncThread)
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)

        monkeypatch.setattr(lmstudio_boot.subprocess, "run", fake_run)
        notifications = []
        monkeypatch.setattr(lmstudio_boot.tray, "notify",
                             lambda title, msg: notifications.append(msg))

        lmstudio_boot.boot("qwen2.5-1.5b-instruct")

        assert calls == [
            [str(exe), "server", "start"],
            [str(exe), "load", "qwen2.5-1.5b-instruct", "-y"],
        ]
        assert notifications[0] == "LM Studio starting…"
        assert notifications[-1] == "LM Studio ready (model loaded)"

    def test_missing_exe_does_not_raise(self, monkeypatch):
        monkeypatch.setattr(lmstudio_boot, "LMS_EXE", "C:\\nowhere\\lms.exe")
        monkeypatch.setattr(lmstudio_boot.threading, "Thread", _SyncThread)
        calls = []
        monkeypatch.setattr(lmstudio_boot.subprocess, "run",
                             lambda args, **kwargs: calls.append(args))
        notifications = []
        monkeypatch.setattr(lmstudio_boot.tray, "notify",
                             lambda title, msg: notifications.append(msg))

        lmstudio_boot.boot("qwen2.5-1.5b-instruct")  # must not raise

        assert calls == []
        assert notifications and "not found" in notifications[-1].lower()

    def test_subprocess_failure_is_logged_not_raised(self, monkeypatch, tmp_path):
        exe = tmp_path / "lms.exe"
        exe.write_text("")
        monkeypatch.setattr(lmstudio_boot, "LMS_EXE", str(exe))
        monkeypatch.setattr(lmstudio_boot.threading, "Thread", _SyncThread)

        def fake_run(args, **kwargs):
            raise OSError("boom")

        monkeypatch.setattr(lmstudio_boot.subprocess, "run", fake_run)
        notifications = []
        monkeypatch.setattr(lmstudio_boot.tray, "notify",
                             lambda title, msg: notifications.append(msg))

        lmstudio_boot.boot("qwen2.5-1.5b-instruct")  # must not raise

        assert "failed" in notifications[-1].lower()


class TestShutdown:
    def test_unloads_model_then_stops_server(self, monkeypatch, tmp_path):
        exe = tmp_path / "lms.exe"
        exe.write_text("")
        monkeypatch.setattr(lmstudio_boot, "LMS_EXE", str(exe))
        monkeypatch.setattr(lmstudio_boot.threading, "Thread", _SyncThread)
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)

        monkeypatch.setattr(lmstudio_boot.subprocess, "run", fake_run)
        notifications = []
        monkeypatch.setattr(lmstudio_boot.tray, "notify",
                             lambda title, msg: notifications.append(msg))

        lmstudio_boot.shutdown("qwen2.5-1.5b-instruct")

        assert calls == [
            [str(exe), "unload", "qwen2.5-1.5b-instruct"],
            [str(exe), "server", "stop"],
        ]
        assert notifications == ["LM Studio stopped"]

    def test_missing_exe_does_not_raise(self, monkeypatch):
        monkeypatch.setattr(lmstudio_boot, "LMS_EXE", "C:\\nowhere\\lms.exe")
        monkeypatch.setattr(lmstudio_boot.threading, "Thread", _SyncThread)
        calls = []
        monkeypatch.setattr(lmstudio_boot.subprocess, "run",
                             lambda args, **kwargs: calls.append(args))

        lmstudio_boot.shutdown("qwen2.5-1.5b-instruct")  # must not raise

        assert calls == []
