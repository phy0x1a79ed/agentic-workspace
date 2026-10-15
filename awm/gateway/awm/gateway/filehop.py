"""Carry local files across a peer hop inside one ``/invoke`` call.

A verb whose service runs on another node reads its file arguments from, and
writes its file results to, *that* node's disk. The manifest marks the verbs
where that matters: a param with ``"file": "in"`` names a local file or folder
the caller sends, and a function with ``"returns_files": true`` returns
``files[].path`` the caller wants back. The catalog projects both into the
verb's schema (:data:`FILE_KEY`, :data:`RETURNS_KEY`), so ``describe`` on the
far node tells the MCP proxy what to carry.

The body that carries them is a tar stream of type :data:`CONTENT_TYPE`:
``call.json`` first, then ``files/<param>/<name>`` for each sent file or folder.
A response carrying files is the same shape, with ``result.json`` and
``files/<i>/<name>`` for ``files[i]``. Tar keeps folders as folders and needs
nothing beyond the standard library on either side. Extraction uses the
``data`` filter, so a member cannot land outside its spool directory.
"""

from __future__ import annotations

import json
import os
import shutil
import tarfile
import tempfile
import time
from collections.abc import Iterator
from typing import Any

CONTENT_TYPE = "application/x-awm-call+tar"
#: Request header: the caller can take files back in a tar response.
RETURN_FILES_HEADER = "X-Awm-Return-Files"
#: inputSchema property key marking a param as an inbound file.
FILE_KEY = "x-awm-file"
#: inputSchema key marking a verb whose result lists files to hand back.
RETURNS_KEY = "x-awm-returns-files"
#: inputSchema key carrying the verb's manifest timeout, in seconds.
TIMEOUT_KEY = "x-awm-timeout"

_CHUNK = 1 << 20


# -- verb spec ---------------------------------------------------------------

def spec_from_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    """``{file_params, returns_files, timeout}`` from one verb's inputSchema."""
    schema = schema or {}
    props = schema.get("properties") or {}
    return {
        "file_params": [k for k, p in props.items()
                        if isinstance(p, dict) and p.get(FILE_KEY) == "in"],
        "returns_files": bool(schema.get(RETURNS_KEY)),
        "timeout": schema.get(TIMEOUT_KEY),
    }


def spec_from_describe(verb: dict[str, Any] | None) -> dict[str, Any]:
    """The same spec from one verb of a ``describe`` answer, which names a file
    param with ``"file": "in"`` and a file-returning verb with ``returns_files``."""
    verb = verb or {}
    params = verb.get("params") or {}
    return {
        "file_params": [k for k, p in params.items()
                        if isinstance(p, dict) and p.get("file") == "in"],
        "returns_files": bool(verb.get("returns_files")),
        "timeout": None,
    }


def inner_args(call: dict[str, Any]) -> dict[str, Any]:
    """The verb's own args: nested under ``args`` for a domain call, else flat."""
    args = call.setdefault("args", {})
    if "verb" in args:
        inner = args.get("args")
        if not isinstance(inner, dict):
            inner = args["args"] = {}
        return inner
    return args


# -- request: caller packs, far gateway unpacks ------------------------------

def local_files(call: dict[str, Any], file_params: list[str]) -> dict[str, str]:
    """``{param: absolute path}`` for each file param naming a path that exists
    here. A value that does not exist locally is left for the far side."""
    inner = inner_args(call)
    out: dict[str, str] = {}
    for param in file_params:
        val = inner.get(param)
        if isinstance(val, str) and val:
            path = os.path.abspath(os.path.expanduser(val))
            if os.path.exists(path):
                out[param] = path
    return out


def _add_json(tar: tarfile.TarFile, name: str, obj: Any) -> None:
    data = json.dumps(obj).encode()
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = int(time.time())
    tar.addfile(info, _BytesReader(data))


class _BytesReader:
    def __init__(self, data: bytes) -> None:
        self._data, self._pos = data, 0

    def read(self, n: int = -1) -> bytes:
        end = len(self._data) if n < 0 else self._pos + n
        out = self._data[self._pos:end]
        self._pos += len(out)
        return out


def pack_call(call: dict[str, Any], files: dict[str, str]):
    """Write the call and its files to a temp tar → open binary file at 0."""
    call = json.loads(json.dumps(call))
    call["files"] = {}
    fh = tempfile.TemporaryFile(prefix="awm-hop-")
    with tarfile.open(fileobj=fh, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for param, path in files.items():
            call["files"][param] = f"files/{param}/{os.path.basename(path.rstrip('/'))}"
        _add_json(tar, "call.json", call)
        for param, path in files.items():
            tar.add(path, arcname=call["files"][param])
    fh.seek(0)
    return fh


def unpack_call(tar_path: str, spool: str) -> dict[str, Any]:
    """Extract a packed call into ``spool`` and point each file param at its
    spooled copy → the call, ready for dispatch."""
    with tarfile.open(tar_path, mode="r") as tar:
        member = tar.extractfile("call.json")
        if member is None:
            raise ValueError("file-hop body has no call.json")
        call = json.loads(member.read())
        files = [m for m in tar.getmembers() if m.name.startswith("files/")]
        tar.extractall(spool, members=files, filter="data")
    root = os.path.realpath(spool)
    inner = inner_args(call)
    for param, rel in (call.pop("files", None) or {}).items():
        path = os.path.realpath(os.path.join(spool, rel))
        if not path.startswith(root + os.sep) or not os.path.exists(path):
            raise ValueError(f"file-hop body is missing {rel!r}")
        inner[param] = path
    return call


# -- response: far gateway streams, caller unpacks ---------------------------

def result_paths(result: str) -> list[tuple[int, str]]:
    """``(i, path)`` for each ``files[i].path`` that may go back: it exists and
    sits under the system temp dir, where every file-returning verb writes."""
    try:
        data = json.loads(result)
    except (TypeError, ValueError):
        return []
    files = data.get("files") if isinstance(data, dict) else None
    tmp = os.path.realpath(tempfile.gettempdir())
    out = []
    for i, f in enumerate(files or []):
        if isinstance(f, dict) and isinstance(f.get("path"), str):
            real = os.path.realpath(f["path"])
            if real.startswith(tmp + os.sep) and os.path.exists(real):
                out.append((i, f["path"]))
    return out


def _header(name: str, size: int, kind: bytes = tarfile.REGTYPE,
            mode: int = 0o644) -> bytes:
    info = tarfile.TarInfo(name)
    info.size = size if kind == tarfile.REGTYPE else 0
    info.type = kind
    info.mode = mode
    info.mtime = int(time.time())
    return info.tobuf(format=tarfile.PAX_FORMAT)


def _pad(size: int) -> bytes:
    return b"\0" * (-size % tarfile.BLOCKSIZE)


def _iter_file(path: str, arcname: str) -> Iterator[bytes]:
    size = os.path.getsize(path)
    yield _header(arcname, size)
    sent = 0
    with open(path, "rb") as fh:
        while sent < size:
            block = fh.read(min(_CHUNK, size - sent))
            if not block:
                raise OSError(f"{path} shrank while being sent")
            sent += len(block)
            yield block
    yield _pad(size)


def iter_result(result: str, paths: list[tuple[int, str]]) -> Iterator[bytes]:
    """Stream ``result.json`` and each ``(i, path)`` as ``files/<i>/…`` in a tar,
    never holding a file whole."""
    data = json.dumps({"result": result}).encode()
    yield _header("result.json", len(data))
    yield data
    yield _pad(len(data))
    for i, path in paths:
        arc = f"files/{i}/{os.path.basename(path.rstrip('/'))}"
        if os.path.isdir(path):
            yield _header(arc, 0, tarfile.DIRTYPE, 0o755)
            for top, dirs, names in os.walk(path):
                dirs.sort()
                rel = os.path.relpath(top, path)
                base = arc if rel == "." else f"{arc}/{rel}"
                for d in dirs:
                    yield _header(f"{base}/{d}", 0, tarfile.DIRTYPE, 0o755)
                for n in sorted(names):
                    yield from _iter_file(os.path.join(top, n), f"{base}/{n}")
        else:
            yield from _iter_file(path, arc)
    yield b"\0" * (2 * tarfile.BLOCKSIZE)


def remove_sent(paths: list[str]) -> None:
    """Delete files handed back to the caller, and their temp dirs once empty.
    Only paths under the system temp dir are touched."""
    tmp = os.path.realpath(tempfile.gettempdir())
    for path in paths:
        real = os.path.realpath(path)
        if not real.startswith(tmp + os.sep):
            continue
        if os.path.isdir(real):
            shutil.rmtree(real, ignore_errors=True)
        else:
            try:
                os.remove(real)
            except OSError:
                pass
        parent = os.path.dirname(real)
        if parent != tmp:
            try:
                os.rmdir(parent)
            except OSError:
                pass


def unpack_result(tar_path: str, dest: str) -> str:
    """Extract a files response into ``dest`` → the result string with every
    ``files[i].path`` (and ``dir``) rewritten to the local copy. A file that
    did not come back gets ``path: null`` and an ``error``, never a far path."""
    with tarfile.open(tar_path, mode="r") as tar:
        member = tar.extractfile("result.json")
        if member is None:
            raise ValueError("file-hop response has no result.json")
        result = json.loads(member.read())["result"]
        files = [m for m in tar.getmembers() if m.name.startswith("files/")]
        tar.extractall(dest, members=files, filter="data")
        sent = {m.name.split("/")[1] for m in files}
    data = json.loads(result)
    local: list[str] = []
    for i, f in enumerate(data.get("files") or []):
        if isinstance(f, dict) and isinstance(f.get("path"), str):
            if str(i) not in sent:
                f["remote_path"], f["path"] = f["path"], None
                f["error"] = "not sent back: missing, or outside the far node's temp dir"
                continue
            f["remote_path"], f["path"] = f["path"], os.path.join(
                dest, "files", str(i), os.path.basename(f["path"].rstrip("/")))
            f.pop("url", None)  # the far gateway deleted the file once sent
            local.append(f["path"])
    if "dir" in data:
        data["dir"] = os.path.dirname(local[0]) if len(local) == 1 else dest
    return json.dumps(data)
