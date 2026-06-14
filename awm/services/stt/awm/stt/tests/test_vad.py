"""Smoke tests for the Silero VAD wrapper.

Run via the per-dist runner, or directly:

    PYTHONPATH=awm/services/stt mamba run -n awm \\
        python -m pytest awm/services/stt/awm/stt/tests/test_vad.py -q

These exercise the bundled faster-whisper Silero model (no network). They assert
the *shape* of the contract the silence logic relies on — silence yields no
regions — not exact thresholds (those are tuning, not contract).
"""

from __future__ import annotations

import numpy as np

from awm.stt import vad


def _silence(seconds: float) -> bytes:
    return np.zeros(int(seconds * vad.SAMPLE_RATE), dtype=np.int16).tobytes()


def test_empty_buffer_has_no_speech():
    assert vad.speech_regions(b"") == []
    assert vad.last_speech_end_s(b"") is None
    assert vad.has_speech(b"") is False


def test_pure_silence_has_no_speech():
    pcm = _silence(2.0)
    assert vad.speech_regions(pcm) == []
    assert vad.last_speech_end_s(pcm) is None
    assert vad.has_speech(pcm) is False


def test_noise_is_not_speech():
    # Gaussian noise is not speech: Silero must not flag it (else trailing-silence
    # measurement would treat room noise as the user still talking).
    rng = np.random.default_rng(0)
    pcm = (rng.normal(0, 8000, vad.SAMPLE_RATE)).astype(np.int16).tobytes()
    assert vad.has_speech(pcm) is False
