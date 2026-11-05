"""Where the shared vault's content lives, and what runs it.

There is **one vault**, shared by everyone who can sign in. Trilium is
single-user per instance, and that is exactly what this design wants: one
instance, one database, one knowledge base that collaborators work in together.
The awm edge session says who is reading; the vault does not need to, and no
longer asks.

**The vault has a repository of its own.** `projects/vault/release` holds the
live database under `live/`, and the DVC-pinned snapshots, Zotero mirror and
markdown export under `data/vault/`. The Trilium fork is public and its release
tags name code, so the vault's nightly pins are kept out of its history.

**The port is defined once, in `awm.config`.** The supervisor binds it and the
edge proxies to it, and neither owns it. One definition means there is no RPC
to make and nothing to go stale.
"""

from __future__ import annotations

import os
import socket
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

#: Per-service *install* artifacts: the unpacked server release and its stamp.
#: Beside the service and gitignored.
#:
#: Not in STATE_DIR: where the install runs as one user and the gateway as
#: another account that owns the state root, an install writing there fails on
#: permissions. Everything written at install time and only read at runtime
#: belongs on the install side of that line.
INSTALL_DIR = SERVICE_DIR

#: The vault repository's checkout, apart from the fork. Every nightly snapshot
#: is a commit, and in the fork those commits would publish with the code.
#:
#: `awm.zotero.sync.VAULT_SCOPE` spells the same two paths and must agree.
VAULT_HOME = WORKSPACE_ROOT / "projects" / "vault" / "release"


#: Where the vault's content lives.
#:
#: A node with no vault checkout gets a plain directory at this path instead.
#: Every write below asks whether the scope is a git checkout before it pins or
#: commits, so one implementation covers both shapes.
SCOPE = Path(os.environ.get("TRILIUM_VAULT_SCOPE") or VAULT_HOME)

#: The fork release `install.sh` unpacks from the tarball `server.pin` names.
#: It carries its own Node runtime, so a serving node needs no toolchain.
SERVER_PIN = SERVICE_DIR / "server.pin"
SERVER_DIR = INSTALL_DIR / "server"
SERVER_ENTRY = SERVER_DIR / "main.cjs"
SERVER_NODE = SERVER_DIR / "node" / "bin" / "node"
#: ``<version> sha256:<hash>`` of the release unpacked in SERVER_DIR.
SERVER_STAMP = INSTALL_DIR / "tarball-stamp"

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
        """Whether the vault's directory is on disk.

        Without it there is nothing to serve and the supervisor says so instead
        of spawning a child against an empty path. A directory is enough: a
        node with no vault checkout has one.
        """
        return self.scope.is_dir()

    @property
    def is_checkout(self) -> bool:
        """Whether this vault can hold history.

        True where the scope is the vault repository's worktree; false where
        the same path is a plain directory. A worktree's `.git` is a file
        rather than a directory, so both forms count.
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
        return self.scope / "data" / "vault" / "backups"

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
        """The markdown export tree — a DVC chunk beside the snapshots.

        A derived view: Trilium stores markup as HTML, so importing it back is
        lossy. Pinned rather than committed as text, because the fork is a
        public repository and a pin publishes a hash.
        """
        return self.scope / "data" / "vault" / "notes"

    @property
    def log_file(self) -> Path:
        return LOG_DIR / "vault.log"


#: The one vault this node serves.
VAULT = Vault()


# -- what is installed to run -----------------------------------------------


def _read_pin() -> dict[str, str]:
    try:
        lines = SERVER_PIN.read_text().splitlines()
    except OSError:
        return {}
    return dict(line.split("=", 1) for line in lines
                if "=" in line and not line.lstrip().startswith("#"))


def entry_point() -> Path | None:
    """The server bundle to launch: the unpacked fork release, once installed."""
    return SERVER_ENTRY if SERVER_ENTRY.is_file() and SERVER_NODE.is_file() else None


def node_exe() -> str:
    """The Node runtime the release carries beside its bundle."""
    return str(SERVER_NODE)


def source_state() -> dict[str, Any]:
    """Which fork release is unpacked, and whether it is the one awm pins.

    `current` false means `server.pin` moved and `install.sh` has not run since,
    so the server still serves the release before.
    """
    try:
        installed = SERVER_STAMP.read_text().split()
    except OSError:
        installed = []
    pin = _read_pin()
    want = [pin.get("version", ""), f"sha256:{pin.get('sha256', '')}"]
    entry = entry_point()
    return {
        "entry": str(entry) if entry else None,
        "installed": installed[0] if installed else None,
        "pinned": pin.get("version") or None,
        "current": bool(entry) and installed == want,
    }


def listening(port: int, *, timeout: float = 0.5) -> bool:
    with socket.socket() as s:
        s.settimeout(timeout)
        return s.connect_ex(("127.0.0.1", port)) == 0
