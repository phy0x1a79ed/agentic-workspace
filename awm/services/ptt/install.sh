#!/usr/bin/env bash
# Canonical install for the awm-ptt service (editable, into the `awm` env).
#
# Installs the component libraries it imports (config, persistence,
# gatewayclient) first, then the service itself — which pulls in
# `faster-whisper` for STT and `numpy`. Components have no install.sh of their
# own — they are pulled in here as plain editable dependencies. The convo
# cleanup loop drives the `awm.agentcore` opencode one-shot path; `agentcore` is
# pure imported source (no install.sh) and is resolved on PYTHONPATH by the dev
# sandbox / installed alongside the gateway in prod. Override the target env
# with AWM_ENV=<name>.
#
# NOTE: faster-whisper downloads its model weights on FIRST use (default
# `small.en`), not at install time — the first transcription pays a one-time
# download. See INSTALL.md.
set -euo pipefail
WS="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
ENV="${AWM_ENV:-awm}"
run() { echo "+ pip install -e $*"; mamba run -n "$ENV" pip install -e "$@"; }

run "$WS/awm/service_components/config" --no-deps
run "$WS/awm/service_components/persistence" --no-deps
run "$WS/awm/service_components/gatewayclient" --no-deps
run "$WS/awm/services/ptt"

# Bake the target env's absolute interpreter into a gitignored `.runtime-env`
# sidecar so the hub supervisor can respawn this service under systemd's
# minimal PATH (no `mamba`).
PYBIN="$(mamba run -n "$ENV" python -c 'import sys; print(sys.executable)')"
printf 'AWM_PYTHON=%s\nAWM_ENV_BIN=%s\n' "$PYBIN" "$(dirname "$PYBIN")" \
    > "$(dirname "$0")/.runtime-env"

echo "Installed awm-ptt into env '$ENV'."
