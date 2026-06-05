"""Outbound federation: forward operations that target ``...@<peer>`` to the
matching remote awm instance over an SSH-tunneled HTTP connection.

Only the operations that the plan calls out are routed:
- ``send_message`` (inbox_send to a remote scope)
- Read fan-out for ``--peer all`` / ``--peer <id>``
- Room operations (post / join / leave / search) — added in M3
- Cross-host agent input (room here, agent there) — added in M3

The local awm peer identity (loaded once per call) is sent in
``X-Awm-From`` so the remote can audit-tag and sender-rewrite.
"""

from __future__ import annotations

import httpx

from awm.services import auth as auth_svc
from awm.services.network import peers as peer_svc
from awm.services.network import ssh_tunnel

# Peers serve HTTPS with self-signed certs; the bearer is the trust
# boundary. Pinning via per-peer tls_fingerprint is a follow-up.
_HTTPS_VERIFY = False


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


_preferred_endpoint: dict[str, dict] = {}


def _resolve(peer_id: str) -> tuple[str, str]:
    """Resolve a peer to ``(base_url, bearer_token)``.

    The returned bearer is **our own local auth token**, not the peer's.
    All ``/peer/*`` routes use ``require_peer_bearer`` which cross-checks
    the bearer against the receiver's stored ``peers/<X-Awm-From>.token`` —
    i.e. the sender's own auth token. Sending the peer's token instead
    fails verify_peer_bearer (it'd match local_token on the receiver,
    not peers/<sender>.token). Plain ``/peer`` (require_bearer) accepts
    either token, so ping_peer keeps working.

    Iterates the peer's ``endpoints`` list in declared order; the first
    one that yields a reachable base URL wins and is cached as preferred
    for this process. Direct endpoints (``kind=direct``) are returned as
    their advertised URL with no tunnel work. SSH endpoints
    (``kind=ssh``) open (or reuse) an SSH tunnel and return the local
    forward URL. Falls back to the legacy ssh_alias/remote_port if no
    explicit endpoints are configured.
    """
    peer = peer_svc.get_peer(peer_id)
    if peer is None:
        raise UnknownPeerError(f"unknown peer: {peer_id}")
    token = auth_svc.local_token()

    # In-process cache: stick with the last working endpoint.
    cached = _preferred_endpoint.get(peer_id)
    candidates = []
    if cached:
        candidates.append(cached)
    for ep in peer.get("endpoints", []) or []:
        if ep != cached:
            candidates.append(ep)

    last_err: Exception | None = None
    for ep in candidates:
        try:
            base = _endpoint_base_url(peer_id, ep)
        except Exception as exc:
            last_err = exc
            continue
        _preferred_endpoint[peer_id] = ep
        return base, token

    if last_err:
        raise PeerCallError(
            f"all endpoints for peer {peer_id} failed: {last_err}"
        ) from last_err
    raise PeerCallError(f"no usable endpoints for peer {peer_id}")


def _endpoint_base_url(peer_id: str, endpoint: dict) -> str:
    """Return the ``base_url`` to call for a single endpoint entry.

    For ``ssh`` endpoints this opens (or reuses) an SSH tunnel; the
    returned URL points at the local forward port. For ``direct``
    endpoints the advertised URL is returned verbatim.
    """
    kind = endpoint.get("kind")
    if kind == "direct":
        return endpoint["url"]
    if kind == "ssh":
        try:
            tun = ssh_tunnel.acquire_tunnel(peer_id)
        except ssh_tunnel.TunnelError as exc:
            raise PeerCallError(
                f"could not tunnel to peer {peer_id}: {exc}"
            ) from exc
        return tun.local_url
    raise PeerCallError(f"unknown endpoint kind for peer {peer_id}: {kind!r}")


def forward_send(target_peer_id: str, payload: dict, timeout: float = 10.0) -> dict:
    """POST a message payload to a remote peer's ``/peer/inbox``.

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
            f"{base_url}/peer/inbox",
            json=payload,
            headers=headers,
            timeout=timeout,
            verify=_HTTPS_VERIFY,
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
            timeout=timeout, verify=_HTTPS_VERIFY,
        )
    except httpx.HTTPError as exc:
        return (peer_id, None, f"{exc.__class__.__name__}: {exc}")
    if r.status_code != 200:
        return (peer_id, None, f"{r.status_code}: {r.text[:200]}")
    try:
        return (peer_id, r.json(), None)
    except ValueError:
        return (peer_id, None, "non-JSON response")


def forward_artifact_content(
    peer_id: str, legacy_id: int, *, timeout: float = 30.0,
) -> bytes:
    """Fetch raw artifact bytes from the owning peer's
    ``GET /peer/artifacts/{legacy_id}/content`` endpoint. Returns the
    response body verbatim (no JSON decode). Raises FederationError on
    transport failure or non-200; the caller turns that into a 404/502.

    Timeout default is 30s — content can be larger than DB payloads.
    """
    base_url, token = _resolve(peer_id)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Awm-From": _local_peer_id(),
    }
    try:
        r = httpx.get(
            f"{base_url}/peer/artifacts/{legacy_id}/content",
            headers=headers, timeout=timeout, verify=_HTTPS_VERIFY,
        )
    except httpx.HTTPError as exc:
        raise FederationError(
            f"artifact content fetch from {peer_id} failed: "
            f"{exc.__class__.__name__}: {exc}"
        ) from exc
    if r.status_code != 200:
        raise FederationError(
            f"artifact content fetch from {peer_id}: HTTP {r.status_code}",
            status_code=r.status_code,
        )
    return r.content


def forward_lock(
    target_peer_id: str, op: str, payload: dict, *, timeout: float = 5.0,
) -> dict:
    """Forward a lock op to ``target_peer_id``'s ``POST /peer/lock/{op}``.
    ``op`` is one of ``acquire`` / ``release`` / ``heartbeat``. Returns
    the parsed JSON; raises FederationError on transport failure or
    non-200."""
    if op not in ("acquire", "release", "heartbeat"):
        raise ValueError(f"unknown lock op: {op}")
    base_url, token = _resolve(target_peer_id)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Awm-From": _local_peer_id(),
        "Content-Type": "application/json",
    }
    try:
        r = httpx.post(
            f"{base_url}/peer/lock/{op}", headers=headers, json=payload,
            timeout=timeout, verify=_HTTPS_VERIFY,
        )
    except httpx.HTTPError as exc:
        raise FederationError(
            f"lock {op} to {target_peer_id} failed: "
            f"{exc.__class__.__name__}: {exc}"
        ) from exc
    if r.status_code != 200:
        raise FederationError(
            f"lock {op} to {target_peer_id}: HTTP {r.status_code}: {r.text[:200]}",
            status_code=r.status_code,
        )
    try:
        return r.json()
    except ValueError as exc:
        raise FederationError(
            f"lock {op} to {target_peer_id}: non-JSON response",
        ) from exc


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


# ---------------------------------------------------------------------------
# Room federation forwards (M3)
# ---------------------------------------------------------------------------

def forward_room_search(peer_id: str, query: str | None = None, *,
                        status: str = "active",
                        participating_scope: str | None = None,
                        limit: int = 50,
                        timeout: float = 5.0) -> dict:
    """Federated room search. `query` is optional — when omitted, returns
    rooms filtered by `status` (default 'active') and `participating_scope`."""
    base_url, token = _resolve(peer_id)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Awm-From": _local_peer_id(),
    }
    params: dict = {"status": status, "limit": limit}
    if query:
        params["query"] = query
    if participating_scope:
        params["participating_scope"] = participating_scope
    try:
        r = httpx.get(f"{base_url}/rooms/search",
                      params=params, headers=headers,
                      timeout=timeout, verify=_HTTPS_VERIFY)
    except httpx.HTTPError as exc:
        raise PeerCallError(f"could not reach peer {peer_id}: {exc}") from exc
    if r.status_code != 200:
        raise PeerCallError(f"peer {peer_id} returned {r.status_code}",
                            status_code=r.status_code)
    return r.json()


def forward_room_post(peer_id: str, room_id: str, *,
                      body: str, kind: str = "text",
                      author: str | None = None,
                      to_scope: str | None = None,
                      timeout: float = 5.0) -> dict:
    base_url, token = _resolve(peer_id)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Awm-From": _local_peer_id(),
        "Content-Type": "application/json",
    }
    if author:
        headers["X-Awm-As"] = author
    payload = {"body": body, "kind": kind}
    if to_scope:
        payload["to"] = to_scope
    try:
        r = httpx.post(f"{base_url}/peer/rooms/{room_id}/posts",
                       json=payload, headers=headers, timeout=timeout, verify=_HTTPS_VERIFY)
    except httpx.HTTPError as exc:
        raise PeerCallError(f"could not reach peer {peer_id}: {exc}") from exc
    if r.status_code != 200:
        raise PeerCallError(f"peer {peer_id} returned {r.status_code}: {r.text[:200]}",
                            status_code=r.status_code)
    return r.json()


def forward_room_join(peer_id: str, room_id: str, *,
                      timeout: float = 5.0) -> dict:
    """Register *this* peer as a shadow_peer participant on a remote room."""
    base_url, token = _resolve(peer_id)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Awm-From": _local_peer_id(),
        "Content-Type": "application/json",
    }
    try:
        r = httpx.post(f"{base_url}/peer/rooms/{room_id}/shadow-join",
                       json={"peer_id": _local_peer_id()},
                       headers=headers, timeout=timeout, verify=_HTTPS_VERIFY)
    except httpx.HTTPError as exc:
        raise PeerCallError(f"could not reach peer {peer_id}: {exc}") from exc
    if r.status_code != 200:
        raise PeerCallError(f"peer {peer_id} returned {r.status_code}",
                            status_code=r.status_code)
    return r.json()


def forward_room_leave(peer_id: str, room_id: str, *,
                       timeout: float = 5.0) -> dict:
    base_url, token = _resolve(peer_id)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Awm-From": _local_peer_id(),
        "Content-Type": "application/json",
    }
    try:
        r = httpx.post(f"{base_url}/peer/rooms/{room_id}/shadow-leave",
                       json={"peer_id": _local_peer_id()},
                       headers=headers, timeout=timeout, verify=_HTTPS_VERIFY)
    except httpx.HTTPError as exc:
        raise PeerCallError(f"could not reach peer {peer_id}: {exc}") from exc
    if r.status_code != 200:
        raise PeerCallError(f"peer {peer_id} returned {r.status_code}",
                            status_code=r.status_code)
    return r.json()


def forward_agent_input(peer_id: str, room_id: str, scope: str, *,
                        body: str, author: str,
                        timeout: float = 5.0) -> dict:
    """Push a single input frame to a remote scope (for the case where
    the room is hosted here but the agent lives on ``peer_id``)."""
    base_url, token = _resolve(peer_id)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Awm-From": _local_peer_id(),
        "Content-Type": "application/json",
    }
    payload = {"scope": scope, "room_id": room_id, "body": body, "author": author}
    try:
        r = httpx.post(f"{base_url}/peer/rooms/agent-input",
                       json=payload, headers=headers, timeout=timeout, verify=_HTTPS_VERIFY)
    except httpx.HTTPError as exc:
        raise PeerCallError(f"could not reach peer {peer_id}: {exc}") from exc
    if r.status_code != 200:
        raise PeerCallError(f"peer {peer_id} returned {r.status_code}",
                            status_code=r.status_code)
    return r.json()
