"""Turn a peer's file references into local files, for the MCP proxies.

A verb that produces a file (``social download_attachments``, ``social
bucket_get``) writes the bytes to a temp dir **on the node that ran it** and
returns that absolute path. Run locally that is exactly right. Run on a peer it
is a path to nothing: the call reports success and the caller cannot open the
result — the whole failure this module exists to remove.

The proxy is the only layer that knows a given call was routed to a peer (it
followed the gateway's redirect and dialled the peer edge itself), so the
rewrite belongs here. Each file in the reply carries a ``url`` alongside its
``path``; we pull the bytes down over the peer's ``fileviewer`` mount and swap
``path`` for the local copy, moving the peer's own path to ``remote_path`` so
the reply stays honest about where it came from.

Two rules this must never break:

* **Never leave a foreign path in ``path``.** A file we could not fetch gets
  ``path: null`` plus an ``error`` naming why. Half the point is that a caller
  can trust ``path`` to be openable; a phantom path that merely looks plausible
  is worse than an error.
* **One file's failure must not lose the others.** A masked ``*.pem`` in a
  three-attachment message still leaves two downloadable files.

Both ``mcp_stdio`` (the default proxy) and ``mcp_server_sdk`` (the documented
rollback) import this, so the behaviour cannot go missing on a rollback.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from typing import Any

log = logging.getLogger("awm.gateway.peer_files")

#: Ceiling on how many files one reply may pull down. A reply is a message's
#: attachments or one bucket file, so this is a runaway guard, not a policy.
_MAX_FILES = 64


def materialize(result_text: Any, peer: str, *, entry: dict | None = None,
                as_: str | None = None) -> Any:
    """Rewrite a peer reply's ``files[]`` to point at locally-downloaded copies.

    ``result_text`` is the proxy's tool result — a JSON **string**, not a dict.
    Anything that is not a JSON object with a ``files`` list of dicts is returned
    byte-for-byte unchanged, so this is safe to run over every peer-routed reply
    regardless of verb. Never raises: a total failure degrades to per-file
    ``error`` fields.
    """
    if not isinstance(result_text, str):
        return result_text
    try:
        obj = json.loads(result_text)
    except (json.JSONDecodeError, ValueError):
        return result_text
    if not isinstance(obj, dict):
        return result_text
    files = obj.get("files")
    if not isinstance(files, list) or not files:
        return result_text
    if not any(isinstance(f, dict) and f.get("url") for f in files):
        return result_text

    from awm import gatewayclient  # deferred: only this path needs httpx + TLS

    dest_dir = tempfile.mkdtemp(prefix="awm-peer-files-")
    landed = 0
    for idx, f in enumerate(files):
        if not isinstance(f, dict):
            continue
        remote_path = f.get("path")
        if remote_path is not None:
            f["remote_path"] = remote_path
        url = f.get("url")
        if idx >= _MAX_FILES:
            f["path"] = None
            f["error"] = f"not fetched: reply exceeds the {_MAX_FILES}-file limit"
            continue
        if not url:
            f["path"] = None
            f["error"] = (f"{peer} returned no url for this file, so its bytes "
                          f"cannot be fetched to this node")
            continue
        try:
            local = gatewayclient.fetch_peer_file_sync(
                peer, url, dest_dir=dest_dir,
                filename=f.get("filename") or None, entry=entry, as_=as_)
        except Exception as exc:  # noqa: BLE001 — one file's failure is per-file
            log.warning("peer file fetch failed (%s %s): %s", peer, url, exc)
            f["path"] = None
            f["error"] = f"{type(exc).__name__}: {exc}"
            continue
        f["path"] = local
        f.pop("error", None)
        landed += 1

    if obj.get("dir") is not None:
        obj["remote_dir"] = obj["dir"]
        obj["dir"] = dest_dir if landed else None
    obj.setdefault("node", peer)
    obj["fetched_from"] = peer
    if not landed:
        try:
            os.rmdir(dest_dir)  # nothing arrived — don't leave an empty temp dir
        except OSError:
            pass
    return json.dumps(obj)


async def materialize_async(result_text: Any, peer: str, *,
                            entry: dict | None = None,
                            as_: str | None = None) -> Any:
    """:func:`materialize` off the event loop — it does blocking ssh + I/O."""
    return await asyncio.to_thread(
        materialize, result_text, peer, entry=entry, as_=as_)
