#!/usr/bin/env bash
# Ship this node's built Trilium server to a node that cannot build one.
#
#   ./ship-bundle.sh [host]        default: sirius
#
# sirius has two vCPUs and no GitHub credential, so it installs the published
# tarball. That tarball carries none of the fork's code — the slice mask above
# all — and `_fork_only` refuses to mint or resolve a slice there for exactly
# that reason. This script closes the gap without asking the box to build:
# `apps/server/dist/` is self-contained, and `entry_point()` picks the fork the
# moment `dist/main.cjs` exists.
#
# CAUTION: the remote fork directory must never become a git checkout.
# `install.sh` chooses build mode by testing for `<fork>/.git`, so a checkout
# there turns every later deploy into a monorepo build the box cannot finish.
# Only the bundle travels.
set -euo pipefail
HOST=${1:-sirius}
HERE="$(cd "$(dirname "$0")" && pwd)"
FORK_DIR="${TRILIUM_FORK_DIR:-$(cd "$HERE/../../.." && pwd)/projects/trilium/release}"
DIST="$FORK_DIR/apps/server/dist"
STAMP="$FORK_DIR/.awm/trilium-build-stamp"

step() { echo; echo "== $*"; }
remote() { ssh "$HOST" "$@"; }
# sudo runs one command rather than a shell, so `||`, redirection and globbing
# in an argument string would belong to the calling user. Feed a script instead.
as_awm() { ssh "$HOST" "sudo -n -u awm bash -s" <<<"$*"; }

step "the local build"
[ -f "$DIST/main.cjs" ] || { echo "no bundle at $DIST — run install.sh here first" >&2; exit 1; }
HEAD_SHA="$(git -C "$FORK_DIR" rev-parse HEAD)"
[ -z "$(git -C "$FORK_DIR" status --porcelain)" ] || { echo "$FORK_DIR is dirty; commit or clear it first" >&2; exit 1; }
grep -q "head=$HEAD_SHA" "$STAMP" 2>/dev/null \
    || { echo "the bundle does not match HEAD ($(git -C "$FORK_DIR" describe --tags --always)) — rebuild with install.sh" >&2; exit 1; }
echo "   $(git -C "$FORK_DIR" describe --tags --always) @ ${HEAD_SHA:0:9}"

step "the remote fork directory"
REMOTE_FORK="$(remote 'AWM_WORKSPACE=/opt/awm awm trilium status' \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["source"]["fork_dir"])')"
REMOTE_DIST="$REMOTE_FORK/apps/server/dist"
as_awm "test ! -e $REMOTE_FORK/.git" \
    || { echo "refusing: $HOST:$REMOTE_FORK is a git checkout, which would put its installs into build mode" >&2; exit 1; }
echo "   $HOST:$REMOTE_FORK"

# The node the bundle runs under. A serving node has no toolchain, so the only
# Node on the box is the one inside the published tarball. `node_exe()` reads
# the tarball's own runtime only while the tarball is the chosen entry, and
# reads `node-bin` once the fork entry exists — so the file has to be written
# before the swap, or the child respawns against a `node` that systemd's PATH
# does not have.
step "node"
REMOTE_NODE="$(dirname "$(remote 'readlink -f /opt/awm/awm/services/trilium/server/node/bin/node')")"
WANT="$(tr -d ' \n' < "$FORK_DIR/.nvmrc")"
GOT="$(remote "$REMOTE_NODE/node --version" | tr -d 'v\n')"
[ "$GOT" = "$WANT" ] \
    || { echo "refusing: $HOST ships node $GOT, the fork is built against $WANT" >&2; exit 1; }
echo "   node $GOT, matching .nvmrc"

step "ship"
as_awm "mkdir -p $REMOTE_DIST.incoming $REMOTE_FORK/.awm"
rsync -a --delete --rsync-path='sudo -n -u awm rsync' "$DIST/" "$HOST:$REMOTE_DIST.incoming/"
# The only provenance a node without a checkout has: `source_state()` stops at
# the missing `.git`, so this file is the sole record of which commit is served.
rsync -a --rsync-path='sudo -n -u awm rsync' "$STAMP" "$HOST:$REMOTE_FORK/.awm/"
remote "printf '%s\n' $REMOTE_NODE > /opt/awm/awm/services/trilium/node-bin"

step "swap"
as_awm "rm -rf $REMOTE_DIST.old"
as_awm "test ! -e $REMOTE_DIST || mv $REMOTE_DIST $REMOTE_DIST.old"
as_awm "mv $REMOTE_DIST.incoming $REMOTE_DIST"
remote 'sudo -n systemctl restart awm'

step "verify"
sleep 15
remote 'AWM_WORKSPACE=/opt/awm awm trilium status' | python3 -c '
import json, sys
d = json.load(sys.stdin)
v, s = d["vault"], d["source"]
print("   source=%s running=%s listening=%s error=%s"
      % (s["source"], v["running"], v["listening"], v["error"]))
sys.exit(0 if s["source"] == "fork" and v["running"] and v["listening"] else 1)'
echo "shipped ${HEAD_SHA:0:9} to $HOST"
# The way back, should the bundle turn out to be wrong:
#   ssh HOST 'sudo -n -u awm rm -f <fork>/apps/server/dist/main.cjs'
#   ssh HOST 'sudo -n systemctl restart awm'
# `entry_point()` then falls through to the tarball again.
