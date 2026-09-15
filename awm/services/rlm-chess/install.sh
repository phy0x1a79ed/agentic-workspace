#!/usr/bin/env bash
# Canonical install for the awm-rlm-chess service (editable, into the `awm` env).
# Needs CHESS_ARENA_ROOT = a game-bot/chess checkout. AWM_ENV and CHESS_ENV pick the envs.
set -euo pipefail
: "${CHESS_ARENA_ROOT:?set CHESS_ARENA_ROOT to a game-bot/chess checkout}"
WS="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
ENV="${AWM_ENV:-awm}"
CHESS_ENV="${CHESS_ENV:-chess}"
ARENA="$(realpath "$CHESS_ARENA_ROOT")"
run() { echo "+ pip install -e $*"; mamba run -n "$ENV" pip install -e "$@"; }

run "$WS/awm/service_components/config" --no-deps
run "$WS/awm/service_components/persistence" --no-deps
run "$WS/awm/service_components/gatewayclient" --no-deps
run "$ARENA"
run "$WS/awm/services/rlm-chess"

PYBIN="$(mamba run -n "$ENV" python -c 'import sys; print(sys.executable)')"
CHESS_PYBIN="$(mamba run -n "$CHESS_ENV" python -c 'import sys; print(sys.executable)')"
printf 'AWM_PYTHON=%s\nAWM_ENV_BIN=%s\nCHESS_ARENA_ROOT=%s\nCHESS_PYTHON=%s\n' \
    "$PYBIN" "$(dirname "$PYBIN")" "$ARENA" "$CHESS_PYBIN" \
    > "$(dirname "$0")/.runtime-env"

echo "Installed awm-rlm-chess into env '$ENV' against the arena at $ARENA."
