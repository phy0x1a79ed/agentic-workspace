"""Voice service package.

A per-call configurable voice-conversation service. Wraps the existing
sidecars (STT / TTS / RVC / LLM) behind a uniform plugin contract and
exposes a single FastAPI app.

See `.awm/context.md` for the architecture brief.
"""
