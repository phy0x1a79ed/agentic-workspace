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
#   scripts/run-tests.sh                 # run all dists
#   scripts/run-tests.sh scopes contract # run only the named dists
#   PYTEST_ARGS="-x -q" scripts/run-tests.sh   # pass extra args to pytest
#   scripts/run-tests.sh --changed <base>  # only dists touched since <base>
#   scripts/run-tests.sh --smoke           # only tests marked smoke
#   scripts/run-tests.sh --summary <file>  # also write "<dist> <result>" lines
#
# The deploy check runs `--smoke --changed $AWM_DEPLOY_BASE` (deploy.toml). A
# change to a shared component selects every dist, since each one imports it.
#
# Reports pass/fail per dist and exits non-zero if any dist failed.
set -uo pipefail

WS="$(git -C "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" rev-parse --show-toplevel)"
cd "$WS"

COMP="$WS/awm/service_components/grantlib:$WS/awm/service_components/config:$WS/awm/service_components/persistence:$WS/awm/service_components/gatewayclient:$WS/awm/service_components/kept:$WS/awm/service_components/usage:$WS/awm/service_components/trustpolicy:$WS/awm/service_components/tlsfront:$WS/awm/service_components/dvcdata"

# dist name -> "<source-root-dir>::<test-dir>"
declare -A DISTS=(
  [contract]="$WS/test/contract::$WS/test/contract"
  [gatewayclient]="$WS/awm/service_components/gatewayclient::$WS/awm/service_components/gatewayclient/tests"
  [config]="$WS/awm/service_components/config::$WS/awm/service_components/config/tests"
  [persistence]="$WS/awm/service_components/persistence::$WS/awm/service_components/persistence/tests"
  [telemetry]="$WS/awm/service_components/telemetry::$WS/awm/service_components/telemetry/tests"
  [usage]="$WS/awm/service_components/usage::$WS/awm/service_components/usage/tests"
  [kept]="$WS/awm/service_components/kept::$WS/awm/service_components/kept/tests"
  [tlsfront]="$WS/awm/service_components/tlsfront::$WS/awm/service_components/tlsfront/tests"
  [dvcdata]="$WS/awm/service_components/dvcdata::$WS/awm/service_components/dvcdata/tests"
  [tether]="$WS/awm/service_components/tether::$WS/awm/service_components/tether/tests"
  [scopes]="$WS/awm/services/scopes::$WS/awm/services/scopes/tests"
  [journal]="$WS/awm/services/journal::$WS/awm/services/journal/tests"
  [search]="$WS/awm/services/search::$WS/awm/services/search/tests"
  [social]="$WS/awm/services/social::$WS/awm/services/social/tests"
  [2fa]="$WS/awm/services/2fa::$WS/awm/services/2fa/tests"
  [ssh]="$WS/awm/services/ssh::$WS/awm/services/ssh/tests"
  [rlm-browser]="$WS/awm/services/rlm-browser::$WS/awm/services/rlm-browser/tests"
  [rlm-factorio]="$WS/awm/services/rlm-factorio::$WS/awm/services/rlm-factorio/tests"
  [vpn]="$WS/awm/services/vpn::$WS/awm/services/vpn/tests"
  [reflection]="$WS/awm/services/reflection::$WS/awm/services/reflection/tests"
  [claude-science]="$WS/awm/services/claude-science::$WS/awm/services/claude-science/tests"
  [engcyc]="$WS/awm/services/engcyc::$WS/awm/services/engcyc/tests"
  [trilium]="$WS/awm/services/trilium::$WS/awm/services/trilium/tests"
  [zotero]="$WS/awm/services/zotero::$WS/awm/services/zotero/tests"
  [channel]="$WS/awm/plugins/channel:$WS/awm/plugins/relay:$WS/awm/service_components/tether::$WS/awm/plugins/channel/tests"
  [relay]="$WS/awm/plugins/relay:$WS/awm/service_components/tether::$WS/awm/plugins/relay/tests"
  [kb]="$WS/awm/services/kb::$WS/awm/services/kb/tests"
  [board]="$WS/awm/services/board::$WS/awm/services/board/tests"
  [transcripts]="$WS/awm/services/transcripts::$WS/awm/services/transcripts/tests"
  [grantlib]="$WS/awm/service_components/grantlib::$WS/awm/service_components/grantlib/tests"
  [trustpolicy]="$WS/awm/service_components/trustpolicy::$WS/awm/service_components/trustpolicy/tests"
  [browser]="$WS/awm/plugins/browser::$WS/awm/plugins/browser/tests"
  [wake]="$WS/awm/plugins/wake::$WS/awm/plugins/wake/tests"
)

# Stable run order.
ORDER=(contract gatewayclient config persistence grantlib telemetry usage kept tlsfront dvcdata tether scopes journal search social 2fa ssh rlm-browser rlm-factorio vpn reflection claude-science engcyc trilium zotero channel relay kb board transcripts trustpolicy browser wake)

CHANGED_BASE=""
SMOKE=0
SUMMARY_FILE=""
NAMED=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --changed) CHANGED_BASE="${2:?--changed needs a base commit}"; shift 2 ;;
    --smoke) SMOKE=1; shift ;;
    --summary) SUMMARY_FILE="${2:?--summary needs a file}"; shift 2 ;;
    *) NAMED+=("$1"); shift ;;
  esac
done

# Allow selecting a subset on the command line.
if [ "${#NAMED[@]}" -gt 0 ]; then
  ORDER=("${NAMED[@]}")
fi

# owns <dist> <path>: the path lies under one of the dist's source or test dirs.
owns() {
  local spec="${DISTS[$1]}" dir
  local dirs="${spec%%::*}:${spec##*::}"
  IFS=: read -ra parts <<< "$dirs"
  for dir in "${parts[@]}"; do
    case "$WS/$2" in "$dir"/*) return 0 ;; esac
  done
  return 1
}

if [ -n "$CHANGED_BASE" ]; then
  mapfile -t CHANGED < <(git diff --name-only "$CHANGED_BASE" HEAD)
  SELECTED=()
  shared=0
  for path in "${CHANGED[@]}"; do
    IFS=: read -ra comp <<< "$COMP"
    for dir in "${comp[@]}"; do
      case "$WS/$path" in "$dir"/*) shared=1 ;; esac
    done
  done
  for name in "${ORDER[@]}"; do
    if [ "$shared" -eq 1 ]; then SELECTED+=("$name"); continue; fi
    for path in "${CHANGED[@]}"; do
      if owns "$name" "$path"; then SELECTED+=("$name"); break; fi
    done
  done
  if [ "${#SELECTED[@]}" -eq 0 ]; then
    echo ">>> no dist changed since $CHANGED_BASE"
    [ -n "$SUMMARY_FILE" ] && : > "$SUMMARY_FILE"
    exit 0
  fi
  ORDER=("${SELECTED[@]}")
fi

PYTEST_ARGS="${PYTEST_ARGS:-}"
if [ "$SMOKE" -eq 1 ]; then
  PYTEST_ARGS="-m smoke ${PYTEST_ARGS}"
fi

# Ask mamba where the env's interpreter is, once, and run pytest with it
# directly. `mamba run --no-capture-output <cmd>` writes `exec -- <cmd>` into a
# script it then runs under bash, and bash's `exec` rejects `--` — so on mamba
# 2.5.0 every dist fails identically before pytest is ever reached, which reads
# like a repo breakage and is not one.
# `mamba run` appends a blank line of its own on some builds, so drop empties
# before taking the last one — otherwise `tail -1` returns "" and every dist is
# skipped with a message that reads like a broken env.
# A service's PATH may hold the env's bin but not condabin, so look for mamba
# under the conda root too before giving up.
conda_root="${AWM_CONDA_ROOT:-$HOME/miniforge3}"
MAMBA="$(command -v mamba 2>/dev/null || true)"
for m in "$MAMBA" "${MAMBA_EXE:-}" "$conda_root/condabin/mamba" "$conda_root/bin/mamba"; do
  if [ -n "$m" ] && [ -x "$m" ]; then MAMBA="$m"; break; fi
done
PY=""
if [ -n "$MAMBA" ] && [ -x "$MAMBA" ]; then
  PY="$("$MAMBA" run -n awm python -c 'import sys; print(sys.executable)' 2>/dev/null | grep -v '^[[:space:]]*$' | tail -1)"
fi
if [ ! -x "$PY" ]; then
  echo "could not resolve the awm env's python (got '${PY:-<empty>}')" >&2
  exit 1
fi

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
    "$PY" -m pytest "$testdir" -p no:cacheprovider ${PYTEST_ARGS}
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
if [ -n "$SUMMARY_FILE" ]; then
  for name in "${ORDER[@]}"; do
    printf '%s %s\n' "$name" "${RESULT[$name]:-?}"
  done > "$SUMMARY_FILE"
fi

exit "$overall"
