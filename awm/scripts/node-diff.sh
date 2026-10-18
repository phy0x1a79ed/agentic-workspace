#!/usr/bin/env bash
# Compare this node's setup with a peer's and report every difference that
# node-intent.toml does not explain.
#   node-diff.sh <peer>       print unexplained differences (exit 1 if any)
#   node-diff.sh -v <peer>    also print the explained ones, with their reasons
#
# The peer is reached as <peer>z, the mesh alias every node carries. The probe
# goes over ssh stdin, so the peer needs no checkout of this branch.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY=$HOME/lib/miniforge3/envs/awm/bin/python
VERBOSE=0
[[ ${1:-} == -v ]] && { VERBOSE=1; shift; }
PEER=${1:?usage: node-diff.sh [-v] <peer>}
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

bash "$HERE/node-probe.sh" >"$TMP/local" || { echo "local probe failed" >&2; exit 2; }
ssh -o BatchMode=yes -o ConnectTimeout=10 "${PEER}z" bash -s <"$HERE/node-probe.sh" >"$TMP/peer" \
  || { echo "probe on ${PEER}z failed" >&2; exit 2; }

VERBOSE=$VERBOSE "$PY" - "$TMP/local" "$TMP/peer" "$HERE/node-intent.toml" <<'PYEOF'
import fnmatch, os, sys, tomllib

VALUED = {"enabled", "checkout", "claude-skill", "claude-hook", "claude-file", "dotfile"}

def load(path):
    facts, node = {}, None
    for line in open(path):
        line = line.rstrip("\n")
        layer, _, item = line.partition(":")
        if layer == "node":
            node = item
            continue
        key, value = line, None
        if layer in VALUED and "=" in item:
            key, _, value = line.rpartition("=")
        facts[key] = value
    if not node:
        sys.exit(f"{path}: probe printed no node name")
    return node, facts

(a, fa), (b, fb) = load(sys.argv[1]), load(sys.argv[2])
intent = tomllib.load(open(sys.argv[3], "rb"))
verbose = os.environ.get("VERBOSE") == "1"

def reason(table, key):
    for pattern, why in intent.get(table, {}).items():
        if fnmatch.fnmatchcase(key, pattern):
            return why
    return None

rows = []
for key in sorted(fa.keys() - fb.keys()):
    rows.append((f"only {a}", key, "", reason(a, key)))
for key in sorted(fb.keys() - fa.keys()):
    rows.append((f"only {b}", key, "", reason(b, key)))
for key in sorted(k for k in fa.keys() & fb.keys() if fa[k] != fb[k]):
    rows.append(("differs", key, f"{a}={fa[key]} {b}={fb[key]}", reason("differs", key)))

unexplained = [r for r in rows if r[3] is None]
width = max((len(r[0]) for r in rows), default=0)
for where, key, values, why in rows:
    if why is None or verbose:
        tail = f"  # {why}" if why else ""
        print(f"{where:<{width}}  {key}  {values}".rstrip() + tail)

for node, facts in ((a, fa), (b, fb)):
    for pattern in intent.get(node, {}):
        if not any(fnmatch.fnmatchcase(k, pattern) for k in facts):
            print(f"note: [{node}] {pattern!r} matches nothing on {node}; drop it?", file=sys.stderr)

print(f"{a} vs {b}: {len(rows)} differences, {len(unexplained)} unexplained", file=sys.stderr)
sys.exit(1 if unexplained else 0)
PYEOF
