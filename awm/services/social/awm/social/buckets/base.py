"""Bucket abstraction — one cloud file-store backend per configured bucket.

A :class:`Bucket` owns access to one remote file store for one configured
identity (a Google Drive, a OneDrive/SharePoint document library). It is the
storage-shaped sibling of :class:`~awm.social.connectors.base.Connector`: where
a connector models *messages* (channels, send, receive), a bucket models *files*
(list, get, put, remove). They deliberately do not share an ABC — bending the
messaging shape over file storage helped nobody.

Paths are POSIX-style and **relative to the bucket's configured root** (``""``
or ``"/"`` is the root itself); a backend resolves them to whatever the platform
addresses by (Drive file ids, SharePoint server-relative urls). Unlike the
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
    async def get(self, path: str) -> tuple[str, str, bytes]:
        """Download one file → ``(filename, mime, bytes)``.

        The adapter writes the bytes to a temp dir (reusing the attachment
        sink); the backend itself never touches local disk.
        """

    @abstractmethod
    async def put(
        self, path: str, data: bytes, *, mime: str | None = None
    ) -> Entry:
        """Upload ``data`` to ``path`` (creating or overwriting), returning the
        resulting :class:`Entry`."""

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

    async def search(self, query: str, *, limit: int = 50) -> list[Entry]:
        """Search the store for files matching ``query`` → :class:`Entry` list.

        Non-abstract: a backend with no usable search surface leaves this
        default, which raises and the verb surfaces a clean error.
        """
        raise NotImplementedError(
            f"{self.kind} bucket does not support search")

    async def close(self) -> None:
        """Release any held resources (HTTP sessions). Default no-op."""
