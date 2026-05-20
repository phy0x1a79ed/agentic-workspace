#!/usr/bin/env bash
# Sync the awm package source to a peer machine and re-install it editably.
#
# Used for fast iteration during federation development: edit locally, push
# to the peer, restart its listeners, re-run the validation step.
#
# Assumes the remote already has:
#   - mamba/conda available on a login shell
#   - the `awm` conda env created (run deploy/install.sh on the peer first)
#
# Usage:
#   deploy/sync-to-peer.sh <ssh-host> [--restart]
#
# Env knobs:
#   REMOTE_SRC   path on remote to sync into            (default: $HOME/awm-src)
#   REMOTE_ENV   conda env name on remote               (default: awm)

set -euo pipefail

HOST="${1:-}"
RESTART="${2:-}"
if [[ -z "$HOST" ]]; then
    echo "Usage: $0 <ssh-host> [--restart]" >&2
    exit 2
fi

# Paths are relative to the SSH login directory (= remote $HOME on first hop).
REMOTE_SRC="${REMOTE_SRC:-awm-src}"
REMOTE_ENV="${REMOTE_ENV:-awm}"
REMOTE_CONDA="${REMOTE_CONDA:-miniforge3}"

# Snippet that sources conda+mamba on a remote where bashrc may not init them.
CONDA_INIT="source ~/$REMOTE_CONDA/etc/profile.d/conda.sh && source ~/$REMOTE_CONDA/etc/profile.d/mamba.sh"

LOCAL_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [[ ! -d "$LOCAL_ROOT/awm" ]] || [[ ! -f "$LOCAL_ROOT/pyproject.toml" ]]; then
    echo "error: expected awm/ and pyproject.toml under $LOCAL_ROOT" >&2
    exit 1
fi

echo "=== syncing awm source to $HOST:$REMOTE_SRC ==="
ssh "$HOST" "mkdir -p $REMOTE_SRC"

rsync -a --delete \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '.pytest_cache' \
    --exclude '*.egg-info' \
    "$LOCAL_ROOT/awm" \
    "$LOCAL_ROOT/deploy" \
    "$LOCAL_ROOT/pyproject.toml" \
    "$LOCAL_ROOT/environment.yml" \
    "$HOST:$REMOTE_SRC/"

echo "=== reinstalling editable package in env '$REMOTE_ENV' on $HOST ==="
ssh "$HOST" "bash -lc '$CONDA_INIT && cd ~/$REMOTE_SRC && mamba run -n $REMOTE_ENV pip install -e . --no-deps --quiet'"

ssh "$HOST" "bash -lc '$CONDA_INIT && mamba run -n $REMOTE_ENV python -c \"import awm; print(\\\"awm package import: ok\\\")\"'"

if [[ "$RESTART" == "--restart" ]]; then
    echo "=== restarting awm services on $HOST ==="
    ssh "$HOST" "systemctl --user restart awm.service awm-exposed.service 2>/dev/null || echo 'note: user units not installed yet, skipping restart'"
fi

echo "=== done ==="
