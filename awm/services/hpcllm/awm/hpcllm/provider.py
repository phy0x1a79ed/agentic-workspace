from __future__ import annotations

from awm.hpcllm.cluster import ClusterConfig
from awm.hpcllm.models import ModelSpec

CONTAINER_SIF = "llamacpp.sif"


def build_sbatch(serve_id: str, model: ModelSpec, cfg: ClusterConfig,
                 hours: int, gpus: int, cpus: int, mem: int,
                 port: int = 8000) -> str:
    """Generate a self-contained sbatch script for the serve request."""

    model_gguf_path = f"{cfg.project_dir}/models/{model.gguf_filename}"
    container_path = f"{cfg.project_dir}/containers/{CONTAINER_SIF}"
    active_dir = f"{cfg.scratch_dir}/active/{serve_id}"
    log_dir = f"{cfg.scratch_dir}/logs"

    constraint_line = ""
    if cfg.constraint:
        constraint_line = f"#SBATCH --constraint={cfg.constraint}"

    partition_line = ""
    if cfg.partition:
        partition_line = f"#SBATCH --partition={cfg.partition}"

    # Alliance clusters (fir) require a GPU type in the gres spec.
    gres = f"gpu:{cfg.gpu_type}:{gpus}" if cfg.gpu_type else f"gpu:{gpus}"

    return f"""\
#!/bin/bash
#SBATCH --account={cfg.account}
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task={cpus}
#SBATCH --mem={mem}G
#SBATCH --time={hours}:00:00
#SBATCH --gres={gres}
#SBATCH --job-name=hpcllm-{serve_id}
#SBATCH --output={log_dir}/{serve_id}-%j.out
#SBATCH --error={log_dir}/{serve_id}-%j.err
{partition_line}
{constraint_line}

set -euo pipefail

ACTIVE_DIR="{active_dir}"
mkdir -p "$ACTIVE_DIR"
hostname > "$ACTIVE_DIR/hostname"

# fir schedules by GPU, so four jobs share one four-card node and a fixed port
# is a collision waiting to happen -- llama-server exits with "couldn't bind
# HTTP server socket" and the serve looks like a model failure. Take the first
# free port in the range and tell the tunnel which one it was.
PORT={port}
for p in $(seq {port} $(({port} + 99))); do
    if ! (exec 3<>/dev/tcp/127.0.0.1/$p) 2>/dev/null; then
        PORT=$p
        break
    fi
    exec 3<&- 2>/dev/null
done
echo "$PORT" > "$ACTIVE_DIR/port"
echo "serving on port $PORT"

echo "running" > "$ACTIVE_DIR/status"

cleanup() {{
    echo "done" > "$ACTIVE_DIR/status"
    rm -f "$ACTIVE_DIR/hostname"
}}
trap cleanup EXIT

echo "=== stage: copying to SLURM_TMPDIR ==="
cp {container_path} $SLURM_TMPDIR/llamacpp.sif

# A model over ~50 GB is published as a split GGUF, named
# ...-00001-of-000NN.gguf. llama.cpp is handed the first part and finds the rest
# by that exact pattern, so the whole set travels and every name is preserved.
# Renaming to a fixed model.gguf -- which this did -- silently reduces a split
# model to its first shard, and it loads far enough to look like it worked.
GGUF_SRC="{model_gguf_path}"
GGUF_NAME=$(basename "$GGUF_SRC")
case "$GGUF_NAME" in
    *-00001-of-*) cp "${{GGUF_SRC%%-00001-of-*}}"-*-of-*.gguf "$SLURM_TMPDIR/" ;;
    *)            cp "$GGUF_SRC" "$SLURM_TMPDIR/$GGUF_NAME" ;;
esac

echo "=== stage: loading module ==="
module purge && module load {cfg.apptainer_module}

echo "=== stage: starting llama-server ==="
echo "loading" > "$ACTIVE_DIR/status"

apptainer exec --nv --cleanenv \\
    --home "$SLURM_TMPDIR" \\
    --env HF_HOME="$SLURM_TMPDIR/hf_cache" \\
    --env HF_HUB_OFFLINE=1 \\
    --env TRANSFORMERS_OFFLINE=1 \\
    --env LD_LIBRARY_PATH=/app \\
    "$SLURM_TMPDIR/llamacpp.sif" \\
    /app/llama-server \\
        --model "$SLURM_TMPDIR/$GGUF_NAME" \\
        --host 0.0.0.0 \\
        --port "$PORT" \\
        --n-gpu-layers 999 \\
        --ctx-size {model.ctx_size} \\
        --cont-batching \\
        --parallel {model.parallel} \\
        --alias {model.api_name}
"""


def build_active_dir(cfg: ClusterConfig, serve_id: str) -> str:
    return f"{cfg.scratch_dir}/active/{serve_id}"


def build_scratch_log_dir(cfg: ClusterConfig, serve_id: str) -> str:
    return f"{cfg.scratch_dir}/logs"


def build_remote_scratch_dir(cfg: ClusterConfig, serve_id: str) -> str:
    return f"{cfg.scratch_dir}/{serve_id}"


def build_req_path(cfg: ClusterConfig, serve_id: str) -> str:
    return f"{cfg.scratch_dir}/{serve_id}/req.sh"
