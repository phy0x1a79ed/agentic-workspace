#!/usr/bin/env bash
# Make each node of the fleet run a release commit: rollout.sh --restart <list> <sha>.
# It runs the routine binary this node's deploy placed. The workspace is AWM_WORKSPACE, else the nearest directory above the cwd
# that holds AGENTS.md and projects/: an awm scope worktree holds AGENTS.md too.
set -euo pipefail
ws=${AWM_WORKSPACE:-}
if [ -z "$ws" ]; then
    ws=$PWD
    while [ "$ws" != / ] && ! { [ -e "$ws/AGENTS.md" ] && [ -d "$ws/projects" ]; }; do ws=$(dirname "$ws"); done
    [ "$ws" != / ] || { echo "rollout.sh: no workspace above $PWD. Set AWM_WORKSPACE" >&2; exit 1; }
fi
export AWM_WORKSPACE=$ws
bin=${AWM_DIR:-$ws/.awm}/bin/routine
[ -x "$bin" ] || { echo "rollout.sh: no routine binary at $bin. Set AWM_WORKSPACE to the workspace that deploys awm" >&2; exit 1; }
exec "$bin" rollout "$@"
