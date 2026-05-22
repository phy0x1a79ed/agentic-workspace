"""Text-to-speech wrapper around kyutai pocket-tts.

API in 2.x:
  TTSModel.load_model()                          → model
  model.get_state_for_audio_prompt("jean")       → voice state
  model.generate_audio_stream(state, text)       → yields 1D float tensors
  model.sample_rate                              → int (typically 24000)

pocket-tts has no native speed parameter. Playback-speed adjustment
happens in the browser (AudioBufferSourceNode.playbackRate).

Voice selection: $VOICE_NAME (default "jean"). Pre-made names from the
catalog work; an absolute path or `hf://` URI also work.
"""

from __future__ import annotations

import os
from typing import Optional

import numpy as np


_VOICE_NAME = os.environ.get("VOICE_NAME", "jean")


class Synthesizer:
    def __init__(self, voice: Optional[str] = None):
        self.voice = voice or _VOICE_NAME
        self._model = None
        self._state = None
        self._sample_rate: Optional[int] = None

    def _ensure_loaded(self):
        if self._model is None:
            from pocket_tts import TTSModel

            self._model = TTSModel.load_model()
            self._state = self._model.get_state_for_audio_prompt(self.voice)
            self._sample_rate = int(self._model.sample_rate)
        return self._model

    @property
    def sample_rate(self) -> int:
        if self._sample_rate is None:
            self._ensure_loaded()
        assert self._sample_rate is not None
        return self._sample_rate

    def synth(self, text: str) -> bytes:
        """Synth text → raw int16 mono PCM at ``self.sample_rate``.

        Synchronous; callers should run in a thread executor.
        """
        if not text.strip():
            return b""
        model = self._ensure_loaded()
        # Use the streaming API and concatenate — generate_audio() runs the
        # same loop under the hood but does it eagerly.
        chunks: list[np.ndarray] = []
        for chunk in model.generate_audio_stream(
            model_state=self._state,
            text_to_generate=text,
            copy_state=True,
        ):
            arr = chunk.detach().cpu().numpy()
            if arr.ndim > 1:
                arr = arr.squeeze()
            chunks.append(arr.astype(np.float32))
        if not chunks:
            return b""
        audio = np.concatenate(chunks)
        # float32 in [-1, 1] → int16 LE.
        audio = np.clip(audio, -1.0, 1.0)
        pcm = (audio * 32767.0).astype(np.int16)
        return pcm.tobytes()


_singleton: Optional[Synthesizer] = None


def get_synthesizer() -> Synthesizer:
    global _singleton
    if _singleton is None:
        _singleton = Synthesizer()
    return _singleton
