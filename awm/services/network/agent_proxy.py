"""Cross-peer room subscription proxy.

When a local subscriber joins a room hosted on another peer
(``flowing-mountain@crux``), we don't open one WebSocket per local
client — instead, we maintain **one upstream WS per (peer, room)** and
fan its events out to all local subscribers via the same
``rooms.subscribe_room`` mechanism that local rooms use.

This module is intentionally small: the heavy lifting (subscriber
attach, transcript backlog, post forwarding) is handled by the rooms
HTTP/WS router; here we only need to open/close the upstream and
forward inbound events into the local broadcast bus.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import websockets

from awm.services import rooms as rooms_svc
from awm.services.network import federation, ssh_tunnel, peers as peer_svc


@dataclass
class RemoteRoomProxy:
    peer_id: str
    room_id: str
    refcount: int = 0
    ws_task: asyncio.Task | None = None
    stop: asyncio.Event = field(default_factory=asyncio.Event)


_proxies: dict[tuple[str, str], RemoteRoomProxy] = {}
_lock = asyncio.Lock()


async def acquire(peer_id: str, room_id: str) -> RemoteRoomProxy:
    """Get (and refcount) a proxy. Opens the upstream WS on first acquire."""
    key = (peer_id, room_id)
    async with _lock:
        proxy = _proxies.get(key)
        if proxy is None:
            proxy = RemoteRoomProxy(peer_id=peer_id, room_id=room_id)
            _proxies[key] = proxy
            proxy.ws_task = asyncio.create_task(_run(proxy))
        proxy.refcount += 1
        return proxy


async def release(peer_id: str, room_id: str) -> None:
    key = (peer_id, room_id)
    async with _lock:
        proxy = _proxies.get(key)
        if proxy is None:
            return
        proxy.refcount -= 1
        if proxy.refcount > 0:
            return
        proxy.stop.set()
        _proxies.pop(key, None)
    try:
        federation.forward_room_leave(peer_id, room_id)
    except federation.FederationError:
        pass
    if proxy.ws_task is not None:
        proxy.ws_task.cancel()


async def _run(proxy: RemoteRoomProxy) -> None:
    """Maintain the upstream WS, re-opening on disconnect, until refcount=0."""
    # Step 1: register ourselves as a shadow_peer on the remote.
    try:
        federation.forward_room_join(proxy.peer_id, proxy.room_id)
    except federation.FederationError as exc:
        rooms_svc._broadcast(proxy.room_id, {
            "type": "upstream_disconnected",
            "peer": proxy.peer_id, "reason": f"join failed: {exc}",
        })
        return

    # Step 2: open the WS via the SSH tunnel.
    try:
        tun = ssh_tunnel.acquire_tunnel(proxy.peer_id)
    except ssh_tunnel.TunnelError as exc:
        rooms_svc._broadcast(proxy.room_id, {
            "type": "upstream_disconnected",
            "peer": proxy.peer_id, "reason": f"tunnel error: {exc}",
        })
        return

    token = peer_svc.load_peer_token(proxy.peer_id)
    ws_url = tun.local_url.replace("http://", "ws://") + f"/rooms/{proxy.room_id}/attach"

    while not proxy.stop.is_set():
        try:
            async with websockets.connect(
                ws_url,
                subprotocols=[f"bearer.{token}"],
                additional_headers=[("X-Awm-From", federation._local_peer_id())],
            ) as ws:
                async for raw in ws:
                    if proxy.stop.is_set():
                        break
                    try:
                        ev = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    # Re-broadcast into the local event bus.
                    rooms_svc._broadcast(proxy.room_id, ev)
        except (websockets.WebSocketException, OSError) as exc:
            if proxy.stop.is_set():
                break
            rooms_svc._broadcast(proxy.room_id, {
                "type": "upstream_disconnected",
                "peer": proxy.peer_id, "reason": str(exc),
            })
            # Backoff before reconnect.
            await asyncio.sleep(2)
