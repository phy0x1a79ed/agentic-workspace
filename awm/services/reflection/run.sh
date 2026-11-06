#!/usr/bin/env bash
# The gateway starts and respawns the reflection service with `bash run.sh`.
# install.sh writes ./.runtime-env with AWM_PYTHON, the env's absolute
# interpreter, so a respawn works under systemd's minimal PATH. The dev sandbox
# sets AWM_PYTHON and PYTHONPATH in its config's [env] instead.
set -euo pipefail
cd "$(dirname "$0")"
[ -f ./.runtime-env ] && . ./.runtime-env
[ -n "${AWM_ENV_BIN:-}" ] && export PATH="$AWM_ENV_BIN:$PATH"
exec "${AWM_PYTHON:-python}" -m awm.reflection.hub_adapter
