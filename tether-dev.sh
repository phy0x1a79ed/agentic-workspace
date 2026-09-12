#!/usr/bin/env bash
# Drive the tether operator daemon from this worktree, before the branch is
# promoted and `awm tether <verb>` exists on this node.
#
#   bash tether-dev.sh invite
#   bash tether-dev.sh status
#   bash tether-dev.sh run    'df -h /'
#   bash tether-dev.sh say    'about to check the disk'
#   bash tether-dev.sh shell                 # open a terminal
#   bash tether-dev.sh keys   '2:ls -la'     # type at task 2, with a newline
#   bash tether-dev.sh keys   '2:^C'         # ctrl-C at task 2
#   bash tether-dev.sh close  '2'
#   bash tether-dev.sh tasks
#   bash tether-dev.sh drain                 # everything so far, as text
#   bash tether-dev.sh watch                 # follow it live
#   bash tether-dev.sh cut    'all done'
#   bash tether-dev.sh stop
#
# The verbs are named to match the awm ones deliberately, so promoting the
# branch is a rename of the driver rather than a redesign of the surface.
#
# `run` no longer waits: the daemon answers with a task id and the output
# arrives on its event stream. This script does the second half for you, which
# is the ergonomics the awm surface deliberately does not bend for — an agent
# wants two calls and a transcript, a person at a terminal wants one command
# and an answer.
#
# It starts the daemon when one is not already up and reuses it when one is, so
# every verb lands in the same process and a session survives between calls.
#
# Delete this file once the branch is promoted. The awm verbs replace it.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SOCK="${TETHER_CONTROL_SOCKET:-$HOME/.tether-dev.sock}"
LOG="$HOME/.tether-dev.log"
ENV_FILE="$HOME/agentic_workspace/.awm/env"
DAEMON="$HERE/awm/services/tether/rust/target/release/tether-operator"
HELPER="$HERE/awm/services/tether/dev-client.py"

VERB="${1:-status}"
ARG="${2:-}"

if [ "$VERB" = stop ]; then
    pkill -f 'tether-operator' 2>/dev/null || true
    rm -f "$SOCK"
    echo "daemon stopped"
    exit 0
fi

[ -x "$DAEMON" ] || {
    echo "no daemon binary at $DAEMON" >&2
    echo "build it: cargo build --release --manifest-path $HERE/awm/services/tether/rust/Cargo.toml" >&2
    exit 1
}

# A socket that answers means a daemon is already up. One that refuses is a
# corpse, and the daemon unlinks it on its own at startup.
if ! python3 "$HELPER" "$SOCK" ask status >/dev/null 2>&1; then
    # The bearer reaches the daemon through its environment rather than through
    # an argument, so it never appears in a process listing on this box.
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
    TETHER_CONTROL_SOCKET="$SOCK" TETHER_WHO="${TETHER_WHO:-awm as tony}" \
        setsid nohup "$DAEMON" >>"$LOG" 2>&1 &
    for _ in $(seq 1 40); do [ -S "$SOCK" ] && break; sleep 0.25; done
    [ -S "$SOCK" ] || { echo "the daemon never bound $SOCK; see $LOG" >&2; exit 1; }
fi

exec python3 "$HELPER" "$SOCK" "$VERB" "$ARG"
