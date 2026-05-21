"""Peer registry: local identity + remote peer CRUD + SSH-tunneled ping.

Local identity (``peer_id`` + optional ``advertise_url``) lives in a JSON
file at ``config.PEER_FILE``. Remote peer entries live in the ``peers``
table; each row records the SSH alias and the peer's remote awm port.
The actual HTTP transport is the SSH tunnel established in
``ssh_tunnel.acquire_tunnel`` — peers do NOT expose public ports.

Bearer tokens for remote peers are stored at the canonical path
``AWM_DIR/peers/<peer_id>.token`` (mode 600). ``awm peer add
--token-file <src>`` copies content into that location.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from awm import config
from awm.db import get_connection


# ---------------------------------------------------------------------------
# Local identity
# ---------------------------------------------------------------------------

class LocalIdentityError(Exception):
    pass


def get_local_identity() -> dict | None:
    """Return {"peer_id", "advertise_url"?} for this awm instance, or None
    if `awm peer init` has not been run."""
    path = config.PEER_FILE
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise LocalIdentityError(f"could not read {path}: {exc}") from exc
    if not isinstance(data, dict) or "peer_id" not in data:
        raise LocalIdentityError(f"{path} is missing required 'peer_id'")
    return data


def set_local_identity(peer_id: str, advertise_url: str | None = None, *,
                       overwrite: bool = False) -> dict:
    """Write the local identity to PEER_FILE. ``advertise_url`` is optional
    (cosmetic; informs peers but transport is SSH-tunneled). Refuses to
    overwrite by default."""
    _validate_peer_id(peer_id)
    path = config.PEER_FILE
    if path.exists() and not overwrite:
        raise LocalIdentityError(f"identity already set at {path}; pass overwrite=True")
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {"peer_id": peer_id}
    if advertise_url:
        if not advertise_url.startswith(("http://", "https://")):
            raise ValueError("advertise_url must be http:// or https://")
        data["advertise_url"] = advertise_url.rstrip("/")
    path.write_text(json.dumps(data, indent=2) + "\n")
    return data


# ---------------------------------------------------------------------------
# Remote peer registry
# ---------------------------------------------------------------------------

_PEER_ID_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SSH_ALIAS_OK = re.compile(r"^[A-Za-z0-9._@-]{1,127}$")
# Empty alias = loopback mode (peer reachable at 127.0.0.1:remote_port via
# an out-of-band reverse SSH forward maintained elsewhere).


def _validate_peer_id(peer_id: str) -> None:
    if not _PEER_ID_OK.match(peer_id):
        raise ValueError(
            f"invalid peer_id {peer_id!r}: must match ^[A-Za-z0-9][A-Za-z0-9_-]{{0,63}}$"
        )


def _validate_ssh_alias(alias: str) -> None:
    if alias == "":
        return  # loopback mode
    if not _SSH_ALIAS_OK.match(alias):
        raise ValueError(f"invalid ssh_alias {alias!r}")


def _peers_token_dir() -> Path:
    return config.AWM_DIR / "peers"


def _canonical_token_path(peer_id: str) -> Path:
    return _peers_token_dir() / f"{peer_id}.token"


def install_peer_token(peer_id: str, source_path: str | Path) -> Path:
    """Copy the bearer token from ``source_path`` into the canonical location
    for ``peer_id`` (mode 0600). Returns the canonical path."""
    src = Path(source_path).expanduser()
    if not src.exists():
        raise FileNotFoundError(f"token file does not exist: {src}")
    content = src.read_text().strip()
    if not content:
        raise ValueError(f"token file {src} is empty")
    dest_dir = _peers_token_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(dest_dir, 0o700)
    except OSError:
        pass
    dest = _canonical_token_path(peer_id)
    dest.write_text(content + "\n")
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass
    return dest


def _row_to_peer(row) -> dict:
    return {
        "peer_id": row["peer_id"],
        "ssh_alias": row["ssh_alias"],
        "remote_port": row["remote_port"],
        "friendly_name": row["friendly_name"],
        "last_seen": row["last_seen"],
        "added_at": row["added_at"],
    }


def add_peer(peer_id: str, ssh_alias: str, *,
             remote_port: int = 7820,
             friendly_name: str | None = None) -> dict:
    """Register a remote awm peer. Idempotent — re-adding overwrites
    ``ssh_alias``, ``remote_port``, and ``friendly_name``. Token install
    is a separate step (``install_peer_token``)."""
    _validate_peer_id(peer_id)
    _validate_ssh_alias(ssh_alias)
    if not (1 <= remote_port <= 65535):
        raise ValueError(f"remote_port out of range: {remote_port}")

    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    try:
        conn.execute(
            """\
            INSERT INTO peers (peer_id, ssh_alias, remote_port, friendly_name, added_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(peer_id) DO UPDATE SET
                ssh_alias = excluded.ssh_alias,
                remote_port = excluded.remote_port,
                friendly_name = excluded.friendly_name
            """,
            (peer_id, ssh_alias, remote_port, friendly_name, now),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM peers WHERE peer_id = ?", (peer_id,)).fetchone()
    finally:
        conn.close()
    return _row_to_peer(row)


def list_peers() -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM peers ORDER BY peer_id").fetchall()
    finally:
        conn.close()
    return [_row_to_peer(r) for r in rows]


def get_peer(peer_id: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM peers WHERE peer_id = ?", (peer_id,)).fetchone()
    finally:
        conn.close()
    return _row_to_peer(row) if row else None


def remove_peer(peer_id: str) -> bool:
    conn = get_connection()
    try:
        cur = conn.execute("DELETE FROM peers WHERE peer_id = ?", (peer_id,))
        conn.commit()
    finally:
        conn.close()
    if cur.rowcount > 0:
        token = _canonical_token_path(peer_id)
        if token.exists():
            try:
                token.unlink()
            except OSError:
                pass
        return True
    return False


def load_peer_token(peer_id: str) -> str:
    """Read the bearer token for a peer from the canonical path. Raises
    if the peer is unknown or the file is missing/empty."""
    if get_peer(peer_id) is None:
        raise KeyError(f"unknown peer: {peer_id}")
    path = _canonical_token_path(peer_id)
    if not path.exists():
        raise FileNotFoundError(
            f"token file missing for peer {peer_id}: {path} "
            f"(use `awm peer add --token-file ...` to install)"
        )
    content = path.read_text().strip()
    if not content:
        raise ValueError(f"token file for peer {peer_id} is empty: {path}")
    return content


def _touch_last_seen(peer_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE peers SET last_seen = ? WHERE peer_id = ?",
            (now, peer_id),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Ping (reachability + identity verification via SSH tunnel)
# ---------------------------------------------------------------------------

class PingResult(dict):
    """Plain dict subclass for slightly nicer reprs in CLI output."""


def ping_peer(peer_id: str, timeout: float = 5.0) -> PingResult:
    """Open (or reuse) the SSH tunnel to ``peer_id``, GET ``/peer`` over it,
    verify the peer_id echo, and update last_seen.

    Returns a dict with ``ok``, ``status_code``, ``echoed_peer_id``, and
    ``advertise_url`` if reachable; ``ok=False`` and ``reason`` otherwise.
    """
    import httpx
    from awm.services.network import ssh_tunnel

    if get_peer(peer_id) is None:
        return PingResult(ok=False, reason=f"unknown peer: {peer_id}")

    try:
        token = load_peer_token(peer_id)
    except (FileNotFoundError, ValueError) as exc:
        return PingResult(ok=False, reason=str(exc))

    try:
        tun = ssh_tunnel.acquire_tunnel(peer_id)
    except ssh_tunnel.TunnelError as exc:
        return PingResult(ok=False, reason=f"tunnel error: {exc}")

    url = tun.local_url + "/peer"
    try:
        r = httpx.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        return PingResult(ok=False, reason=f"HTTP error: {exc}")

    if r.status_code != 200:
        return PingResult(ok=False, status_code=r.status_code, reason=r.text[:200])

    try:
        body = r.json()
    except ValueError:
        return PingResult(ok=False, reason="non-JSON response")

    echoed = body.get("peer_id")
    if echoed != peer_id:
        return PingResult(
            ok=False,
            reason=f"peer_id mismatch: configured {peer_id!r}, server reports {echoed!r}",
        )

    _touch_last_seen(peer_id)
    return PingResult(
        ok=True,
        status_code=200,
        echoed_peer_id=echoed,
        advertise_url=body.get("advertise_url"),
        local_port=tun.local_port,
    )
