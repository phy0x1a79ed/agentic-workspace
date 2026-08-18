from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ClusterConfig:
    name: str
    ssh_host: str
    user: str
    project_dir: str
    scratch_dir: str
    account: str
    partition: str
    constraint: str
    apptainer_module: str
    # GPU type qualifier for --gres (e.g. "h100"). Alliance clusters now
    # REQUIRE a type: --gres=gpu:h100:N. Empty → bare --gres=gpu:N (sockeye).
    gpu_type: str = ""
    default_gpus: int = 1
    default_cpus: int = 4
    default_mem_gb: int = 32
    default_hours: int = 4
    base_forward_port: int = 8100


CLUSTERS: dict[str, ClusterConfig] = {
    "sockeye": ClusterConfig(
        name="sockeye",
        ssh_host="sockeye",
        user="txyliu",
        project_dir="/arc/project/st-shallam-1/pwy_group/hpcllm",
        scratch_dir="/scratch/st-shallam-1/pwy_group/hpcllm",
        account="st-shallam-1-gpu",
        partition="gpu",
        constraint="gpu_mem_32",
        apptainer_module="gcc/9.4.0 apptainer/1.3.1",
    ),
    "fir": ClusterConfig(
        name="fir",
        ssh_host="fir",
        user="phyberos",
        # Absolute paths REQUIRED: SLURM --output/--error do not expand ~ (they
        # create literal "~" dirs), and cleanenv batch scripts can't rely on
        # $USER. Home on fir is /home/phyberos.
        project_dir="/home/phyberos/projects/rpp-shallam/phyberos/hpcllm",
        scratch_dir="/home/phyberos/scratch/hpcllm",
        account="def-shallam_gpu",
        partition="",
        constraint="",
        apptainer_module="apptainer/1.3.5",
        gpu_type="h100",
        # The sbatch stages the GGUF into $SLURM_TMPDIR before loading it, so the
        # allocation has to hold the largest model on this cluster with room to
        # spare rather than the smallest. 32 was sized for a 10.5 GB model.
        default_mem_gb=64,
    ),
}


def resolve_cluster(name: str) -> ClusterConfig:
    cfg = CLUSTERS.get(name)
    if cfg is None:
        raise ValueError(
            f"unknown cluster {name!r}; known: {', '.join(sorted(CLUSTERS))}"
        )
    return cfg
