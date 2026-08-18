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
                "parallel": spec.parallel,
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
