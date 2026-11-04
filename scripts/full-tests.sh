#!/usr/bin/env bash
# The nightly full suite (docs/redux/routines.md R30): run every dist, then fail
# only on a dist that newly fails. The known failures live per checkout in
# .awm/full-tests/known-failures as "<dist> <commit>" lines, the commit being
# the first one seen failing.
set -uo pipefail

ROOT="$(git rev-parse --show-toplevel)"
STATE="$ROOT/.awm/full-tests"
KNOWN="$STATE/known-failures"
mkdir -p "$STATE"
touch "$KNOWN"
HEAD="$(git -C "$ROOT" rev-parse HEAD)"
SUMMARY="$(mktemp)"
trap 'rm -f "$SUMMARY"' EXIT

PYTEST_ARGS="${PYTEST_ARGS:--q}" "$ROOT/scripts/run-tests.sh" --summary "$SUMMARY" >"$STATE/last.log" 2>&1

failing="$(awk '$2 == "FAIL" {print $1}' "$SUMMARY" | sort -u)"
new=()
next=()
for dist in $failing; do
  seen="$(awk -v d="$dist" '$1 == d {print $2}' "$KNOWN")"
  if [ -n "$seen" ]; then
    next+=("$dist $seen")
  else
    new+=("$dist")
    next+=("$dist $HEAD")
  fi
done
printf '%s\n' "${next[@]+"${next[@]}"}" | sed '/^$/d' > "$KNOWN"
cp "$SUMMARY" "$STATE/last-summary"

echo "commit $HEAD"
echo "known failures: $(cut -d' ' -f1 "$KNOWN" | tr '\n' ' ')"
if [ "${#new[@]}" -gt 0 ]; then
  echo "new failures: ${new[*]}"
  echo "log: $STATE/last.log"
  for dist in "${new[@]}"; do
    grep -A40 ">>> $dist " "$STATE/last.log" | grep -E "FAILED|Error" | head -20
  done
  exit 1
fi
echo "no new failures"
