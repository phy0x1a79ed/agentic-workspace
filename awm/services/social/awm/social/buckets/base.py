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
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

log = logging.getLogger("awm.social.buckets")


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
    platform's last-modified string when reported.
    """

    name: str
    path: str
    is_dir: bool = False
    size: int = 0
    mime: str = ""
    modified: str = ""
    id: str = ""
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
    async def get(self, path: str, dest_dir: str) -> dict:
        """Download one file into ``dest_dir`` →
        ``{filename, mime, size, path}``, ``path`` being the local file.

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
