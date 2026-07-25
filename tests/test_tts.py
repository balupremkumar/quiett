"""Tests for tts.py — client logic against a fake tts-server. No subprocess,
no GPU, no audio device: the server-spawn path and sounddevice are stubbed."""
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
import pytest

import narration
import tts


class _FakeTTSHandler(BaseHTTPRequestHandler):
    registered: list = []
    speech_requests: list = []
    # 4800 samples = a whole number of the silence gate's 20 ms frames, all of
    # them above its floor, so an untrimmed render passes through byte for byte
    pcm_payload = b"\x00\x01" * 4800

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

    def test_pinned_reference_with_sidecar_wins(self, fake_server, monkeypatch, tmp_path):
        profile = tts._PROFILE_DIR
        import pathlib
        pinned = pathlib.Path(profile) / "tts_reference.wav"
        pinned.write_bytes(b"RIFFpinned")
        (pathlib.Path(profile) / "tts_reference.txt").write_text("pinned transcript")
        port = fake_server.server_address[1]
        tts.set_cfg_getter(lambda: {"tts_port": port,
                                    "tts_reference": "tts_reference.wav"})
        tts.ensure_ready()
        reg = _FakeTTSHandler.registered[0]
        assert base64.b64decode(reg["wav_b64"]) == b"RIFFpinned"
        assert reg["ref_text"] == "pinned transcript"


class TestSpeed:
    @pytest.mark.parametrize("raw,expected", [
        (0.8, 0.8), (1.0, 1.0), (0.1, 0.5), (9.0, 2.0),
        ("nonsense", 1.0), (None, 1.0),
    ])
    def test_config_speed_is_clamped(self, raw, expected):
        tts.set_cfg_getter(lambda: {"tts_speed": raw})
        assert tts._speed() == expected

    def test_study_mode_reads_its_own_speed(self):
        cfg = {"tts_speed": 1.2, "study_speed": 0.8, "study_mode": True}
        tts.set_cfg_getter(lambda: cfg)
        assert tts._speed() == 0.8
        cfg["study_mode"] = False
        assert tts._speed() == 1.2

    def test_study_speed_defaults_when_unset(self):
        tts.set_cfg_getter(lambda: {"study_mode": True})
        assert tts._speed() == tts._STUDY_SPEED

    def _sine(self, seconds=1.0, hz=200.0):
        t = np.arange(int(tts._SAMPLE_RATE * seconds)) / tts._SAMPLE_RATE
        return (np.sin(2 * np.pi * hz * t) * 12000).astype(np.int16)

    def _dominant_hz(self, pcm: bytes) -> float:
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        spectrum = np.abs(np.fft.rfft(x * np.hanning(len(x))))
        return float(np.fft.rfftfreq(len(x), 1 / tts._SAMPLE_RATE)[np.argmax(spectrum)])

    @pytest.mark.parametrize("speed", [0.7, 0.8, 1.25])
    def test_stretch_changes_duration_not_pitch(self, speed):
        src = self._sine()
        st = tts._TimeStretcher(speed)
        out = st.feed(src.tobytes()) + st.flush()
        assert abs(len(out) / len(src.tobytes()) - 1 / speed) < 0.01
        # Pitch must survive: a resample would move this by the speed factor.
        assert abs(self._dominant_hz(out) - 200.0) < 5.0

    def test_stretch_is_chunk_size_agnostic(self):
        src = self._sine(seconds=0.5)
        whole = tts._TimeStretcher(0.8)
        chunked = tts._TimeStretcher(0.8)
        a = whole.feed(src.tobytes()) + whole.flush()
        data = src.tobytes()
        b = b"".join(chunked.feed(data[i:i + 3000]) for i in range(0, len(data), 3000))
        b += chunked.flush()
        assert a == b


class TestSegmentedSynthesis:
    """The talker rushes the more text one request carries, so long input is
    split by narration.py into segment-sized requests. Segmenting itself is
    tested in test_narration.py; these cover the tts side of the contract."""

    def test_speak_issues_one_request_per_segment(self, fake_server):
        port = fake_server.server_address[1]
        tts.set_cfg_getter(lambda: {"tts_port": port, "tts_max_chunk_chars": 20})
        tts.speak("Sentence one here. Sentence two here. Sentence three here.")
        assert [r["input"] for r in _FakeTTSHandler.speech_requests] == [
            "Sentence one here.", "Sentence two here.", "Sentence three here."]

    def test_gaps_are_inserted_between_segments(self, fake_server):
        port = fake_server.server_address[1]
        tts.set_cfg_getter(lambda: {"tts_port": port, "tts_max_chunk_chars": 20})
        tts.speak("Sentence one here. Sentence two here. Sentence three here.")
        played = len(b"".join(_FakeStream.written))
        renders = 3 * len(_FakeTTSHandler.pcm_payload)
        # The fake payload has no silence to reclaim, so every gap is added on
        # top. Two gaps, each the pause narration asked for after a sentence.
        pauses = [seg[1] for seg in narration.plan(
            "Sentence one here. Sentence two here. Sentence three here.",
            budget=20, base_speed=1.0)][:-1]
        expected = renders + sum(int(tts._SAMPLE_RATE * p) * 2 for p in pauses)
        assert played == expected

    def test_study_mode_pauses_longer_than_normal(self, fake_server):
        port = fake_server.server_address[1]
        text = "Sentence one here. Sentence two here. Sentence three here."
        tts.set_cfg_getter(lambda: {"tts_port": port, "tts_max_chunk_chars": 20})
        tts.speak(text)
        normal = len(b"".join(_FakeStream.written))
        tts.set_cfg_getter(lambda: {"tts_port": port, "tts_max_chunk_chars": 20,
                                    "study_mode": True, "study_speed": 1.0})
        tts.speak(text)
        assert len(b"".join(_FakeStream.written)) > normal

    def test_producer_failure_reaches_the_caller(self, fake_server):
        port = fake_server.server_address[1]
        tts.set_cfg_getter(lambda: {"tts_port": port, "tts_max_chunk_chars": 20})
        tts.ensure_ready()
        monkey = tts._post_json

        def _boom(path, payload, timeout=60.0):
            if path == "/v1/audio/speech":
                raise RuntimeError("server exploded")
            return monkey(path, payload, timeout)

        tts._post_json = _boom
        try:
            with pytest.raises(RuntimeError, match="server exploded"):
                tts.speak("Sentence one here. Sentence two here.")
        finally:
            tts._post_json = monkey
        assert not tts.is_speaking()


class TestSilenceGate:
    def _frames(self, *levels):
        out = []
        for lvl in levels:
            out.append((np.full(tts._SilenceGate._FRAME, lvl, dtype=np.int16)))
        return np.concatenate(out).tobytes()

    def test_trims_lead_in_and_tail(self):
        gate = tts._SilenceGate()
        pcm = self._frames(0, 0, 5000, 5000, 0, 0)
        out = gate.feed(pcm)
        gate.end()
        assert out == self._frames(5000, 5000)

    def test_keeps_silence_inside_the_chunk(self):
        gate = tts._SilenceGate()
        out = gate.feed(self._frames(0, 5000, 0, 5000, 0))
        gate.end()
        assert out == self._frames(5000, 0, 5000)


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

    def test_speed_stretches_playback(self, fake_server):
        port = fake_server.server_address[1]
        tts.set_cfg_getter(lambda: {"tts_port": port, "tts_speed": 0.8})
        tts.speak("read this back slowly")
        played = len(b"".join(_FakeStream.written))
        assert played == len(_FakeTTSHandler.pcm_payload) / 0.8

    def test_speed_of_one_is_a_straight_passthrough(self, fake_server):
        port = fake_server.server_address[1]
        tts.set_cfg_getter(lambda: {"tts_port": port, "tts_speed": 1.0})
        tts.speak("normal pace")
        assert b"".join(_FakeStream.written) == _FakeTTSHandler.pcm_payload

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
