#!/usr/bin/env bash
# Put every awm Claude Code hook, and its settings.json entries, on its nodes.
#   deploy-hooks.sh --check   report drift, write nothing (exit 1 if any)
#   deploy-hooks.sh           back up each stale target, then deploy
#
# Deploy the hooks BEFORE the services they pair with: a node that runs the
# service without its hook behaves worse than one that has neither (the cx
# naming hook is the sharp case: `claimed <noun>` stays as a session's name).
#
# Hooks are copied, never symlinked into the awm tree: a copy works on a node
# whose checkout does not carry the hook yet, and its md5 is checkable.
#
# `local` is the node this runs on. Capella refuses every peer, so it runs this
# itself. Reporting runs on the fleet nodes only. mira is a station and gets none
# of it.
set -uo pipefail

AWM="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TS=$(date +%Y%m%d-%H%M%S)
CHECK=0
[[ ${1:-} == --check ]] && CHECK=1

# name | source under awm/ | file in ~/.claude/hooks | nodes | extra file (same dir as source)
HOOKS=(
  "cx-name|services/cx/hooks/name_on_prompt.py|awm-cx-name-hook.py|local miraz|"
  "compute|services/compute/hooks/compute_hook.py|awm-compute-hook.py|local|"
  "reflection|services/reflection/hooks/plan_mode_hook.py|awm-reflection-mode.py|local|"
  "reporting|services/reporting/hooks/reporting_hook.py|awm-reporting-hook.py|local|awm-reporting-settings.json"
)

# settings.json entries per hook, as {event: entry}; @file reads the "hooks" of a shipped
# settings file. $HOME becomes the node's home.
declare -A ENTRIES=(
  [cx-name]='{"UserPromptSubmit":{"hooks":[{"type":"command","command":"python3 -S $HOME/.claude/hooks/awm-cx-name-hook.py 2>/dev/null || true","timeout":5}]}}'
  [compute]='{"PreToolUse":{"matcher":"Bash","hooks":[{"type":"command","command":"python3 -S $HOME/.claude/hooks/awm-compute-hook.py 2>/dev/null || true","timeout":5,"statusMessage":"Compute watchdog"}]},"PostToolUse":{"matcher":"Bash","hooks":[{"type":"command","command":"python3 -S $HOME/.claude/hooks/awm-compute-hook.py 2>/dev/null || true","timeout":5,"statusMessage":"Compute watchdog"}]}}'
  [reflection]='{"PostToolUse":{"matcher":"ExitPlanMode","hooks":[{"type":"command","command":"python3 -S $HOME/.claude/hooks/awm-reflection-mode.py 2>/dev/null || true","timeout":20}]}}'
  [reporting]='@awm-reporting-settings.json'
)

on() {
  local node=$1; shift
  if [[ $node == local ]]; then bash -c "$*"
  else ssh -o ConnectTimeout=10 -o BatchMode=yes "$node" "$*"; fi
}

# Per event: add the entry, or replace a drifted one, keyed on the script name so a
# second run is a no-op and the other hooks on the event are left alone.
read -r -d '' PATCH <<'PYEOF'
import base64, json, os, shutil, sys
p = os.path.expanduser("~/.claude/settings.json")
key = sys.argv[2]
want = json.loads(base64.b64decode(sys.argv[3]).decode().replace("$HOME", os.path.expanduser("~")))
s = json.load(open(p))
out, changed = [], False
for event, entry in want.items():
    lst = s.setdefault("hooks", {}).setdefault(event, [])
    at = next((i for i, e in enumerate(lst) if any(key in (h.get("command") or "") for h in e.get("hooks", []))), None)
    if at is not None and lst[at] == entry:
        out.append(event + ":ok"); continue
    out.append(event + (":absent" if at is None else ":drift"))
    if os.environ.get("HOOK_CHECK") != "1":
        out[-1] = event + (":added" if at is None else ":replaced")
        if at is None: lst.append(entry)
        else: lst[at] = entry
        changed = True
if changed:
    shutil.copy(p, os.path.expanduser(f"~/.claude/backups/settings.json.{sys.argv[1]}"))
    tmp = p + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(s, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, p)
print(" ".join(out))
PYEOF

# Copies $1 to ~/.claude/hooks/$2 on $3 through a temp name, so a symlink there is replaced, never written through.
put() {
  local src=$1 dst=hooks/$2 node=$3 want got state
  want=$(md5sum "$src" | cut -d' ' -f1)
  got=$(on "$node" "md5sum ~/.claude/$dst 2>/dev/null | cut -d' ' -f1")
  if [[ $got == "$want" ]]; then printf '  %-10s %-28s ok\n' "$node" "$2"; return 0; fi
  state=absent; [[ -n $got ]] && state=drift
  if (( CHECK )); then printf '  %-10s %-28s %s\n' "$node" "$2" "$state"; return 1; fi
  on "$node" "mkdir -p ~/.claude/hooks ~/.claude/backups
              test -f ~/.claude/$dst && cp ~/.claude/$dst ~/.claude/backups/${2}.$TS
              true"
  if [[ $node == local ]]; then cp "$src" "$HOME/.claude/$dst.tmp"
  else scp -q -o ConnectTimeout=10 "$src" "$node:.claude/$dst.tmp"; fi
  on "$node" "chmod +x ~/.claude/$dst.tmp && mv -f ~/.claude/$dst.tmp ~/.claude/$dst"
  printf '  %-10s %-28s deployed (was %s)\n' "$node" "$2" "$state"
}

rc=0
declare -A up
for row in "${HOOKS[@]}"; do
  IFS='|' read -r name src dst nodes extra <<<"$row"
  [[ -f $AWM/$src ]] || { echo "MISSING IN REPO  $AWM/$src"; rc=1; continue; }
  entries=${ENTRIES[$name]}
  [[ $entries == @* ]] && entries=$(python3 -c 'import json,sys; print(json.dumps({e: v[0] for e, v in json.load(open(sys.argv[1]))["hooks"].items()}))' "$AWM/$(dirname "$src")/${entries#@}")
  echo "$name"
  for node in $nodes; do
    if [[ -z ${up[$node]:-} ]]; then on "$node" true >/dev/null 2>&1 && up[$node]=1 || up[$node]=0; fi
    if [[ ${up[$node]} == 0 ]]; then echo "  UNREACHABLE  $node"; rc=1; continue; fi
    put "$AWM/$src" "$dst" "$node" || rc=1
    [[ -n $extra ]] && { put "$AWM/$(dirname "$src")/$extra" "$extra" "$node" || rc=1; }
    out=$(printf '%s' "$PATCH" | on "$node" "mkdir -p ~/.claude/backups; HOOK_CHECK=$CHECK python3 - $TS $dst $(printf '%s' "$entries" | base64 -w0)")
    printf '  %-10s settings.json  %s\n' "$node" "${out:-failed}"
    [[ $out == *absent* || $out == *drift* || -z $out ]] && rc=1
  done
done
exit $rc
