#!/usr/bin/env bash
# Entry point the gateway spawns and respawns. Dev sandbox when DEV_PYTHONPATH
# is set, otherwise the interpreter install.sh recorded. The tether binaries the
# machine kind supervises are fetched first, and a failed fetch is not fatal:
# the binaries in place keep running.
set -euo pipefail
cd "$(dirname "$0")"
MODULE="awm.channel.hub_adapter"

bash ./fetch-release.sh || echo "channel: fetch-release.sh failed; see .awm/services/tether/fetch.log" >&2

if [ -n "${DEV_PYTHONPATH:-}" ]; then
    exec env PYTHONPATH="$DEV_PYTHONPATH" \
        mamba run -n "${AWM_ENV:-awm}" --no-capture-output \
        python -m "$MODULE"
fi

[ -f ./.runtime-env ] && . ./.runtime-env
[ -n "${AWM_ENV_BIN:-}" ] && export PATH="$AWM_ENV_BIN:$PATH"
exec "${AWM_PYTHON:-python}" -m "$MODULE"
