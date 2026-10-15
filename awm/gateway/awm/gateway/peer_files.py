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

A verb that declares the file hop (:mod:`awm.gateway.filehop`) does not need
this pull: :func:`invoke_with_files` sends its local file arguments up with the
call and takes its result files back in the same response, folders included.

Both ``mcp_stdio`` (the default proxy) and ``mcp_server_sdk`` (the documented
rollback) import this, so the behaviour cannot go missing on a rollback.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
from typing import Any

from awm.gateway import filehop

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


# -- the one-call file hop (:mod:`awm.gateway.filehop`) -----------------------

#: Seconds a peer verb's file-hop spec stays cached in this proxy.
_SPEC_TTL_S = 600.0
_spec_cache: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}


def invoke_with_files(peer: str, entry: dict | None, call: dict[str, Any],
                      as_: str | None = None):
    """Send ``call`` to ``peer`` as one tar stream when its verb carries files.

    The far verb's ``describe`` says which params are local files to send and
    whether its result's files come back. Returns the edge's ``httpx.Response``,
    whose JSON ``result`` already names local paths, or ``None`` when the verb
    carries nothing and the caller should send plain JSON.
    """
    from awm import gatewayclient  # deferred: only this path needs httpx + TLS

    verb = (call.get("args") or {}).get("verb")
    if not isinstance(verb, str) or verb == "describe":
        return None
    if not entry or not entry.get("edge_url"):
        entry = gatewayclient.resolve_peer(peer)
    spec = _hop_spec(peer, entry, call["name"], verb, as_)
    if not spec:
        return None
    files = filehop.local_files(call, spec["file_params"])
    if not files and not spec["returns_files"]:
        return None
    return _post_files(peer, entry, call, files, spec["returns_files"], as_)


def _hop_spec(peer: str, entry: dict, tool: str, verb: str,
              as_: str | None) -> dict[str, Any] | None:
    """The far verb's file-hop spec, or None when it carries no files or the
    peer cannot say (an older peer then gets plain JSON)."""
    key = (entry["edge_url"], tool, verb)
    hit = _spec_cache.get(key)
    if hit and time.monotonic() - hit[0] < _SPEC_TTL_S:
        return hit[1] or None
    import httpx  # noqa: PLC0415
    from awm import gatewayclient  # noqa: PLC0415

    edge, ca = entry["edge_url"].rstrip("/"), gatewayclient._peer_ca()
    describe = {"name": tool, "args": {"verb": "describe", "args": {"verb": verb}}}

    def send(bearer: str):
        with httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0), verify=ca) as cli:
            return cli.post(f"{edge}/invoke", json=describe,
                            headers=_headers(bearer, as_))

    try:
        resp = gatewayclient.peer_send_sync(peer, entry, send)
        resp.raise_for_status()
        verbs = json.loads(resp.json()["result"]).get("verbs") or []
        found = filehop.spec_from_describe(verbs[0] if verbs else None)
    except Exception as exc:  # noqa: BLE001
        log.info("no file-hop spec for %s %s on %s: %s", tool, verb, peer, exc)
        return None
    spec = found if found["file_params"] or found["returns_files"] else {}
    _spec_cache[key] = (time.monotonic(), spec)
    return spec or None


def _headers(bearer: str, as_: str | None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {bearer}"}
    if as_:
        headers["X-Awm-As"] = as_
    return headers


def _post_files(peer: str, entry: dict, call: dict[str, Any],
                files: dict[str, str], returns_files: bool, as_: str | None):
    import httpx  # noqa: PLC0415
    from awm import gatewayclient  # noqa: PLC0415
    from awm.gateway import mcp_http  # noqa: PLC0415

    edge, ca = entry["edge_url"].rstrip("/"), gatewayclient._peer_ca()
    timeout = httpx.Timeout(mcp_http.read_timeout(), connect=15.0)
    body = filehop.pack_call(call, files)
    size = os.fstat(body.fileno()).st_size

    def send(bearer: str):
        headers = _headers(bearer, as_)
        headers["Content-Type"] = filehop.CONTENT_TYPE
        headers["Content-Length"] = str(size)
        if returns_files:
            headers[filehop.RETURN_FILES_HEADER] = "1"
        body.seek(0)
        with httpx.Client(timeout=timeout, verify=ca) as cli:
            with cli.stream("POST", f"{edge}/invoke", content=_chunks(body),
                            headers=headers) as resp:
                if resp.status_code == 200 and resp.headers.get(
                        "content-type", "").startswith(filehop.CONTENT_TYPE):
                    return httpx.Response(200, json={"result": _receive(resp)})
                resp.read()
                return resp

    try:
        return gatewayclient.peer_send_sync(peer, entry, send)
    finally:
        body.close()


def _chunks(fh, size: int = 1 << 20):
    while block := fh.read(size):
        yield block


def _receive(resp) -> str:
    """Spool a files response to disk, then unpack it into a fresh temp dir."""
    dest = tempfile.mkdtemp(prefix="awm-peer-")
    tar_path = os.path.join(dest, ".result.tar")
    try:
        with open(tar_path, "wb") as fh:
            for chunk in resp.iter_bytes():
                fh.write(chunk)
        return filehop.unpack_result(tar_path, dest)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    finally:
        if os.path.exists(tar_path):
            os.remove(tar_path)
