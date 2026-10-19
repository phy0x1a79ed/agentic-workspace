#!/usr/bin/env bash
# Check from this node that <project> on <peer> is hidden from (or visible to) peer calls.
set -euo pipefail

usage() {
  echo "usage: $(basename "$0") <peer> <project> [--expect visible|hidden] [--post]" \
       "[--doc-id ID] [--scope S] [--file PATH] [--term T]... [--no-ssh] [--wait S]" >&2
  exit 2
}
[[ $# -ge 2 && $1 != -* && $2 != -* ]] || usage

exec mamba run -n awm --no-capture-output python "$(dirname "$(readlink -f "$0")")/privacy-probe.py" "$@"
