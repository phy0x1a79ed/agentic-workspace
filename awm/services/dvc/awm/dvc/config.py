"""Endpoint + path configuration for the dvc service.

Lives at ``$AWM_DIR/dvc.toml`` (workspace-local). Every value here is
**node-specific** — the Globus endpoint that fronts *this* machine's filesystem
and the prefix under which *this* machine's backups live — which is why none of
it is baked into the repo: the same service source is deployed to every node in
the fleet, and a hardcoded endpoint UUID would silently make one node write into
another's backup tree. Shape::

    [chinook]
    local_endpoint  = "57b23332-9048-11f1-ad24-02ce27bde401"  # this node's GCP endpoint
    remote_endpoint = "2602486c-1e0f-47a0-be15-eec1b0ff0f96"  # the chinook collection
    prefix          = "/Workspace_backups/Tony_Liu/altair"    # this node's tree on chinook
    globus_bin      = "/home/tony/lib/miniforge3/envs/globus/bin/globus"

``prefix`` is load-bearing in both directions: ``mirror`` writes the workspace
there, and ``pull`` reads objects back from exactly that tree. They must name
the same place or a restore silently finds nothing — so they read one value,
rather than the two hand-synced copies the predecessor scripts carried.

Env vars of the same names (``AWM_DVC_LOCAL_ENDPOINT``, ``AWM_DVC_REMOTE_ENDPOINT``,
``AWM_DVC_PREFIX``, ``AWM_DVC_GLOBUS_BIN``) override the file, so a round trip can
be exercised against a scratch prefix without touching the mirrored path — which
matters more than usual here, because the mirror runs with
``delete_destination_extra``.
"""

from __future__ import annotations

import os
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path

from awm import config

CONFIG_FILE: Path = config.AWM_DIR / "dvc.toml"

WORKSPACE_ROOT: Path = config.WORKSPACE_ROOT


class DvcConfigError(Exception):
    """Raised when dvc.toml is missing, malformed, or lacks a required field."""


@dataclass(frozen=True)
class ChinookConfig:
    local_endpoint: str
    remote_endpoint: str
    prefix: str
    globus_bin: str


def _load_toml() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        with open(CONFIG_FILE, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise DvcConfigError(f"could not parse {CONFIG_FILE}: {exc}") from exc


def _resolve_globus_bin(from_file: str) -> str:
    """Absolute path to the ``globus`` CLI.

    Resolved rather than trusted to PATH: the gateway respawns services under
    systemd's minimal PATH, and the CLI lives in its own mamba env, so a bare
    ``globus`` would work interactively and fail on respawn.
    """
    if env := os.environ.get("AWM_DVC_GLOBUS_BIN"):
        return env
    if from_file:
        return from_file
    if found := shutil.which("globus"):
        return found
    return "globus"


def load() -> ChinookConfig:
    """Load + validate the chinook endpoint config (env overrides the file)."""
    section = _load_toml().get("chinook") or {}

    def field(key: str, env_key: str) -> str:
        val = os.environ.get(env_key) or section.get(key) or ""
        return val.strip() if isinstance(val, str) else ""

    local = field("local_endpoint", "AWM_DVC_LOCAL_ENDPOINT")
    remote = field("remote_endpoint", "AWM_DVC_REMOTE_ENDPOINT")
    prefix = field("prefix", "AWM_DVC_PREFIX")

    missing = [
        name
        for name, val in (
            ("local_endpoint", local),
            ("remote_endpoint", remote),
            ("prefix", prefix),
        )
        if not val
    ]
    if missing:
        raise DvcConfigError(
            f"{CONFIG_FILE} [chinook] is missing {', '.join(missing)} — "
            "these are node-specific and have no safe default (see INSTALL.md)."
        )

    return ChinookConfig(
        local_endpoint=local,
        remote_endpoint=remote,
        # A trailing slash would double up when joined with a workspace-relative
        # path and address a directory that does not exist on the collection.
        prefix=prefix.rstrip("/"),
        globus_bin=_resolve_globus_bin(str(section.get("globus_bin") or "")),
    )
