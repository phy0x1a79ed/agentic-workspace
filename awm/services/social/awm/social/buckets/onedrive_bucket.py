"""OneDrive / SharePoint bucket backend — a thin client of the mira daemon.

The UBC student account's OneDrive/SharePoint can't be reached with an offline
OAuth refresh token the way Google Drive can (it would need a Microsoft app
registration + tenant admin consent). Instead this backend reuses the
established **mira** pattern: the mira host keeps a logged-in SharePoint tab in
Opera, and the mira daemon's ``OneDriveDriver`` drives the SharePoint REST API
in-page over CDP (same-origin, the session cookie rides automatically). This
class is a pure shim — it proxies each op to the daemon's ``/v1/fs/<name>/…``
routes (mirroring :class:`~awm.social.connectors.mira_conn.MiraConnector`).

The bucket's configured ``root`` (the Hallam Lab folder's server-relative path)
is prefixed onto every path here, so the daemon driver stays stateless and only
ever sees absolute server-relative SharePoint urls. Bytes cross the JSON hop as
base64 (the same convention the Teams/Slack attachment download already uses).
The daemon serves a self-signed cert, so TLS verification defaults off.
"""

from __future__ import annotations

import base64
import logging

from awm.social.buckets.base import Bucket, Bucket_Account, Entry

log = logging.getLogger("awm.social.buckets.onedrive")

# The daemon route segment — one storage "name" per logged-in store.
_FS_NAME = "onedrive"


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
        """Join the configured root onto a relative bucket path → server-relative
        SharePoint url. ``""`` resolves to the root itself."""
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

    async def _get(self, route: str, **params) -> dict:
        import aiohttp

        sess = await self._ensure_session()
        async with sess.get(self._base + route, params=params,
                            timeout=aiohttp.ClientTimeout(total=120)) as r:
            data = await r.json()
            if r.status >= 400:
                raise RuntimeError(
                    f"mira GET {route} -> {r.status}: {data.get('error')}")
            return data

    async def _post(self, route: str, body: dict) -> dict:
        import aiohttp

        sess = await self._ensure_session()
        async with sess.post(self._base + route, json=body,
                            timeout=aiohttp.ClientTimeout(total=300)) as r:
            data = await r.json()
            if r.status >= 400:
                raise RuntimeError(
                    f"mira POST {route} -> {r.status}: {data.get('error')}")
            return data

    # -- mapping ------------------------------------------------------------

    def _entry(self, d: dict, fallback_path: str = "") -> Entry:
        # The daemon returns absolute server-relative paths; strip the root back
        # off so callers always see paths relative to the bucket.
        abs_path = d.get("path", "") or ""
        rel = abs_path
        if self._root and abs_path.startswith(self._root):
            rel = abs_path[len(self._root):].lstrip("/")
        return Entry(
            name=d.get("name", "") or "",
            path=rel or fallback_path,
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

    async def get(self, path: str) -> tuple[str, str, bytes]:
        resp = await self._get(f"/v1/fs/{_FS_NAME}/get", path=self._abs(path))
        return (
            resp.get("filename", "") or (path.strip("/").rsplit("/", 1)[-1]),
            resp.get("mime", "") or "application/octet-stream",
            base64.b64decode(resp.get("b64", "") or ""),
        )

    async def put(
        self, path: str, data: bytes, *, mime: str | None = None
    ) -> Entry:
        body = {
            "path": self._abs(path),
            "b64": base64.b64encode(data).decode("ascii"),
        }
        if mime:
            body["mime"] = mime
        resp = await self._post(f"/v1/fs/{_FS_NAME}/put", body)
        return self._entry(resp.get("entry", {}), fallback_path=path.strip("/"))

    async def rm(self, path: str) -> None:
        await self._post(f"/v1/fs/{_FS_NAME}/rm", {"path": self._abs(path)})

    async def search(self, query: str, *, limit: int = 50) -> list[Entry]:
        resp = await self._get(
            f"/v1/fs/{_FS_NAME}/search", query=query, limit=limit,
            root=self._root)
        return [self._entry(e) for e in resp.get("entries", [])]

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
