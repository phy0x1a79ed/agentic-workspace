"""Minimal Piper smoke test: synth one sentence, write a WAV, play it."""

import os
import wave
from piper import PiperVoice

VOICE = os.environ["PIPER_VOICE"]
TEXT = "Hello. This is a simple piper synthesis test. One, two, three."
OUT = "smoke.wav"

voice = PiperVoice.load(VOICE)
sr = int(voice.config.sample_rate)

pcm = b"".join(chunk.audio_int16_bytes for chunk in voice.synthesize(TEXT))

with wave.open(OUT, "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(sr)
    w.writeframes(pcm)

print(f"wrote {OUT}: {len(pcm)} bytes at {sr} Hz")
