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


class TestPrewarm:
    def test_boots_hidden_when_nothing_is_listening(self, monkeypatch):
        launched = []

        class _FakeProc:
            def poll(self):
                return None

        monkeypatch.setattr(dashboard, "_control_port", lambda: 1)
        monkeypatch.setattr(dashboard.subprocess, "Popen",
                            lambda *a, **kw: launched.append(a) or _FakeProc())
        monkeypatch.setattr(dashboard, "_proc", None)
        dashboard.prewarm()
        assert len(launched) == 1
        assert "--hidden" in launched[0][0]

    def test_no_op_when_a_dashboard_already_holds_the_port(self, stub_dashboard, monkeypatch):
        """Prewarm must never disturb a window the user already has open — it
        pings, sees an owner, and stops."""
        launched = []
        monkeypatch.setattr(dashboard.subprocess, "Popen",
                            lambda *a, **kw: launched.append(a))
        monkeypatch.setattr(dashboard, "_proc", None)
        dashboard.prewarm()
        assert launched == []
        assert stub_dashboard == [{"cmd": "ping"}]


class TestShutdown:
    def test_sends_quit_and_clears_the_handle(self, stub_dashboard, monkeypatch):
        class _FakeProc:
            def __init__(self):
                self.waited = False

            def poll(self):
                return None

            def wait(self, timeout=None):
                self.waited = True
                return 0

        proc = _FakeProc()
        monkeypatch.setattr(dashboard, "_proc", proc)
        dashboard.shutdown()
        assert stub_dashboard == [{"cmd": "quit"}]
        assert proc.waited is True
        assert dashboard._proc is None

    def test_kills_a_dashboard_that_ignores_quit(self, stub_dashboard, monkeypatch):
        class _StuckProc:
            def __init__(self):
                self.killed = False

            def poll(self):
                return None

            def wait(self, timeout=None):
                raise dashboard.subprocess.TimeoutExpired("dashboard", timeout)

            def kill(self):
                self.killed = True

        proc = _StuckProc()
        monkeypatch.setattr(dashboard, "_proc", proc)
        dashboard.shutdown()
        assert proc.killed is True


class TestServeControlQuit:
    def test_quit_flags_the_close_as_real_and_destroys(self, monkeypatch):
        """Closing the window normally only hides it, so the quit command has
        to arm the _quitting flag before the destroy or the close is cancelled."""
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        port = srv.getsockname()[1]
        quitting = threading.Event()
        ready = threading.Event()
        ready.set()
        destroyed = threading.Event()
        monkeypatch.setattr(dashboard, "_destroy_window",
                            lambda window: destroyed.set())

        t = threading.Thread(target=dashboard._serve_control,
                             args=(srv, object(), ready, quitting), daemon=True)
        t.start()
        with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
            sock.settimeout(2)
            sock.sendall(b'{"cmd": "quit"}\n')
            assert sock.recv(32).startswith(b"ok")
        t.join(timeout=2)
        assert quitting.is_set()
        assert destroyed.wait(2)
        assert not t.is_alive()   # the accept loop is done; the port goes with us
        srv.close()

    def test_show_unhides_the_window_before_raising_it(self, monkeypatch):
        calls = []

        class _FakeWindow:
            def show(self):
                calls.append("show")

            def evaluate_js(self, script):
                calls.append(script)

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        port = srv.getsockname()[1]
        ready = threading.Event()
        ready.set()
        monkeypatch.setattr(dashboard, "_raise_self", lambda: calls.append("raise"))

        t = threading.Thread(target=dashboard._serve_control,
                             args=(srv, _FakeWindow(), ready), daemon=True)
        t.start()
        with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
            sock.settimeout(2)
            sock.sendall(b'{"cmd": "show", "page": "history"}\n')
            assert sock.recv(32).startswith(b"ok")
        srv.close()
        assert calls == ["show", "navigateTo('history')", "raise"]


class TestNotifyChange:
    def test_notify_reaches_the_window(self, stub_dashboard):
        dashboard.notify_change("history")
        deadline = threading.Event()
        deadline.wait(1.5)
        assert stub_dashboard == [{"cmd": "refresh", "what": "history"}]

    def test_notify_never_raises_without_a_dashboard(self, monkeypatch):
        monkeypatch.setattr(dashboard, "_control_port", lambda: 1)
        dashboard.notify_change("history")  # fire-and-forget, must not raise
