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


def install_peer_token_via_ssh(peer_id: str, ssh_alias: str, *,
                               remote_path: str = "~/.awm/auth.token",
                               timeout: float = 15.0) -> Path:
    """Fetch a peer's bearer token over SSH and install it locally.

    Runs ``ssh <alias> "cat <remote_path>"`` (default ``~/.awm/auth.token``
    on the remote) and writes the result to the canonical token path
    with mode 0600. SSH is the trust root — if the remote allows us to
    read the token file, we accept it as that peer's bearer.

    Returns the canonical path. Raises ``RuntimeError`` on ssh failure
    or empty output.
    """
    import subprocess

    if not ssh_alias:
        raise ValueError("ssh_alias is required")
    _validate_ssh_alias(ssh_alias)
    _validate_peer_id(peer_id)

    cmd = ["ssh", "-o", "BatchMode=yes", ssh_alias, f"cat {remote_path}"]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"ssh fetch timed out after {timeout}s") from exc
    if result.returncode != 0:
        stderr = result.stderr.strip() or "(no stderr)"
        raise RuntimeError(
            f"ssh {ssh_alias} failed (exit {result.returncode}): {stderr}"
        )
    content = result.stdout.strip()
    if not content:
        raise RuntimeError(
            f"remote {remote_path} on {ssh_alias} is empty — "
            "is the awm daemon initialized over there?"
        )

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
    raw_endpoints = row["endpoints"] if "endpoints" in row.keys() else None
    try:
        endpoints = json.loads(raw_endpoints) if raw_endpoints else None
    except (TypeError, ValueError):
        endpoints = None
    # Synthesize the legacy ssh entry when endpoints is empty so callers
    # always see the SSH-tunnel fallback as the last item.
    if not endpoints:
        if row["ssh_alias"]:
            endpoints = [{
                "kind": "ssh",
                "alias": row["ssh_alias"],
                "port": row["remote_port"],
            }]
        else:
            endpoints = []
    return {
        "peer_id": row["peer_id"],
        "ssh_alias": row["ssh_alias"],
        "remote_port": row["remote_port"],
        "friendly_name": row["friendly_name"],
        "last_seen": row["last_seen"],
        "added_at": row["added_at"],
        "endpoints": endpoints,
        "tls_fingerprint": row["tls_fingerprint"] if "tls_fingerprint" in row.keys() else None,
        "peer_priority": row["peer_priority"] if "peer_priority" in row.keys() else 100,
    }


def _normalize_endpoints(endpoints: list[dict] | None) -> list[dict]:
    """Validate + canonicalize endpoint entries.

    Each entry is a dict with ``kind`` ∈ {``direct``, ``ssh``}. ``direct``
    requires ``url`` (https://host:port). ``ssh`` requires ``alias`` and
    ``port``. Other keys are preserved.
    """
    if not endpoints:
        return []
    out: list[dict] = []
    for raw in endpoints:
        if not isinstance(raw, dict):
            raise ValueError(f"endpoint must be a dict, got {type(raw).__name__}")
        kind = raw.get("kind")
        if kind == "direct":
            url = raw.get("url")
            if not url or not isinstance(url, str):
                raise ValueError("direct endpoint requires url=https://host:port")
            if not url.startswith(("https://", "http://")):
                raise ValueError(f"direct endpoint url must be http(s): {url}")
            entry = {"kind": "direct", "url": url}
        elif kind == "ssh":
            alias = raw.get("alias")
            port = raw.get("port")
            if not alias or not isinstance(alias, str):
                raise ValueError("ssh endpoint requires alias=<host>")
            try:
                port = int(port)
            except (TypeError, ValueError):
                raise ValueError("ssh endpoint requires integer port")
            if not (1 <= port <= 65535):
                raise ValueError(f"ssh endpoint port out of range: {port}")
            entry = {"kind": "ssh", "alias": alias, "port": port}
        else:
            raise ValueError(f"unknown endpoint kind: {kind!r}")
        if "tls_fingerprint" in raw and raw["tls_fingerprint"]:
            entry["tls_fingerprint"] = raw["tls_fingerprint"]
        out.append(entry)
    return out


def add_peer(peer_id: str, ssh_alias: str, *,
             remote_port: int = 7820,
             friendly_name: str | None = None,
             endpoints: list[dict] | None = None,
             tls_fingerprint: str | None = None,
             peer_priority: int | None = None) -> dict:
    """Register a remote awm peer. Idempotent — re-adding overwrites
    ``ssh_alias``, ``remote_port``, ``friendly_name``, ``endpoints``, and
    ``tls_fingerprint``. Token install is a separate step
    (:func:`install_peer_token`).

    ``endpoints`` is an optional ordered list of ``{kind, ...}`` dicts. The
    peer-client tries each one in order; the legacy ``ssh_alias`` +
    ``remote_port`` are synthesized as a trailing fallback entry if
    ``endpoints`` is empty/None.
    """
    _validate_peer_id(peer_id)
    _validate_ssh_alias(ssh_alias)
    if not (1 <= remote_port <= 65535):
        raise ValueError(f"remote_port out of range: {remote_port}")
    normalized = _normalize_endpoints(endpoints)
    endpoints_json = json.dumps(normalized) if normalized else None

    now = datetime.now(timezone.utc).isoformat()
    effective_priority = 100 if peer_priority is None else int(peer_priority)
    conn = get_connection()
    try:
        if peer_priority is None:
            # Preserve existing priority on update; seed default on insert.
            conn.execute(
                """\
                INSERT INTO peers (peer_id, ssh_alias, remote_port, friendly_name, added_at, endpoints, tls_fingerprint, peer_priority)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(peer_id) DO UPDATE SET
                    ssh_alias = excluded.ssh_alias,
                    remote_port = excluded.remote_port,
                    friendly_name = excluded.friendly_name,
                    endpoints = excluded.endpoints,
                    tls_fingerprint = excluded.tls_fingerprint
                """,
                (peer_id, ssh_alias, remote_port, friendly_name, now,
                 endpoints_json, tls_fingerprint, effective_priority),
            )
        else:
            conn.execute(
                """\
                INSERT INTO peers (peer_id, ssh_alias, remote_port, friendly_name, added_at, endpoints, tls_fingerprint, peer_priority)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(peer_id) DO UPDATE SET
                    ssh_alias = excluded.ssh_alias,
                    remote_port = excluded.remote_port,
                    friendly_name = excluded.friendly_name,
                    endpoints = excluded.endpoints,
                    tls_fingerprint = excluded.tls_fingerprint,
                    peer_priority = excluded.peer_priority
                """,
                (peer_id, ssh_alias, remote_port, friendly_name, now,
                 endpoints_json, tls_fingerprint, effective_priority),
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


def list_remote_peers() -> list[dict]:
    """All peers except this host's self-row. Ordered by peer_priority ASC
    (highest precedence first). Used by leader election to find peers worth
    deferring to."""
    ident = get_local_identity()
    self_id = ident["peer_id"] if ident else None
    out = []
    for p in list_peers():
        if self_id and p["peer_id"] == self_id:
            continue
        out.append(p)
    out.sort(key=lambda p: (p.get("peer_priority", 100), p["peer_id"]))
    return out


def set_peer_priority(peer_id: str, priority: int) -> dict | None:
    """Update peer_priority for an existing peer. Returns the updated row,
    or None if peer is unknown."""
    if not isinstance(priority, int) or priority < 0:
        raise ValueError(f"peer_priority must be a non-negative integer, got {priority!r}")
    conn = get_connection()
    try:
        cur = conn.execute(
            "UPDATE peers SET peer_priority = ? WHERE peer_id = ?",
            (priority, peer_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
        row = conn.execute("SELECT * FROM peers WHERE peer_id = ?", (peer_id,)).fetchone()
    finally:
        conn.close()
    return _row_to_peer(row) if row else None


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
    """Reach ``peer_id`` via its preferred endpoint, GET ``/peer``,
    verify the ``peer_id`` echo, update last_seen.

    Returns a dict with ``ok``, ``status_code``, ``echoed_peer_id``,
    ``advertise_url``, and ``endpoint`` (which endpoint kind succeeded)
    if reachable; ``ok=False`` and ``reason`` otherwise.
    """
    import httpx
    from awm.services.network import federation

    if get_peer(peer_id) is None:
        return PingResult(ok=False, reason=f"unknown peer: {peer_id}")

    try:
        base_url, token = federation._resolve(peer_id)
    except federation.FederationError as exc:
        return PingResult(ok=False, reason=str(exc))

    url = base_url + "/peer"
    try:
        r = httpx.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            verify=False,
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
    chosen = federation._preferred_endpoint.get(peer_id)
    return PingResult(
        ok=True,
        status_code=200,
        echoed_peer_id=echoed,
        advertise_url=body.get("advertise_url"),
        endpoint=chosen,
    )
