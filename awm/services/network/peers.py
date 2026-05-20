"""Peer registry: local identity + remote peer CRUD + reachability check.

Local identity (peer_id + advertise_url) lives in a JSON file at
``config.PEER_FILE``. Remote peer entries live in the ``peers`` table.

Tokens for remote peers are stored as **file paths**, not values — rotation
is just rewriting the target file, no DB update needed.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

from awm import config
from awm.db import get_connection


# ---------------------------------------------------------------------------
# Local identity
# ---------------------------------------------------------------------------

class LocalIdentityError(Exception):
    pass


def get_local_identity() -> dict | None:
    """Return {"peer_id", "advertise_url"} for this awm instance, or None
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


def set_local_identity(peer_id: str, advertise_url: str, *, overwrite: bool = False) -> dict:
    """Write the local identity to PEER_FILE. Refuses to overwrite by default."""
    _validate_peer_id(peer_id)
    if not advertise_url.startswith(("http://", "https://")):
        raise ValueError("advertise_url must be http:// or https://")
    path = config.PEER_FILE
    if path.exists() and not overwrite:
        raise LocalIdentityError(f"identity already set at {path}; pass overwrite=True")
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"peer_id": peer_id, "advertise_url": advertise_url.rstrip("/")}
    path.write_text(json.dumps(data, indent=2) + "\n")
    return data


# ---------------------------------------------------------------------------
# Remote peer registry
# ---------------------------------------------------------------------------

_PEER_ID_OK = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _validate_peer_id(peer_id: str) -> None:
    if not _PEER_ID_OK.match(peer_id):
        raise ValueError(
            f"invalid peer_id {peer_id!r}: must match ^[A-Za-z0-9][A-Za-z0-9_-]{{0,63}}$"
        )


def _row_to_peer(row) -> dict:
    return {
        "peer_id": row["peer_id"],
        "base_url": row["base_url"],
        "token_path": row["token_path"],
        "friendly_name": row["friendly_name"],
        "last_seen": row["last_seen"],
        "added_at": row["added_at"],
    }


def add_peer(peer_id: str, base_url: str, token_path: str,
             friendly_name: str | None = None) -> dict:
    """Register a remote awm peer. Idempotent: re-adding overwrites fields."""
    _validate_peer_id(peer_id)
    if not base_url.startswith(("http://", "https://")):
        raise ValueError("base_url must be http:// or https://")
    token_path = str(Path(token_path).expanduser())
    if not Path(token_path).exists():
        raise FileNotFoundError(f"token file does not exist: {token_path}")

    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    try:
        conn.execute(
            """\
            INSERT INTO peers (peer_id, base_url, token_path, friendly_name, added_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(peer_id) DO UPDATE SET
                base_url = excluded.base_url,
                token_path = excluded.token_path,
                friendly_name = excluded.friendly_name
            """,
            (peer_id, base_url.rstrip("/"), token_path, friendly_name, now),
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
    return cur.rowcount > 0


def load_peer_token(peer_id: str) -> str:
    """Read the bearer token file for a peer. Raises if peer or file missing."""
    peer = get_peer(peer_id)
    if peer is None:
        raise KeyError(f"unknown peer: {peer_id}")
    path = Path(peer["token_path"])
    if not path.exists():
        raise FileNotFoundError(f"token file missing for peer {peer_id}: {path}")
    return path.read_text().strip()


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
# Ping (reachability + identity verification)
# ---------------------------------------------------------------------------

class PingResult(dict):
    """Plain dict subclass for slightly nicer reprs in CLI output."""


def ping_peer(peer_id: str, timeout: float = 5.0) -> PingResult:
    """GET /peer on the remote, verify the peer_id echo, update last_seen.

    Returns a dict with ``ok``, ``status_code``, ``echoed_peer_id``, and
    ``advertise_url`` if reachable; ``ok=False`` and ``reason`` otherwise.
    """
    peer = get_peer(peer_id)
    if peer is None:
        return PingResult(ok=False, reason=f"unknown peer: {peer_id}")

    token = load_peer_token(peer_id)
    url = peer["base_url"].rstrip("/") + "/peer"
    try:
        r = httpx.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            verify=False,  # self-signed certs are the norm for ZeroTier/LAN
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
    )
