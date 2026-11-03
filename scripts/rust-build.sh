#!/usr/bin/env bash
# Run cargo in mizar's Rust build container (install.md R113).
# Usage: scripts/rust-build.sh [cargo args...]   e.g. scripts/rust-build.sh test
#        AWM_RUST_WORKDIR=<dir> scripts/rust-build.sh build   (another Cargo project)
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
image="${AWM_RUST_IMAGE:-awm-rust-build:1.99.1}"
volume="${AWM_CARGO_CACHE:-awm-cargo-cache}"
workdir="$(cd "${AWM_RUST_WORKDIR:-$here/core}" && pwd)"

if ! docker image inspect "$image" >/dev/null 2>&1; then
    docker build -t "$image" -f "$here/core/Dockerfile" "$here/core"
fi
docker volume inspect "$volume" >/dev/null 2>&1 || docker volume create "$volume" >/dev/null

mounts=(-v "$volume:/cargo/registry" -v "$workdir:$workdir")
[ "$workdir" != "$here/core" ] || mounts+=(-v "$here:$here:ro")
conda="${AWM_CONDA_ROOT:-$HOME/miniforge3}"
[ -d "$conda" ] && mounts+=(-v "$conda:$conda:ro") && env_conda=(-e "AWM_CONDA_ROOT=$conda")

tty=(); [ -t 0 ] && tty=(-it)
exec docker run --rm "${tty[@]}" "${mounts[@]}" "${env_conda[@]}" \
    -e CARGO_TARGET_DIR="$workdir/target" -e HOME=/tmp \
    -w "$workdir" "$image" cargo "$@"
