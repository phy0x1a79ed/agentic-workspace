"""Engine plugin registry.

Each engine module exports:
- ENGINE_ID: str
- CONFIG_SCHEMA: type[BaseModel]
- make(cfg: BaseModel) -> Engine

Engines self-register on import. Adding an engine = drop a file in
voice/engines/{stt,tts,llm}/ and add an import line below.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel

from voice.config import EngineRef

log = logging.getLogger("voice.engines")


_REGISTRIES: dict[str, dict[str, tuple[type[BaseModel], Callable[[Any], Any]]]] = {
    "stt": {},
    "tts": {},
}


# Single live engine per kind. Switching engines unloads the previous one so
# that model weights, GPU memory, and sidecar HTTP clients aren't duplicated.
_LOADED: dict[str, dict[str, Any]] = {
    "stt": {},
    "tts": {},
}


def _register(kind: str, engine_id: str, schema: type[BaseModel], factory: Callable[[Any], Any]) -> None:
    if engine_id in _REGISTRIES[kind]:
        log.debug("re-registering %s engine %r", kind, engine_id)
    _REGISTRIES[kind][engine_id] = (schema, factory)


def register_stt(module) -> None:
    _register("stt", module.ENGINE_ID, module.CONFIG_SCHEMA, module.make)


def register_tts(module) -> None:
    _register("tts", module.ENGINE_ID, module.CONFIG_SCHEMA, module.make)


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


def current_instance(kind: str) -> Any | None:
    """Live engine object for `kind`, or None if nothing is loaded.

    Separate from current() so the public describe-state API stays
    serializable; backends that actually need to call synth/transcribe
    reach here.
    """
    if kind not in _LOADED:
        raise ValueError(f"unknown engine kind {kind!r}")
    return _LOADED[kind].get("instance")


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
    from voice.engines.tts import piper, kokoro_rvc, f5tts, gptsovits, sbv2  # noqa: F401

    register_stt(whisper); register_stt(sherpa); register_stt(whisper_stream)
    register_tts(piper); register_tts(kokoro_rvc)
    register_tts(f5tts); register_tts(gptsovits); register_tts(sbv2)


_bootstrap()


# ── hot-load from data dir ─────────────────────────────────────────────
# Engines dropped into ``$AWM_DATA_DIR/voice/engines/{stt,tts,llm}/*.py``
# are picked up without restarting the service. A background asyncio loop
# scans every 2s and (re)registers modules whose mtime changed; files that
# disappear unregister their engine id.

_DYNAMIC_REGISTERS: dict[str, Callable[[Any], None]] = {
    "stt": register_stt,
    "tts": register_tts,
}
# Track filename → (kind, engine_id, mtime_ns). Used to detect change /
# removal and to map files back to the engine they registered.
_DYNAMIC: dict[Path, tuple[str, str, int]] = {}


def _data_engines_root() -> Path:
    base = os.environ.get("AWM_DATA_DIR") or "data/awm"
    return Path(base) / "voice" / "engines"


def _unregister(kind: str, engine_id: str) -> None:
    _REGISTRIES.get(kind, {}).pop(engine_id, None)


def _load_module(path: Path) -> Any:
    # Use a unique module name per file so importlib.reload-style replacement
    # works without touching the regular `voice.engines.*` namespace.
    mod_name = f"voice_engines_dynamic.{path.parent.name}.{path.stem}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot spec {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _scan_dynamic_once() -> None:
    root = _data_engines_root()
    if not root.is_dir():
        return
    seen: set[Path] = set()
    for kind, register in _DYNAMIC_REGISTERS.items():
        kdir = root / kind
        if not kdir.is_dir():
            continue
        for path in kdir.glob("*.py"):
            if path.name.startswith("_"):
                continue
            seen.add(path)
            try:
                mtime = path.stat().st_mtime_ns
            except OSError:
                continue
            prev = _DYNAMIC.get(path)
            if prev is not None and prev[2] == mtime:
                continue
            try:
                mod = _load_module(path)
                register(mod)
                engine_id = getattr(mod, "ENGINE_ID", path.stem)
                # If reloading and the engine id changed, clear the stale one.
                if prev is not None and prev[1] != engine_id:
                    _unregister(prev[0], prev[1])
                _DYNAMIC[path] = (kind, engine_id, mtime)
                log.info("hot-loaded %s engine %r from %s", kind, engine_id, path)
            except Exception:
                # Stamp the failure with this mtime so the watcher doesn't
                # retry every tick — user re-saves the file to retry.
                _DYNAMIC[path] = (kind, prev[1] if prev else f"<broken:{path.stem}>", mtime)
                log.exception("failed to hot-load %s", path)
    # Drop entries whose files have disappeared.
    for stale in [p for p in _DYNAMIC if p not in seen]:
        kind, engine_id, _ = _DYNAMIC.pop(stale)
        _unregister(kind, engine_id)
        log.info("hot-unloaded %s engine %r (file gone: %s)", kind, engine_id, stale)


async def _watch_dynamic(interval: float = 2.0) -> None:
    """Background poller — call from the FastAPI lifespan."""
    while True:
        try:
            await asyncio.sleep(interval)
            _scan_dynamic_once()
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("dynamic engine watcher iteration failed")


# Initial sweep so engines are visible on the very first /engines call without
# having to wait for the watcher to tick.
try:
    _scan_dynamic_once()
except Exception:
    log.exception("initial dynamic engine scan failed")
