"""Tests for the two QUIETT_UI_PLAN P7 endpoints on api_server.py:
GET /diag/sockets and POST /speak. Uses Flask's test client; the model-server
Popen handles and netstat are mocked so these run without any real subprocess.
"""
import json
import os

import pytest

import api_server
import dashboard
import transcribe
import tts


class _FakeProc:
    def __init__(self, pid: int, alive: bool = True):
        self.pid = pid
        self._alive = alive

    def poll(self):
        return None if self._alive else 0


@pytest.fixture
def client():
    api_server.configure(
        get_config=lambda: {"tts_enabled": True, "api_server_port": 8090},
        get_history=lambda: [],
        trigger_dictate=lambda: None,
        patch_config=lambda d: None,
    )
    return api_server._app.test_client()


@pytest.fixture(autouse=True)
def _clear_procs(monkeypatch):
    """Every test starts with no model-server children — each test opts in."""
    monkeypatch.setattr(transcribe, "_proc", None)
    monkeypatch.setattr(tts, "_proc", None)
    monkeypatch.setattr(dashboard, "_proc", None)


NETSTAT_SAMPLE = """
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  TCP    0.0.0.0:8090           0.0.0.0:0              LISTENING       {me}
  TCP    127.0.0.1:8089         0.0.0.0:0              LISTENING       {whisper}
  TCP    127.0.0.1:8089         127.0.0.1:55231        ESTABLISHED     {whisper}
  TCP    127.0.0.1:8093         0.0.0.0:0              LISTENING       {dash}
  TCP    0.0.0.0:445            0.0.0.0:0              LISTENING       999999
  TCP    10.0.0.5:51000         203.0.113.9:443        ESTABLISHED     {me}
"""


class _FakeCompleted:
    def __init__(self, stdout: str):
        self.stdout = stdout


class TestDiagSockets:
    def test_only_owned_pids_reported(self, client, monkeypatch):
        me = os.getpid()
        monkeypatch.setattr(transcribe, "_proc", _FakeProc(1111))
        monkeypatch.setattr(tts, "_proc", _FakeProc(2222))
        monkeypatch.setattr(dashboard, "_proc", _FakeProc(3333))
        text = NETSTAT_SAMPLE.format(me=me, whisper=1111, dash=3333)
        monkeypatch.setattr(api_server.subprocess, "run",
                             lambda *a, **kw: _FakeCompleted(text))

        resp = client.get("/diag/sockets")
        assert resp.status_code == 200
        body = resp.get_json()
        pids_seen = {int(row["laddr"].split(":")[-1]) for row in body["sockets"]}
        # The foreign 445 row (unowned PID) must never appear.
        assert 445 not in pids_seen
        laddrs = {row["laddr"] for row in body["sockets"]}
        assert "0.0.0.0:8090" in laddrs
        assert "127.0.0.1:8089" in laddrs
        assert "127.0.0.1:8093" in laddrs

    def test_labels_match_the_owning_process(self, client, monkeypatch):
        me = os.getpid()
        monkeypatch.setattr(transcribe, "_proc", _FakeProc(1111))
        monkeypatch.setattr(dashboard, "_proc", _FakeProc(3333))
        text = NETSTAT_SAMPLE.format(me=me, whisper=1111, dash=3333)
        monkeypatch.setattr(api_server.subprocess, "run",
                             lambda *a, **kw: _FakeCompleted(text))

        body = client.get("/diag/sockets").get_json()
        by_laddr = {row["laddr"]: row["proc"] for row in body["sockets"]}
        assert by_laddr["127.0.0.1:8089"] == "whisper-server"
        assert by_laddr["127.0.0.1:8093"] == "dashboard"
        assert by_laddr["0.0.0.0:8090"] == "quiett"

    def test_external_counts_only_non_loopback_established(self, client, monkeypatch):
        me = os.getpid()
        text = NETSTAT_SAMPLE.format(me=me, whisper=1111, dash=3333)
        monkeypatch.setattr(api_server.subprocess, "run",
                             lambda *a, **kw: _FakeCompleted(text))
        body = client.get("/diag/sockets").get_json()
        # Only the loopback-owned rows are ever reported since whisper/dash
        # PIDs aren't "owned" in this test (no _proc set) — but the outbound
        # row IS under our own PID, and its remote isn't loopback.
        assert body["external"] == 1

    def test_dead_child_pid_excluded(self, client, monkeypatch):
        me = os.getpid()
        monkeypatch.setattr(transcribe, "_proc", _FakeProc(1111, alive=False))
        text = NETSTAT_SAMPLE.format(me=me, whisper=1111, dash=3333)
        monkeypatch.setattr(api_server.subprocess, "run",
                             lambda *a, **kw: _FakeCompleted(text))
        body = client.get("/diag/sockets").get_json()
        assert "127.0.0.1:8089" not in {row["laddr"] for row in body["sockets"]}

    def test_netstat_failure_is_a_clean_empty_response(self, client, monkeypatch):
        def _boom(*a, **kw):
            raise OSError("netstat missing")
        monkeypatch.setattr(api_server.subprocess, "run", _boom)
        resp = client.get("/diag/sockets")
        assert resp.status_code == 200
        assert resp.get_json() == {"sockets": [], "external": 0}

    def test_response_shape_exact_keys(self, client, monkeypatch):
        me = os.getpid()
        text = NETSTAT_SAMPLE.format(me=me, whisper=1111, dash=3333)
        monkeypatch.setattr(api_server.subprocess, "run",
                             lambda *a, **kw: _FakeCompleted(text))
        body = client.get("/diag/sockets").get_json()
        assert set(body.keys()) == {"sockets", "external"}
        assert isinstance(body["external"], int)
        for row in body["sockets"]:
            assert set(row.keys()) == {"laddr", "proc", "state"}


class TestSpeak:
    def test_disabled_returns_403(self, monkeypatch):
        api_server.configure(
            get_config=lambda: {"tts_enabled": False},
            get_history=lambda: [], trigger_dictate=lambda: None,
            patch_config=lambda d: None,
        )
        client = api_server._app.test_client()
        resp = client.post("/speak", json={"text": "hello"})
        assert resp.status_code == 403
        assert resp.get_json() == {"error": "tts_disabled"}

    def test_empty_text_is_400(self, client):
        resp = client.post("/speak", json={"text": "   "})
        assert resp.status_code == 400

    def test_missing_text_is_400(self, client):
        resp = client.post("/speak", json={})
        assert resp.status_code == 400

    def test_oversize_text_is_400(self, client):
        resp = client.post("/speak", json={"text": "x" * 2001})
        assert resp.status_code == 400

    def test_at_cap_is_accepted(self, client, monkeypatch):
        called = []
        monkeypatch.setattr(tts, "speak", lambda t: called.append(t))
        resp = client.post("/speak", json={"text": "x" * 2000})
        assert resp.status_code == 200

    def test_valid_text_speaks_in_background(self, client, monkeypatch):
        called = []
        done = __import__("threading").Event()

        def _fake_speak(t):
            called.append(t)
            done.set()

        monkeypatch.setattr(tts, "speak", _fake_speak)
        resp = client.post("/speak", json={"text": "hello world"})
        assert resp.status_code == 200
        assert resp.get_json() == {"status": "speaking"}
        assert done.wait(2.0)
        assert called == ["hello world"]

    def test_speak_failure_does_not_500_the_request(self, client, monkeypatch):
        monkeypatch.setattr(tts, "speak", lambda t: (_ for _ in ()).throw(RuntimeError("boom")))
        resp = client.post("/speak", json={"text": "hello"})
        # The response already returned before the worker thread ran; a
        # background failure must not surface as an HTTP error.
        assert resp.status_code == 200
