#!/usr/bin/env bash
# Install the awm-kb service into the awm env, and the `kb` env its server runs in.
#
# The server's dependencies (Cognee, torch, PyMuPDF, OCR) never enter the awm
# env. They live in a separate env built from the kb checkout's own
# envs/kb/base.yml, and this script records that env's interpreter for the
# supervisor. A node with no kb checkout installs the service and warns: it
# registers, and `kb status` says why it has no server.
#
# Override the awm env with AWM_ENV, the kb env with KB_ENV, the checkout with KB_CHECKOUT.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ENV="${AWM_ENV:-awm}"
KB_ENV="${KB_ENV:-kb}"
REPO="$(git -C "$HERE" rev-parse --show-toplevel)"
workspace_root() {
    [ -n "${AWM_WORKSPACE:-}" ] && { printf '%s\n' "$AWM_WORKSPACE"; return; }
    local d="$1"
    while [ "$d" != "/" ]; do
        if [ -d "$d/projects" ] && [ -f "$d/AGENTS.md" ]; then
            printf '%s\n' "$d"
            return
        fi
        d="$(dirname "$d")"
    done
    printf '%s\n' "$REPO"
}
WS="$(workspace_root "$HERE")"
CHECKOUT="${KB_CHECKOUT:-$WS/projects/kb/release}"
ENV_FILE="$CHECKOUT/envs/kb/base.yml"

run() { echo "+ pip install -e $*"; mamba run -n "$ENV" pip install -e "$@"; }

run "$REPO/awm/service_components/config" --no-deps
run "$REPO/awm/service_components/gatewayclient" --no-deps
run "$REPO/awm/services/kb"

PYBIN="$(mamba run -n "$ENV" python -c 'import sys; print(sys.executable)')"
printf 'AWM_PYTHON=%s\nAWM_ENV_BIN=%s\n' "$PYBIN" "$(dirname "$PYBIN")" > "$HERE/.runtime-env"

if [ ! -f "$ENV_FILE" ]; then
    echo "warning: no $ENV_FILE — kb will register without a server." >&2
    echo "  check out the kb project, or set KB_CHECKOUT." >&2
    exit 0
fi

WANT="$(sha256sum "$ENV_FILE" | cut -d' ' -f1)"
STAMP="$HERE/kb-env-stamp"
if mamba run -n "$KB_ENV" python -c 'import cognee' >/dev/null 2>&1 \
   && [ "$(cat "$STAMP" 2>/dev/null || true)" = "$WANT" ]; then
    echo "env '$KB_ENV' matches $ENV_FILE — nothing to do."
elif mamba run -n "$KB_ENV" python -c 'import sys' >/dev/null 2>&1; then
    echo "Updating env '$KB_ENV' from $ENV_FILE …"
    mamba env update -n "$KB_ENV" -f "$ENV_FILE"
    printf '%s\n' "$WANT" > "$STAMP"
else
    echo "Creating env '$KB_ENV' from $ENV_FILE (several minutes) …"
    mamba env create -n "$KB_ENV" -f "$ENV_FILE"
    printf '%s\n' "$WANT" > "$STAMP"
fi

mamba run -n "$KB_ENV" python -c 'import sys; print(sys.executable)' > "$HERE/kb-python"
echo "Installed awm-kb into env '$ENV'."
echo "Server:  $CHECKOUT (env '$KB_ENV': $(cat "$HERE/kb-python"))"
