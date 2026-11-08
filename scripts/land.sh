#!/usr/bin/env bash
# The land queue: submit, wait, batch, hold, bounce, status. See the skills ci/land and
# ci/merge. It runs the routine binary this node's deploy placed, so every branch lands by
# the same rules. The binary finds the workspace and AWM_DIR as this script does.
set -euo pipefail
ws=${AWM_WORKSPACE:-}
if [ -z "$ws" ]; then
    ws=$PWD
    while [ "$ws" != / ] && [ ! -e "$ws/AGENTS.md" ]; do ws=$(dirname "$ws"); done
    [ -e "$ws/AGENTS.md" ] || ws=$PWD
fi
bin=${AWM_DIR:-$ws/.awm}/bin/routine
[ -x "$bin" ] || { echo "land.sh: no routine binary at $bin. Set AWM_WORKSPACE to the workspace that deploys awm" >&2; exit 1; }
AWM_LAND_CWD=$PWD exec "$bin" land "$@"
