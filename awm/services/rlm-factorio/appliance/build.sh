#!/bin/bash
# Build the appliance image (host + seat in one).
#
# The full Factorio build is behind factorio.com account auth, so this is an
# explicit, credentialed step rather than a side effect of `acquire`: the realm
# service only builds through here, and only when the image is missing.
#
# Credentials come from a file of shell assignments --
#     FACTORIO_USER=<factorio.com username>
#     FACTORIO_TOKEN=<service token from that account>
# -- at $FACTORIO_CREDS (default ./secrets/factorio_creds, gitignored). They
# reach the build ONLY as a BuildKit secret mount, so they never land in a layer
# and never appear in the build log. The token is the same one a desktop install
# keeps in player-data.json.
set -euo pipefail

cd "$(dirname "$0")"

CREDS="${FACTORIO_CREDS:-$PWD/secrets/factorio_creds}"
if [[ ! -f "$CREDS" ]]; then
    cat >&2 <<MSG
build: no factorio.com credentials at $CREDS

Create it (mode 600) with:
    FACTORIO_USER=<username>
    FACTORIO_TOKEN=<service token>
or point FACTORIO_CREDS at an existing file.
MSG
    exit 78
fi

export FACTORIO_CREDS="$CREDS"
export DOCKER_BUILDKIT=1
exec docker compose -f docker-compose.yml build "$@"
