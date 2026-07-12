"""Google Drive bucket backend — full read/write over the Drive v3 API.

Authenticates per account with an OAuth2 **refresh token** (no callback server
at runtime): ``client_id`` + ``client_secret`` + ``refresh_token`` mint a fresh
access token on demand. The refresh token comes from a one-time consent run of
``scripts/gdrive_auth.py``. The scope is the full ``drive`` scope, so the bucket
can read, overwrite, and delete *any* file in the account — ``drive.file`` would
only ever see files this client created and is the wrong choice for "full
access".

Drive is **id-addressed**, not path-addressed, so this backend keeps a small
path→id resolver: it walks ``name=``/``'<parent>' in parents`` queries from the
configured ``root`` folder id (default the account root, ``"root"``). A name
collision inside a folder is resolved **first-match-wins** (logged), matching
how the messaging connectors treat ambiguous lookups.

The google client is synchronous and does blocking HTTP, so **every** call is
dispatched through ``asyncio.to_thread`` — the adapter's control-WS loop must
never block (AGENTS.md "Concurrency"). Google libraries are imported lazily so
this module loads (for the registry/manifest) even where they aren't installed.
"""

from __future__ import annotations

import asyncio
import logging

from awm.social.buckets.base import Bucket, Bucket_Account, Entry

log = logging.getLogger("awm.social.buckets.gdrive")

# Full drive scope — read + write + delete across the whole account (see module
# docstring on why drive.file is insufficient for "full access").
SCOPES = ["https://www.googleapis.com/auth/drive"]
TOKEN_URI = "https://oauth2.googleapis.com/token"
_FOLDER_MIME = "application/vnd.google-apps.folder"

# Google-native docs have no byte stream; export them to a portable format.
_EXPORT_MAP = {
    "application/vnd.google-apps.document":
        ("application/pdf", ".pdf"),
    "application/vnd.google-apps.spreadsheet":
        ("text/csv", ".csv"),
    "application/vnd.google-apps.presentation":
        ("application/pdf", ".pdf"),
    "application/vnd.google-apps.drawing":
        ("image/png", ".png"),
}


def _split(path: str) -> list[str]:
    """Split a POSIX-ish bucket path into non-empty segments."""
    return [seg for seg in (path or "").strip("/").split("/") if seg]


class GoogleDriveBucket(Bucket):
    kind = "google_drive"

    def __init__(self, account: Bucket_Account) -> None:
        super().__init__(account)
        self._svc = None  # cached Drive v3 service (built lazily, off-loop)
        self._root_id = account.root or "root"

    # -- service ------------------------------------------------------------

    def _build_service(self):
        """Build the Drive v3 service (blocking — call via ``asyncio.to_thread``).

        Split out so tests can inject a fake by setting ``self._svc`` directly
        and never importing the google libraries.
        """
        from google.oauth2.credentials import Credentials  # lazy: optional dep
        from googleapiclient.discovery import build

        a = self.account
        if not (a.client_id and a.client_secret and a.refresh_token):
            raise RuntimeError(
                f"bucket {a.name!r} is missing client_id/client_secret/"
                f"refresh_token for Google Drive")
        creds = Credentials(
            None,
            refresh_token=a.refresh_token,
            client_id=a.client_id,
            client_secret=a.client_secret,
            token_uri=TOKEN_URI,
            scopes=SCOPES,
        )
        return build("drive", "v3", credentials=creds, cache_discovery=False)

    async def _service(self):
        if self._svc is None:
            self._svc = await asyncio.to_thread(self._build_service)
        return self._svc

    @staticmethod
    async def _execute(request):
        """Run one google-api request off the event loop."""
        return await asyncio.to_thread(request.execute)

    # -- path → id resolver -------------------------------------------------

    async def _children(self, parent_id: str) -> list[dict]:
        """All non-trashed children of ``parent_id`` (paged)."""
        svc = await self._service()
        out: list[dict] = []
        page = None
        while True:
            req = svc.files().list(
                q=f"'{parent_id}' in parents and trashed=false",
                fields="nextPageToken, files(id, name, mimeType, size, "
                       "modifiedTime)",
                pageSize=1000,
                pageToken=page,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            resp = await self._execute(req)
            out.extend(resp.get("files", []))
            page = resp.get("nextPageToken")
            if not page:
                return out

    async def _resolve(self, path: str) -> dict:
        """Resolve ``path`` to its file metadata dict (raises if missing).

        Returns a synthetic root descriptor for the empty path so ``ls("")``
        works without a Drive round-trip on the root id.
        """
        segs = _split(path)
        if not segs:
            return {"id": self._root_id, "name": "", "mimeType": _FOLDER_MIME}
        parent = self._root_id
        meta: dict = {}
        for i, seg in enumerate(segs):
            matches = [c for c in await self._children(parent)
                       if c.get("name") == seg]
            if not matches:
                raise FileNotFoundError(
                    f"bucket {self.account.name!r}: no such path {path!r} "
                    f"(missing segment {seg!r})")
            if len(matches) > 1:
                log.warning("bucket %s: %d entries named %r under %r; "
                            "using first", self.account.name, len(matches),
                            seg, parent)
            meta = matches[0]
            parent = meta["id"]
        return meta

    def _entry(self, meta: dict, path: str) -> Entry:
        mime = meta.get("mimeType", "") or ""
        return Entry(
            name=meta.get("name", "") or "",
            path=path,
            is_dir=(mime == _FOLDER_MIME),
            size=int(meta.get("size", 0) or 0),
            mime=mime,
            modified=meta.get("modifiedTime", "") or "",
            id=meta.get("id", "") or "",
            ref={"id": meta.get("id", "")},
        )

    # -- operations ---------------------------------------------------------

    async def ls(self, path: str = "") -> list[Entry]:
        folder = await self._resolve(path)
        if folder.get("mimeType") != _FOLDER_MIME:
            raise NotADirectoryError(
                f"bucket {self.account.name!r}: {path!r} is not a folder")
        base = path.strip("/")
        children = await self._children(folder["id"])
        return [
            self._entry(c, f"{base}/{c['name']}" if base else c["name"])
            for c in sorted(children, key=lambda c: (
                c.get("mimeType") != _FOLDER_MIME, c.get("name", "")))
        ]

    async def stat(self, path: str) -> Entry:
        return self._entry(await self._resolve(path), path.strip("/"))

    async def get(self, path: str) -> tuple[str, str, bytes]:
        from googleapiclient.http import MediaIoBaseDownload  # lazy
        import io

        meta = await self._resolve(path)
        if meta.get("mimeType") == _FOLDER_MIME:
            raise IsADirectoryError(
                f"bucket {self.account.name!r}: {path!r} is a folder")
        svc = await self._service()
        name = meta.get("name", "") or _split(path)[-1]
        src_mime = meta.get("mimeType", "") or ""

        export = _EXPORT_MAP.get(src_mime)
        if export:
            out_mime, ext = export
            req = svc.files().export_media(fileId=meta["id"], mimeType=out_mime)
            if not name.lower().endswith(ext):
                name = name + ext
            mime = out_mime
        else:
            req = svc.files().get_media(
                fileId=meta["id"], supportsAllDrives=True)
            mime = src_mime

        def _download() -> bytes:
            buf = io.BytesIO()
            dl = MediaIoBaseDownload(buf, req)
            done = False
            while not done:
                _status, done = dl.next_chunk()
            return buf.getvalue()

        data = await asyncio.to_thread(_download)
        return name, mime, data

    async def put(
        self, path: str, data: bytes, *, mime: str | None = None
    ) -> Entry:
        from googleapiclient.http import MediaInMemoryUpload  # lazy

        segs = _split(path)
        if not segs:
            raise ValueError("put requires a file path")
        name = segs[-1]
        parent_path = "/".join(segs[:-1])
        parent = (await self._resolve(parent_path))["id"] if parent_path \
            else self._root_id
        svc = await self._service()
        media = MediaInMemoryUpload(
            data, mimetype=mime or "application/octet-stream", resumable=False)

        # Overwrite an existing same-named file in the parent; else create.
        existing = [c for c in await self._children(parent)
                    if c.get("name") == name
                    and c.get("mimeType") != _FOLDER_MIME]
        fields = "id, name, mimeType, size, modifiedTime"
        if existing:
            req = svc.files().update(
                fileId=existing[0]["id"], media_body=media, fields=fields,
                supportsAllDrives=True)
        else:
            req = svc.files().create(
                body={"name": name, "parents": [parent]},
                media_body=media, fields=fields, supportsAllDrives=True)
        meta = await self._execute(req)
        return self._entry(meta, path.strip("/"))

    async def rm(self, path: str) -> None:
        meta = await self._resolve(path)
        svc = await self._service()
        await self._execute(
            svc.files().delete(fileId=meta["id"], supportsAllDrives=True))

    async def mkdir(self, path: str) -> Entry:
        segs = _split(path)
        if not segs:
            raise ValueError("mkdir requires a path")
        svc = await self._service()
        parent = self._root_id
        meta: dict = {}
        acc: list[str] = []
        for seg in segs:
            acc.append(seg)
            existing = [c for c in await self._children(parent)
                        if c.get("name") == seg
                        and c.get("mimeType") == _FOLDER_MIME]
            if existing:
                meta = existing[0]
            else:
                req = svc.files().create(
                    body={"name": seg, "parents": [parent],
                          "mimeType": _FOLDER_MIME},
                    fields="id, name, mimeType, modifiedTime",
                    supportsAllDrives=True)
                meta = await self._execute(req)
            parent = meta["id"]
        return self._entry(meta, "/".join(segs))

    async def search(self, query: str, *, limit: int = 50) -> list[Entry]:
        # Drive query can't express "descendant of <root>", so search spans the
        # whole drive the credential can see (documented in INSTALL.md).
        svc = await self._service()
        safe = query.replace("'", "\\'")
        req = svc.files().list(
            q=f"name contains '{safe}' and trashed=false",
            fields="files(id, name, mimeType, size, modifiedTime)",
            pageSize=max(1, min(int(limit), 1000)),
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        resp = await self._execute(req)
        return [self._entry(c, c.get("name", "")) for c in resp.get("files", [])]
