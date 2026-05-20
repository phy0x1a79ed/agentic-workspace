"""Outbound federation: forward operations that target ``...@<peer>`` to the
matching remote awm instance.

Only the operations that the plan calls out are routed:
- ``send_message`` (inbox_send to a remote scope)
- Read fan-out for ``--peer all`` / ``--peer <id>`` (separate helper, M5)

The local awm peer identity (loaded once per call) is sent in
``X-Awm-From`` so the remote can audit-tag and sender-rewrite.
"""

from __future__ import annotations

import httpx

from awm.services.network import peers as peer_svc


class FederationError(Exception):
    """Base class for federation-related failures."""


class UnknownPeerError(FederationError):
    """Target peer is not in the local registry."""


class LocalIdentityRequiredError(FederationError):
    """Local peer identity is not configured but federation was requested."""


class PeerCallError(FederationError):
    """Remote peer returned a non-2xx response or was unreachable."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _local_peer_id() -> str:
    ident = peer_svc.get_local_identity()
    if ident is None:
        raise LocalIdentityRequiredError(
            "this awm has no local peer identity; run `awm peer init` first"
        )
    return ident["peer_id"]


def _resolve(peer_id: str) -> tuple[str, str]:
    peer = peer_svc.get_peer(peer_id)
    if peer is None:
        raise UnknownPeerError(f"unknown peer: {peer_id}")
    return peer["base_url"].rstrip("/"), peer_svc.load_peer_token(peer_id)


def forward_send(target_peer_id: str, payload: dict, timeout: float = 10.0) -> dict:
    """POST a message payload to a remote peer's ``/inbox``.

    ``payload`` is the message body with ``scope`` already stripped of the
    ``@<peer-id>`` suffix — only the base scope identifier is forwarded.
    Returns the remote ``MessageActionResponse`` as a plain dict.
    """
    base_url, token = _resolve(target_peer_id)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Awm-From": _local_peer_id(),
        "Content-Type": "application/json",
    }
    try:
        r = httpx.post(
            f"{base_url}/inbox",
            json=payload,
            headers=headers,
            timeout=timeout,
            verify=False,
        )
    except httpx.HTTPError as exc:
        raise PeerCallError(f"could not reach peer {target_peer_id}: {exc}") from exc

    if r.status_code != 200:
        raise PeerCallError(
            f"peer {target_peer_id} returned {r.status_code}: {r.text[:200]}",
            status_code=r.status_code,
        )

    try:
        return r.json()
    except ValueError as exc:
        raise PeerCallError(f"peer {target_peer_id} returned non-JSON") from exc


# ---------------------------------------------------------------------------
# Read fan-out
# ---------------------------------------------------------------------------

def _peer_get(peer_id: str, path: str, params: dict | None, timeout: float) -> tuple[str, dict | None, str | None]:
    """Single peer call. Returns (peer_id, body, error_str). Never raises."""
    try:
        base_url, token = _resolve(peer_id)
    except FederationError as exc:
        return (peer_id, None, str(exc))
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Awm-From": _local_peer_id(),
    }
    try:
        r = httpx.get(
            f"{base_url}{path}", params=params, headers=headers,
            timeout=timeout, verify=False,
        )
    except httpx.HTTPError as exc:
        return (peer_id, None, f"{exc.__class__.__name__}: {exc}")
    if r.status_code != 200:
        return (peer_id, None, f"{r.status_code}: {r.text[:200]}")
    try:
        return (peer_id, r.json(), None)
    except ValueError:
        return (peer_id, None, "non-JSON response")


def fan_out_get(
    peer_ids: list[str],
    path: str,
    params: dict | None = None,
    *,
    result_key: str,
    timeout: float = 5.0,
) -> dict:
    """GET a path across multiple peers and merge the results.

    Each successful response is expected to be a JSON object containing
    a list at ``result_key`` (e.g. ``"skills"``, ``"scopes"``). Each
    item in that list is tagged with ``origin_peer_id`` before merging.
    A peer that times out or errors goes into ``degraded`` and is
    omitted from the merged list (the operation still returns 200).
    """
    merged: list[dict] = []
    degraded: list[dict] = []
    for pid in peer_ids:
        _, body, err = _peer_get(pid, path, params, timeout)
        if err is not None:
            degraded.append({"peer_id": pid, "reason": err})
            continue
        items = body.get(result_key, []) if isinstance(body, dict) else []
        for it in items:
            if isinstance(it, dict):
                it = {**it, "origin_peer_id": pid}
            merged.append(it)
    return {result_key: merged, "total": len(merged), "degraded": degraded}
