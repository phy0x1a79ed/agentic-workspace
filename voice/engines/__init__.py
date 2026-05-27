"""Engine plugin registry.

Each engine module exports:
- ENGINE_ID: str
- CONFIG_SCHEMA: type[BaseModel]
- make(cfg: BaseModel) -> Engine

Engines self-register on import. Adding an engine = drop a file in
voice/engines/{stt,tts,llm}/ and add an import line below.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from pydantic import BaseModel

from voice.config import EngineRef

log = logging.getLogger("voice.engines")


_REGISTRIES: dict[str, dict[str, tuple[type[BaseModel], Callable[[Any], Any]]]] = {
    "stt": {},
    "tts": {},
    "llm": {},
}


def _register(kind: str, engine_id: str, schema: type[BaseModel], factory: Callable[[Any], Any]) -> None:
    if engine_id in _REGISTRIES[kind]:
        log.debug("re-registering %s engine %r", kind, engine_id)
    _REGISTRIES[kind][engine_id] = (schema, factory)


def register_stt(module) -> None:
    _register("stt", module.ENGINE_ID, module.CONFIG_SCHEMA, module.make)


def register_tts(module) -> None:
    _register("tts", module.ENGINE_ID, module.CONFIG_SCHEMA, module.make)


def register_llm(module) -> None:
    _register("llm", module.ENGINE_ID, module.CONFIG_SCHEMA, module.make)


def _make(kind: str, ref: EngineRef):
    if ref.engine not in _REGISTRIES[kind]:
        raise ValueError(
            f"unknown {kind} engine {ref.engine!r}; "
            f"available: {sorted(_REGISTRIES[kind])}"
        )
    schema, factory = _REGISTRIES[kind][ref.engine]
    cfg = schema(**ref.params)
    return factory(cfg)


def make_stt(ref: EngineRef):
    return _make("stt", ref)


def make_tts(ref: EngineRef):
    return _make("tts", ref)


def make_llm(ref: EngineRef):
    return _make("llm", ref)


def list_engines() -> dict[str, list[str]]:
    return {kind: sorted(reg.keys()) for kind, reg in _REGISTRIES.items()}


# Import all engine modules so they self-register. New engines: add here.
def _bootstrap() -> None:
    from voice.engines.stt import whisper, sherpa, whisper_stream  # noqa: F401
    from voice.engines.tts import piper, pocket, kokoro_rvc, f5tts, gptsovits, sbv2  # noqa: F401
    from voice.engines.llm import openrouter, claude  # noqa: F401

    register_stt(whisper); register_stt(sherpa); register_stt(whisper_stream)
    register_tts(piper); register_tts(pocket); register_tts(kokoro_rvc)
    register_tts(f5tts); register_tts(gptsovits); register_tts(sbv2)
    register_llm(openrouter); register_llm(claude)


_bootstrap()
