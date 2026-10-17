"""Where tether's pieces live on a host, and which role this host plays.

One service folder runs on two kinds of box and they share no code path at
runtime: the operator's node supervises the operator daemon, the public host
supervises the relay. ``AWM_TETHER_ROLE`` decides, and defaults to operator
because that is what a workstation is.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from awm.config import AWM_DIR

#: This service's folder — the one holding run.sh, install.sh and RELEASE.
SERVICE_DIR = Path(__file__).resolve().parents[2]

#: Per-host state: the fetched binaries, the daemon's control socket, its log.
STATE_DIR = AWM_DIR / "services" / "tether"

#: Where this host's binaries live. fetch-release.sh pulls the pinned release
#: into STATE_DIR/bin on start, outside the checkout a deploy's clean of
#: untracked files would wipe. AWM_TETHER_BIN overrides it for a developer who
#: points it at a tether checkout's cargo target dir.
BIN_DIR = Path(os.environ.get("AWM_TETHER_BIN") or STATE_DIR / "bin")

OPERATOR_BIN = BIN_DIR / "tether-operator"
RELAY_BIN = BIN_DIR / "tether-relay"
OWNER_BIN = BIN_DIR / "tether"

#: The release this host is pinned to, versioned with the adapter, and the one
#: it has actually fetched. fetch-release.sh writes VERSION_FILE last, so a
#: VERSION that lags RELEASE means the most recent fetch did not complete.
RELEASE_FILE = SERVICE_DIR / "RELEASE"
VERSION_FILE = BIN_DIR / "VERSION"

#: What fetch-release.sh wrote about its last run, surfaced in `status` so a
#: failed fetch is a line on the screen rather than a silently stale binary.
FETCH_LOG = STATE_DIR / "fetch.log"

#: Set when a developer points BIN_DIR at their own build; the fetch then does
#: nothing and `status` says the version came from an override, not a release.
BIN_OVERRIDE = os.environ.get("AWM_TETHER_BIN") or None


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip() or None
    except OSError:
        return None


def pinned_release() -> str | None:
    """The tag RELEASE pins, versioned with this adapter."""
    return _read(RELEASE_FILE)


def installed_release() -> str | None:
    """The tag fetch-release.sh last stamped into BIN_DIR, if any."""
    return _read(VERSION_FILE)

#: The operator daemon's control socket. Local, unauthenticated, and reachable
#: only by this user: the adapter is the only thing that speaks to it, and a
#: socket keeps one warm connection open across many verbs.
CONTROL_SOCKET = STATE_DIR / "operator.sock"

DAEMON_LOG = STATE_DIR / "daemon.log"

#: The transient user unit the operator daemon runs in. Named after STATE_DIR
#: so a dev shadow, which has its own state root, never adopts or stops the
#: daemon a deployed gateway is keeping.
OPERATOR_UNIT = "awm-tether-operator-" + hashlib.sha1(str(STATE_DIR).encode()).hexdigest()[:8]

#: What the adapter last launched: pid, release and when. Read back to decide
#: whether a daemon that is already answering is the one the pin asks for.
LAUNCH_FILE = STATE_DIR / "daemon.json"

#: The daemon's environment, handed to systemd as an EnvironmentFile rather
#: than on a command line, because it carries the relay bearer. Mode 0600.
ENV_FILE = STATE_DIR / "daemon.env"

#: How far this host has recorded and announced the daemon's stream, and each
#: caller's drain cursor, so a restarted adapter resumes rather than repeats.
STREAM_STATE = STATE_DIR / "stream.json"

#: One file per session: what was run, what came back, and what the two people
#: said to each other. Beside this service's own state rather than inside a git
#: worktree, which is where every other service keeps its content and which
#: keeps a credential-adjacent record out of version control.
#:
#: Written only in the operator role. The relay promises to keep nothing about a
#: session and that promise stays literally true, because the relay role never
#: opens one of these.
SESSIONS_DIR = STATE_DIR / "sessions"

ROLE = os.environ.get("AWM_TETHER_ROLE", "operator").strip().lower()

OPERATOR = "operator"
RELAY = "relay"
ROLES = (OPERATOR, RELAY)


def role_binary() -> Path:
    """The binary this host's role supervises."""
    return RELAY_BIN if ROLE == RELAY else OPERATOR_BIN
