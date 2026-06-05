#!/usr/bin/env bash
# Install awm on the current machine as a user-level service.
#
# Idempotent: re-running with --update skips anything already present.
# Assumes the awm source has already been placed at $AWM_SRC (default
# $HOME/awm-src). `git clone` it manually, or rsync from another host.
#
# Usage:
#   deploy/install.sh [--update] [--workspace PATH] [--source PATH]
#                     [--env NAME] [--no-systemd]
#
# After this completes:
#   ~/.local/bin/awm                  — wrapper for ad-hoc invocations
#   $WORKSPACE/.awm/                  — workspace state (db, logs)
#   ~/.awm/auth.token                 — bearer token (chmod 600)
#   ~/.config/systemd/user/awm.service — user unit (started + enabled)
#
# After this completes, awm.service is running on 127.0.0.1:7819. Verify
# with: systemctl --user status awm.

set -euo pipefail

UPDATE_MODE=0
WORKSPACE="${AWM_WORKSPACE:-$HOME/agentic_workspace}"
SRC="${AWM_SRC:-$HOME/awm-src}"
ENV_NAME="${AWM_ENV:-awm}"
INSTALL_SYSTEMD=1

while [[ $# -gt 0 ]]; do
    case "$1" in
        --update)      UPDATE_MODE=1; shift ;;
        --workspace)   WORKSPACE="$2"; shift 2 ;;
        --source)      SRC="$2"; shift 2 ;;
        --env)         ENV_NAME="$2"; shift 2 ;;
        --no-systemd)  INSTALL_SYSTEMD=0; shift ;;
        -h|--help)
            sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)             echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

CONDA_ROOT=""
for d in "$HOME/miniforge3" "$HOME/miniconda3" "$HOME/anaconda3" "/opt/conda"; do
    if [[ -x "$d/bin/conda" ]]; then CONDA_ROOT="$d"; break; fi
done
if [[ -z "$CONDA_ROOT" ]]; then
    echo "error: no miniforge/miniconda found in standard locations" >&2
    exit 1
fi
echo "using conda at: $CONDA_ROOT"

if [[ ! -d "$SRC" ]] || [[ ! -f "$SRC/pyproject.toml" ]]; then
    echo "error: awm source not found at $SRC (expected pyproject.toml)" >&2
    exit 1
fi

ENV_PREFIX="$CONDA_ROOT/envs/$ENV_NAME"
MAMBA_BIN="$CONDA_ROOT/condabin/mamba"
CONDA_BIN="$CONDA_ROOT/condabin/conda"

if [[ ! -d "$ENV_PREFIX" ]]; then
    echo "=== creating conda env '$ENV_NAME' ==="
    if [[ -x "$MAMBA_BIN" ]]; then
        "$MAMBA_BIN" env create -f "$SRC/environment.yml" -y
    else
        "$CONDA_BIN" env create -f "$SRC/environment.yml" -y
    fi
elif [[ "$UPDATE_MODE" -eq 0 ]]; then
    echo "=== env '$ENV_NAME' exists; reusing (pass --update to refresh) ==="
else
    echo "=== env '$ENV_NAME' exists; --update mode — refreshing deps ==="
    if [[ -x "$MAMBA_BIN" ]]; then
        "$MAMBA_BIN" env update -n "$ENV_NAME" -f "$SRC/environment.yml" --prune
    else
        "$CONDA_BIN" env update -n "$ENV_NAME" -f "$SRC/environment.yml" --prune
    fi
fi

echo "=== installing awm package (editable) ==="
"$ENV_PREFIX/bin/pip" install -e "$SRC" --no-deps --quiet

echo "=== initializing workspace at $WORKSPACE ==="
mkdir -p "$WORKSPACE"
AWM_WORKSPACE="$WORKSPACE" "$ENV_PREFIX/bin/awm" init

echo "=== installing wrapper at ~/.local/bin/awm ==="
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/awm" <<WRAPPER
#!/usr/bin/env bash
export AWM_WORKSPACE="${WORKSPACE}"
exec "${ENV_PREFIX}/bin/awm" "\$@"
WRAPPER
chmod +x "$HOME/.local/bin/awm"

# Token lives outside the workspace tree so it survives workspace re-init.
mkdir -p "$HOME/.awm"
chmod 700 "$HOME/.awm"

TOKEN_FILE="$HOME/.awm/auth.token"
if [[ ! -f "$TOKEN_FILE" ]]; then
    echo "=== generating bearer token ==="
    head -c 32 /dev/urandom | base64 | tr -d '=' | tr '/+' '_-' > "$TOKEN_FILE"
fi
chmod 600 "$TOKEN_FILE"

if [[ "$INSTALL_SYSTEMD" -eq 1 ]]; then
    echo "=== installing systemd user unit ==="
    UNIT_DIR="$HOME/.config/systemd/user"
    mkdir -p "$UNIT_DIR"

    cat > "$UNIT_DIR/awm.service" <<UNIT
[Unit]
Description=AWM core (FastAPI tool dispatch + UI + hub on 127.0.0.1:7819)
After=network.target

[Service]
Type=simple
Environment=AWM_WORKSPACE=${WORKSPACE}
Environment=AWM_IDLE_SHUTDOWN=0
Environment=AWM_AUTH_TOKEN_FILE=${TOKEN_FILE}
ExecStart=${ENV_PREFIX}/bin/awm serve
Restart=on-failure
RestartSec=1
StandardOutput=append:${WORKSPACE}/.awm/awm.log
StandardError=append:${WORKSPACE}/.awm/awm.log

[Install]
WantedBy=default.target
UNIT

    systemctl --user daemon-reload
    systemctl --user enable --now awm.service
    systemctl --user restart awm.service
fi

echo ""
echo "=== install complete ==="
echo "  workspace: $WORKSPACE"
echo "  awm wrapper: $HOME/.local/bin/awm (ensure ~/.local/bin is on PATH)"
echo "  token: $TOKEN_FILE"
if [[ "$INSTALL_SYSTEMD" -eq 1 ]]; then
    echo ""
    systemctl --user --no-pager status awm.service 2>&1 | grep -E "Active|●" | head -2
fi
