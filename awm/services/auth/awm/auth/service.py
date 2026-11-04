"""Auth service orchestration — rotation policy, the verb handlers, and the
``$AWM_PEER_CRED`` file the SSH peer-auth channel reads.

The service owns one authoritative credential state (via :mod:`awm.auth.store`)
and is the *authority*; the ``httpsfront`` edge only *enforces*, using material
this service hands it (the signing secret + the currently-valid peer credentials)
over loopback.

Rotation contract
-----------------
On startup, mint a peer credential if none is valid or the newest is older than
the cadence (so a restart never leaves a stale-only state — ``events`` is not
relied on). Then a background loop mints every cadence. Each mint:

* a log line,
* rewrites the ``$AWM_PEER_CRED`` file with the current peer credential,
* prunes fully-expired generations.

Logins
------
No login lives here. A person signs in with the passkey at alcor's approver,
and the edge turns that grant into a session it signs with this service's
secret (fleet.md R84, users.md R130).

Device tokens
-------------
An unattended device or app that must not store a password holds a device token
(:mod:`awm.config.usertoken`) instead. This service mints it, keeps only the
SHA-256 of its secret, and hands the edge the digests of the live tokens in
``edge_material``; the edge verifies offline, so a revocation lands with the
edge's next material refresh. The edge reports the tokens it saw on that same
refresh, which makes ``last_used_at`` as coarse as the refresh interval. Every
token belongs to ``operator``, and only the operator (a call with no edge
identity) may mint, list or revoke one.

``AWM_AUTH_PROFILE=public`` (the internet-facing host) turns rotation off
entirely: no minting, and the edge receives no peer credentials.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import stat
import time
from pathlib import Path
from typing import Any

from awm import config
from awm.config import SERVICES_DIR, peertoken, usertoken

from awm.auth import store
from awm.auth.config import CONTRACT

log = logging.getLogger("awm.auth.service")

# The current peer credential is mirrored to this file so a peer can fetch it
# with `ssh <host> 'cat "$AWM_PEER_CRED"'` (see the T2 SSH peer-auth channel).
# $AWM_PEER_CRED in the operator's shell rc points here.
PEER_CRED_FILE = SERVICES_DIR / "auth" / "peer_cred.current"

_HOUR = 3600.0
_DAY = 86400.0

_PROFILE_ENV = "AWM_AUTH_PROFILE"

# While this is unset or anything but "0", the edge still accepts the shared
# peer bearer next to node tokens. Setting it to "0" retires the bearer.
LEGACY_BEARER_ENV = "AWM_PEER_LEGACY_BEARER"

NODE_KEY_NAME = "node_ed25519.key"

#: The owner of every device token.
OPERATOR = "operator"

def _settings() -> Any:
    """Current config values (defaults merged with stored overrides)."""
    return CONTRACT.load_model()


def profile() -> str:
    return (os.environ.get(_PROFILE_ENV) or "").strip().lower()


def rotation_enabled() -> bool:
    """The rotating peer credentials are off on the public profile."""
    return profile() != "public"


def legacy_bearer_enabled() -> bool:
    return (os.environ.get(LEGACY_BEARER_ENV) or "").strip() != "0"


# ---------------------------------------------------------------------------
# Node identity
# ---------------------------------------------------------------------------


def node_key_file() -> Path:
    """Where the node's private key lives, beside the service database."""
    return Path(config.SERVICES_DIR) / store.SERVICE / NODE_KEY_NAME


class NodeKeyError(RuntimeError):
    """The node key file is not something auth may sign with."""


def _write_new_key_file(path: Path, private: str) -> None:
    """Put ``private`` at ``path`` atomically, unless something is already there.

    The key is written complete and flushed to disk under a private temporary
    name, then linked into place. ``link`` fails if ``path`` exists, so a reader
    never sees a partial key and two starts racing each other cannot mint two.
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(private + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            return
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        log.info("auth: minted this node's ed25519 key (%s)",
                 peertoken.fingerprint(peertoken.public_key_of(private)))
    finally:
        tmp.unlink(missing_ok=True)


def _read_node_key(path: Path) -> str:
    """The key in ``path``, after checking it is a private 32-byte seed."""
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode):
        raise NodeKeyError(f"{path} is not a regular file")
    if stat.S_IMODE(st.st_mode) != 0o600:
        raise NodeKeyError(
            f"{path} has mode {stat.S_IMODE(st.st_mode):04o}; expected 0600. "
            "Run chmod 600 on it, or move it aside to mint a new identity")
    private = path.read_text().strip()
    try:
        peertoken.public_key_of(private)
    except (peertoken.TokenError, ValueError) as exc:
        raise NodeKeyError(f"{path} does not hold a 32-byte ed25519 key: {exc}") from exc
    return private


def ensure_node_key() -> str:
    """The node's private key, minted once and kept in a 0600 file."""
    path = node_key_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not os.path.lexists(path):
        _write_new_key_file(path, peertoken.generate_private_key())
    return _read_node_key(path)


def _operator_only(as_: str | None, verb: str) -> None:
    """Refuse a verb that arrived through an edge listener.

    The edge always stamps ``X-Awm-As``; a call from this host's own CLI or
    service has none. The verbs guarded this way hand out the means to act as
    this node or to forge a session, which no caller across an edge may have.
    """
    if as_ is not None:
        raise PermissionError(
            f"{verb} is an operator verb: run it on the host, not through an edge")


def _edge_peers() -> list[dict[str, Any]]:
    """Each book peer's name and public key, for the edge to verify node
    tokens against. Never carries a secret."""
    return [{"name": r["name"], "public_key": r.get("public_key")}
            for r in config.list_records()]


def _write_peer_cred_file(peer_credential: str) -> None:
    """Mirror the current peer credential to $AWM_PEER_CRED's target file."""
    try:
        PEER_CRED_FILE.parent.mkdir(parents=True, exist_ok=True)
        PEER_CRED_FILE.write_text(peer_credential + "\n")
        PEER_CRED_FILE.chmod(0o600)
    except OSError as exc:
        log.error("auth: could not write peer-cred file %s: %s", PEER_CRED_FILE, exc)


async def mint_now(*, reason: str = "rotation") -> dict[str, Any]:
    """Mint a fresh peer credential, refresh the peer file, prune."""
    s = _settings()
    gen = store.mint_generation(validity_seconds=s.validity_hours * _HOUR)
    log.info("auth: minted credential generation %d (%s), valid ~%.0fh",
             gen["generation"], reason, s.validity_hours)
    _write_peer_cred_file(gen["peer_credential"])
    removed = store.prune_expired()
    if removed:
        log.info("auth: pruned %d expired generation(s)", removed)
    return gen


async def _mint_if_stale() -> None:
    """Mint on startup unless a fresh-enough generation already exists."""
    s = _settings()
    latest = store.latest()
    now = time.time()
    if latest is None:
        await mint_now(reason="startup (none existed)")
        return
    age = now - latest["minted_at"]
    if age >= s.mint_cadence_hours * _HOUR:
        await mint_now(reason=f"startup (newest was {age / _HOUR:.1f}h old)")
    else:
        # Still fresh — just make sure the peer-cred file matches the live value.
        _write_peer_cred_file(latest["peer_credential"])
        log.info("auth: newest generation %d is %.1fh old (< %.0fh cadence); no mint",
                 latest["generation"], age / _HOUR, s.mint_cadence_hours)


async def _rotation_loop() -> None:
    """Sleep until the next cadence boundary, mint, repeat — forever."""
    while True:
        s = _settings()
        latest = store.latest()
        cadence = s.mint_cadence_hours * _HOUR
        if latest is None:
            delay = 0.0
        else:
            delay = max(0.0, (latest["minted_at"] + cadence) - time.time())
        await asyncio.sleep(delay)
        try:
            await mint_now(reason="scheduled")
        except Exception:  # noqa: BLE001 — never let the loop die on one mint
            log.exception("auth: scheduled mint failed; retrying next cadence")
            # Avoid a tight loop if mint_now itself keeps throwing.
            await asyncio.sleep(60)


async def on_start() -> None:
    """Adapter ``on_start``: init DB, ensure secret, mint-if-stale, spawn loops."""
    store.init()
    store.ensure_secret()
    ensure_node_key()
    if not rotation_enabled():
        log.info("auth: profile %r: no peer credentials rotate here", profile())
        return
    await _mint_if_stale()
    asyncio.create_task(_rotation_loop())
    log.info("auth: rotation loop started (peer-cred file: %s)", PEER_CRED_FILE)


# ---------------------------------------------------------------------------
# Verb handlers
# ---------------------------------------------------------------------------


def h_peer_credential(args: dict) -> dict:
    """Return the current peer credential + the file path it is mirrored to."""
    latest = store.latest()
    return {
        "peer_credential": latest["peer_credential"] if latest else None,
        "path": str(PEER_CRED_FILE),
    }


def _record_device_use(used: Any) -> None:
    """Apply the edge's ``{id: epoch_seconds}`` report of device-token use."""
    if not isinstance(used, dict):
        return
    clean = {str(k): float(v) for k, v in used.items()
             if isinstance(v, (int, float)) and not isinstance(v, bool)}
    store.device_touch(clean)


def _edge_devices(enforced: Any) -> list[dict]:
    """The live tokens an edge that enforces ``enforced`` scopes may honour."""
    known = set(enforced) if isinstance(enforced, list) else set()
    out = []
    for t in store.device_live():
        entry = {"id": t["id"], "sub": t["username"], "sha256": t["secret_sha256"]}
        if t["scope"]:
            if t["scope"] not in known:
                continue
            entry["scope"] = t["scope"]
        out.append(entry)
    return out


def h_edge_material(args: dict, as_: str | None = None) -> dict:
    """Material the httpsfront edge caches to enforce auth offline.

    Returns the signing secret (to verify+slide cookies without an RPC per
    request), the currently-valid peer credentials (to check peer bearers), the
    book's peers with their public keys (to verify node tokens), the digests of
    the live device tokens (to check user bearers), and the session-lifetime
    knobs. ``device_used`` is the edge's report of the device tokens it has
    accepted since its last fetch. ``token_scopes`` names the scopes the edge
    enforces. A scoped token goes out only to an edge that names its scope,
    because an edge that cannot read the scope would honour it everywhere. Operator-only: the edge itself fetches it with no
    ``as_``, and a call that crossed an edge carries one and is refused.

    The legacy bearers are withheld once ``AWM_PEER_LEGACY_BEARER=0``, and
    always on the public profile. The keys are not secret and go out on every
    profile: whether a listener honours a node token is the edge's call, because
    a public node can also run a mesh listener that peers must reach.
    """
    _operator_only(as_, "edge_material")
    _record_device_use((args or {}).get("device_used"))
    s = _settings()
    legacy = ([g["peer_credential"] for g in store.valid_generations()]
              if rotation_enabled() and legacy_bearer_enabled() else [])
    return {
        "secret": store.ensure_secret(),
        "peer_credentials": legacy,
        "peers": _edge_peers(),
        "device_tokens": _edge_devices((args or {}).get("token_scopes")),
        "legacy_bearer": legacy_bearer_enabled(),
        "session_ttl_seconds": s.session_ttl_hours * _HOUR,
        "max_session_seconds": s.max_session_days * _DAY,
    }


def h_node_key(args: dict) -> dict:
    """This node's public key and its fingerprint, for pairing with a peer."""
    public = peertoken.public_key_of(ensure_node_key())
    return {"public_key": public, "fingerprint": peertoken.fingerprint(public)}


def h_sign_peer_token(args: dict, as_: str | None = None) -> dict:
    """A short-lived token naming this node, for the node called ``aud``.

    Operator-only: whoever can ask for this speaks as this node to every other.
    """
    _operator_only(as_, "sign_peer_token")
    aud = peertoken.node_label((args or {}).get("aud"))
    if not aud:
        raise ValueError("aud is required")
    iss = peertoken.node_label(config.node_name())
    token = peertoken.sign(ensure_node_key(), iss=iss, aud=aud)
    return {"token": token, "iss": iss, "aud": aud,
            "expires_in": peertoken.TOKEN_TTL_SECONDS}


async def h_rotate(args: dict) -> dict:
    """Force a mint now (ops/testing). Returns the new generation's window."""
    if not rotation_enabled():
        raise ValueError("no peer credentials rotate on the %r profile" % profile())
    gen = await mint_now(reason="manual rotate")
    return {
        "generation": gen["generation"],
        "minted_at": gen["minted_at"],
        "expires_at": gen["expires_at"],
    }


def h_status(args: dict) -> dict:
    valid = store.valid_generations()
    latest = store.latest()
    s = _settings()
    return {
        "valid_generations": len(valid),
        "latest_generation": latest["generation"] if latest else None,
        "latest_minted_at": latest["minted_at"] if latest else None,
        "latest_expires_at": latest["expires_at"] if latest else None,
        "mint_cadence_hours": s.mint_cadence_hours,
        "validity_hours": s.validity_hours,
        "peer_cred_path": str(PEER_CRED_FILE),
        "profile": profile() or "default",
        "rotation_enabled": rotation_enabled(),
    }


# --- device tokens -----------------------------------------------------------
#
# CLI/HTTP only, and operator-only: every token belongs to ``operator``, and a
# call that crossed an edge is refused.

_LABEL_MAX = 64


def _public_device(t: dict) -> dict:
    return {"id": t["id"], "username": t["username"], "label": t["label"],
            "created_at": t["created_at"], "last_used_at": t["last_used_at"],
            "revoked_at": t["revoked_at"], "scope": t["scope"]}


def h_device_mint(args: dict, as_: str | None = None) -> dict:
    """Mint a device token. The token is returned once, here.

    A device token speaks for one device, as ``device:<id>``, never for the
    operator, and it carries a scope that bounds what the device reaches.
    """
    _operator_only(as_, "device_mint")
    args = args or {}
    label = str(args.get("label") or "").strip()
    if not label:
        raise ValueError("label is required")
    if len(label) > _LABEL_MAX:
        raise ValueError(f"label is longer than {_LABEL_MAX} characters")
    scope = str(args.get("scope") or "").strip()
    if scope not in usertoken.SCOPES:
        raise ValueError(f"a device token needs a scope; known: "
                         f"{', '.join(sorted(usertoken.SCOPES))}")
    token_id, secret, token = usertoken.mint()
    row = store.device_insert(token_id, username=f"device:{token_id}", label=label,
                              secret_sha256=usertoken.digest(secret), scope=scope)
    log.warning("auth: minted device token %s (%s, scope %s)", token_id, label, scope)
    return {**_public_device(row), "token": token}


def h_device_list(args: dict, as_: str | None = None) -> dict:
    """Every device token, revoked ones included. Never the digest."""
    _operator_only(as_, "device_list")
    return {"tokens": [_public_device(t) for t in store.device_list()]}


def h_device_revoke(args: dict, as_: str | None = None) -> dict:
    """Revoke a device token by id. The edge drops it on its next refresh."""
    _operator_only(as_, "device_revoke")
    args = args or {}
    token_id = str(args.get("id") or "").strip()
    if not token_id:
        raise ValueError("id is required")
    if store.device_get(token_id) is None:
        raise ValueError(f"no such device token {token_id!r}")
    store.device_revoke(token_id)
    row = store.device_get(token_id)
    log.warning("auth: revoked device token %s of %s", token_id, row["username"])
    return _public_device(row)  # type: ignore[arg-type]
