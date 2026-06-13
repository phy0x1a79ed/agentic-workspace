#!/usr/bin/env bash
# Canonical install for the awm-tts service (editable, into the `awm` env).
#
# Installs the component libraries it imports (config, persistence,
# gatewayclient) first, then the service itself. Components have no install.sh
# of their own — they are pulled in here as plain editable dependencies.
# Override the target env with AWM_ENV=<name>.
#
# NOTE: this installs only the import-time deps (httpx / numpy / pydantic +
# the component libs). The actual speech synthesis runs through OUT-OF-PROCESS
# sidecars (kokoro_rvc / sbv2 / f5tts / gptsovits HTTP servers) and an optional
# in-process piper backend (`piper_tts`). Those heavyweight deps (torch, the
# voice models, first-run weight downloads) are operator-provisioned — see
# INSTALL.md. The service registers and answers listEngines/presets/state with
# none of them present; only opening a `call` session against an engine whose
# sidecar/binary is missing fails (gracefully, per-call).
set -euo pipefail
WS="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
ENV="${AWM_ENV:-awm}"
run() { echo "+ pip install -e $*"; mamba run -n "$ENV" pip install -e "$@"; }

run "$WS/awm/service_components/config" --no-deps
run "$WS/awm/service_components/persistence" --no-deps
run "$WS/awm/service_components/gatewayclient" --no-deps
run "$WS/awm/services/tts"

# Bake the target env's absolute interpreter into a gitignored `.runtime-env`
# sidecar so the hub supervisor can respawn this service under systemd's
# minimal PATH (no `mamba`).
PYBIN="$(mamba run -n "$ENV" python -c 'import sys; print(sys.executable)')"
printf 'AWM_PYTHON=%s\nAWM_ENV_BIN=%s\n' "$PYBIN" "$(dirname "$PYBIN")" \
    > "$(dirname "$0")/.runtime-env"

echo "Installed awm-tts into env '$ENV'."
