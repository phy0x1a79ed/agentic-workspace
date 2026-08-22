"""TTS feature service — control-WS adapter registered at ``/svc/tts/``.

TTS-only: the engine registry holds the production TTS engines
(``kokoro_rvc`` / ``piper`` / ``sbv2`` plus loadable ``f5tts`` / ``gptsovits``),
named-preset + keyed-state storage, and a single direct ``call`` playback
session that streams synthesized PCM back to the browser. The duplicate STT /
orchestrator conversation loop was pruned in the modular migration.

Frontend lives in ``awm/pages/tts/``.
"""
