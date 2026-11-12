#!/usr/bin/env bash
# Run cargo in mizar's Rust build container (install.md R113).
# Usage: scripts/rust-build.sh [cargo args...]   e.g. scripts/rust-build.sh test
#        AWM_CARGO_TARGET_DIR=<dir> scripts/rust-build.sh build   (build into this target dir)
#        AWM_RUST_WORKDIR=<dir> scripts/rust-build.sh build   (another Cargo project)
#        scripts/rust-build.sh --target-dir   (print the target dir a build uses)
#        scripts/rust-build.sh --gc [--yes]   (delete the target dirs no live session builds in, and
#                                               the build images no worktree's Dockerfile matches)
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
    gc_images "$apply"
    [ -n "$apply" ] || echo "--- dry run: run --gc --yes to delete ---"
}

# The build image tag each worktree of this repo uses: its Dockerfile's hash when its
# rust-build.sh tags by hash, else the fixed tag its rust-build.sh names.
image_tags_in_use() {
    local wt base
    git -C "$here" worktree list --porcelain | sed -n 's/^worktree //p' | while read -r wt; do
        [ -f "$wt/scripts/rust-build.sh" ] && [ -f "$wt/core/Dockerfile" ] || continue
        base="$(grep -o 'awm-rust-build:[0-9][0-9.]*' "$wt/scripts/rust-build.sh" | head -n1)"
        [ -n "$base" ] || continue
        if grep -qF 'sha256sum < "$here/core/Dockerfile"' "$wt/scripts/rust-build.sh"; then
            printf '%s-%s\n' "$base" "$(sha256sum < "$wt/core/Dockerfile" | cut -c1-12)"
        else
            printf '%s\n' "$base"
        fi
    done | sort -u
}

gc_images() {
    local apply="$1" used tag
    command -v docker >/dev/null || return 0
    used="$(image_tags_in_use)"
    [ -n "$used" ] || { echo "--gc: no worktree names a build image: deleting no image" >&2; return 0; }
    docker image ls awm-rust-build --format '{{.Repository}}:{{.Tag}}' | while read -r tag; do
        if grep -qxF "$tag" <<<"$used"; then
            echo "keep    $tag (a worktree's Dockerfile)"
        elif [ -n "$apply" ]; then
            echo "delete  $tag"
            docker image rm "$tag" >/dev/null || echo "warning: could not remove $tag" >&2
        else
            echo "would delete  $tag"
        fi
    done
}

if [ "${1:-}" = --gc ]; then
    gc "${2:-}"
    exit
fi

# The tag carries the Dockerfile's hash, so an edit to it builds a new image.
image="${AWM_RUST_IMAGE:-awm-rust-build:1.99.1-$(sha256sum < "$here/core/Dockerfile" | cut -c1-12)}"
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
# Cargo never deletes a superseded output, so every edit leaves another test binary and
# incremental session behind. Drop those untouched for three days; a current one relinks.
find "$target_dir" -mindepth 3 -maxdepth 4 \
    \( -path '*/deps/*' -type f -perm -u+x ! -name '*.*' -o -path '*/incremental/*' -prune \) \
    -mtime +3 -exec rm -rf {} + 2>/dev/null || true
mounts=(-v "$volume:/cargo/registry" -v "$workdir:$workdir" -v "$target_dir:$target_dir")
[ "$workdir" != "$here/core" ] || mounts+=(-v "$here:$here:ro")
env_conda=()
conda="${AWM_CONDA_ROOT:-$HOME/miniforge3}"
[ -d "$conda" ] && mounts+=(-v "$conda:$conda:ro") && env_conda=(-e "AWM_CONDA_ROOT=$conda")

tty=(); [ -t 0 ] && tty=(-it)
exec docker run --rm "${tty[@]}" --user "$(id -u):$(id -g)" "${mounts[@]}" "${env_conda[@]}" \
    -e CARGO_TARGET_DIR="$target_dir" -e HOME=/tmp \
    -w "$workdir" "$image" cargo "$@"
