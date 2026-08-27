"""Who gets a Trilium, where their content lives, and which ports they own.

Trilium is single-user per instance. That is not a limitation awm works around
— it is what supplies per-user identity, which the edge does not have: awm's
httpsfront auth is one shared password for the whole workspace, so two people
behind it are the same person. One server per person, each with its own login
and its own database, tells them apart.

**Users are discovered, not configured.** A person exists here because a scope
`userdata/trilium/<user>` exists on disk. Creating the scope is the whole of
adding a user, and there is no second list to keep in step with the first.

**Ports are allocated once and remembered.** Deriving a port from a sorted
position looks equivalent and is not: adding a user whose name sorts early
would renumber everyone after them, silently moving live URLs. The allocation
file only ever grows.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from awm.config import AWM_DIR, WORKSPACE_ROOT

HERE = Path(__file__).resolve().parent          # …/trilium/awm/trilium
SERVICE_DIR = HERE.parents[1]                   # …/awm/services/trilium

#: Per-service *runtime* state: the port allocation, the ETAPI tokens and the
#: logs. Written by whoever runs the service. Never a user's content — that
#: lives in their scope.
STATE_DIR = Path(os.environ.get("TRILIUM_STATE_DIR") or (AWM_DIR / "services" / "trilium"))
PORTS_FILE = STATE_DIR / "ports.json"
LOG_DIR = STATE_DIR / "logs"

#: Per-service *install* artifacts: the unpacked server tarball, the recorded
#: node bin, the tarball stamp. Beside the service and gitignored, the way
#: drawio keeps its patched webapp.
#:
#: Not in STATE_DIR, and the reason is sirius. There the install runs as the
#: dev user and the gateway runs as the application account, which owns the
#: state root — an install writing there fails on permissions. Everything
#: written at install time and only read at runtime belongs on the install
#: side of that line.
INSTALL_DIR = SERVICE_DIR
NODE_BIN_FILE = INSTALL_DIR / "node-bin"

#: One scope per person, branch `trilium/<user>`. See `projects/userdata/README.md`.
USERDATA_DIR = Path(os.environ.get("TRILIUM_USERDATA_DIR")
                    or (WORKSPACE_ROOT / "projects" / "userdata" / "trilium"))

#: The fork worktree this node serves. `release` by default. A dev sandbox
#: points TRILIUM_FORK_DIR at `dev`.
FORK_DIR = Path(os.environ.get("TRILIUM_FORK_DIR")
                or (WORKSPACE_ROOT / "projects" / "trilium" / "release"))

#: What `pnpm server:build` emits — one bundle, plus an `assets/` beside it.
FORK_ENTRY = FORK_DIR / "apps" / "server" / "dist" / "main.cjs"

#: Where `install.sh` unpacks the published server tarball when there is no
#: fork to build. Upstream ships a Node runtime inside it, so a node serving
#: from here needs no node toolchain of its own — which is the whole reason
#: sirius can install in a minute.
TARBALL_DIR = INSTALL_DIR / "server"
TARBALL_ENTRY = TARBALL_DIR / "main.cjs"
TARBALL_NODE = TARBALL_DIR / "node" / "bin" / "node"

#: What `install.sh` last built, written where the worktree already ignores it.
BUILD_STAMP = FORK_DIR / ".awm" / "trilium-build-stamp"

#: Mesh-facing TLS port for user 0. Continues the per-service band: httpsfront
#: 12100, claude-science 12201/12202, dsh 12301, hermes 12401.
FRONT_PORT_BASE = int(os.environ.get("TRILIUM_FRONT_PORT_BASE", "12501"))

#: Loopback port for user 0's node process. Ten above the front band, the same
#: offset dsh uses between its front (12301) and its upstream (12311).
UPSTREAM_PORT_BASE = int(os.environ.get("TRILIUM_UPSTREAM_PORT_BASE", "12511"))

#: How many people this node will serve. The bands are ten apart, so nine is
#: the point at which one service's upstreams would collide with the next
#: service's fronts. Refuse rather than collide.
MAX_USERS = 9

HEALTH_INTERVAL_S = float(os.environ.get("TRILIUM_HEALTH_INTERVAL_S", "20"))
START_TIMEOUT_S = float(os.environ.get("TRILIUM_START_TIMEOUT_S", "120"))


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "off", "false", "no")


#: Whether to raise a mesh TLS front per user.
#:
#: True on a mesh node, where the front is the only way a browser reaches a
#: loopback server. False where something else is already the public edge —
#: sirius fronts every request with nginx behind Cloudflare, and its firewall
#: admits 80 and 443 alone, so a listener in the 12501 band would bind a port
#: nothing can reach and mint a certificate nothing would trust.
FRONTS_ENABLED = _flag("TRILIUM_FRONTS", True)

_USER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


@dataclass(frozen=True)
class Instance:
    """One person's server: where their content is and which ports it owns."""

    user: str
    slot: int
    scope: Path

    @property
    def front_port(self) -> int:
        return FRONT_PORT_BASE + self.slot

    @property
    def upstream_port(self) -> int:
        return UPSTREAM_PORT_BASE + self.slot

    @property
    def data_dir(self) -> Path:
        """TRILIUM_DATA_DIR — the live database, its WAL, and the session store.

        Gitignored by the userdata template. Never DVC-pinned: the database and
        its write-ahead log are one logical unit, so a pin taken while the
        server runs records a state that never existed.
        """
        return self.scope / "live"

    @property
    def rolling_dir(self) -> Path:
        """TRILIUM_BACKUP_DIR — `backup-daily.db` and friends, rewritten in place.

        Inside `live/`, and gitignored with it. It is tempting to make this the
        DVC chunk, because these are the only consistent database copies on
        disk — Trilium writes them under its sync mutex. It cannot be one.
        `dvc add` replaces every file it pins with a read-only hardlink into the
        shared cache, and Trilium's next rolling backup overwrites the same
        name: the write fails on permissions, and the daily backup stops.

        So Trilium churns here, and `snapshots_dir` holds the copies awm pins.
        """
        return self.scope / "live" / "backups"

    @property
    def snapshots_dir(self) -> Path:
        """The DVC chunk — one immutable file per named snapshot.

        awm writes here and Trilium does not, which is what makes read-only
        hardlinks safe. Each file is written once under a name that is never
        reused, so nothing ever has to overwrite a pinned file.
        """
        return self.scope / "data" / "backups"

    @property
    def superseded_dir(self) -> Path:
        """Where a restore puts the vault it replaced. Inside `live/`, so it is
        ignored by git and pinned by nothing, but it is still on disk — a
        restore of the wrong snapshot is undone by moving a file back."""
        return self.scope / "live" / "superseded"

    @property
    def document_db(self) -> Path:
        """The live database. Its `-wal` and `-shm` siblings are part of it."""
        return self.data_dir / "document.db"

    @property
    def token_file(self) -> Path:
        """This user's ETAPI token, in service state and never in their scope.

        A token, never their password: `POST /etapi/auth/login` exchanges one
        for the other, and only the token is written down. It is revocable from
        Trilium's own options screen, which a password is not.
        """
        return STATE_DIR / "tokens" / f"{self.user}.token"

    @property
    def notes_dir(self) -> Path:
        """The markdown export tree. Plain committed text, and a derived view —
        Trilium stores markup as HTML, so importing it back is lossy."""
        return self.scope / "notes"

    @property
    def log_file(self) -> Path:
        return LOG_DIR / f"{self.user}.log"


def _read_slots() -> dict[str, int]:
    try:
        doc = json.loads(PORTS_FILE.read_text())
    except (OSError, ValueError):
        return {}
    return {k: int(v) for k, v in doc.items() if isinstance(v, int)}


def _assign_slot(user: str, slots: dict[str, int]) -> int:
    """The user's slot, allocating the lowest free one and persisting it."""
    if user in slots:
        return slots[user]
    taken = set(slots.values())
    slot = next(i for i in range(MAX_USERS) if i not in taken)
    slots[user] = slot
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PORTS_FILE.write_text(json.dumps(slots, indent=2, sort_keys=True) + "\n")
    return slot


def discovered_users() -> list[str]:
    """Every user with a scope on disk, in name order.

    A directory is a user's scope when it holds a `.git` — a worktree's is a
    file, not a directory, so both forms are accepted. The check exists so a
    stray directory beside the scopes is not handed a port and a server.

    Names are constrained because they become a port allocation key and a log
    filename.
    """
    try:
        entries = sorted(p for p in USERDATA_DIR.iterdir() if p.is_dir())
    except OSError:
        return []
    return [p.name for p in entries
            if _USER_RE.match(p.name) and (p / ".git").exists()]


def instances() -> list[Instance]:
    """Every instance this node serves, ports assigned.

    Beyond MAX_USERS the extra users are dropped rather than given a port that
    belongs to the next service's band. `service_status` reports the count, so
    the loss is visible.
    """
    slots = _read_slots()
    out = []
    for user in discovered_users():
        try:
            slot = _assign_slot(user, slots)
        except StopIteration:
            break
        out.append(Instance(user=user, slot=slot, scope=USERDATA_DIR / user))
    return out


def instance(user: str) -> Instance | None:
    return next((i for i in instances() if i.user == user), None)


# -- what is installed to run -----------------------------------------------


def entry_point() -> tuple[Path, str] | None:
    """The server bundle to launch, and which of the two it is.

    The fork wins when it is built, because a change we authored is only real
    if it is the thing running. The tarball is what a node that cannot build
    serves — sirius holds no GitHub credential and has two vCPUs.
    """
    if FORK_ENTRY.is_file():
        return FORK_ENTRY, "fork"
    if TARBALL_ENTRY.is_file():
        return TARBALL_ENTRY, "tarball"
    return None


def node_exe() -> str:
    """Absolute node for the chosen entry point, falling back to PATH.

    The published tarball carries its own Node runtime beside the bundle, so a
    tarball install needs no toolchain. A fork build does, and reads the
    absolute bin directory `install.sh` recorded — the supervisor respawns
    under systemd's minimal PATH, where neither `node` nor the `mamba` that
    could find it exists.
    """
    chosen = entry_point()
    if chosen and chosen[1] == "tarball" and TARBALL_NODE.is_file():
        return str(TARBALL_NODE)
    try:
        recorded = NODE_BIN_FILE.read_text().strip()
    except OSError:
        recorded = ""
    return str(Path(recorded) / "node") if recorded else "node"


def _git(*args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(FORK_DIR), *args],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def source_state() -> dict[str, Any]:
    """Which revision is on disk, and whether the build matches it.

    Without this a stale or hand-edited deployment is invisible: the server
    serves whatever bundle happens to be in `dist/`, and nothing relates it to
    a commit. `dirty` and `built_current` are the two ways that goes wrong —
    an uncommitted edit, and a commit that was never rebuilt.
    """
    chosen = entry_point()
    state: dict[str, Any] = {
        "entry": str(chosen[0]) if chosen else None,
        "source": chosen[1] if chosen else None,
        "fork_dir": str(FORK_DIR),
    }
    if not (FORK_DIR / ".git").exists():
        return state
    status = _git("status", "--porcelain")
    head = _git("rev-parse", "HEAD")
    stamp = None
    try:
        stamp = dict(line.split("=", 1) for line in
                     BUILD_STAMP.read_text().split() if "=" in line)
    except (OSError, ValueError):
        pass
    state.update({
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "describe": _git("describe", "--tags", "--always", "--dirty"),
        "head": head,
        "dirty": None if status is None else bool(status),
        "built_head": (stamp or {}).get("head"),
        "built_current": (None if (stamp is None or head is None)
                          else stamp.get("head") == head
                          and stamp.get("dirty") == ("1" if status else "0")),
    })
    return state


def listening(port: int, *, timeout: float = 0.5) -> bool:
    with socket.socket() as s:
        s.settimeout(timeout)
        return s.connect_ex(("127.0.0.1", port)) == 0
