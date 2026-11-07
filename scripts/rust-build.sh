#!/usr/bin/env bash
# Run cargo in mizar's Rust build container (install.md R113).
# Usage: scripts/rust-build.sh [cargo args...]   e.g. scripts/rust-build.sh test
#        AWM_CARGO_TARGET_DIR=<dir> scripts/rust-build.sh build   (build into this target dir)
#        AWM_RUST_WORKDIR=<dir> scripts/rust-build.sh build   (another Cargo project)
#        scripts/rust-build.sh --target-dir   (print the target dir a build uses)
#        scripts/rust-build.sh --gc [--yes]   (delete the target dirs no live session builds in)
#
# The default target dir is ~/.cache/awm-target/<worktree>, so build output stays out of the
# worktree. --gc keeps <worktree> and <worktree>-* for the worktree of each live session that
# `awm reflection list_other` lists, and deletes nothing when it cannot list them. Without --yes
# it prints what it would delete.
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
cache="${HOME:?}/.cache/awm-target"

worktree_of() {
    basename "$(git -C "$1" rev-parse --show-toplevel 2>/dev/null || printf '%s' "$1")"
}

gc() {
    local apply="" rows live dir name w keep
    case "${1:-}" in
        "") ;;
        --yes) apply=1 ;;
        *) echo "usage: rust-build.sh --gc [--yes]" >&2; return 2 ;;
    esac
    [ -d "$cache" ] || { echo "no $cache: nothing to delete"; return 0; }
    for tool in awm jq; do
        command -v "$tool" >/dev/null || { echo "--gc: no $tool on PATH, so no live session is known: deleting nothing" >&2; return 0; }
    done
    if ! rows="$(awm reflection list_other 2>/dev/null)" || [ "$(jq -r '.ok' <<<"$rows" 2>/dev/null)" != true ]; then
        echo "--gc: awm reflection list_other gave no session list: deleting nothing" >&2
        return 0
    fi
    jq -r '.errors[]? | "warning: \(.harness // "a harness") listed no sessions: \(.error)"' <<<"$rows" >&2
    live="$(jq -r '.sessions[]?.cwd // empty' <<<"$rows" | while read -r cwd; do worktree_of "$cwd"; done | sort -u)"
    for dir in "$cache"/*/; do
        [ -d "$dir" ] || continue
        name="$(basename "$dir")"
        keep=""
        while read -r w; do
            [ -n "$w" ] && { [ "$name" = "$w" ] || [[ $name == "$w"-* ]]; } && keep="$w"
        done <<<"$live"
        if [ -n "$keep" ]; then
            echo "keep    $name (a session runs in $keep)"
        elif [ -n "$apply" ]; then
            echo "delete  $name ($(du -sh "$dir" 2>/dev/null | cut -f1))"
            find "$cache" -mindepth 1 -maxdepth 1 -type d -name "$name" -exec rm -rf {} +
        else
            echo "would delete  $name ($(du -sh "$dir" 2>/dev/null | cut -f1))"
        fi
    done
    [ -n "$apply" ] || echo "--- dry run: run --gc --yes to delete ---"
}

if [ "${1:-}" = --gc ]; then
    gc "${2:-}"
    exit
fi

image="${AWM_RUST_IMAGE:-awm-rust-build:1.99.1}"
volume="${AWM_CARGO_CACHE:-awm-cargo-cache}"
workdir="$(cd "${AWM_RUST_WORKDIR:-$here/core}" && pwd)"
target_dir="${AWM_CARGO_TARGET_DIR:-$cache/$(worktree_of "$workdir")}"
case "$target_dir" in /*) ;; *) target_dir="$PWD/$target_dir" ;; esac
if [ "${1:-}" = --target-dir ]; then
    printf '%s\n' "$target_dir"
    exit 0
fi

if ! docker image inspect "$image" >/dev/null 2>&1; then
    docker build -t "$image" -f "$here/core/Dockerfile" "$here/core"
fi
if ! docker volume inspect "$volume" >/dev/null 2>&1; then
    docker volume create "$volume" >/dev/null
fi
# Any uid may write the shared cache: each lane runs cargo as its own host user.
docker run --rm -v "$volume:/c" "$image" sh -c 'chmod -R a+rwX /c 2>/dev/null || true'

mkdir -p "$target_dir"
mounts=(-v "$volume:/cargo/registry" -v "$workdir:$workdir" -v "$target_dir:$target_dir")
[ "$workdir" != "$here/core" ] || mounts+=(-v "$here:$here:ro")
env_conda=()
conda="${AWM_CONDA_ROOT:-$HOME/miniforge3}"
[ -d "$conda" ] && mounts+=(-v "$conda:$conda:ro") && env_conda=(-e "AWM_CONDA_ROOT=$conda")

tty=(); [ -t 0 ] && tty=(-it)
exec docker run --rm "${tty[@]}" --user "$(id -u):$(id -g)" "${mounts[@]}" "${env_conda[@]}" \
    -e CARGO_TARGET_DIR="$target_dir" -e HOME=/tmp \
    -w "$workdir" "$image" cargo "$@"
