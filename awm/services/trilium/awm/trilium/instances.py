"""Where the shared vault's content lives, and what runs it.

There is **one vault**, shared by everyone who can sign in. Trilium is
single-user per instance, and that is exactly what this design wants: one
instance, one database, one knowledge base that collaborators work in together.
The awm edge session says who is reading; the vault does not need to, and no
longer asks.

**The vault is a project, not per-user data.** `projects/vault/<branch>` holds
the live database, the DVC-pinned snapshots and the markdown export. It is
deliberately not under `projects/userdata/`, whose every subdirectory is one
person's data on one person's branch — a shared vault is neither.

**The port is defined once, in `awm.config`.** The supervisor binds it and the
edge proxies to it, and neither owns it. One definition means there is no RPC
to make and nothing to go stale.
"""

from __future__ import annotations

import os
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from awm import config
from awm.config import AWM_DIR, WORKSPACE_ROOT

HERE = Path(__file__).resolve().parent          # …/trilium/awm/trilium
SERVICE_DIR = HERE.parents[1]                   # …/awm/services/trilium

#: Per-service *runtime* state: the logs, and nothing else now. Written by
#: whoever runs the service. Never vault content — that lives in the scope.
STATE_DIR = Path(os.environ.get("TRILIUM_STATE_DIR") or (AWM_DIR / "services" / "trilium"))
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

#: The shared vault's worktree. Its branch is named per host (`vault/<host>`)
#: so two hosts' vaults can never be mistaken for one another; the directory
#: name stays fixed so nothing has to look the branch up.
SCOPE = Path(os.environ.get("TRILIUM_VAULT_SCOPE")
             or (WORKSPACE_ROOT / "projects" / "vault" / "main"))

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

#: The loopback port the vault's node process binds. Defined in `awm.config`
#: because the edge needs the same number; see its comment there.
UPSTREAM_PORT = config.VAULT_PORT

HEALTH_INTERVAL_S = float(os.environ.get("TRILIUM_HEALTH_INTERVAL_S", "20"))
START_TIMEOUT_S = float(os.environ.get("TRILIUM_START_TIMEOUT_S", "120"))


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "off", "false", "no")


#: Whether an awm edge listener is the only way to reach the vault.
#:
#: True everywhere this service is deployed, and it is what makes it safe to
#: run Trilium with its own authentication switched off: the child binds
#: loopback, no awm code binds it anywhere else, and the edge authenticates a
#: session before forwarding a byte. Setting this to 0 restores Trilium's own
#: login, and is the only supported way to reach the vault by any other route.
EDGE_ONLY = _flag("TRILIUM_EDGE_ONLY", True)


@dataclass(frozen=True)
class Vault:
    """The shared knowledge base: where its content is and what runs it."""

    scope: Path = SCOPE

    @property
    def exists(self) -> bool:
        """Whether the vault's worktree is on disk.

        A worktree's `.git` is a file rather than a directory, so both forms
        count. Without it there is nothing to serve and the supervisor says so
        instead of spawning a child against an empty path.
        """
        return (self.scope / ".git").exists()

    @property
    def data_dir(self) -> Path:
        """TRILIUM_DATA_DIR — the live database, its WAL, and the session store.

        Gitignored. Never DVC-pinned: the database and its write-ahead log are
        one logical unit, so a pin taken while the server runs records a state
        that never existed.
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
    def notes_dir(self) -> Path:
        """The markdown export tree. Plain committed text, and a derived view —
        Trilium stores markup as HTML, so importing it back is lossy."""
        return self.scope / "notes"

    @property
    def log_file(self) -> Path:
        return LOG_DIR / "vault.log"


#: The one vault this node serves.
VAULT = Vault()


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
