#!/usr/bin/env bash
# Dry-run the routine plugin's deploy engine in a scratch directory from
# mktemp. It lands two branches, then fails a conflicting pair, and touches no
# real repo, worktree or node. Pass --keep to leave the scratch directory.
set -euo pipefail

WS="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
COMP="$WS/awm/service_components/config:$WS/awm/service_components/persistence"
ROOT="$(mktemp -d -t awm-deploy-dryrun.XXXXXX)"
if [ "${1:-}" = "--keep" ]; then
  echo "scratch: $ROOT"
else
  trap 'rm -rf "$ROOT"' EXIT
fi

PY="$(mamba run -n awm python -c 'import sys; print(sys.executable)' 2>/dev/null | grep -v '^[[:space:]]*$' | tail -1)"
cd "$ROOT"
AWM_WORKSPACE="$ROOT" PYTHONPATH="$WS/awm/plugins/routine:$COMP" \
  "$PY" -m awm.routine.dryrun "$ROOT/run"
