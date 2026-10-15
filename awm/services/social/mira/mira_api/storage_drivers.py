"""Storage drivers — drive a cloud file store via a logged-in web session.

The storage-shaped sibling of :mod:`mira_api.drivers`: where those drivers model
*messages*, these model *files* (list / get / put / remove / search). Each driver
runs JavaScript in its Opera page (through :class:`CDPPage`) so every request
rides the live session's own cookies — the daemon never reconstructs
credentials. The idempotent *prelude* installs helpers on
``window.__awmMira.<name>`` (re-asserted every call, so a page reload self-heals).

:class:`OneDriveDriver` drives **SharePoint / OneDrive** via the SharePoint REST
API, same-origin on a logged-in ``*.sharepoint.com`` tab (the ``FedAuth``/``rtFa``
cookies ride automatically). Server-relative paths are absolute (the awm-side
bucket prefixes its configured root), so the driver is stateless. Writes carry an
``X-RequestDigest`` fetched from ``_api/contextinfo`` (SharePoint rejects a
write without it). The ``_api`` web base is derived from the path's managed-path
prefix (``/teams/<x>``, ``/sites/<x>``, ``/personal/<x>``) so a team site, a
project site, and personal OneDrive all resolve correctly.

Normalised entry dict (what the server returns):
    {name, path, is_dir, size, mime, modified, id}
where ``path`` is the absolute server-relative url and ``id`` is the SharePoint
``UniqueId``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from abc import ABC, abstractmethod
from typing import Any

from mira_api.cdp import CDPPage

log = logging.getLogger("mira_api.storage_drivers")

#: CDP evaluate budget for one transfer call (a chunk to or from SharePoint).
TRANSFER_TIMEOUT_S = 180.0
#: How long a cross-site copy job may run before mv/cp gives up waiting.
COPY_JOB_TIMEOUT_S = 3000.0


def _call(helper: str, *args: Any) -> str:
    """Build a JS expression invoking a window.__awmMira helper with JSON args."""
    arglist = ", ".join(json.dumps(a) for a in args)
    return f"(async()=>{{ return JSON.stringify(await {helper}({arglist})); }})()"


# The prelude throws ``Error("<CODE>: <message>")`` for the failures a caller can
# act on. ``evaluate`` reports a throw as ``JS error: Error: <CODE>: ...`` plus a
# stack, so the code is searched for rather than anchored.
_ERROR_CLASSES: dict[str, type[Exception]] = {
    "ENOENT": FileNotFoundError,
    "ENOTDIR": NotADirectoryError,
    "EISDIR": IsADirectoryError,
    "EEXIST": FileExistsError,
    "EAUTH": PermissionError,
    "EINVAL": ValueError,
}
_CODE_RE = re.compile(r"\b(" + "|".join(_ERROR_CLASSES) + r"): ([^\n]*)")

#: (exception class, HTTP status, wire code), most specific first.
ERROR_STATUS: list[tuple[type[Exception], int, str]] = [
    (FileNotFoundError, 404, "ENOENT"),
    (NotADirectoryError, 409, "ENOTDIR"),
    (IsADirectoryError, 409, "EISDIR"),
    (FileExistsError, 409, "EEXIST"),
    (PermissionError, 403, "EAUTH"),
    (ValueError, 400, "EINVAL"),
    (NotImplementedError, 501, "ENOTSUP"),
]


def classify(exc: Exception) -> Exception:
    """Map a coded JS failure to its Python exception; pass anything else through."""
    m = _CODE_RE.search(str(exc))
    if not m:
        return exc
    return _ERROR_CLASSES[m.group(1)](m.group(2).strip())


def error_status(exc: Exception) -> tuple[int, str]:
    """HTTP status and wire code for a storage failure (502 for anything unclassified)."""
    for cls, status, code in ERROR_STATUS:
        if isinstance(exc, cls):
            return status, code
    return 502, "EUPSTREAM"


class StorageDriver(ABC):
    name: str = "base"
    #: substring identifying this store's Opera page target
    target_needle: str = ""
    #: idempotent JS installing window.__awmMira.<name>
    PRELUDE: str = ""

    def __init__(self, page: CDPPage) -> None:
        self.page = page

    @classmethod
    def create(cls, cdp_http_base: str) -> "StorageDriver":
        return cls(CDPPage(cdp_http_base, cls.target_needle))

    def pages(self) -> dict[str, Any]:
        """Every page this driver drives, by role (for health and cleanup)."""
        return {"main": self.page}

    def _page_for(self, path: str | None) -> Any:
        return self.page

    async def _run(self, helper: str, *args: Any, path: str | None = None,
                   **eval_kw: Any) -> Any:
        expr = self.PRELUDE + "\n" + _call(helper, *args)
        try:
            return await self._page_for(path).evaluate_json(expr, **eval_kw)
        except Exception as exc:  # noqa: BLE001
            mapped = classify(exc)
            if mapped is exc:
                raise
            raise mapped from exc

    @abstractmethod
    async def ls(self, path: str) -> list[dict]: ...

    @abstractmethod
    async def stat(self, path: str) -> dict: ...

    @abstractmethod
    async def get(self, path: str, offset: int = 0,
                  length: int | None = None) -> dict:
        """Download bytes ``[offset, offset+length)`` of one file →
        ``{filename, mime, size, offset, etag, b64}``. ``length`` None reads to
        the end. ``size`` is the whole file's."""

    @abstractmethod
    async def put(self, path: str, b64: str, mime: str | None = None,
                  parents: bool = False) -> dict:
        """Upload base64 bytes to ``path`` (create/overwrite) → entry dict.

        ``parents`` creates missing folders above ``path``; without it a missing
        parent is ``FileNotFoundError``."""

    @abstractmethod
    async def rm(self, path: str) -> None: ...

    async def upload(self, path: str, upload_id: str, offset: int, final: bool,
                     b64: str, parents: bool = False) -> dict:
        """Send one chunk of a chunked upload → ``{offset}``, or ``{entry}`` on
        the final chunk. The caller owns ``upload_id`` and ``offset``."""
        raise NotImplementedError(
            f"{self.name} storage driver does not support chunked upload")

    async def upload_cancel(self, path: str, upload_id: str) -> None:
        """Abandon a chunked upload (best effort)."""

    async def mkdir(self, path: str) -> dict:
        """Create the folder at ``path`` and any missing parents → entry dict.
        Non-abstract: a store without folders leaves this default (501)."""
        raise NotImplementedError(
            f"{self.name} storage driver does not support mkdir")

    async def mv(self, src: str, dst: str, *, overwrite: bool = False,
                 parents: bool = False) -> dict:
        """Move or rename ``src`` to the full path ``dst`` → the new entry."""
        raise NotImplementedError(
            f"{self.name} storage driver does not support mv")

    async def cp(self, src: str, dst: str, *, overwrite: bool = False,
                 parents: bool = False) -> dict:
        """Copy ``src`` to the full path ``dst`` → the new entry."""
        raise NotImplementedError(
            f"{self.name} storage driver does not support cp")

    async def share(self, path: str, scope: str, role: str,
                    people: list[str] | None = None,
                    send_email: bool = False) -> dict:
        """Create a link (``organization``, ``anyone``) or grant ``people``
        access → ``{url, scope, role, ...}``."""
        raise NotImplementedError(f"{self.name} storage driver does not support sharing")

    async def shares(self, path: str) -> dict:
        """The item's links and the people with access → ``{links, people}``."""
        raise NotImplementedError(f"{self.name} storage driver does not support sharing")

    async def unshare(self, path: str, link: str | None = None,
                      person: str | None = None) -> dict:
        """Revoke one link (url or share id) or one person's access."""
        raise NotImplementedError(f"{self.name} storage driver does not support sharing")

    async def resolve(self, url: str) -> dict:
        """A share link → ``{entry, via}``."""
        raise NotImplementedError(f"{self.name} storage driver does not resolve links")

    async def sites(self, site: str | None = None) -> list[dict]:
        """The sites the session reaches, or ``site``'s document libraries →
        ``{name, path, kind, web_url}`` dicts."""
        raise NotImplementedError(
            f"{self.name} storage driver does not support site discovery")

    async def search(self, query: str, limit: int,
                     root: str | None = None) -> list[dict]:
        """Search the store → entry dicts. Non-abstract: a store with no usable
        search surface leaves this default, surfaced as 501."""
        raise NotImplementedError(
            f"{self.name} storage driver does not support search")


# --------------------------------------------------------------------------- #
# OneDrive / SharePoint                                                        #
# --------------------------------------------------------------------------- #

ONEDRIVE_PRELUDE = r"""
window.__awmMira = window.__awmMira || {};
if (!window.__awmMira.onedrive || window.__awmMira.onedrive._v !== 7) {
  const O = {};
  O._v = 7;  // bump when changing helpers so a long-lived page self-upgrades
  O._b64 = function (buf) {
    const bytes = new Uint8Array(buf);
    let bin = '';
    const CH = 0x8000;
    for (let i = 0; i < bytes.length; i += CH)
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CH));
    return btoa(bin);
  };
  O._unb64 = function (b64) {
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return bytes;
  };
  O._err = function (code, msg) { return new Error(code + ': ' + msg); };
  O._isCode = function (e, code) { return String(e && e.message).startsWith(code + ':'); };
  O._segs = function (path) { return String(path || '').split('/').filter(Boolean); };
  O._parent = function (path) {
    const s = O._segs(path); s.pop(); return '/' + s.join('/');
  };
  // A decodedurl='...' literal: quotes doubled (OData), then percent-encoded
  // with the slashes kept, so '#', '?', '%' and '&' in a name survive the URL.
  O._lit = function (path) {
    return encodeURIComponent(String(path).replace(/'/g, "''")).replace(/%2F/g, '/');
  };
  // The site collection that owns a server-relative path: a managed path
  // (/teams/<x>, /sites/<x>, /personal/<x>) has its own _api; else the root web.
  O._webBase = function (path) {
    const segs = O._segs(path);
    const mp = (segs[0] || '').toLowerCase();
    if (segs.length >= 2 && (mp === 'teams' || mp === 'sites' || mp === 'personal'))
      return '/' + segs[0] + '/' + segs[1];
    return '';
  };
  // Segment count up to and including the document library.
  O._libDepth = function (path) { return O._segs(O._webBase(path)).length + 1; };
  O._api = function (path) {
    return location.origin +
      O._webBase(path).split('/').map(encodeURIComponent).join('/') + '/_api';
  };
  O._hdr = {'Accept': 'application/json;odata=nometadata'};
  // redirect:'manual' turns an expired session's bounce to the sign-in page
  // into an opaqueredirect we can name, instead of a CORS TypeError.
  O._fetch = function (url, opts) {
    return fetch(url, Object.assign({credentials: 'include', redirect: 'manual'}, opts || {}));
  };
  O._fail = async function (r, what, path) {
    if (r.type === 'opaqueredirect' || r.status === 0)
      return O._err('EAUTH', what + ' ' + path + ': the SharePoint session redirected to sign-in');
    if (r.status === 401 || r.status === 403)
      return O._err('EAUTH', what + ' ' + path + ': HTTP ' + r.status +
        ' (no access to this item, or the session needs re-authentication)');
    let text = '';
    try { text = (await r.text()).slice(0, 300); } catch (e) {}
    if (r.status === 404 ||
        /does not exist|FileNotFound|DirectoryNotFound|-2147024894|-2130575338/.test(text))
      return O._err('ENOENT', what + ' ' + path + ': not found');
    return new Error(what + ' ' + path + ': HTTP ' + r.status + ' ' + text);
  };
  O._getJson = async function (url, what, path) {
    const r = await O._fetch(url, {headers: O._hdr});
    if (!r.ok) throw await O._fail(r, what, path);
    return r.json();
  };
  O._digest = async function (path) {
    const r = await O._fetch(O._api(path) + '/contextinfo', {method: 'POST', headers: O._hdr});
    if (!r.ok) throw await O._fail(r, 'contextinfo', path);
    const j = await r.json();
    return j.FormDigestValue ||
      (j.d && j.d.GetContextWebInformation && j.d.GetContextWebInformation.FormDigestValue);
  };
  O._post = async function (url, digest, body) {
    return O._fetch(url, {method: 'POST', body: body,
      headers: {'X-RequestDigest': digest, 'Accept': 'application/json;odata=nometadata'}});
  };
  O._folderUrl = function (path) {
    return O._api(path) + "/web/GetFolderByServerRelativePath(decodedurl='" + O._lit(path) + "')";
  };
  O._fileUrl = function (path) {
    return O._api(path) + "/web/GetFileByServerRelativePath(decodedurl='" + O._lit(path) + "')";
  };
  // Metadata of the folder at path, or null. SharePoint answers a missing
  // folder with 200 and Exists:false, never a 404.
  O._folder = async function (path) {
    const r = await O._fetch(O._folderUrl(path), {headers: O._hdr});
    if (!r.ok) {
      const e = await O._fail(r, 'stat', path);
      if (O._isCode(e, 'ENOENT')) return null;
      throw e;
    }
    const d = await r.json();
    return d.Exists === false ? null : d;
  };
  // Metadata of the file at path, or null.
  O._file = async function (path) {
    const r = await O._fetch(O._fileUrl(path), {headers: O._hdr});
    if (r.ok) return r.json();
    const e = await O._fail(r, 'stat', path);
    if (O._isCode(e, 'ENOENT')) return null;
    throw e;
  };
  O._missing = async function (path) {
    if (await O._file(path)) return O._err('ENOTDIR', path + ' is a file');
    return O._err('ENOENT', path + ': not found');
  };
  // A browser link: a folder opens in its library view, a file in Office for
  // the web (or the preview, for other types).
  O._webUrl = function (path, isDir) {
    if (!path) return '';
    const enc = function (p) { return p.split('/').map(encodeURIComponent).join('/'); };
    if (!isDir) return O._origin(path) + enc(path) + '?web=1';
    const lib = '/' + O._segs(path).slice(0, O._libDepth(path)).join('/');
    return O._origin(path) + enc(lib) + '/Forms/AllItems.aspx?id=' + encodeURIComponent(path);
  };
  O._folderEntry = function (d) {
    const path = d.ServerRelativeUrl || '';
    return {name: d.Name || '', path: path, is_dir: true,
            size: 0, mime: '', modified: d.TimeLastModified || '', id: d.UniqueId || '',
            web_url: O._webUrl(path, true)};
  };
  O._fileEntry = function (f) {
    const path = f.ServerRelativeUrl || '';
    return {name: f.Name || '', path: path, is_dir: false,
            size: Number(f.Length || 0) || 0, mime: '',
            modified: f.TimeLastModified || '', id: f.UniqueId || '',
            web_url: O._webUrl(path, false)};
  };
  O.ls = async function (path) {
    if (!(await O._folder(path))) throw await O._missing(path);
    const fu = O._folderUrl(path);
    let fj, xj;
    try {
      fj = await O._getJson(fu + '/Folders', 'ls', path);
      xj = await O._getJson(fu + '/Files', 'ls', path);
    } catch (e) {
      if (!O._throttled(e)) throw e;
      return O._lsPaged(path);
    }
    const out = [];
    for (const d of (fj.value || []))
      if ((d.Name || '') !== 'Forms') out.push(O._folderEntry(d));
    for (const f of (xj.value || [])) out.push(O._fileEntry(f));
    return out;
  };
  // A folder over the list view threshold (5000 items) refuses /Folders and
  // /Files; the list's paged view still serves it.
  O._throttled = function (e) {
    return /SPQueryThrottledException|-2147024860|list view threshold/i.test(String(e && e.message));
  };
  O._lsPaged = async function (path) {
    const lib = '/' + O._segs(path).slice(0, O._libDepth(path)).join('/');
    const view = "<View><ViewFields><FieldRef Name='FileLeafRef'/><FieldRef Name='FileRef'/>" +
      "<FieldRef Name='FSObjType'/><FieldRef Name='File_x0020_Size'/><FieldRef Name='Modified'/>" +
      "<FieldRef Name='UniqueId'/></ViewFields><RowLimit Paged='TRUE'>5000</RowLimit></View>";
    const digest = await O._digest(path);
    const dirs = [], files = [];
    let next = '';
    do {
      const url = O._api(path) + "/web/GetList(@l)/RenderListDataAsStream?@l='" + O._lit(lib) + "'" +
        (next ? '&' + next.replace(/^\?/, '') : '');
      const r = await O._postJson(url, digest, {parameters: {RenderOptions: 2,
        FolderServerRelativeUrl: path, ViewXml: view}});
      if (!r.ok) throw await O._fail(r, 'ls', path);
      const j = await r.json();
      for (const row of (j.Row || [])) {
        const isDir = String(row.FSObjType) === '1';
        const name = row.FileLeafRef || '';
        if (isDir && name === 'Forms') continue;
        (isDir ? dirs : files).push({name: name, path: row.FileRef || '', is_dir: isDir,
          size: isDir ? 0 : (Number(row.File_x0020_Size || 0) || 0), mime: '',
          modified: row['Modified.'] || row.Modified || '',
          id: String(row.UniqueId || '').replace(/[{}]/g, ''),
          web_url: O._webUrl(row.FileRef || '', isDir)});
      }
      next = j.NextHref || '';
    } while (next);
    return dirs.concat(files);
  };
  O.stat = async function (path) {
    const f = await O._file(path);
    if (f) return O._fileEntry(f);
    const d = await O._folder(path);
    if (d) return O._folderEntry(d);
    throw O._err('ENOENT', path + ': not found');
  };
  // How this tenant serves a byte range: '$value' honours Range, else
  // download.aspx does, else the whole body is fetched and sliced.
  O._rangeMode = null;
  O._range = async function (path, start, end) {
    const hdr = {'Range': 'bytes=' + start + '-' + (end - 1)};
    const dl = location.origin + O._webBase(path).split('/').map(encodeURIComponent).join('/') +
      '/_layouts/15/download.aspx?SourceUrl=' + encodeURIComponent(path);
    if (O._rangeMode === null || O._rangeMode === 'value') {
      const r = await O._fetch(O._fileUrl(path) + '/$value', {headers: hdr});
      if (!r.ok) throw await O._fail(r, 'get', path);
      if (r.status === 206) { O._rangeMode = 'value'; return r; }
      if (O._rangeMode === null) {
        const d = await O._fetch(dl, {headers: hdr});
        if (d.status === 206) { O._rangeMode = 'download'; return d; }
        O._rangeMode = 'slice';
      }
      return {sliced: true, r: r};
    }
    if (O._rangeMode === 'download') {
      const r = await O._fetch(dl, {headers: hdr});
      if (r.status === 206) return r;
      if (!r.ok) throw await O._fail(r, 'get', path);
      return {sliced: true, r: r};
    }
    const r = await O._fetch(O._fileUrl(path) + '/$value', {});
    if (!r.ok) throw await O._fail(r, 'get', path);
    return {sliced: true, r: r};
  };
  // Bytes [offset, offset+length) of a file; length null reads to the end.
  // etag lets a chunked reader notice the file changing under it.
  O.get = async function (path, offset, length) {
    const f = await O._file(path);
    if (!f) {
      if (await O._folder(path)) throw O._err('EISDIR', path + ' is a folder');
      throw O._err('ENOENT', path + ': not found');
    }
    const size = Number(f.Length || 0) || 0;
    const start = Math.min(Math.max(Number(offset) || 0, 0), size);
    const end = length == null ? size : Math.min(size, start + Number(length));
    let buf = new ArrayBuffer(0), mime = '';
    if (start === 0 && end === size) {
      const r = await O._fetch(O._fileUrl(path) + '/$value', {});
      if (!r.ok) throw await O._fail(r, 'get', path);
      mime = r.headers.get('Content-Type') || '';
      buf = await r.arrayBuffer();
    } else if (end > start) {
      const got = await O._range(path, start, end);
      const r = got.sliced ? got.r : got;
      mime = r.headers.get('Content-Type') || '';
      buf = await r.arrayBuffer();
      if (got.sliced) buf = buf.slice(start, end);
    }
    return {filename: f.Name || O._segs(path).pop() || 'file', mime: mime,
            size: size, offset: start, etag: f.ETag || '', b64: O._b64(buf)};
  };
  O.mkdir = async function (path) {
    const segs = O._segs(path);
    const lib = O._libDepth(path);
    if (segs.length < lib)
      throw O._err('EINVAL', path + ' is above a document library; mkdir cannot create sites or libraries');
    let cur = '/' + segs.slice(0, lib).join('/');
    let d = await O._folder(cur);
    if (!d) throw O._err('ENOENT', cur + ': no such document library');
    let digest = null;
    for (let i = lib; i < segs.length; i++) {
      cur += '/' + segs[i];
      d = await O._folder(cur);
      if (d) continue;
      if (await O._file(cur)) throw O._err('ENOTDIR', cur + ' is a file');
      digest = digest || await O._digest(path);
      const r = await O._post(O._api(path) + "/web/folders/AddUsingPath(decodedurl='" +
        O._lit(cur) + "')", digest);
      if (r.ok) { d = await r.json(); continue; }
      d = await O._folder(cur);  // a concurrent mkdir may have won the race
      if (!d) throw await O._fail(r, 'mkdir', cur);
    }
    return O._folderEntry(d);
  };
  O.put = async function (path, b64, mime, parents) {
    const name = O._segs(path).pop();
    const parent = O._parent(path);
    if (!name) throw O._err('EINVAL', 'put needs a file path');
    if (parents) await O.mkdir(parent);
    const digest = await O._digest(path);
    const r = await O._post(O._folderUrl(parent) + "/Files/add(url='" + O._lit(name) +
      "',overwrite=true)", digest, O._unb64(b64));
    if (r.ok) return O._fileEntry(await r.json());
    const e = await O._fail(r, 'put', path);
    if (O._isCode(e, 'ENOENT') && !(await O._folder(parent)))
      throw O._err('ENOENT', 'parent folder ' + parent +
        ' does not exist (pass parents:true to create it)');
    throw e;
  };
  O._guid = function (id) {
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(String(id)))
      throw O._err('EINVAL', 'upload_id must be a GUID: ' + id);
    return "uploadId=guid'" + id + "'";
  };
  // One chunk of a chunked upload; the caller owns upload_id and offset, so the
  // daemon keeps no state. Offset 0 creates the file empty and starts the
  // session, a final chunk finishes it. A one-chunk file is a plain put.
  O.upload = async function (path, uploadId, offset, final, b64, parents) {
    offset = Number(offset) || 0;
    if (offset === 0 && final) return {entry: await O.put(path, b64, null, parents)};
    const g = O._guid(uploadId);
    const fu = O._fileUrl(path);
    let url;
    if (offset === 0) {
      await O.put(path, '', null, parents);
      url = fu + '/StartUpload(' + g + ')';
    } else if (final) url = fu + '/FinishUpload(' + g + ',fileOffset=' + offset + ')';
    else url = fu + '/ContinueUpload(' + g + ',fileOffset=' + offset + ')';
    const digest = await O._digest(path);
    const r = await O._post(url, digest, O._unb64(b64));
    if (!r.ok) {
      const e = await O._fail(r, 'upload', path);
      await O.uploadCancel(path, uploadId);
      throw e;
    }
    const j = await r.json();
    if (final) return {entry: O._fileEntry(j)};
    return {offset: Number(j.value !== undefined ? j.value : j)};
  };
  // Best effort: an abandoned session otherwise holds the file locked until it expires.
  O.uploadCancel = async function (path, uploadId) {
    try {
      const digest = await O._digest(path);
      const r = await O._post(O._fileUrl(path) + '/CancelUpload(' + O._guid(uploadId) + ')', digest);
      return {ok: r.ok};
    } catch (e) { return {ok: false}; }
  };
  O.rm = async function (path) {
    if (O._segs(path).length <= O._libDepth(path))
      throw O._err('EINVAL', 'refusing to remove a site or document library: ' + path);
    const digest = await O._digest(path);
    let r = await O._post(O._fileUrl(path) + '/recycle()', digest);
    if (r.ok) return {ok: true};
    const e = await O._fail(r, 'rm', path);
    if (!O._isCode(e, 'ENOENT') || !(await O._folder(path))) throw e;
    r = await O._post(O._folderUrl(path) + '/recycle()', digest);
    if (!r.ok) throw await O._fail(r, 'rm', path);
    return {ok: true};
  };
  // The origin serving a path: personal OneDrive lives on the tenant's -my host.
  O._origin = function (path) {
    const tenant = location.origin.replace(/-my\.sharepoint\.com$/i, '.sharepoint.com');
    if ((O._segs(path)[0] || '').toLowerCase() === 'personal')
      return tenant.replace(/\.sharepoint\.com$/i, '-my.sharepoint.com');
    return tenant;
  };
  O._full = function (path) { return O._origin(path) + path; };
  O._postJson = async function (url, digest, obj, verbose) {
    return O._fetch(url, {method: 'POST', body: JSON.stringify(obj), headers: {
      'X-RequestDigest': digest, 'Accept': 'application/json;odata=nometadata',
      'Content-Type': verbose ? 'application/json;odata=verbose' : 'application/json;odata=nometadata'}});
  };
  O._resPath = function (path) {
    return {__metadata: {type: 'SP.ResourcePath'}, DecodedUrl: O._full(path)};
  };
  O._sameSite = function (a, b) {
    return O._origin(a) === O._origin(b) &&
      O._webBase(a).toLowerCase() === O._webBase(b).toLowerCase();
  };
  // Move or copy within one site collection. The caller has cleared the target.
  O.transfer = async function (src, dst, isDir, copy) {
    if (!O._sameSite(src, dst))
      throw O._err('EINVAL', 'transfer needs one site collection; use copyJob for ' + src + ' -> ' + dst);
    const op = (copy ? 'Copy' : 'Move') + (isDir ? 'Folder' : 'File') + 'ByPath';
    const options = {__metadata: {type: 'SP.MoveCopyOptions'}, KeepBoth: false,
      ResetAuthorAndCreatedOnCopy: false, ShouldBypassSharedLocks: true};
    if (!copy) options.RetainEditorAndModifiedOnMove = true;
    const url = O._api(src) + '/SP.MoveCopyUtil.' + op +
      (isDir ? '()' : '(overwrite=@a1)?@a1=false');
    const digest = await O._digest(src);
    const r = await O._postJson(url, digest,
      {srcPath: O._resPath(src), destPath: O._resPath(dst), options: options}, true);
    if (!r.ok) throw await O._fail(r, copy ? 'cp' : 'mv', src + ' -> ' + dst);
    return {ok: true};
  };
  // Start a cross-site copy or move of src into the folder destDir. The item
  // keeps its name. Poll it with copyJobProgress.
  O.copyJob = async function (src, destDir, move) {
    const digest = await O._digest(src);
    const r = await O._postJson(O._api(src) + '/site/CreateCopyJobs', digest, {
      exportObjectUris: [O._full(src)], destinationUri: O._full(destDir),
      options: {IgnoreVersionHistory: false, IsMoveMode: !!move,
                AllowSchemaMismatch: true, NameConflictBehavior: 0}});
    if (!r.ok) throw await O._fail(r, move ? 'mv' : 'cp', src + ' -> ' + destDir);
    const j = await r.json();
    const job = (j.value || [])[0];
    if (!job) throw new Error('CreateCopyJobs returned no job for ' + src);
    return job;
  };
  // {done, error}: done once the job leaves the queue. A logged JobError or
  // JobFatalError is the job's failure.
  O.copyJobProgress = async function (src, job) {
    const digest = await O._digest(src);
    const r = await O._postJson(O._api(src) + '/site/GetCopyJobProgress', digest,
      {copyJobInfo: job});
    if (!r.ok) throw await O._fail(r, 'copy job', src);
    const j = await r.json();
    let error = '';
    for (const line of (j.Logs || [])) {
      let ev = {};
      try { ev = JSON.parse(line); } catch (e) { continue; }
      if (ev.Event === 'JobError' || ev.Event === 'JobFatalError')
        error = error || (ev.Message || ev.Event);
    }
    return {done: Number(j.JobState) === 0, error: error};
  };
  O.search = async function (query, limit, root) {
    const api = O._api(root || '');
    let q = String(query || '');
    if (root) q = q + ' Path:"' + location.origin + root + '*"';
    const url = api + "/search/query?querytext='" + encodeURIComponent(q.replace(/'/g, "''")) +
      "'&rowlimit=" + (limit || 50) +
      "&selectproperties='Title,Path,FileExtension,Size,LastModifiedTime,UniqueId,IsContainer'";
    const j = await O._getJson(url, 'search', root || '/');
    let rows = [];
    try { rows = j.PrimaryQueryResult.RelevantResults.Table.Rows; } catch (e) { rows = []; }
    return (rows || []).map(row => {
      const c = {};
      for (const cell of (row.Cells || [])) c[cell.Key] = cell.Value;
      const path = c.Path || c.OriginalPath || '';
      // SharePoint search Path is a full url; reduce to a server-relative path.
      let sr = path;
      try { sr = new URL(path).pathname; } catch (e) {}
      const isDir = String(c.IsContainer).toLowerCase() === 'true';
      return {name: c.Title || (sr.split('/').filter(Boolean).pop()) || '',
        path: decodeURIComponent(sr), is_dir: isDir,
        size: Number(c.Size || 0) || 0, mime: isDir ? '' : (c.FileExtension || ''),
        modified: c.LastModifiedTime || '', id: c.UniqueId || '', web_url: path};
    });
  };
  // The ListItemAllFields url of the file or folder at path.
  O._item = async function (path) {
    if (await O._file(path)) return {base: O._fileUrl(path) + '/ListItemAllFields', isDir: false};
    if (await O._folder(path)) return {base: O._folderUrl(path) + '/ListItemAllFields', isDir: true};
    throw O._err('ENOENT', path + ': not found');
  };
  O._linkKind = {organization: {view: 2, edit: 3}, anyone: {view: 4, edit: 5}};
  O._scopeOf = {1: 'direct', 2: 'organization', 3: 'organization', 4: 'anyone',
                5: 'anyone', 6: 'people'};
  O._claim = function (who) {
    who = String(who || '');
    return who.indexOf('|') >= 0 ? who : 'i:0#.f|membership|' + who;
  };
  // A link (organization or anyone) on the item, or direct access for people.
  O.share = async function (path, scope, role, people, sendEmail) {
    if (role !== 'view' && role !== 'edit')
      throw O._err('EINVAL', 'role must be view or edit, not ' + role);
    const it = await O._item(path);
    const digest = await O._digest(path);
    if (scope === 'people') {
      if (!people || !people.length) throw O._err('EINVAL', 'scope people needs people');
      const r = await O._postJson(O._api(path) + '/SP.Web.ShareObject', digest, {
        url: O._full(path),
        peoplePickerInput: JSON.stringify(people.map(function (p) { return {Key: O._claim(p)}; })),
        roleValue: role === 'edit' ? 'role:1073741827' : 'role:1073741826',
        groupId: 0, propagateAcl: false, sendEmail: !!sendEmail,
        includeAnonymousLinkInEmail: false, emailSubject: '', emailBody: '',
        useSimplifiedRoles: true});
      if (!r.ok) throw await O._fail(r, 'share', path);
      const j = await r.json();
      if (Number(j.StatusCode) < 0)
        throw O._err(Number(j.StatusCode) === -2 ? 'EAUTH' : 'EINVAL', 'share ' + path + ': ' +
          (j.ErrorMessage || 'SharePoint status ' + j.StatusCode));
      return {url: j.Url || O._webUrl(path, it.isDir), scope: scope, role: role, people: people};
    }
    const kinds = O._linkKind[scope];
    if (!kinds) throw O._err('EINVAL', 'scope must be organization, people or anyone, not ' + scope);
    const r = await O._postJson(it.base + '/ShareLink', digest,
      {request: {createLink: true, settings: {linkKind: kinds[role], expiration: null}}});
    if (!r.ok) throw await O._fail(r, 'share', path);
    const info = (await r.json()).sharingLinkInfo || {};
    return {url: info.Url || '', share_id: info.ShareId || '', scope: scope, role: role};
  };
  O.shares = async function (path) {
    const it = await O._item(path);
    const digest = await O._digest(path);
    const r = await O._postJson(it.base + '/GetSharingInformation?$expand=permissionsInformation',
      digest, {request: {maxPrincipalsToReturn: 100, maxLinkMembersToReturn: 100}});
    if (!r.ok) throw await O._fail(r, 'shares', path);
    const pi = (await r.json()).permissionsInformation || {};
    const links = [];
    for (const l of (pi.links || [])) {
      const d = l.linkDetails || {};
      if (!d.Url) continue;
      links.push({url: d.Url, share_id: d.ShareId || '', link_kind: Number(d.LinkKind),
                  scope: O._scopeOf[d.LinkKind] || String(d.LinkKind),
                  role: d.IsEditLink ? 'edit' : 'view', expires: d.Expiration || ''});
    }
    const people = (pi.principals || []).map(function (p) {
      const q = p.principal || {};
      return {name: q.name || '', email: q.email || '', login: q.loginName || '',
              role: ({1: 'view', 2: 'edit', 3: 'owner'})[p.role] || String(p.role)};
    });
    return {links: links, people: people};
  };
  // Revoke one link (by url or share id) or one person's access (by email or claim).
  O.unshare = async function (path, link, person) {
    const it = await O._item(path);
    const digest = await O._digest(path);
    if (link) {
      const L = (await O.shares(path)).links.find(function (l) {
        return l.url === link || l.share_id === link;
      });
      if (!L) throw O._err('ENOENT', path + ' has no share link ' + link);
      const r = await O._postJson(it.base + '/UnshareLink', digest,
        {linkKind: L.link_kind, shareId: L.share_id});
      if (!r.ok) throw await O._fail(r, 'unshare', path);
      return {ok: true, link: L.url};
    }
    if (!person) throw O._err('EINVAL', 'unshare needs a link or a person');
    const who = String(person);
    const lit = encodeURIComponent(who.replace(/'/g, "''"));
    const uurl = O._api(path) + (who.indexOf('|') >= 0
      ? "/web/SiteUsers/GetByLoginName(@v)?@v='" + lit + "'"
      : "/web/SiteUsers/GetByEmail('" + lit + "')");
    const u = await O._getJson(uurl, 'unshare', who);
    const r = await O._fetch(it.base + '/RoleAssignments/GetByPrincipalId(' + Number(u.Id) + ')',
      {method: 'POST', headers: {'X-RequestDigest': digest, 'X-HTTP-Method': 'DELETE',
                                 'Accept': 'application/json;odata=nometadata'}});
    if (!r.ok) throw await O._fail(r, 'unshare', path + ' for ' + who);
    return {ok: true, person: who};
  };
  O._shareToken = function (url) {
    const u = new URL(url);
    const segs = u.pathname.split('/').filter(Boolean);
    if (segs.length < 3 || !/^:[a-z]:$/i.test(segs[0]))
      throw O._err('EINVAL', 'not a SharePoint share link: ' + url);
    const kind = segs[1].toLowerCase(), rest = segs.slice(2, -1);
    let web = '';
    if (kind === 't' && rest.length) web = '/teams/' + rest[0];
    else if (kind === 's' && rest.length) web = '/sites/' + rest[0];
    else web = rest.length ? '/' + rest.map(decodeURIComponent).join('/') : '';
    return {web: web, token: segs[segs.length - 1]};
  };
  O._guidAt = function (b, k) {
    const h = function (i) { return b[k + i].toString(16).padStart(2, '0'); };
    return h(3) + h(2) + h(1) + h(0) + '-' + h(5) + h(4) + '-' + h(7) + h(6) + '-' +
      h(8) + h(9) + '-' + h(10) + h(11) + h(12) + h(13) + h(14) + h(15);
  };
  // A share link → the path it points at. The v2.0 shares API answers first.
  // Failing that, the token carries the item's UniqueId little-endian after a
  // short prefix (two bytes in the one decoded sample, one tried as well).
  O.resolve = async function (url) {
    try { new URL(url); } catch (e) { throw O._err('EINVAL', 'not a url: ' + url); }
    const enc = 'u!' + btoa(unescape(encodeURIComponent(url)))
      .replace(/=+$/, '').replace(/\//g, '_').replace(/\+/g, '-');
    const r = await O._fetch(location.origin + '/_api/v2.0/shares/' + enc + '/driveItem',
      {headers: {'Accept': 'application/json'}});
    if (r.ok) {
      const j = await r.json();
      if (j.webUrl)
        return {entry: await O.stat(decodeURIComponent(new URL(j.webUrl).pathname)), via: 'shares'};
    }
    const t = O._shareToken(url);
    let b64 = t.token.replace(/-/g, '+').replace(/_/g, '/');
    while (b64.length % 4) b64 += '=';
    let bytes;
    try { bytes = O._unb64(b64); } catch (e) { throw O._err('EINVAL', 'undecodable share token in ' + url); }
    for (const k of [2, 1]) {
      if (bytes.length < k + 16) continue;
      const g = O._guidAt(bytes, k);
      for (const what of ['Folder', 'File']) {
        const fr = await O._fetch(O._api(t.web) + '/web/Get' + what + "ById('" + g + "')",
          {headers: O._hdr});
        if (!fr.ok) continue;
        const d = await fr.json();
        if (d.Exists === false || !d.ServerRelativeUrl) continue;
        return {entry: what === 'Folder' ? O._folderEntry(d) : O._fileEntry(d), via: 'token'};
      }
    }
    throw O._err('ENOENT', 'share link ' + url + ' resolves to nothing this session can see');
  };
  // Sites the session can reach (search-backed, so minutes behind a new
  // site), its personal OneDrive and followed sites; or, given a site, its
  // visible document libraries.
  O.sites = async function (site) {
    if (site) {
      const url = O._api(site) + '/web/lists?$filter=' +
        encodeURIComponent('BaseTemplate eq 101 and Hidden eq false') +
        '&$select=Title,ItemCount,LastItemModifiedDate,RootFolder/ServerRelativeUrl&$expand=RootFolder';
      const j = await O._getJson(url, 'sites', site);
      return (j.value || []).map(function (l) {
        const p = (l.RootFolder || {}).ServerRelativeUrl || '';
        return {name: l.Title || '', path: p, kind: 'library',
                items: Number(l.ItemCount || 0) || 0,
                modified: l.LastItemModifiedDate || '', web_url: O._webUrl(p, true)};
      });
    }
    const out = [], seen = {};
    const add = function (e) {
      const k = e.path.toLowerCase();
      if (seen[k]) { if (e.followed) seen[k].followed = true; return; }
      seen[k] = e; out.push(e);
    };
    const pathOf = function (u) {
      try { return decodeURIComponent(new URL(u).pathname).replace(/\/+$/, ''); }
      catch (e) { return ''; }
    };
    try {
      const me = await O._getJson(O._api('') +
        '/SP.UserProfiles.PeopleManager/GetMyProperties?$select=PersonalUrl', 'sites', '/');
      const p = pathOf(me.PersonalUrl || '');
      if (p) add({name: 'OneDrive (personal)', path: p, kind: 'personal',
                  web_url: me.PersonalUrl});
    } catch (e) { if (O._isCode(e, 'EAUTH')) throw e; }
    const q = O._api('') + "/search/query?querytext='contentclass:STS_Site'&rowlimit=500" +
      "&trimduplicates=false&selectproperties='Title,Path'";
    const j = await O._getJson(q, 'sites', '/');
    let rows = [];
    try { rows = j.PrimaryQueryResult.RelevantResults.Table.Rows; } catch (e) { rows = []; }
    for (const row of (rows || [])) {
      const c = {};
      for (const cell of (row.Cells || [])) c[cell.Key] = cell.Value;
      const p = pathOf(c.Path || '');
      if (p) add({name: c.Title || p, path: p, kind: 'site', web_url: c.Path});
    }
    try {
      const f = await O._getJson(O._api('') + '/social.following/my/followed(types=4)',
        'sites', '/');
      for (const s of (f.value || [])) {
        const p = pathOf(s.Uri || s.Url || '');
        if (p) add({name: s.Name || p, path: p, kind: 'site', followed: true,
                    web_url: s.Uri || s.Url});
      }
    } catch (e) { if (O._isCode(e, 'EAUTH')) throw e; }
    return out;
  };
  window.__awmMira.onedrive = O;
}
"""


def _segs(path: str) -> list[str]:
    return [s for s in (path or "").split("/") if s]


def _parent(path: str) -> str:
    return "/" + "/".join(_segs(path)[:-1])


def _site_key(path: str) -> str:
    """The site collection owning a path, as the prelude's ``_webBase`` sees it."""
    segs = _segs(path)
    if len(segs) >= 2 and segs[0].lower() in ("teams", "sites", "personal"):
        return f"/{segs[0]}/{segs[1]}".lower()
    return ""


def _is_personal(path: str | None) -> bool:
    segs = [s for s in (path or "").split("/") if s]
    return bool(segs) and segs[0].lower() == "personal"


class OneDriveDriver(StorageDriver):
    """SharePoint team/project sites on the tenant tab, personal OneDrive
    (``/personal/...``) on the ``-my`` tab. Same-origin fetch only reaches the
    tab's own host, so each host needs its own page."""

    name = "onedrive"
    target_needle = ".sharepoint.com"
    personal_needle = "-my.sharepoint.com"
    PRELUDE = ONEDRIVE_PRELUDE

    def __init__(self, page: CDPPage, personal_page: CDPPage | None = None) -> None:
        super().__init__(page)
        self.personal_page = personal_page

    @classmethod
    def create(cls, cdp_http_base: str) -> "OneDriveDriver":
        return cls(
            CDPPage(cdp_http_base, cls.target_needle, exclude=cls.personal_needle),
            CDPPage(cdp_http_base, cls.personal_needle))

    def pages(self) -> dict[str, Any]:
        out: dict[str, Any] = {"tenant": self.page}
        if self.personal_page is not None:
            out["personal"] = self.personal_page
        return out

    def _page_for(self, path: str | None) -> Any:
        if _is_personal(path):
            if self.personal_page is None:
                raise RuntimeError(
                    "no personal OneDrive page: open the tenant's -my.sharepoint.com "
                    "in mira's Opera")
            return self.personal_page
        return self.page

    async def ls(self, path: str) -> list[dict]:
        rows = await self._run("window.__awmMira.onedrive.ls", path, path=path)
        return rows or []

    async def stat(self, path: str) -> dict:
        res = await self._run("window.__awmMira.onedrive.stat", path, path=path)
        return res or {}

    async def get(self, path: str, offset: int = 0,
                  length: int | None = None) -> dict:
        res = await self._run("window.__awmMira.onedrive.get", path, offset,
                              length, path=path, timeout=TRANSFER_TIMEOUT_S)
        return res or {}

    async def upload(self, path: str, upload_id: str, offset: int, final: bool,
                     b64: str, parents: bool = False) -> dict:
        res = await self._run("window.__awmMira.onedrive.upload", path,
                              upload_id, offset, bool(final), b64, bool(parents),
                              path=path, timeout=TRANSFER_TIMEOUT_S, retry=False)
        return res or {}

    async def upload_cancel(self, path: str, upload_id: str) -> None:
        await self._run("window.__awmMira.onedrive.uploadCancel", path,
                        upload_id, path=path)

    async def put(self, path: str, b64: str, mime: str | None = None,
                  parents: bool = False) -> dict:
        res = await self._run("window.__awmMira.onedrive.put", path, b64, mime,
                              bool(parents), path=path,
                              timeout=TRANSFER_TIMEOUT_S, retry=False)
        return res or {}

    async def mkdir(self, path: str) -> dict:
        res = await self._run("window.__awmMira.onedrive.mkdir", path, path=path)
        return res or {}

    async def rm(self, path: str) -> None:
        await self._run("window.__awmMira.onedrive.rm", path, path=path)

    async def mv(self, src: str, dst: str, *, overwrite: bool = False,
                 parents: bool = False) -> dict:
        return await self._transfer(src, dst, copy=False,
                                    overwrite=overwrite, parents=parents)

    async def cp(self, src: str, dst: str, *, overwrite: bool = False,
                 parents: bool = False) -> dict:
        return await self._transfer(src, dst, copy=True,
                                    overwrite=overwrite, parents=parents)

    #: Seconds between polls: a copy job's progress, and a target settling.
    poll_delays = (0.5, 1.0, 2.0, 3.0, 5.0)

    async def _transfer(self, src: str, dst: str, *, copy: bool,
                        overwrite: bool, parents: bool) -> dict:
        """Clear the way, run the move or copy, and wait for the target.

        One site collection uses SharePoint's MoveCopyUtil. Across site
        collections (or onto personal OneDrive) it runs a copy job, which
        keeps version history where a download and re-upload would not."""
        verb = "cp" if copy else "mv"
        if not _segs(dst) or not _segs(src):
            raise ValueError(f"{verb} needs a source and a target path")
        if dst == src or dst.lower().startswith(src.lower() + "/"):
            raise ValueError(f"{verb}: cannot {verb} {src} onto itself or into itself")
        entry = await self.stat(src)
        try:
            existing = await self.stat(dst)
        except FileNotFoundError:
            existing = None
        if existing and existing.get("id") != entry.get("id"):
            if not overwrite:
                raise FileExistsError(
                    f"{dst} exists (pass overwrite:true to replace it)")
            await self.rm(dst)
        parent = _parent(dst)
        if parents:
            await self.mkdir(parent)
        else:
            try:
                p = await self.stat(parent)
            except FileNotFoundError:
                raise FileNotFoundError(
                    f"parent folder {parent} does not exist "
                    f"(pass parents:true to create it)") from None
            if not p.get("is_dir"):
                raise NotADirectoryError(f"{parent} is a file")
        is_dir = bool(entry.get("is_dir"))
        if _site_key(src) == _site_key(dst):
            await self._run("window.__awmMira.onedrive.transfer", src, dst,
                            is_dir, copy, path=src, timeout=TRANSFER_TIMEOUT_S,
                            retry=False)
        else:
            if _segs(dst)[-1] != _segs(src)[-1]:
                raise ValueError(
                    f"{verb} across site collections keeps the name "
                    f"{_segs(src)[-1]!r}; rename it after with mv")
            await self._copy_job(src, parent, move=not copy)
        return await self._settled(dst)

    async def _copy_job(self, src: str, dest_dir: str, *, move: bool) -> None:
        job = await self._run("window.__awmMira.onedrive.copyJob", src,
                              dest_dir, move, path=src, retry=False)
        deadline = time.monotonic() + COPY_JOB_TIMEOUT_S
        i = 0
        while True:
            prog = await self._run("window.__awmMira.onedrive.copyJobProgress",
                                   src, job, path=src) or {}
            if prog.get("error"):
                raise RuntimeError(f"copy job {src} -> {dest_dir}: {prog['error']}")
            if prog.get("done"):
                return
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"copy job {src} -> {dest_dir} still running after "
                    f"{COPY_JOB_TIMEOUT_S:.0f}s; it may yet finish")
            await asyncio.sleep(self.poll_delays[min(i, len(self.poll_delays) - 1)])
            i += 1

    async def _settled(self, path: str) -> dict:
        """The target's entry, waiting while SharePoint still settles it."""
        for delay in (*self.poll_delays, None):
            try:
                return await self.stat(path)
            except FileNotFoundError:
                if delay is None:
                    raise
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def share(self, path: str, scope: str, role: str,
                    people: list[str] | None = None,
                    send_email: bool = False) -> dict:
        return await self._run("window.__awmMira.onedrive.share", path, scope,
                               role, list(people or []), bool(send_email),
                               path=path, retry=False) or {}

    async def shares(self, path: str) -> dict:
        return await self._run("window.__awmMira.onedrive.shares", path,
                               path=path) or {}

    async def unshare(self, path: str, link: str | None = None,
                      person: str | None = None) -> dict:
        return await self._run("window.__awmMira.onedrive.unshare", path,
                               link or "", person or "", path=path) or {}

    async def resolve(self, url: str) -> dict:
        page_path = "/personal" if "-my.sharepoint.com" in url.lower() else None
        return await self._run("window.__awmMira.onedrive.resolve", url,
                               path=page_path) or {}

    async def sites(self, site: str | None = None) -> list[dict]:
        rows = await self._run("window.__awmMira.onedrive.sites", site or "",
                               path=site or None)
        return rows or []

    async def search(self, query: str, limit: int,
                     root: str | None = None) -> list[dict]:
        rows = await self._run(
            "window.__awmMira.onedrive.search", query, limit, root, path=root)
        return rows or []


STORAGE_REGISTRY: dict[str, type[StorageDriver]] = {
    "onedrive": OneDriveDriver,
}
