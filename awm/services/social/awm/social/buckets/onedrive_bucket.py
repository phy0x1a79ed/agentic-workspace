"""OneDrive / SharePoint bucket backend — a thin client of the mira daemon.

The UBC student account's OneDrive/SharePoint can't be reached with an offline
OAuth refresh token the way Google Drive can (it would need a Microsoft app
registration + tenant admin consent). Instead this backend reuses the
established **mira** pattern: the mira host keeps a logged-in SharePoint tab in
Opera, and the mira daemon's ``OneDriveDriver`` drives the SharePoint REST API
in-page over CDP (same-origin, the session cookie rides automatically). This
class is a pure shim — it proxies each op to the daemon's ``/v1/fs/<name>/…``
routes (mirroring :class:`~awm.social.connectors.mira_conn.MiraConnector`).

A relative path is joined onto the configured ``root``. An absolute
server-relative path (``/teams/<x>/...``, ``/sites/<x>/...``,
``/personal/<x>/...``) passes through unchanged, so one bucket reaches every
site the mira session can. SharePoint's own permissions are the boundary: a
refusal comes back as ``PermissionError``. Entries under the root come back
relative, entries elsewhere absolute.

The daemon codes each failure (``code`` in the JSON body) and the bucket raises
the matching builtin: ``FileNotFoundError``, ``NotADirectoryError``,
``IsADirectoryError``, ``FileExistsError``, ``PermissionError`` or
``ValueError``. Bytes cross the JSON hop as base64. The daemon serves a
self-signed cert, so TLS verification defaults off.
"""

from __future__ import annotations

import base64
import logging

from awm.social.buckets.base import Bucket, Bucket_Account, Entry

log = logging.getLogger("awm.social.buckets.onedrive")

# The daemon route segment — one storage "name" per logged-in store.
_FS_NAME = "onedrive"

_MANAGED_PATHS = ("teams", "sites", "personal")

_CODE_ERRORS: dict[str, type[Exception]] = {
    "ENOENT": FileNotFoundError,
    "ENOTDIR": NotADirectoryError,
    "EISDIR": IsADirectoryError,
    "EEXIST": FileExistsError,
    "EAUTH": PermissionError,
    "EINVAL": ValueError,
    "ENOTSUP": NotImplementedError,
}


def is_absolute(path: str) -> bool:
    """True for a server-relative SharePoint path such as ``/teams/<x>/...``."""
    if not (path or "").startswith("/"):
        return False
    first = path.lstrip("/").split("/", 1)[0].lower()
    return first in _MANAGED_PATHS


class OneDriveBucket(Bucket):
    kind = "onedrive"

    def __init__(self, account: Bucket_Account) -> None:
        super().__init__(account)
        self._base = (account.mira_url or "").rstrip("/")
        self._token = account.mira_token or ""
        self._verify = account.mira_verify_tls
        self._root = (account.root or "").rstrip("/")
        self._session = None  # aiohttp.ClientSession

    # -- path scoping -------------------------------------------------------

    def _abs(self, path: str) -> str:
        """Bucket path → server-relative SharePoint url. An absolute path passes
        through; a relative one joins the root (``""`` is the root itself)."""
        if is_absolute(path):
            return "/" + path.strip("/")
        rel = (path or "").strip("/")
        if not self._root:
            return "/" + rel if rel else ""
        return f"{self._root}/{rel}" if rel else self._root

    # -- transport (mirrors MiraConnector) ----------------------------------

    def _connector(self):
        import aiohttp

        return aiohttp.TCPConnector(ssl=None if self._verify else False)

    async def _ensure_session(self):
        import aiohttp

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=self._connector(),
                headers={"Authorization": f"Bearer {self._token}"})
        return self._session

    async def _request(self, method: str, route: str, *, params=None,
                       body=None, timeout: float = 120) -> dict:
        import aiohttp

        sess = await self._ensure_session()
        async with sess.request(method, self._base + route, params=params,
                                json=body,
                                timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            try:
                data = await r.json(content_type=None)
            except ValueError:
                data = {"error": (await r.text())[:300]}
            if r.status >= 400:
                raise self._error(route, r.status, data or {})
            return data

    def _error(self, route: str, status: int, data: dict) -> Exception:
        msg = data.get("error") or f"HTTP {status}"
        cls = _CODE_ERRORS.get(data.get("code") or "")
        if cls is PermissionError:
            return cls(f"bucket {self.account.name!r}: {msg}. If this is not a "
                       f"permissions refusal, re-authenticate the SharePoint "
                       f"session in mira's Opera")
        if cls is not None:
            return cls(f"bucket {self.account.name!r}: {msg}")
        return RuntimeError(f"mira {route} -> {status}: {msg}")

    async def _get(self, route: str, **params) -> dict:
        return await self._request("GET", route, params=params)

    async def _post(self, route: str, body: dict) -> dict:
        return await self._request("POST", route, body=body, timeout=300)

    # -- mapping ------------------------------------------------------------

    def _entry(self, d: dict, fallback_path: str = "") -> Entry:
        # The daemon returns absolute server-relative paths. Strip the root back
        # off, by whole segments, so entries under it read relative to the bucket.
        abs_path = d.get("path", "") or ""
        rel = abs_path
        if self._root and abs_path == self._root:
            rel = ""
        elif self._root and abs_path.startswith(self._root + "/"):
            rel = abs_path[len(self._root) + 1:]
        return Entry(
            name=d.get("name", "") or "",
            path=rel if abs_path else fallback_path,
            is_dir=bool(d.get("is_dir")),
            size=int(d.get("size", 0) or 0),
            mime=d.get("mime", "") or "",
            modified=d.get("modified", "") or "",
            id=d.get("id", "") or "",
            ref=d.get("ref") or {},
        )

    # -- operations ---------------------------------------------------------

    async def ls(self, path: str = "") -> list[Entry]:
        resp = await self._get(f"/v1/fs/{_FS_NAME}/ls", path=self._abs(path))
        return [self._entry(e) for e in resp.get("entries", [])]

    async def stat(self, path: str) -> Entry:
        resp = await self._get(f"/v1/fs/{_FS_NAME}/stat", path=self._abs(path))
        return self._entry(resp.get("entry", {}), fallback_path=path.strip("/"))

    async def probe(self) -> None:
        await self.stat("")

    async def mkdir(self, path: str) -> Entry:
        resp = await self._post(f"/v1/fs/{_FS_NAME}/mkdir", {"path": self._abs(path)})
        return self._entry(resp.get("entry", {}), fallback_path=path.strip("/"))

    async def get(self, path: str) -> tuple[str, str, bytes]:
        resp = await self._get(f"/v1/fs/{_FS_NAME}/get", path=self._abs(path))
        return (
            resp.get("filename", "") or (path.strip("/").rsplit("/", 1)[-1]),
            resp.get("mime", "") or "application/octet-stream",
            base64.b64decode(resp.get("b64", "") or ""),
        )

    async def put(
        self, path: str, data: bytes, *, mime: str | None = None,
        parents: bool = False,
    ) -> Entry:
        body = {
            "path": self._abs(path),
            "b64": base64.b64encode(data).decode("ascii"),
            "parents": bool(parents),
        }
        if mime:
            body["mime"] = mime
        resp = await self._post(f"/v1/fs/{_FS_NAME}/put", body)
        return self._entry(resp.get("entry", {}), fallback_path=path.strip("/"))

    async def rm(self, path: str) -> None:
        await self._post(f"/v1/fs/{_FS_NAME}/rm", {"path": self._abs(path)})

    async def search(self, query: str, *, limit: int = 50,
                     path: str = "") -> list[Entry]:
        resp = await self._get(
            f"/v1/fs/{_FS_NAME}/search", query=query, limit=limit,
            root=self._abs(path))
        return [self._entry(e) for e in resp.get("entries", [])]

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
