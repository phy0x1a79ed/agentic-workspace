"""Bucket abstraction — one cloud file-store backend per configured bucket.

A :class:`Bucket` owns access to one remote file store for one configured
identity (a Google Drive, a OneDrive/SharePoint document library). It is the
storage-shaped sibling of :class:`~awm.social.connectors.base.Connector`: where
a connector models *messages* (channels, send, receive), a bucket models *files*
(list, get, put, remove). They deliberately do not share an ABC — bending the
messaging shape over file storage helped nobody.

Paths are POSIX-style and **relative to the bucket's configured root** (``""``
or ``"/"`` is the root itself); a backend resolves them to whatever the platform
addresses by (Drive file ids, SharePoint server-relative urls). A backend may
also accept absolute paths of its own (SharePoint's ``/teams/<x>/...``).

Failures use the builtin exceptions: ``FileNotFoundError`` for a missing path,
``NotADirectoryError``/``IsADirectoryError`` for the wrong kind, and
``PermissionError`` when the store refuses the identity. Unlike the
connectors, a bucket is a plain request/response object — there is no live
socket and no ``start`` loop, so the adapter never schedules a task for it.

Adding a backend = one new module implementing this ABC + one line in
``buckets/__init__.REGISTRY``.
"""

from __future__ import annotations

import logging
import os
import posixpath
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

log = logging.getLogger("awm.social.buckets")


def _local_segment(name: str) -> str:
    """A remote name as one local path segment, kept as close to it as the
    filesystem allows."""
    seg = (name or "").replace("/", "_").replace("\0", "")
    return "_" if seg in ("", ".", "..") else seg


def _failure(path: str, exc: Exception) -> dict:
    return {"path": path, "error": f"{type(exc).__name__}: {exc}"}


@dataclass(frozen=True)
class Bucket_Account:
    """The configured identity a bucket acts as (the config, minus parsing).

    Mirrors :class:`~awm.social.connectors.base.Account`: a flat value object the
    backend ctor receives so it never reaches back into the config module. The
    ``oauth_*`` fields drive a Google Drive backend; the ``mira_*`` fields drive
    a mira-routed OneDrive backend; ``root`` scopes both to a sub-tree.
    """

    name: str
    kind: str                         # "google_drive" | "onedrive"
    root: str = ""                    # backend-specific root (Drive folder id / SP url)
    # Google Drive (OAuth2 refresh-token flow)
    client_id: str | None = None
    client_secret: str | None = None
    refresh_token: str | None = None
    # mira-routed buckets (source="mira"): a thin client of the mira daemon
    source: str | None = None         # "mira" routes via OneDriveBucket→daemon
    site: str | None = None           # SharePoint site origin, e.g. https://x.sharepoint.com
    mira_url: str | None = None
    mira_token: str | None = None
    mira_verify_tls: bool = False


@dataclass(frozen=True)
class Entry:
    """One file or folder in a bucket, normalised across backends.

    ``path`` is the POSIX path relative to the bucket root (what ``get``/``rm``
    take back). ``id`` is the backend's own handle (a Drive file id, a SharePoint
    unique id) — advisory, never required from a caller. ``size`` is the byte
    count for files (``0`` for folders / when unknown); ``modified`` is the
    platform's last-modified string when reported. ``web_url`` opens the item
    in a browser.
    """

    name: str
    path: str
    is_dir: bool = False
    size: int = 0
    mime: str = ""
    modified: str = ""
    id: str = ""
    web_url: str = ""
    ref: dict = field(default_factory=dict, compare=False)


class Bucket(ABC):
    """One cloud file store for one configured identity.

    Methods are request/response — each opens what it needs and returns. A
    backend whose platform can't do an optional op (``search``) leaves the
    default, which raises a clean "not supported" the verb surfaces.
    """

    kind: str = "base"

    def __init__(self, account: Bucket_Account) -> None:
        self.account = account

    @abstractmethod
    async def ls(self, path: str = "") -> list[Entry]:
        """List the immediate children of ``path`` (a folder; ``""`` = root)."""

    @abstractmethod
    async def stat(self, path: str) -> Entry:
        """Return the :class:`Entry` for one file/folder at ``path``."""

    @abstractmethod
    async def get(self, path: str, dest_dir: str, *,
                  version: str | None = None) -> dict:
        """Download one file into ``dest_dir`` →
        ``{filename, mime, size, path}``, ``path`` being the local file.
        ``version`` is a label from :meth:`versions`.

        The backend streams to disk, so a file never has to fit in memory.
        """

    @abstractmethod
    async def put(
        self, path: str, src: str, *, mime: str | None = None,
        parents: bool = False,
    ) -> Entry:
        """Upload the local file ``src`` to ``path`` (creating or
        overwriting), returning the resulting :class:`Entry`. ``parents``
        creates missing folders above ``path``. Without it a missing parent is
        ``FileNotFoundError``."""

    @abstractmethod
    async def rm(self, path: str) -> None:
        """Delete the file/folder at ``path``."""

    async def mkdir(self, path: str) -> Entry:
        """Create a folder at ``path`` (and parents as needed), returning it.

        Non-abstract: a backend without a folder-create path leaves this
        default, which raises and the verb surfaces a clean error.
        """
        raise NotImplementedError(
            f"{self.kind} bucket does not support mkdir")

    async def mv(self, src: str, dst: str, *, overwrite: bool = False,
                 parents: bool = False) -> Entry:
        """Move or rename ``src`` to the full path ``dst`` → the new entry.

        An existing ``dst`` is ``FileExistsError`` unless ``overwrite``.
        ``parents`` creates missing folders above ``dst``."""
        raise NotImplementedError(f"{self.kind} bucket does not support mv")

    async def cp(self, src: str, dst: str, *, overwrite: bool = False,
                 parents: bool = False) -> Entry:
        """Copy ``src`` to the full path ``dst`` → the new entry, as for
        :meth:`mv`."""
        raise NotImplementedError(f"{self.kind} bucket does not support cp")

    async def walk(self, path: str = "", *, limit: int = 1000
                   ) -> tuple[list[Entry], bool]:
        """Every entry below the folder ``path``, breadth first →
        ``(entries, truncated)``. ``truncated`` is true when more than
        ``limit`` entries exist."""
        out: list[Entry] = []
        queue = [path]
        while queue:
            for e in await self.ls(queue.pop(0)):
                if len(out) >= limit:
                    return out, True
                out.append(e)
                if e.is_dir:
                    queue.append(e.path)
        return out, False

    async def get_tree(self, path: str, dest_dir: str, *,
                       limit: int = 10000) -> dict:
        """Download the folder ``path`` as a local tree under ``dest_dir`` →
        ``{filename, mime, size, path, is_dir, count, failures}``, ``path``
        being the local folder. A file that fails is listed in ``failures``
        and the rest still download."""
        root = await self.stat(path)
        if not root.is_dir:
            raise NotADirectoryError(f"bucket {self.account.name!r}: {path!r} is a file")
        entries, truncated = await self.walk(path, limit=limit)
        if truncated:
            raise ValueError(
                f"bucket {self.account.name!r}: {path!r} holds more than "
                f"{limit} entries (pass a larger limit)")
        name = root.name or posixpath.basename(path.rstrip("/")) or self.account.name
        local_root = os.path.join(dest_dir, _local_segment(name))
        os.makedirs(local_root)
        prefix = root.path.strip("/")
        count, size, failures = 0, 0, []
        for e in entries:
            rel = e.path.strip("/")
            if prefix:
                if not rel.startswith(prefix + "/"):
                    failures.append(_failure(e.path, ValueError("outside the folder")))
                    continue
                rel = rel[len(prefix) + 1:]
            local = os.path.join(local_root, *map(_local_segment, rel.split("/")))
            try:
                if e.is_dir:
                    os.makedirs(local, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(local), exist_ok=True)
                info = await self.get(e.path, os.path.dirname(local))
                if info["path"] != local:
                    os.replace(info["path"], local)
                count += 1
                size += int(info.get("size") or 0)
            except Exception as exc:  # noqa: BLE001
                failures.append(_failure(e.path, exc))
        return {"filename": os.path.basename(local_root), "mime": "inode/directory",
                "size": size, "path": local_root, "is_dir": True,
                "count": count, "failures": failures}

    async def put_tree(self, path: str, src_dir: str, *,
                       parents: bool = False) -> dict:
        """Upload the local folder ``src_dir`` as the folder ``path`` →
        ``{entry, count, failures}``. Existing files are overwritten. A file
        or folder that fails is listed in ``failures`` and the rest still
        upload."""
        target = path.rstrip("/")
        parent = posixpath.dirname(target)
        if not parents and parent not in ("", "/"):
            pe = await self.stat(parent)
            if not pe.is_dir:
                raise NotADirectoryError(
                    f"bucket {self.account.name!r}: {parent!r} is a file")
        root = await self.mkdir(target) if target.strip("/") else await self.stat("")
        count, failures = 0, []
        for top, dirs, files in os.walk(src_dir):
            dirs.sort()
            rel = os.path.relpath(top, src_dir)
            remote = target if rel == "." else f"{target}/{rel.replace(os.sep, '/')}"
            for d in list(dirs):
                try:
                    await self.mkdir(f"{remote}/{d}")
                except Exception as exc:  # noqa: BLE001
                    failures.append(_failure(f"{remote}/{d}", exc))
                    dirs.remove(d)
            for f in sorted(files):
                try:
                    await self.put(f"{remote}/{f}", os.path.join(top, f))
                    count += 1
                except Exception as exc:  # noqa: BLE001
                    failures.append(_failure(f"{remote}/{f}", exc))
        return {"entry": root, "count": count, "failures": failures}

    async def versions(self, path: str) -> list[dict]:
        """A file's versions, newest first → ``{label, size, modified,
        author, current}`` dicts."""
        raise NotImplementedError(f"{self.kind} bucket does not keep versions")

    async def restore(self, path: str, *, version: str | None = None) -> Entry:
        """Restore ``version`` of the file at ``path``, or without a version
        the item deleted from ``path`` → its entry."""
        raise NotImplementedError(f"{self.kind} bucket does not support restore")

    async def recycled(self, path: str = "", *, limit: int = 500) -> list[dict]:
        """Deleted items at or below ``path``, newest first."""
        raise NotImplementedError(f"{self.kind} bucket has no recycle bin")

    async def share(self, path: str, *, scope: str, role: str,
                    people: list[str] | None = None,
                    send_email: bool = False) -> dict:
        """Share ``path``: a link for ``organization`` or ``anyone``, direct
        access for ``people`` → ``{url, scope, role, ...}``."""
        raise NotImplementedError(f"{self.kind} bucket does not support sharing")

    async def shares(self, path: str) -> dict:
        """The links on ``path`` and the people with access → ``{links, people}``."""
        raise NotImplementedError(f"{self.kind} bucket does not support sharing")

    async def unshare(self, path: str, *, link: str = "",
                      person: str = "") -> dict:
        """Revoke one link (url or share id) or one person's access."""
        raise NotImplementedError(f"{self.kind} bucket does not support sharing")

    async def resolve(self, url: str) -> tuple[Entry, str]:
        """A share link → ``(entry, how it was resolved)``."""
        raise NotImplementedError(f"{self.kind} bucket does not resolve share links")

    async def sites(self, site: str = "") -> list[dict]:
        """The sites and libraries this identity reaches →
        ``{name, path, kind, web_url}`` dicts; with ``site``, that site's
        document libraries."""
        raise NotImplementedError(
            f"{self.kind} bucket does not support site discovery")

    async def probe(self) -> None:
        """Raise unless the store answers for this identity right now."""
        await self.stat("")

    async def search(self, query: str, *, limit: int = 50,
                     path: str = "") -> list[Entry]:
        """Search the store for files matching ``query`` → :class:`Entry` list.
        ``path`` scopes the search to a folder where the backend can.

        Non-abstract: a backend with no usable search surface leaves this
        default, which raises and the verb surfaces a clean error.
        """
        raise NotImplementedError(
            f"{self.kind} bucket does not support search")

    async def close(self) -> None:
        """Release any held resources (HTTP sessions). Default no-op."""
