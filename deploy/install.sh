#!/usr/bin/env bash
# Install awm on the current machine as a user-level service.
#
# Idempotent: re-running with --update skips anything already present.
# Assumes the awm source has already been placed at $AWM_SRC (default
# $HOME/awm-src). Use deploy/sync-to-peer.sh from a developer machine to
# put it there, or `git clone` it manually.
#
# Usage:
#   deploy/install.sh [--update] [--workspace PATH] [--source PATH]
#                     [--env NAME] [--port N] [--san HOSTS]
#                     [--no-systemd]
#
# After this completes:
#   ~/.local/bin/awm                  — wrapper for ad-hoc invocations
#   $WORKSPACE/.awm/                  — workspace state (db, logs)
#   ~/.awm/auth.token                 — bearer token (chmod 600)
#   ~/.awm/tls/{cert,key}.pem         — self-signed TLS cert
#   ~/.config/systemd/user/awm*.service — user units (started + enabled)
#
# After this completes, both awm.service and awm-exposed.service are
# running. Verify with: systemctl --user status awm awm-exposed.

set -euo pipefail

UPDATE_MODE=0
WORKSPACE="${AWM_WORKSPACE:-$HOME/agentic_workspace}"
SRC="${AWM_SRC:-$HOME/awm-src}"
ENV_NAME="${AWM_ENV:-awm}"
EXPOSED_PORT="${AWM_EXPOSED_PORT:-7820}"
EXTRA_SAN=""
INSTALL_SYSTEMD=1

while [[ $# -gt 0 ]]; do
    case "$1" in
        --update)      UPDATE_MODE=1; shift ;;
        --workspace)   WORKSPACE="$2"; shift 2 ;;
        --source)      SRC="$2"; shift 2 ;;
        --env)         ENV_NAME="$2"; shift 2 ;;
        --port)        EXPOSED_PORT="$2"; shift 2 ;;
        --san)         EXTRA_SAN="$2"; shift 2 ;;
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
    echo "       run deploy/sync-to-peer.sh from a developer machine first" >&2
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
    # New dependencies (e.g. discord.py for the operator-login bot) land in
    # environment.yml from time to time. Pull them into the existing env so
    # the bot doesn't fail with ImportError on startup.
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

# Token + TLS live outside the workspace tree so they survive workspace re-init.
mkdir -p "$HOME/.awm/tls"
chmod 700 "$HOME/.awm"

TOKEN_FILE="$HOME/.awm/auth.token"
if [[ -f "$TOKEN_FILE" ]] && [[ "$UPDATE_MODE" -eq 1 ]]; then
    echo "=== reusing existing $TOKEN_FILE ==="
elif [[ -f "$TOKEN_FILE" ]]; then
    echo "=== reusing existing $TOKEN_FILE ==="
else
    echo "=== generating bearer token ==="
    AWM_WORKSPACE="$WORKSPACE" AWM_AUTH_TOKEN_FILE="$TOKEN_FILE" \
        "$ENV_PREFIX/bin/awm" exposed init-token
fi
chmod 600 "$TOKEN_FILE"

CERT="$HOME/.awm/tls/cert.pem"
KEY="$HOME/.awm/tls/key.pem"
if [[ -f "$CERT" ]] && [[ -f "$KEY" ]]; then
    echo "=== reusing existing TLS cert at $CERT ==="
else
    echo "=== generating self-signed TLS cert ==="
    HOSTNAME_SHORT="$(hostname -s)"
    SAN="DNS:${HOSTNAME_SHORT},IP:127.0.0.1"
    if [[ -n "$EXTRA_SAN" ]]; then
        SAN="$SAN,$EXTRA_SAN"
    fi
    openssl req -x509 -newkey rsa:4096 -nodes -days 365 \
        -keyout "$KEY" -out "$CERT" \
        -subj "/CN=${HOSTNAME_SHORT}" \
        -addext "subjectAltName=${SAN}" 2>&1 | tail -2
    chmod 600 "$KEY"
fi

if [[ "$INSTALL_SYSTEMD" -eq 1 ]]; then
    echo "=== installing systemd user units ==="
    UNIT_DIR="$HOME/.config/systemd/user"
    mkdir -p "$UNIT_DIR"

    cat > "$UNIT_DIR/awm.service" <<UNIT
[Unit]
Description=AWM core (FastAPI tool dispatch service)
After=network.target

[Service]
Type=simple
Environment=AWM_WORKSPACE=${WORKSPACE}
Environment=AWM_IDLE_SHUTDOWN=0
ExecStart=${ENV_PREFIX}/bin/awm serve
Restart=on-failure
RestartSec=1
StandardOutput=append:${WORKSPACE}/.awm/awm.log
StandardError=append:${WORKSPACE}/.awm/awm.log

[Install]
WantedBy=default.target
UNIT

    cat > "$UNIT_DIR/awm-exposed.service" <<UNIT
[Unit]
Description=AWM network-exposed listener (HTTPS REST + WebSocket)
After=network.target
Wants=awm.service

[Service]
Type=simple
Environment=AWM_WORKSPACE=${WORKSPACE}
Environment=AWM_IDLE_SHUTDOWN=0
Environment=AWM_EXPOSED_HOST=0.0.0.0
Environment=AWM_EXPOSED_PORT=${EXPOSED_PORT}
Environment=AWM_TLS_CERT=${CERT}
Environment=AWM_TLS_KEY=${KEY}
Environment=AWM_AUTH_TOKEN_FILE=${TOKEN_FILE}
ExecStart=${ENV_PREFIX}/bin/awm serve-exposed
Restart=on-failure
RestartSec=2
StandardOutput=append:${WORKSPACE}/.awm/awm-exposed.log
StandardError=append:${WORKSPACE}/.awm/awm-exposed.log

[Install]
WantedBy=default.target
UNIT

    systemctl --user daemon-reload
    systemctl --user enable --now awm.service awm-exposed.service
    systemctl --user restart awm-exposed.service  # pick up any source change
fi

echo ""
echo "=== install complete ==="
echo "  workspace: $WORKSPACE"
echo "  awm wrapper: $HOME/.local/bin/awm (ensure ~/.local/bin is on PATH)"
echo "  token: $TOKEN_FILE"
echo "  cert: $CERT"
if [[ "$INSTALL_SYSTEMD" -eq 1 ]]; then
    echo ""
    systemctl --user --no-pager status awm.service awm-exposed.service 2>&1 | grep -E "Active|●" | head -4
fi
