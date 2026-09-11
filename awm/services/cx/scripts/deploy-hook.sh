#!/usr/bin/env bash
# Put the naming hook, and its settings entry, on every fleet node.
#   deploy-hook.sh --check   report drift, write nothing (exit 1 if any)
#   deploy-hook.sh           back up each stale target, then deploy
#
# Deploy this BEFORE the service. A node that gets the claim's rename without
# the hook names every session it hands out `claimed <noun>` and leaves it that
# way, which reads as deliberate and is worse than the name it replaced. A node
# with the hook and no service has nothing named `claimed ` to act on, so the
# hook returns immediately on every prompt.
#
# The hook is copied rather than symlinked into the awm tree, as the other
# three hooks are. It has to work on a node whose awm checkout does not carry
# this service yet, which is every node until the branch merges.
set -uo pipefail

SVC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$SVC/hooks/name_on_prompt.py"
DST=hooks/awm-cx-name-hook.py
NODES=(local capellaz miraz)
TS=$(date +%Y%m%d-%H%M%S)
CHECK=0
[[ ${1:-} == --check ]] && CHECK=1

on() {
  local node=$1; shift
  if [[ $node == local ]]; then bash -c "$*"
  else ssh -o ConnectTimeout=10 -o BatchMode=yes "$node" "$*"; fi
}

# Adds one UserPromptSubmit entry, keyed on the script path so a second run is
# a no-op and the other hooks on the event are left alone.
read -r -d '' PATCH <<'PYEOF'
import json, os, shutil, sys
p = os.path.expanduser("~/.claude/settings.json")
cmd = (f"python3 -S {os.path.expanduser('~/.claude/hooks/awm-cx-name-hook.py')} "
       "2>/dev/null || true")
s = json.load(open(p))
hooks = s.setdefault("hooks", {}).setdefault("UserPromptSubmit", [])
for entry in hooks:
    for h in entry.get("hooks", []):
        if "awm-cx-name-hook.py" in (h.get("command") or ""):
            print("ok" if h["command"] == cmd else "drift")
            sys.exit(0)
if os.environ.get("CX_CHECK") == "1":
    print("absent"); sys.exit(0)
shutil.copy(p, os.path.expanduser(f"~/.claude/backups/settings.json.{sys.argv[1]}"))
hooks.append({"hooks": [{"type": "command", "command": cmd, "timeout": 5}]})
tmp = p + ".tmp"
with open(tmp, "w") as fh:
    json.dump(s, fh, indent=2)
    fh.write("\n")
os.replace(tmp, p)
print("added")
PYEOF

rc=0
reachable=()
for node in "${NODES[@]}"; do
  if on "$node" true >/dev/null 2>&1; then reachable+=("$node")
  else echo "UNREACHABLE  $node"; rc=1; fi
done

want=$(md5sum "$SRC" 2>/dev/null | cut -d' ' -f1)
if [[ -z $want ]]; then echo "MISSING IN REPO  $SRC"; exit 1; fi

echo "$SRC -> ~/.claude/$DST"
for node in "${reachable[@]}"; do
  got=$(on "$node" "md5sum ~/.claude/$DST 2>/dev/null | cut -d' ' -f1")
  if [[ $got == "$want" ]]; then
    printf '  %-10s ok\n' "$node"
  else
    state=absent; [[ -n $got ]] && state=drift
    if (( CHECK )); then printf '  %-10s %s\n' "$node" "$state"; rc=1; continue; fi
    on "$node" "mkdir -p ~/.claude/hooks ~/.claude/backups
                test -f ~/.claude/$DST && cp ~/.claude/$DST ~/.claude/backups/${DST//\//_}.$TS
                true"
    if [[ $node == local ]]; then cp "$SRC" "$HOME/.claude/$DST"
    else scp -q -o ConnectTimeout=10 "$SRC" "$node:.claude/$DST"; fi
    on "$node" "chmod +x ~/.claude/$DST"
    printf '  %-10s deployed (was %s)\n' "$node" "$state"
  fi
done

echo "settings.json UserPromptSubmit entry"
for node in "${reachable[@]}"; do
  out=$(printf '%s' "$PATCH" | on "$node" "mkdir -p ~/.claude/backups; CX_CHECK=$CHECK python3 - $TS")
  printf '  %-10s %s\n' "$node" "${out:-failed}"
  [[ $out == ok || $out == added ]] || rc=1
done

exit $rc
