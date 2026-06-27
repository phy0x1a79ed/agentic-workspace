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
import pytest

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


def test_min_region_ms_drops_short_blips(monkeypatch):
    # The blip floor (used by the silence/submit clock) drops speech regions
    # shorter than min_region_ms, so a momentary noise isn't read as "the last
    # speech." Drive synthetic Silero timestamps so this is model-free.
    sr = vad.SAMPLE_RATE
    fake_ts = [
        {"start": int(0.10 * sr), "end": int(0.50 * sr)},  # 400ms real word
        {"start": int(2.40 * sr), "end": int(2.50 * sr)},  # 100ms blip
    ]
    monkeypatch.setattr(
        "faster_whisper.vad.get_speech_timestamps", lambda *a, **k: fake_ts
    )
    pcm = _silence(3.0)  # non-empty buffer; content ignored (timestamps stubbed)

    # No floor: both regions kept → last end is the blip's (~2.5s).
    assert vad.last_speech_end_s(pcm, min_region_ms=0) == pytest.approx(2.5, abs=0.01)
    assert len(vad.speech_regions(pcm, min_region_ms=0)) == 2

    # Floor 250ms: the 100ms blip is dropped → last *real* end is ~0.5s.
    assert vad.last_speech_end_s(pcm, min_region_ms=250) == pytest.approx(0.5, abs=0.01)
    assert len(vad.speech_regions(pcm, min_region_ms=250)) == 1

    # Floor above every region → nothing survives.
    assert vad.last_speech_end_s(pcm, min_region_ms=600) is None
    assert vad.has_speech(pcm, min_region_ms=600) is False
