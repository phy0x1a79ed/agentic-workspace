"""TTS engine package.

Wraps the production TTS sidecars (kokoro_rvc / piper / sbv2, plus loadable
f5tts / gptsovits) behind a uniform plugin contract — see
``voice.engines`` for the registry. The STT / RVC-conversation half of the old
``voice`` package was pruned in the modular migration; ``stt`` owns STT now.
"""
