"""The models each cluster can serve, and where that list comes from.

The built-in :data:`REGISTRY` below is a floor, not the whole list. A sidecar
``models.json`` under the service's own data directory adds entries and extends
the per-cluster lists, and it is re-read whenever it changes. That is what makes
trying another model a staged GGUF plus a JSON object, rather than an edit to
this file followed by a merge to release and a service restart.

**CAUTION** the sidecar lives outside the git tree on purpose. The workspace
checkout is a deploy target that is reset onto upstream ``release``, so a model
added here by editing source would survive only until the next deploy.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace

from awm.persistence.databases import service_db_path

log = logging.getLogger("awm.hpcllm.models")


@dataclass(frozen=True)
class ModelSpec:
    name: str
    gguf_filename: str
    api_name: str
    min_gpus: int = 1
    min_mem_gb: int = 16
    ctx_size: int = 32768
    # llama.cpp divides --ctx-size across --parallel slots, so the two are chosen
    # together or not at all: 8 slots out of 65536 leaves 8192 apiece. Default 1
    # keeps the single-request behaviour every existing entry was measured under.
    parallel: int = 1


@dataclass(frozen=True)
class ClusterModel:
    model: ModelSpec
    cluster: str


REGISTRY: dict[str, ModelSpec] = {
    "Qwen/Qwen3-8B": ModelSpec(
        name="Qwen/Qwen3-8B",
        gguf_filename="Qwen_Qwen3-8B-q4_k_m.gguf",
        api_name="claude-3-5-sonnet-20241022",
        min_gpus=1,
        min_mem_gb=16,
        ctx_size=32768,
    ),
    "Qwen/Qwen2.5-14B-Instruct": ModelSpec(
        name="Qwen/Qwen2.5-14B-Instruct",
        gguf_filename="Qwen2.5-14B-Instruct-Q5_K_M.gguf",
        api_name="qwen2.5-14b-instruct",
        min_gpus=1,
        min_mem_gb=24,
        ctx_size=32768,
    ),
    # fir schedules a whole 80 GB H100 per GPU, and the 14B above uses 10.5 GB of
    # it -- about an eighth of the card. This is the same card actually filled:
    # 23.2 GB of weights, a 64k context, and eight concurrent slots, which is
    # what makes a panel sweep cost minutes instead of an hour.
    "Qwen/Qwen3-32B": ModelSpec(
        name="Qwen/Qwen3-32B",
        gguf_filename="Qwen3-32B-Q5_K_M.gguf",
        api_name="qwen3-32b",
        min_gpus=1,
        min_mem_gb=64,
        ctx_size=65536,
        parallel=8,
    ),
}

CLUSTER_MODELS: dict[str, list[str]] = {
    "sockeye": ["Qwen/Qwen3-8B"],
    "fir": ["Qwen/Qwen2.5-14B-Instruct", "Qwen/Qwen3-32B"],
}

#: Sidecar overlay: ``{"models": {name: {...}}, "clusters": {cluster: [name]}}``.
#: A model key repeated from :data:`REGISTRY` overrides only the fields it names.
SIDECAR = service_db_path("hpcllm").parent / "models.json"

_overlay: tuple[dict[str, ModelSpec], dict[str, list[str]]] = ({}, {})
#: Staleness key. **CAUTION** ``st_mtime`` is a float, and near a 2026 epoch it
#: resolves to about a quarter of a microsecond, so two edits in quick
#: succession can share one value and the second is then never read. Nanoseconds
#: plus size is what makes a hand edit take effect on the next call.
_overlay_key: tuple[int, int] = (-1, -1)

_FIELDS = {"gguf_filename", "api_name", "min_gpus", "min_mem_gb",
           "ctx_size", "parallel"}
_ALIASES = {"gguf": "gguf_filename", "filename": "gguf_filename"}


def _load_overlay() -> tuple[dict[str, ModelSpec], dict[str, list[str]]]:
    """Re-read the sidecar when its mtime moves, so no restart is needed.

    A malformed sidecar is logged and ignored rather than raised. It is edited
    by hand between serve requests, and a typo in it must not take the whole
    service down with it.
    """
    global _overlay, _overlay_key
    try:
        stat = SIDECAR.stat()
    except OSError:
        _overlay, _overlay_key = ({}, {}), (-1, -1)
        return _overlay
    key = (stat.st_mtime_ns, stat.st_size)
    if key == _overlay_key:
        return _overlay

    models: dict[str, ModelSpec] = {}
    clusters: dict[str, list[str]] = {}
    try:
        raw = json.loads(SIDECAR.read_text())
        for name, fields in (raw.get("models") or {}).items():
            fields = {_ALIASES.get(k, k): v for k, v in fields.items()}
            unknown = set(fields) - _FIELDS
            if unknown:
                raise ValueError(f"{name}: unknown field(s) {sorted(unknown)}")
            base = REGISTRY.get(name)
            if base is None:
                missing = {"gguf_filename", "api_name"} - set(fields)
                if missing:
                    raise ValueError(f"{name}: missing {sorted(missing)}")
                models[name] = ModelSpec(name=name, **fields)
            else:
                models[name] = replace(base, **fields)
        for cluster, names in (raw.get("clusters") or {}).items():
            clusters[cluster] = list(names)
    except (ValueError, OSError) as exc:
        log.warning("ignoring %s: %s", SIDECAR, exc)
        _overlay, _overlay_key = ({}, {}), key
        return _overlay

    _overlay, _overlay_key = (models, clusters), key
    return _overlay


def registry() -> dict[str, ModelSpec]:
    """Built-in specs with the sidecar's additions and overrides applied."""
    return {**REGISTRY, **_load_overlay()[0]}


def cluster_models() -> dict[str, list[str]]:
    """Per-cluster model lists, with the sidecar's entries appended."""
    overlay = _load_overlay()[1]
    out = {c: list(names) for c, names in CLUSTER_MODELS.items()}
    for cluster, names in overlay.items():
        seen = out.setdefault(cluster, [])
        seen.extend(n for n in names if n not in seen)
    return out


def list_models(cluster: str | None = None) -> list[dict]:
    known, per_cluster = registry(), cluster_models()
    models: list[dict] = []
    for name, spec in known.items():
        if cluster is None or name in per_cluster.get(cluster, []):
            models.append({
                "name": spec.name,
                "gguf": spec.gguf_filename,
                "api_name": spec.api_name,
                "min_gpus": spec.min_gpus,
                "min_mem_gb": spec.min_mem_gb,
                "ctx_size": spec.ctx_size,
                "parallel": spec.parallel,
                "clusters": [
                    c for c, names in per_cluster.items()
                    if name in names
                ],
            })
    return models


def resolve_model(name: str, cluster: str) -> ModelSpec | None:
    spec = registry().get(name)
    if spec is None:
        return None
    if name not in cluster_models().get(cluster, []):
        return None
    return spec
