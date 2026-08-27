#!/usr/bin/env bash
# Deploy a branch to sirius. Run from a dev box that has `ssh sirius`, from a
# checkout of that branch whose pages are built (`npm run build` in awm/):
#
#   scripts/sirius/deploy.sh [branch]      default: release
#
# The box holds no GitHub credential and no node. The checkout at /opt/awm
# is a plain repo with receive.denyCurrentBranch=updateInstead, so a push
# from here updates its working tree; the built pages (gitignored dist/)
# are rsynced beside it. install-awm.sh then reinstalls when an install
# file changed, and restarts the unit either way.
set -euo pipefail
HOST=${SIRIUS_HOST:-sirius}
BRANCH=${1:-release}
REMOTE="$HOST:/opt/awm"
ROOT="$(git rev-parse --show-toplevel)"
PAGES="notes drawio"

[ "$(git rev-parse "$BRANCH")" = "$(git rev-parse HEAD)" ] \
    || { echo "refusing: HEAD is not $BRANCH; run from the tree whose pages were built" >&2; exit 1; }
for p in $PAGES; do
    [ -f "$ROOT/awm/pages/$p/dist/index.html" ] \
        || { echo "refusing: awm/pages/$p/dist is not built (npm run build in awm/)" >&2; exit 1; }
done

before=$(ssh "$HOST" 'git -C /opt/awm rev-parse --verify -q HEAD || true')
git push -q "$REMOTE" "$BRANCH:$BRANCH"
ssh "$HOST" "git -C /opt/awm checkout -q $BRANCH"
after=$(ssh "$HOST" 'git -C /opt/awm rev-parse HEAD')
for p in $PAGES; do
    rsync -a --delete "$ROOT/awm/pages/$p/dist/" "$REMOTE/awm/pages/$p/dist/"
done

if [ -z "$before" ] || [ -n "$(git diff --name-only "$before" "$after" -- 'awm/gateway/install.sh' 'awm/gateway/environment.yml' '**/pyproject.toml' 'awm/services/*/install.sh' 'awm/services/drawio/patches/' 'scripts/sirius/' 2>/dev/null)" ]; then
    ssh "$HOST" /opt/awm/scripts/sirius/install-awm.sh
else
    ssh "$HOST" 'sudo systemctl restart awm && sleep 2 && systemctl is-active awm'
fi
echo "deployed $BRANCH @ ${after:0:9}"
curl -sS -o /dev/null -w 'https://nexus.tony-xy-liu.com -> %{http_code}\n' https://nexus.tony-xy-liu.com/ || true
