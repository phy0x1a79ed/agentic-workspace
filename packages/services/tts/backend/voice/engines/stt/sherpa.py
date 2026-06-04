"""sherpa-onnx streaming zipformer STT.

Wraps the existing demo.stt_streaming.StreamingRecognizer.
"""

from __future__ import annotations

from pydantic import BaseModel

ENGINE_ID = "sherpa"


class SherpaConfig(BaseModel):
    pass  # all knobs come from env vars in stt_streaming.py


CONFIG_SCHEMA = SherpaConfig


class SherpaEngine:
    """Streaming STT. accept(pcm) returns running partial; finalize() returns final."""

    streaming = True
    push_partials = True

    def __init__(self, cfg: SherpaConfig):
        from stt_streaming import get_streaming_recognizer
        self._recognizer = get_streaming_recognizer()

    def warmup(self) -> None:
        self._recognizer._ensure_loaded()

    def new_session(self, on_partial=None):
        sess = self._recognizer.new_session()
        # sherpa's accept() returns partials inline; the orchestrator polls
        # it and routes to on_partial itself. on_partial here is unused.
        return sess


def make(cfg: SherpaConfig) -> SherpaEngine:
    return SherpaEngine(cfg)
