"""Tests for the dashboard control channel (single instance + live refresh).

The channel is what stops a second Quiett window ever opening: whoever
holds the port owns the window, everyone else hands their request over. These
tests drive the real socket code with a stub listener standing in for a
running dashboard.
"""
import json
import socket
import threading

import pytest

import dashboard


@pytest.fixture
def stub_dashboard(monkeypatch):
    """A listener on a free port that records what it is asked to do."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    port = srv.getsockname()[1]
    monkeypatch.setattr(dashboard, "_control_port", lambda: port)
    received = []
    stop = threading.Event()

    def _serve():
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                try:
                    received.append(json.loads(conn.recv(4096).decode("utf-8")))
                    conn.sendall(b"ok\n")
                except Exception:
                    pass

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    yield received
    stop.set()
    srv.close()


class TestSendControl:
    def test_delivers_payload_and_reports_success(self, stub_dashboard):
        assert dashboard._send_control({"cmd": "show", "page": "history"}) is True
        assert stub_dashboard == [{"cmd": "show", "page": "history"}]

    def test_no_listener_is_a_clean_false(self, monkeypatch):
        # Port 1 is never a dashboard; the caller must get False, not an
        # exception — this runs on the tray click path.
        monkeypatch.setattr(dashboard, "_control_port", lambda: 1)
        assert dashboard._send_control({"cmd": "ping"}, timeout=0.3) is False


class TestOpenWindow:
    def test_existing_window_is_reused_not_relaunched(self, stub_dashboard, monkeypatch):
        """The bug this fixes: a dashboard left over from a previous main.py
        run isn't in _proc, so it used to get a second window stacked on it."""
        launched = []
        monkeypatch.setattr(dashboard.subprocess, "Popen",
                            lambda *a, **kw: launched.append(a))
        monkeypatch.setattr(dashboard, "_proc", None)
        dashboard.open_window("history")
        assert launched == []
        assert stub_dashboard == [{"cmd": "show", "page": "history"}]

    def test_launches_when_nothing_is_listening(self, monkeypatch):
        launched = []

        class _FakeProc:
            def poll(self):
                return None

        monkeypatch.setattr(dashboard, "_control_port", lambda: 1)
        monkeypatch.setattr(dashboard.subprocess, "Popen",
                            lambda *a, **kw: launched.append(a) or _FakeProc())
        monkeypatch.setattr(dashboard, "_proc", None)
        dashboard.open_window("settings")
        assert len(launched) == 1
        assert "settings" in launched[0][0]

    def test_does_not_stack_a_second_launch_while_one_is_booting(self, monkeypatch):
        """A booting dashboard hasn't bound its port yet — the _proc handle is
        the only thing standing between an impatient double-click and two
        windows."""
        launched = []

        class _FakeProc:
            def poll(self):
                return None  # still running

        monkeypatch.setattr(dashboard, "_control_port", lambda: 1)
        monkeypatch.setattr(dashboard.subprocess, "Popen",
                            lambda *a, **kw: launched.append(a) or _FakeProc())
        monkeypatch.setattr(dashboard, "_proc", _FakeProc())
        dashboard.open_window("home")
        assert launched == []


class TestNotifyChange:
    def test_notify_reaches_the_window(self, stub_dashboard):
        dashboard.notify_change("history")
        deadline = threading.Event()
        deadline.wait(1.5)
        assert stub_dashboard == [{"cmd": "refresh", "what": "history"}]

    def test_notify_never_raises_without_a_dashboard(self, monkeypatch):
        monkeypatch.setattr(dashboard, "_control_port", lambda: 1)
        dashboard.notify_change("history")  # fire-and-forget, must not raise
