#!/usr/bin/env bash
# Entry point the gateway spawns for the rlm-chess realm service. See INSTALL.md.
set -euo pipefail
cd "$(dirname "$0")"
MODULE="awm.rlm_chess.hub_adapter"

if [ -f ./.runtime-env ]; then
    set -a; . ./.runtime-env; set +a
fi

if [ -n "${DEV_PYTHONPATH:-}" ]; then
    exec env PYTHONPATH="$DEV_PYTHONPATH${CHESS_ARENA_ROOT:+:$CHESS_ARENA_ROOT/src/chess}" \
        mamba run -n "${AWM_ENV:-awm}" --no-capture-output \
        python -m "$MODULE"
fi

[ -n "${AWM_ENV_BIN:-}" ] && export PATH="$AWM_ENV_BIN:$PATH"
exec "${AWM_PYTHON:-python}" -m "$MODULE"
