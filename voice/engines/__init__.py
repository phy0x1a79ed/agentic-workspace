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


# Single live engine per kind. Switching engines unloads the previous one so
# that model weights, GPU memory, and sidecar HTTP clients aren't duplicated.
_LOADED: dict[str, dict[str, Any]] = {
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


def load(kind: str, engine_id: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Instantiate `engine_id` of `kind`, unloading any previous instance.

    Returns ``{id, params, instance_id}``. ``instance_id`` is stable across
    re-loads of the same (id, params) pair so the UI can detect no-op reloads.
    """
    if kind not in _REGISTRIES:
        raise ValueError(f"unknown engine kind {kind!r}")
    if engine_id not in _REGISTRIES[kind]:
        raise ValueError(
            f"unknown {kind} engine {engine_id!r}; available: {sorted(_REGISTRIES[kind])}"
        )
    params = params or {}
    cur = _LOADED[kind]
    if cur.get("id") == engine_id and cur.get("params") == params and cur.get("instance") is not None:
        return {"id": cur["id"], "params": cur["params"], "instance_id": cur["instance_id"]}

    schema, factory = _REGISTRIES[kind][engine_id]
    cfg = schema(**params)
    instance = factory(cfg)
    # Best-effort cleanup of the previous instance (engines don't currently
    # define a close() but some sidecar clients hold open HTTP connections).
    prev = cur.get("instance")
    if prev is not None:
        closer = getattr(prev, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                log.exception("engine close() raised; continuing")
    import uuid as _uuid
    cur.clear()
    cur.update({
        "id": engine_id,
        "params": dict(params),
        "instance": instance,
        "instance_id": _uuid.uuid4().hex,
    })
    return {"id": engine_id, "params": dict(params), "instance_id": cur["instance_id"]}


def unload(kind: str) -> bool:
    if kind not in _LOADED:
        raise ValueError(f"unknown engine kind {kind!r}")
    cur = _LOADED[kind]
    inst = cur.get("instance")
    if inst is None:
        return False
    closer = getattr(inst, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:
            log.exception("engine close() raised; continuing")
    cur.clear()
    return True


def current(kind: str) -> dict[str, Any] | None:
    if kind not in _LOADED:
        raise ValueError(f"unknown engine kind {kind!r}")
    cur = _LOADED[kind]
    if not cur.get("instance"):
        return None
    return {"id": cur["id"], "params": cur["params"], "instance_id": cur["instance_id"]}


def loaded() -> dict[str, dict[str, Any] | None]:
    return {kind: current(kind) for kind in _LOADED}


def list_engine_ids() -> dict[str, list[str]]:
    """Lightweight {kind: [engine_id...]} — for membership checks and logs."""
    return {kind: sorted(reg.keys()) for kind, reg in _REGISTRIES.items()}


def list_engines() -> dict[str, dict[str, dict[str, Any]]]:
    """Full {kind: {engine_id: {schema, defaults}}} for UI form rendering.

    `defaults` is a best-effort instantiation of the Pydantic schema with no
    arguments; engines whose schema has required fields yield {} here.
    """
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for kind, reg in _REGISTRIES.items():
        engines: dict[str, dict[str, Any]] = {}
        for eid in sorted(reg.keys()):
            schema, _factory = reg[eid]
            try:
                defaults = schema().model_dump()
            except Exception:
                defaults = {}
            engines[eid] = {
                "schema": schema.model_json_schema(),
                "defaults": defaults,
            }
        out[kind] = engines
    return out


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
