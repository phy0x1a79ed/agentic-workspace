#!/usr/bin/env bash
# Deploy the current branch to sirius. Run from a dev box that has `ssh sirius`:
#
#   scripts/sirius/deploy.sh [branch]      default: the checked-out branch
#
# The box holds no GitHub credential. The checkout at /opt/awm is a plain
# repo with receive.denyCurrentBranch=updateInstead, so a push from here
# updates its working tree. install-awm.sh then reinstalls when a
# pyproject/install file changed, and restarts the unit either way.
set -euo pipefail
HOST=${SIRIUS_HOST:-sirius}
BRANCH=${1:-$(git rev-parse --abbrev-ref HEAD)}
REMOTE="$HOST:/opt/awm"

before=$(ssh "$HOST" 'git -C /opt/awm rev-parse HEAD 2>/dev/null || true')
git push -q "$REMOTE" "$BRANCH:$BRANCH"
ssh "$HOST" "git -C /opt/awm checkout -q $BRANCH"
after=$(ssh "$HOST" 'git -C /opt/awm rev-parse HEAD')

if [ -z "$before" ] || [ -n "$(git diff --name-only "$before" "$after" -- 'awm/gateway/install.sh' 'awm/gateway/environment.yml' '**/pyproject.toml' 'scripts/sirius/' 2>/dev/null)" ]; then
    ssh -t "$HOST" /opt/awm/scripts/sirius/install-awm.sh
else
    ssh "$HOST" 'sudo systemctl restart awm && sleep 2 && systemctl is-active awm'
fi
echo "deployed $BRANCH @ ${after:0:9}"
curl -sS -o /dev/null -w 'https://nexus.tony-xy-liu.com -> %{http_code}\n' https://nexus.tony-xy-liu.com/ || true
