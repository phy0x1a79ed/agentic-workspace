from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelSpec:
    name: str
    gguf_filename: str
    api_name: str
    min_gpus: int = 1
    min_mem_gb: int = 16
    ctx_size: int = 32768


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
}

CLUSTER_MODELS: dict[str, list[str]] = {
    "sockeye": ["Qwen/Qwen3-8B"],
}


def list_models(cluster: str | None = None) -> list[dict]:
    models: list[dict] = []
    for name, spec in REGISTRY.items():
        if cluster is None or name in CLUSTER_MODELS.get(cluster, []):
            models.append({
                "name": spec.name,
                "gguf": spec.gguf_filename,
                "api_name": spec.api_name,
                "min_gpus": spec.min_gpus,
                "min_mem_gb": spec.min_mem_gb,
                "ctx_size": spec.ctx_size,
                "clusters": [
                    c for c, names in CLUSTER_MODELS.items()
                    if name in names
                ],
            })
    return models


def resolve_model(name: str, cluster: str) -> ModelSpec | None:
    spec = REGISTRY.get(name)
    if spec is None:
        return None
    if name not in CLUSTER_MODELS.get(cluster, []):
        return None
    return spec
