#!/usr/bin/env bash
# Run every dist's test suite, each with the correct PYTHONPATH.
#
# This repo is a nested tree of pip dists that all merge into the PEP 420
# `awm` namespace. The `awm` namespace can only resolve ONE dist's source
# roots per interpreter, so a single `pytest` over the whole tree can't
# import everything at once. Instead we invoke pytest once per dist, putting
# that dist's source root + the shared components (config / persistence /
# gatewayclient) on PYTHONPATH. We deliberately do NOT add other dists'
# source roots: two roots both providing the `awm` namespace make Python's
# PEP 420 finder resolve `awm` to one of them, shadowing the other dist's
# submodules. Cross-dist imports in tests (rare) must therefore be lazy
# (inside a fixture/function), never at module top level.
#
# Usage:
#   awm/gateway/scripts/run-tests.sh                 # run all dists
#   awm/gateway/scripts/run-tests.sh scopes gateway  # run only the named dists
#   PYTEST_ARGS="-x -q" awm/gateway/scripts/run-tests.sh   # pass extra args to pytest
#
# Reports pass/fail per dist and exits non-zero if any dist failed.
set -uo pipefail

# This script lives at awm/gateway/scripts/run-tests.sh; the dist roots below
# are resolved from the repo/clone root (git toplevel), not the script's dir.
WS="$(git -C "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" rev-parse --show-toplevel)"
cd "$WS"

COMP="$WS/awm/service_components/config:$WS/awm/service_components/persistence:$WS/awm/service_components/gatewayclient"

# dist name -> "<source-root-dir>::<test-dir>"
# agentcore is a leaf component (no service folder) — it has its own source
# root + tests/ like the feature dists; it ships in the same per-dist runner.
declare -A DISTS=(
  [gateway]="$WS/awm/gateway::$WS/awm/gateway/tests"
  [agentcore]="$WS/awm/service_components/agentcore::$WS/awm/service_components/agentcore/tests"
  [gatewayclient]="$WS/awm/service_components/gatewayclient::$WS/awm/service_components/gatewayclient/tests"
  [telemetry]="$WS/awm/service_components/telemetry::$WS/awm/service_components/telemetry/tests"
  [scopes]="$WS/awm/services/scopes::$WS/awm/services/scopes/tests"
  [workspace]="$WS/awm/services/workspace::$WS/awm/services/workspace/tests"
  [agents]="$WS/awm/services/agents::$WS/awm/services/agents/awm/tests"
  [artifacts]="$WS/awm/services/artifacts::$WS/awm/services/artifacts/tests"
  [writing]="$WS/awm/services/writing::$WS/awm/services/writing/tests"
  [precedence]="$WS/awm/services/precedence::$WS/awm/services/precedence/tests"
  [social]="$WS/awm/services/social::$WS/awm/services/social/tests"
  [2fa]="$WS/awm/services/2fa::$WS/awm/services/2fa/tests"
  [rlm-browser]="$WS/awm/services/rlm-browser::$WS/awm/services/rlm-browser/tests"
  [orchestrator]="$WS/awm/services/orchestrator::$WS/awm/services/orchestrator/tests"
  [graphify]="$WS/awm/services/graphify::$WS/awm/services/graphify/tests"
  [stt]="$WS/awm/services/stt::$WS/awm/services/stt/awm/stt/tests"
  [tts]="$WS/awm/services/tts::$WS/awm/services/tts/awm/tts/tests"
  [fileviewer]="$WS/awm/services/fileviewer::$WS/awm/services/fileviewer/tests"
)

# Stable run order.
ORDER=(gateway agentcore gatewayclient telemetry scopes workspace agents artifacts writing precedence social 2fa rlm-browser orchestrator graphify stt tts fileviewer)

# Allow selecting a subset on the command line.
if [ "$#" -gt 0 ]; then
  ORDER=("$@")
fi

PYTEST_ARGS="${PYTEST_ARGS:-}"

declare -A RESULT
overall=0

for name in "${ORDER[@]}"; do
  spec="${DISTS[$name]:-}"
  if [ -z "$spec" ]; then
    echo ">>> unknown dist: $name (known: ${!DISTS[*]})" >&2
    overall=1
    continue
  fi
  src="${spec%%::*}"
  testdir="${spec##*::}"

  echo
  echo "============================================================"
  echo ">>> $name  ($testdir)"
  echo "============================================================"

  PYTHONPATH="$src:$COMP" \
    mamba run -n awm --no-capture-output \
    python -m pytest "$testdir" -p no:cacheprovider ${PYTEST_ARGS}
  rc=$?
  if [ "$rc" -eq 0 ]; then
    RESULT[$name]="PASS"
  elif [ "$rc" -eq 5 ]; then
    RESULT[$name]="NO TESTS"
  else
    RESULT[$name]="FAIL (rc=$rc)"
    overall=1
  fi
done

echo
echo "============================================================"
echo ">>> SUMMARY"
echo "============================================================"
for name in "${ORDER[@]}"; do
  printf '  %-12s %s\n' "$name" "${RESULT[$name]:-?}"
done

exit "$overall"
