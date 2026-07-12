"""Tests for tts.py — client logic against a fake tts-server. No subprocess,
no GPU, no audio device: the server-spawn path and sounddevice are stubbed."""
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import tts


class _FakeTTSHandler(BaseHTTPRequestHandler):
    registered: list = []
    speech_requests: list = []
    pcm_payload = b"\x00\x01" * 4000  # 8000 bytes of fake s16le

    def log_message(self, *args):  # silence
        pass

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/v1/voices":
            type(self).registered.append(body)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"name":"user","status":"registered"}')
        elif self.path == "/v1/audio/speech":
            type(self).speech_requests.append(body)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(type(self).pcm_payload)
        else:
            self.send_response(404)
            self.end_headers()


class _FakeStream:
    """Stands in for sounddevice.RawOutputStream, recording written bytes."""
    written: list = []

    def __init__(self, **kwargs):
        type(self).written = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def write(self, chunk):
        type(self).written.append(bytes(chunk))


@pytest.fixture
def fake_server(monkeypatch, tmp_path):
    _FakeTTSHandler.registered = []
    _FakeTTSHandler.speech_requests = []
    srv = HTTPServer(("127.0.0.1", 0), _FakeTTSHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    # Fake voice profile
    profile = tmp_path / "voice_profile"
    profile.mkdir()
    (profile / "best.wav").write_bytes(b"RIFFfakewav")
    (profile / "manifest.json").write_text(json.dumps({
        "best": "best.wav",
        "samples": [{"file": "best.wav", "transcript": "hello there"}],
    }))

    monkeypatch.setattr(tts, "_PROFILE_DIR", str(profile))
    monkeypatch.setattr(tts, "_MANIFEST", str(profile / "manifest.json"))
    monkeypatch.setattr(tts, "sd", type("FakeSD", (), {"RawOutputStream": _FakeStream}))
    tts.set_cfg_getter(lambda: {"tts_port": port, "tts_unload_idle_seconds": 300})
    # Server is "already up": ensure_ready must not try to spawn anything.
    monkeypatch.setattr(tts, "check_prerequisites", lambda: [])
    tts._voice_ok = False
    tts._abort.clear()
    tts._speaking.clear()

    yield srv
    srv.shutdown()


class TestVoiceRegistration:
    def test_registers_with_transcript_icl_mode(self, fake_server):
        tts.ensure_ready()
        assert len(_FakeTTSHandler.registered) == 1
        reg = _FakeTTSHandler.registered[0]
        assert reg["name"] == "user"
        assert reg["ref_text"] == "hello there"
        assert base64.b64decode(reg["wav_b64"]) == b"RIFFfakewav"

    def test_registration_cached_across_calls(self, fake_server):
        tts.ensure_ready()
        tts.ensure_ready()
        assert len(_FakeTTSHandler.registered) == 1


class TestSpeak:
    def test_speak_streams_pcm_to_output(self, fake_server):
        tts.speak("read this back")
        assert _FakeTTSHandler.speech_requests[0]["input"] == "read this back"
        assert _FakeTTSHandler.speech_requests[0]["response_format"] == "pcm"
        assert b"".join(_FakeStream.written) == _FakeTTSHandler.pcm_payload
        assert not tts.is_speaking()

    def test_empty_text_is_a_noop(self, fake_server):
        tts.speak("   ")
        assert _FakeTTSHandler.speech_requests == []

    def test_stop_aborts_playback(self, fake_server):
        # Pre-set abort: the stream loop must exit before writing anything.
        tts.ensure_ready()
        tts._abort.set()
        tts._speaking.clear()
        orig_clear = tts._abort.clear
        tts._abort.clear = lambda: None  # keep the abort armed through speak()
        try:
            tts.speak("should be cut off")
        finally:
            tts._abort.clear = orig_clear
            tts._abort.clear()
        assert _FakeStream.written == []
