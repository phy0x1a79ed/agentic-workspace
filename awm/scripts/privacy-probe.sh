#!/usr/bin/env bash
# Check from this node that <project> on <peer> is hidden from (or visible to) peer calls.
set -euo pipefail

usage() {
  echo "usage: $(basename "$0") <peer> <project> [--expect visible|hidden] [--post]" \
       "[--control PROJECT] [--strict-mentions] [--allow-hit SOURCE:ID]... [--strict] [--skip N]..." \
       "[--doc-id ID] [--scope S] [--file PATH] [--term T]... [--no-ssh] [--wait S]" >&2
  exit 2
}
[[ $# -ge 2 && $1 != -* && $2 != -* ]] || usage

probe="$(dirname "$(readlink -f "$0")")/privacy-probe.py"
if python -c 'import awm.gatewayclient' 2>/dev/null; then
  exec python "$probe" "$@"
fi
exec mamba run -n awm --no-capture-output python "$probe" "$@"
