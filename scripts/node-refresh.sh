#!/usr/bin/env bash
# Refresh this node's awm onto a release commit, with no agent (routines.md R32):
#   node-refresh.sh <release sha, 40 hex>        (or $AWM_REFRESH_TO)
#
# Steps: sync (scripts/refresh-checkout.sh), deploy (scripts/awm-deploy.sh), health
# (every enabled service ready, no plugin refused). They run in a transient user unit,
# awm-refresh-<sha 12>, so the awm restart that deploy causes cannot kill them, and a
# caller that dies or drops its ssh leaves them running.
#
# The result goes to <AWM_DIR>/state/refresh/<sha>.json, the steps' output to <sha>.log
# beside it. A second call for the same sha waits for the unit that runs, or answers from
# the result, and never runs the steps twice. Delete the result to run them again.
# The last line of output is `AWM-REFRESH-RESULT <result json>`, which the deploy reads.
#
# The body is one function, called on the last line: sync rewrites this file while
# bash reads it.
main() {
    set -uo pipefail
    local to=${1:-${AWM_REFRESH_TO:-}}
    [[ $to =~ ^[0-9a-f]{40}$ ]] || { echo "usage: node-refresh.sh <release sha, 40 hex>" >&2; return 2; }
    root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
    export AWM_DIR=${AWM_DIR:-$root/.awm}
    local conda=${AWM_CONDA_ROOT:-$HOME/miniforge3}
    export PATH=$HOME/.local/bin:$conda/condabin:$conda/bin:$PATH
    dir=$AWM_DIR/state/refresh
    result=$dir/$to.json
    log=$dir/$to.log
    unit=awm-refresh-${to:0:12}
    mkdir -p "$dir"
    if [[ -n ${AWM_REFRESH_IN_UNIT:-} ]]; then
        steps "$to"
    else
        launch "$to"
    fi
}

launch() {
    local to=$1
    if [[ ! -s $result ]] && ! running; then
        rm -f "$log"
        local env=(AWM_REFRESH_IN_UNIT=1 PATH="$PATH" AWM_DIR="$AWM_DIR" HOME="$HOME")
        local v
        for v in AWM_WORKSPACE AWM_NODE_NAME MAMBA_ROOT_PREFIX MAMBA_EXE AWM_CONDA_ROOT; do
            [[ -n ${!v:-} ]] && env+=("$v=${!v}")
        done
        if command -v systemd-run >/dev/null && systemctl --user show-environment >/dev/null 2>&1; then
            local args=(--user --unit "$unit" --collect --quiet --working-directory "$root")
            for v in "${env[@]}"; do args+=(--setenv "$v"); done
            systemd-run "${args[@]}" bash "$root/scripts/node-refresh.sh" "$to" \
                || fail_launch "$to" "systemd-run could not start $unit"
        else
            # No user manager to hold a unit: run the steps in this process.
            env "${env[@]}" bash "$root/scripts/node-refresh.sh" "$to"
        fi
    fi
    local budget=${AWM_REFRESH_BUDGET:-1800} t0=$SECONDS
    while running; do
        if (( SECONDS - t0 > budget )); then
            echo "$unit still runs after ${budget}s; its log is $log" >&2
            return 1
        fi
        sleep 2
    done
    [[ -s $result ]] || fail_launch "$to" "$unit ended without a result"
    [[ -f $log ]] && tail -n 40 "$log"
    printf 'AWM-REFRESH-RESULT %s\n' "$(tr -d '\n' < "$result")"
    grep -q '"ok": true' "$result"
}

running() { systemctl --user is-active --quiet "$unit" 2>/dev/null; }

fail_launch() {
    printf 'launch: %s\n' "$2" >> "$log"
    write_result "$1" false launch "[]"
}

steps() {
    local to=$1 done="" step code t0
    exec >> "$log" 2>&1
    for step in sync deploy health; do
        echo "== $step $(date -u +%FT%TZ)"
        t0=$SECONDS
        "step_$step" "$to"
        code=$?
        done+="${done:+, }{\"step\": \"$step\", \"code\": $code, \"seconds\": $((SECONDS - t0))}"
        if (( code != 0 )); then
            echo "== $step failed with exit $code"
            write_result "$to" false "$step" "[$done]"
            return 1
        fi
    done
    write_result "$to" true "" "[$done]"
}

step_sync() { (cd "$root" && AWM_REFRESH_TO=$1 sh scripts/refresh-checkout.sh); }

step_deploy() { (cd "$root" && bash scripts/awm-deploy.sh); }

# awm deploy already waits for the services; this asks the gateway again, as a check
# that does not trust the step it checks.
step_health() {
    local budget=${AWM_REFRESH_HEALTH_BUDGET:-120} t0=$SECONDS why
    while true; do
        why=$(health_problems) && return 0
        if (( SECONDS - t0 > budget )); then
            echo "not healthy after ${budget}s: $why"
            return 1
        fi
        sleep 3
    done
}

health_problems() {
    local services plugins
    services=$(awm services 2>&1) || { echo "awm services: $services"; return 1; }
    plugins=$(awm plugins 2>&1) || { echo "awm plugins: $plugins"; return 1; }
    python3 -I -c '
import json, sys
services, plugins = json.loads(sys.argv[1]), json.loads(sys.argv[2])
bad = ["{} ({})".format(s.get("name"), s.get("status")) for s in services
       if s.get("enabled") and not s.get("external") and s.get("status") != "ready"]
bad += ["refused plugin {}: {}".format(r.get("plugin"), r.get("reason"))
        for r in plugins.get("refused") or []]
print(", ".join(bad))
sys.exit(1 if bad else 0)' "$services" "$plugins"
}

write_result() {
    local to=$1 ok=$2 failed=$3 steps=$4 node=${AWM_NODE_NAME:-$(hostname -s)}
    printf '{"node": "%s", "to": "%s", "ok": %s, "failed_step": "%s", "steps": %s, "log": "%s", "finished": "%s"}\n' \
        "$node" "$to" "$ok" "$failed" "$steps" "$log" "$(date -u +%FT%TZ)" > "$result.tmp"
    mv "$result.tmp" "$result"
}

main "$@"; exit $?
