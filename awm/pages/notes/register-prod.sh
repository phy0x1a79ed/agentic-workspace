#!/usr/bin/env bash
# Register (or re-register) the built notes page on a running awm gateway.
#
# Page bases are NOT journaled by the gateway supervisor (only kind="service"
# is). The notes *service* respawns on gateway restart; the notes *page* does
# not — its /ui/notes base is dropped whenever the gateway restarts. Run this
# after a gateway restart to bring the page back at /ui/notes.
#
# Usage:  bash register-prod.sh [PORT]     (PORT defaults to 7819 = prod)
set -euo pipefail

PORT="${1:-7819}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIST="$HERE/dist"

if [[ ! -d "$DIST" ]]; then
  echo "no dist/ — build first:  (cd $HERE/../.. && mamba run -n awm npm run build)" >&2
  exit 1
fi

code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/ui/notes/")
if [[ "$code" == "200" ]]; then
  echo "already serving /ui/notes on :$PORT (HTTP $code) — nothing to do"
  exit 0
fi

curl -s -X POST "http://127.0.0.1:$PORT/hub/register" \
  -H 'content-type: application/json' \
  -d "{\"name\":\"notes\",\"prefix\":\"/ui/notes\",\"page\":{\"dir\":\"$DIST\"}}" \
  >/dev/null

code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/ui/notes/")
echo "registered notes page on :$PORT → /ui/notes/ (HTTP $code)"
