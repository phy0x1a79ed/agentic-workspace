#!/bin/bash
# Role switch for the one appliance image. See Dockerfile for why both roles
# ship in the same image.
set -euo pipefail

case "${FACTORIO_ROLE:-host}" in
    host)
        exec python3 -u /factorio/supervise.py "$@"
        ;;
    seat)
        exec /factorio/seat.sh "$@"
        ;;
    shell)
        # Escape hatch for probing a live image by hand.
        exec "$@"
        ;;
    *)
        echo "entrypoint: unknown FACTORIO_ROLE=${FACTORIO_ROLE}" >&2
        exit 64
        ;;
esac
