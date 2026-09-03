"""Reading the Zotero library, wherever the machine holding it happens to be.

Zotero is a desktop application. It keeps one SQLite database and one folder of
stored files, and it publishes a read-only copy of the Zotero Web API on
`127.0.0.1:23119` when the user has ticked "Allow other applications on this
computer to communicate with Zotero".

**The API is the read path; the database file is not.** `zotero.sqlite` is open
and journalled the whole time Zotero runs, so a copy taken from underneath it
is a copy of a state that may never have existed. The API answers from the
running application, which is the same reason `trilium` snapshots through ETAPI
rather than copying `document.db`.

**Three facts about reaching it that are not guessable.**

*The host header is checked.* Zotero's server refuses any request whose `Host`
is not localhost — a defence against DNS rebinding — and answers `400 Bad
Request` with no explanation. Reaching it from anywhere but the loopback
interface therefore means sending the address in the URL and `127.0.0.1` in the
header. Without that this looks like a Zotero that is running but broken.

*The file endpoint hands back a path, not the bytes.* `/items/<key>/file`
answers `302` with a `file:///C:/…` location. So the PDF comes off the
filesystem, and on a Windows host reached through WSL that means translating
the drive letter to its `/mnt/` mount. An endpoint that redirects to the local
filesystem is only useful to something already on that filesystem.

*Only some items have a file.* An attachment whose `linkMode` is
`imported_url` records a filename whether or not the bytes were ever
downloaded. What is on disk is what is in `storage/<key>/`, and the item list
does not say which those are.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from typing import Any, Iterator

#: The node the library lives on, as ssh addresses it. Empty means this host —
#: which is the ordinary case for a laptop running both, and not the case here.
HOST = os.environ.get("ZOTERO_SSH_HOST", "capella")

#: Where the Zotero HTTP server answers *from the point of view of `HOST`*. On
#: a WSL host that is the Windows side, so it is the default gateway rather
#: than loopback; on a plain Linux host it is loopback.
ORIGIN = os.environ.get("ZOTERO_ORIGIN", "http://172.25.176.1:23119")

#: What the server insists on seeing in `Host`. Not derived from ORIGIN: that
#: is the point of it.
HOST_HEADER = os.environ.get("ZOTERO_HOST_HEADER", "127.0.0.1:23119")

#: The Zotero data directory as `HOST` can read it. `storage/<key>/<filename>`
#: hangs off this.
DATA_DIR = os.environ.get("ZOTERO_DATA_DIR", "/mnt/c/Users/phybe/Zotero")

#: `0` means "whichever user this Zotero is logged in as", which is the only
#: one a local API can serve.
PERSONAL = os.environ.get("ZOTERO_LIBRARY", "users/0")

#: Whether to mirror the group libraries as well as the personal one.
#:
#: On by default, and that default is load-bearing rather than generous. A
#: shared research library is where the PDFs are: this account's personal
#: library has 2 stored files and its one active group has 50, so mirroring
#: `users/0` alone would produce a bibliography with almost no papers attached
#: to it and no error anywhere saying why.
GROUPS = os.environ.get("ZOTERO_GROUPS", "1") not in ("0", "false", "no")

#: One page of items. Zotero's own cap is 100.
PAGE = 100

TIMEOUT_S = float(os.environ.get("ZOTERO_TIMEOUT_S", "60"))


class ZoteroUnavailable(RuntimeError):
    """The library could not be reached.

    Its own class because it is an ordinary outcome, not a defect: Zotero is a
    desktop application and the desktop is sometimes asleep. A sync tick that
    hits this reports and waits rather than failing the service.
    """


class ZoteroError(RuntimeError):
    """Zotero answered, and said no."""


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


def _run(argv: list[str], *, binary: bool = False) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, timeout=TIMEOUT_S,
                              check=False)
    except FileNotFoundError as e:                      # no ssh, no curl
        raise ZoteroUnavailable(str(e)) from e
    except subprocess.TimeoutExpired as e:
        raise ZoteroUnavailable(f"timed out after {TIMEOUT_S}s") from e


def _remote(command: str) -> list[str]:
    """The argv that runs `command` where the library is.

    A local library is not a special case worth a second code path; it is this
    one with the ssh hop removed.
    """
    if not HOST:
        return ["bash", "-lc", command]
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", HOST,
            command]


def request(path: str, params: dict[str, Any] | None = None,
            headers: dict[str, str] | None = None,
            library: str | None = None) -> Response:
    """One call against the local API, with the Host override that makes it
    answer at all.

    Status and headers come back separately from the body because the whole
    sync turns on two headers — `Last-Modified-Version`, which is the cursor,
    and `Total-Results`, which is how many pages are left.
    """
    query = ""
    if params:
        query = "?" + "&".join(
            f"{k}={_quote(str(v))}" for k, v in params.items())
    url = f"{ORIGIN}/api/{library or PERSONAL}{path}{query}"
    argv = ["curl", "-sS", "-m", str(int(TIMEOUT_S)), "-D", "-",
            "-H", f"Host: {HOST_HEADER}"]
    for name, value in (headers or {}).items():
        argv += ["-H", f"{name}: {value}"]
    argv.append(url)

    done = _run(_remote(" ".join(shlex.quote(a) for a in argv)))
    if done.returncode != 0:
        raise ZoteroUnavailable(
            f"cannot reach Zotero at {ORIGIN} via {HOST or 'this host'}: "
            f"{done.stderr.decode('utf-8', 'replace').strip()[:300]}")
    return _split(done.stdout)


def _quote(value: str) -> str:
    return "".join(
        c if c.isalnum() or c in "-_.~" else f"%{ord(c):02X}"
        for c in value)


def _split(raw: bytes) -> Response:
    """Split curl's `-D -` output into headers and body.

    Loops over blocks because a 302 is answered as two header blocks, and the
    last one is the one that describes what came back.
    """
    head, sep, body = raw.partition(b"\r\n\r\n")
    while sep and body[:5] in (b"HTTP/",):
        head, sep, body = body.partition(b"\r\n\r\n")
    lines = head.decode("utf-8", "replace").splitlines()
    status = int(lines[0].split()[1]) if lines and lines[0].startswith("HTTP/") else 0
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return Response(status=status, headers=headers, body=body)


def _checked(res: Response, what: str) -> Response:
    if res.status == 400:
        raise ZoteroError(
            f"{what}: Zotero answered 400. It refuses a Host header that is "
            f"not localhost, so ZOTERO_HOST_HEADER ({HOST_HEADER}) has to name "
            f"one however the address in ZOTERO_ORIGIN is spelled.")
    if res.status == 0:
        raise ZoteroUnavailable(f"{what}: no HTTP response")
    if res.status >= 400:
        raise ZoteroError(f"{what}: {res.status} "
                          f"{res.body.decode('utf-8', 'replace')[:200]}")
    return res


def groups() -> list[dict]:
    """The shared libraries this Zotero is a member of."""
    if not GROUPS:
        return []
    res = _checked(request("/groups", {"limit": 100}), "groups")
    return [{"id": f"groups/{g['id']}",
             "name": (g.get("data") or {}).get("name") or str(g["id"])}
            for g in res.json()]


def libraries() -> list[dict]:
    """Everything to mirror: the personal library, then each group."""
    return [{"id": PERSONAL, "name": "My Library"}, *groups()]


def library_version(library: str | None = None) -> int:
    """Where one library is now. One request, and the whole cost of a tick that
    has nothing to do."""
    res = _checked(request("/items", {"limit": 1, "format": "json"},
                           library=library),
                   "library version")
    return int(res.headers.get("last-modified-version") or 0)


def versions() -> dict[str, int]:
    """Every library's version, which together are the sync cursor.

    A dict rather than one number because the libraries move independently: a
    paper added to a shared group changes nothing about the personal library,
    and one version for the lot would either miss that or re-read everything.
    """
    return {lib["id"]: library_version(lib["id"]) for lib in libraries()}


def unchanged_since(version: int, library: str | None = None) -> bool:
    """Whether a library has moved. `304` here is the answer a scheduled sync
    gets almost every time it runs."""
    if version <= 0:
        return False
    res = request("/items", {"limit": 1, "format": "json"},
                  {"If-Modified-Since-Version": str(version)}, library=library)
    if res.status == 304:
        return True
    _checked(res, "modified check")
    return False


def _paged(path: str, params: dict[str, Any] | None = None,
           library: str | None = None) -> Iterator[dict]:
    start = 0
    while True:
        res = _checked(
            request(path, {**(params or {}), "limit": PAGE, "start": start,
                           "format": "json"}, library=library),
            f"GET {path}")
        page = res.json()
        yield from page
        total = int(res.headers.get("total-results") or 0)
        start += PAGE
        if start >= total or not page:
            return


def collections(library: str | None = None) -> list[dict]:
    return list(_paged("/collections", library=library))


def items(library: str | None = None) -> list[dict]:
    """Every item, including attachments and notes.

    Not `/items/top`: an attachment is where the file is, and a child note is
    the annotation somebody wrote. Sorting them out belongs to whatever builds
    the bundle, which can see all three.
    """
    return list(_paged("/items", library=library))


def stored_files() -> dict[str, str]:
    """Which attachment keys have their bytes on disk, and under what filename.

    Listed from the filesystem rather than asked of the API, because the API
    reports the filename an attachment *records* whether or not it was ever
    downloaded. `storage/<key>/` is the only place that knows.
    """
    listing = _run(_remote(
        f"find {shlex.quote(DATA_DIR)}/storage -mindepth 2 -maxdepth 2 "
        f"-type f ! -name '.zotero*' -printf '%h/%f\\n' 2>/dev/null"))
    out: dict[str, str] = {}
    for line in listing.stdout.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.rsplit("/", 2)
        if len(parts) == 3:
            out[parts[1]] = parts[2]
    return out


def fetch_files(keys: list[str], into) -> dict[str, str]:
    """Copy each named attachment's stored file into `into`, keeping its name.

    One `tar` over the ssh channel rather than a connection per file: 48 files
    is 48 handshakes otherwise, and the library grows.
    """
    if not keys:
        return {}
    into.mkdir(parents=True, exist_ok=True)
    listed = " ".join(shlex.quote(k) for k in keys)
    # `.zotero-ft-cache` and `.zotero-ft-info` are Zotero's own full-text
    # index, not the document — several times the size of the PDF beside them
    # and of no use to anything here.
    command = (f"cd {shlex.quote(DATA_DIR)}/storage && "
               f"tar -cf - --exclude='.zotero*' {listed} 2>/dev/null")
    proc = subprocess.Popen(_remote(command), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    try:
        extract = subprocess.run(["tar", "-xf", "-", "-C", str(into)],
                                 stdin=proc.stdout, capture_output=True,
                                 timeout=TIMEOUT_S * 10, check=False)
    except subprocess.TimeoutExpired as e:
        proc.kill()
        raise ZoteroUnavailable("copying stored files timed out") from e
    finally:
        if proc.stdout:
            proc.stdout.close()
        proc.wait(timeout=10)
    if extract.returncode != 0:
        raise ZoteroUnavailable(
            "copying stored files failed: "
            + extract.stderr.decode("utf-8", "replace")[:300])
    return {k: str(into / k) for k in keys if (into / k).is_dir()}
