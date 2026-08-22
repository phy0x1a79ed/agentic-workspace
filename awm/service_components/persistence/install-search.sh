#!/usr/bin/env bash
# Install the persistence component's `search` extra — the semantic-search stack
# (sentence-transformers + sqlite-vec) shared by every service that embeds.
#
# Called explicitly from the install.sh of each consuming service. It is
# deliberately NOT a dependency of the component or of any service's
# pyproject.toml: the extra pulls torch and is multi-GB, so a service that never
# searches must not pay for it, and a bare `pip install -e awm/services/<svc>`
# in a dev env must not block on that download.
#
# The CPU pin is load-bearing. sentence-transformers depends on torch, and
# PyPI's default linux wheel drags the whole nvidia-* CUDA runtime in behind it
# (several GB). No node in this fleet has a usable CUDA GPU, so torch is
# installed first from PyTorch's own CPU index; the extra's resolve below then
# finds it already satisfied and never reaches the CUDA wheel. Override with
# AWM_TORCH_CPU= if a node's Python moves past what that pin has wheels for.
#
# Idempotent — re-running is two "Requirement already satisfied" passes, which
# is what the five consuming install.sh scripts do during a full install.
set -euo pipefail
WS="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
ENV="${1:-${AWM_ENV:-awm}}"
TORCH="${AWM_TORCH_CPU:-2.12.0+cpu}"

echo "+ pip install torch==$TORCH (from the CPU wheel index)"
mamba run -n "$ENV" pip install \
    --index-url https://download.pytorch.org/whl/cpu "torch==$TORCH"

# Quote path+extra as one word: unquoted, bash reads `[search]` as a glob
# character class, and with nullglob off it would fall through literally —
# one shell option away from silently installing the bare component instead.
echo "+ pip install -e $WS/awm/service_components/persistence[search]"
mamba run -n "$ENV" pip install -e "$WS/awm/service_components/persistence[search]"

echo "Installed awm-persistence[search] into env '$ENV'."
