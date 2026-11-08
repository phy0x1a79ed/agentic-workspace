#!/usr/bin/env bash
# The land queue: submit, wait, batch, hold, bounce, status. See the skills ci/land and
# ci/merge. It runs the routine binary this node's deploy placed, so every branch lands by
# the same rules. The workspace is AWM_WORKSPACE, else the nearest directory above the cwd
# that holds AGENTS.md and projects/: an awm scope worktree holds AGENTS.md too.
set -euo pipefail
ws=${AWM_WORKSPACE:-}
if [ -z "$ws" ]; then
    ws=$PWD
    while [ "$ws" != / ] && ! { [ -e "$ws/AGENTS.md" ] && [ -d "$ws/projects" ]; }; do ws=$(dirname "$ws"); done
    [ "$ws" != / ] || { echo "land.sh: no workspace above $PWD. Set AWM_WORKSPACE" >&2; exit 1; }
fi
export AWM_WORKSPACE=$ws
bin=${AWM_DIR:-$ws/.awm}/bin/routine
[ -x "$bin" ] || { echo "land.sh: no routine binary at $bin. Set AWM_WORKSPACE to the workspace that deploys awm" >&2; exit 1; }
AWM_LAND_CWD=$PWD exec "$bin" land "$@"
