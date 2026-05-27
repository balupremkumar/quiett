"""Tests for silence detection logic in audio.py."""
import sys
from unittest.mock import MagicMock

# Stub sounddevice and winsound; force-reload audio to pick up the stubs
sys.modules["sounddevice"] = MagicMock()
sys.modules["winsound"]    = MagicMock()
sys.modules.pop("audio", None)

import numpy as np
import audio


class TestSilenceDetection:
    """Test the silence auto-stop behaviour via the callback directly."""

    def _make_chunk(self, rms: float) -> np.ndarray:
        """Return a 1-channel float32 chunk with approximately the given RMS."""
        n = 512
        if rms == 0:
            return np.zeros((n, 1), dtype="float32")
        return np.full((n, 1), rms, dtype="float32")

    def setup_method(self):
        """Reset audio module state before each test."""
        audio._recording_event.clear()
        audio._session_chunks    = []
        audio._had_voice         = False
        audio._last_voice_t      = 0.0
        audio._silence_triggered = False
        audio._on_stop           = None
        audio._silence_timeout   = 3.0
        audio._silence_threshold = 0.01

    def test_voice_chunk_sets_had_voice(self):
        audio._recording_event.set()
        audio._session_chunks = []
        chunk = self._make_chunk(0.05)
        audio._callback(chunk, 512, None, None)
        assert audio._had_voice is True

    def test_silent_chunk_before_voice_does_not_stop(self):
        """Silence before any voice should never trigger stop."""
        stop_calls = []
        audio._on_stop = lambda c: stop_calls.append(c)
        audio._recording_event.set()
        audio._session_chunks = []
        audio._had_voice    = False
        audio._last_voice_t = 0.0
        # Simulate many silent frames
        for _ in range(100):
            audio._callback(self._make_chunk(0.0), 512, None, None)
        import time; time.sleep(0.05)  # let any spawned thread fire
        assert audio._had_voice is False

    def test_silence_after_voice_eventually_stops(self):
        """After voice is detected, sustained silence should trigger stop."""
        import time
        stopped = []

        def mock_stop():
            audio._recording_event.clear()
            stopped.append(True)

        audio.stop = mock_stop
        audio._recording_event.set()
        audio._session_chunks = []
        audio._had_voice    = True
        audio._last_voice_t = time.time() - 5.0  # 5 seconds ago — past timeout
        audio._silence_timeout = 3.0

        audio._callback(self._make_chunk(0.0), 512, None, None)
        time.sleep(0.1)
        assert len(stopped) > 0

    def test_silence_disabled_when_timeout_zero(self):
        """silence_auto_stop_seconds=0 must never trigger stop mid-silence."""
        audio._silence_timeout = 0.0
        audio._recording_event.set()
        audio._session_chunks = []
        audio._had_voice    = True
        audio._last_voice_t = 0.0  # very old
        # With timeout=0 the condition is skipped
        import time
        for _ in range(50):
            audio._callback(self._make_chunk(0.0), 512, None, None)
        time.sleep(0.05)
        assert audio._recording_event.is_set()

    def test_configure_updates_silence_params(self):
        audio.configure(on_stop=None, silence_timeout_seconds=5.0,
                        silence_threshold=0.02)
        assert audio._silence_timeout   == 5.0
        assert audio._silence_threshold == 0.02
