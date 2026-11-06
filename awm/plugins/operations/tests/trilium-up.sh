#!/usr/bin/env bash
# Start a throwaway Trilium for the board tests and print an ETAPI token.
# Usage: trilium-up.sh [port]   ->   prints "http://127.0.0.1:<port> <token>"
#        trilium-up.sh --down
set -euo pipefail
NAME=awm-queue-trilium
IMAGE=triliumnext/trilium@sha256:d87d810fdcb549db65710a40933acaa16bc75c9426a9164c547f7ba95592941b
PASSWORD=awm-queue-test
if [ "${1:-}" = "--down" ]; then docker rm -f "$NAME" >/dev/null 2>&1 || true; exit 0; fi
PORT=${1:-18080}
URL="http://127.0.0.1:$PORT"
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" -p "127.0.0.1:$PORT:8080" "$IMAGE" >/dev/null
for _ in $(seq 120); do curl -sf "$URL/api/setup/status" >/dev/null 2>&1 && break; sleep 1; done
curl -sf -X POST "$URL/api/setup/new-document" >/dev/null
for _ in $(seq 60); do
    curl -sf "$URL/api/setup/status" | grep -q '"isInitialized":true' && break; sleep 1
done
curl -sf -X POST "$URL/set-password" --data-urlencode "password1=$PASSWORD" --data-urlencode "password2=$PASSWORD" -o /dev/null || true
TOKEN=$(curl -sf -X POST "$URL/etapi/auth/login" -H 'Content-Type: application/json' -d "{\"password\":\"$PASSWORD\"}" | sed -n 's/.*"authToken":"\([^"]*\)".*/\1/p')
echo "$URL $TOKEN"
