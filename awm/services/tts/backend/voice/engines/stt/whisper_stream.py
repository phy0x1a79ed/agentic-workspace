"""Rolling-window faster-whisper streaming STT.

Wraps demo.stt_streaming.WhisperStreamingRecognizer. Note: slow on CPU
(30 s mel padding per partial). Use sherpa for true streaming.
"""

from __future__ import annotations

from pydantic import BaseModel

ENGINE_ID = "whisper-stream"


class WhisperStreamConfig(BaseModel):
    pass


CONFIG_SCHEMA = WhisperStreamConfig


class WhisperStreamEngine:
    streaming = True
    push_partials = True

    def __init__(self, cfg: WhisperStreamConfig):
        from stt_streaming import get_whisper_streaming_recognizer
        self._recognizer = get_whisper_streaming_recognizer()

    def warmup(self) -> None:
        self._recognizer._ensure_loaded()

    def new_session(self, on_partial=None):
        sess = self._recognizer.new_session(on_partial=on_partial)
        sess.start()
        return sess


def make(cfg: WhisperStreamConfig) -> WhisperStreamEngine:
    return WhisperStreamEngine(cfg)
