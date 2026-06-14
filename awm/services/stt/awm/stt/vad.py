"""Voice-activity detection over raw PCM, using faster-whisper's bundled Silero VAD.

The convo silence logic needs to know "is there voice in this audio, and where
does the last speech region end" **directly from the microphone signal** — not
inferred from whisper's transcribed text, which hallucinates words onto held
trailing silence and so is an unreliable "is the user talking" proxy.
``faster-whisper`` ships the Silero VAD model, so this needs no extra dependency:
we drive ``faster_whisper.vad.get_speech_timestamps`` with options tuned for
sub-second trailing-gap measurement.

PCM input is int16 LE mono at 16 kHz (the mic worklet's format), matching
``stt.py``. ``faster_whisper`` is imported lazily inside the helpers so importing
this module stays cheap and the service still boots without the model present.
"""

from __future__ import annotations

import os
from typing import Optional

import numpy as np

SAMPLE_RATE = 16000

# VAD tuning. The bundled Silero defaults (min_silence_duration_ms=2000,
# speech_pad_ms=400) are WRONG for us: we measure sub-second trailing silence, so
# a 2s silence-merge would hold a speech region "open" long past the actual last
# word, and 400ms of padding would inflate the region end. Override both. The
# knobs sit beside the other PTT_* knobs in registry.py.
_THRESHOLD = float(os.environ.get("PTT_VAD_THRESHOLD", "0.5"))
_MIN_SPEECH_MS = int(os.environ.get("PTT_VAD_MIN_SPEECH_MS", "120"))
_MIN_SILENCE_MS = int(os.environ.get("PTT_VAD_MIN_SILENCE_MS", "100"))


def _vad_options():
    from faster_whisper.vad import VadOptions

    return VadOptions(
        threshold=_THRESHOLD,
        min_speech_duration_ms=_MIN_SPEECH_MS,
        min_silence_duration_ms=_MIN_SILENCE_MS,
        speech_pad_ms=0,
    )


def speech_regions(pcm_bytes: bytes, sample_rate: int = SAMPLE_RATE) -> list[tuple[float, float]]:
    """Return ``[(start_s, end_s), ...]`` speech regions, in order.

    Empty list when the buffer is all non-speech. Drives faster-whisper's bundled
    Silero model (``get_speech_timestamps`` auto-loads + caches it).
    """
    if not pcm_bytes:
        return []
    from faster_whisper.vad import get_speech_timestamps

    audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    ts = get_speech_timestamps(audio, _vad_options(), sampling_rate=sample_rate)
    return [(t["start"] / sample_rate, t["end"] / sample_rate) for t in ts]


def last_speech_end_s(pcm_bytes: bytes, sample_rate: int = SAMPLE_RATE) -> Optional[float]:
    """End time (seconds, relative to the buffer start) of the last detected
    speech region, or ``None`` if the buffer contains no speech."""
    regions = speech_regions(pcm_bytes, sample_rate)
    if not regions:
        return None
    return regions[-1][1]


def has_speech(pcm_bytes: bytes, sample_rate: int = SAMPLE_RATE) -> bool:
    """True iff the PCM contains at least one detected speech region."""
    return bool(speech_regions(pcm_bytes, sample_rate))
