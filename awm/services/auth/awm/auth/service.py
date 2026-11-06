"""Auth service orchestration: the session secret, device tokens and the verb handlers.

The service owns one authoritative credential state (via :mod:`awm.auth.store`)
and is the *authority*; the ``httpsfront`` edge only *enforces*, using material
this service hands it (the signing secret and the device token digests) over loopback.

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
"""

from __future__ import annotations

import logging
from typing import Any

from awm.config import usertoken

from awm.auth import store
from awm.auth.config import CONTRACT

log = logging.getLogger("awm.auth.service")

_HOUR = 3600.0
_DAY = 86400.0

#: The owner of every device token.
OPERATOR = "operator"


def _settings() -> Any:
    """Current config values (defaults merged with stored overrides)."""
    return CONTRACT.load_model()


def _operator_only(as_: str | None, verb: str) -> None:
    """Refuse a verb that arrived through an edge listener.

    The edge always stamps ``X-Awm-As``; a call from this host's own CLI or
    service has none. The verbs guarded this way hand out the means to act as
    this node or to forge a session, which no caller across an edge may have.
    """
    if as_ is not None:
        raise PermissionError(
            f"{verb} is an operator verb: run it on the host, not through an edge")


async def on_start() -> None:
    """Adapter ``on_start``: init the DB and make sure the signing secret exists."""
    store.init()
    store.ensure_secret()


# ---------------------------------------------------------------------------
# Verb handlers
# ---------------------------------------------------------------------------


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
    request), the digests of the live device tokens (to check user bearers), and
    the session-lifetime knobs. ``device_used`` is the edge's report of the device
    tokens it has accepted since its last fetch. ``token_scopes`` names the scopes
    the edge enforces. A scoped token goes out only to an edge that names its
    scope, because an edge that cannot read the scope would honour it everywhere.
    Operator-only: the edge itself fetches it with no ``as_``, and a call that
    crossed an edge carries one and is refused.
    """
    _operator_only(as_, "edge_material")
    _record_device_use((args or {}).get("device_used"))
    s = _settings()
    return {
        "secret": store.ensure_secret(),
        "device_tokens": _edge_devices((args or {}).get("token_scopes")),
        "session_ttl_seconds": s.session_ttl_hours * _HOUR,
        "max_session_seconds": s.max_session_days * _DAY,
    }


def h_status(args: dict) -> dict:
    s = _settings()
    return {"session_ttl_hours": s.session_ttl_hours, "max_session_days": s.max_session_days,
            "device_tokens": len(store.device_live())}


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
