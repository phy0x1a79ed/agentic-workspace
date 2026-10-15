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

import json
import logging
import re
from abc import ABC, abstractmethod
from typing import Any

from mira_api.cdp import CDPPage

log = logging.getLogger("mira_api.storage_drivers")


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

    async def _run(self, helper: str, *args: Any, path: str | None = None) -> Any:
        expr = self.PRELUDE + "\n" + _call(helper, *args)
        try:
            return await self._page_for(path).evaluate_json(expr)
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
    async def get(self, path: str) -> dict:
        """Download one file → ``{filename, mime, b64}`` (base64 over the hop)."""

    @abstractmethod
    async def put(self, path: str, b64: str, mime: str | None = None,
                  parents: bool = False) -> dict:
        """Upload base64 bytes to ``path`` (create/overwrite) → entry dict.

        ``parents`` creates missing folders above ``path``; without it a missing
        parent is ``FileNotFoundError``."""

    @abstractmethod
    async def rm(self, path: str) -> None: ...

    async def mkdir(self, path: str) -> dict:
        """Create the folder at ``path`` and any missing parents → entry dict.
        Non-abstract: a store without folders leaves this default (501)."""
        raise NotImplementedError(
            f"{self.name} storage driver does not support mkdir")

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
if (!window.__awmMira.onedrive || window.__awmMira.onedrive._v !== 2) {
  const O = {};
  O._v = 2;  // bump when changing helpers so a long-lived page self-upgrades
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
  O._folderEntry = function (d) {
    return {name: d.Name || '', path: d.ServerRelativeUrl || '', is_dir: true,
            size: 0, mime: '', modified: d.TimeLastModified || '', id: d.UniqueId || ''};
  };
  O._fileEntry = function (f) {
    return {name: f.Name || '', path: f.ServerRelativeUrl || '', is_dir: false,
            size: Number(f.Length || 0) || 0, mime: '',
            modified: f.TimeLastModified || '', id: f.UniqueId || ''};
  };
  O.ls = async function (path) {
    if (!(await O._folder(path))) throw await O._missing(path);
    const fu = O._folderUrl(path);
    const fj = await O._getJson(fu + '/Folders', 'ls', path);
    const xj = await O._getJson(fu + '/Files', 'ls', path);
    const out = [];
    for (const d of (fj.value || []))
      if ((d.Name || '') !== 'Forms') out.push(O._folderEntry(d));
    for (const f of (xj.value || [])) out.push(O._fileEntry(f));
    return out;
  };
  O.stat = async function (path) {
    const f = await O._file(path);
    if (f) return O._fileEntry(f);
    const d = await O._folder(path);
    if (d) return O._folderEntry(d);
    throw O._err('ENOENT', path + ': not found');
  };
  O.get = async function (path) {
    const f = await O._file(path);
    if (!f) {
      if (await O._folder(path)) throw O._err('EISDIR', path + ' is a folder');
      throw O._err('ENOENT', path + ': not found');
    }
    const r = await O._fetch(O._fileUrl(path) + '/$value', {});
    if (!r.ok) throw await O._fail(r, 'get', path);
    const buf = await r.arrayBuffer();
    return {filename: f.Name || O._segs(path).pop() || 'file',
            mime: r.headers.get('Content-Type') || '', b64: O._b64(buf)};
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
        modified: c.LastModifiedTime || '', id: c.UniqueId || ''};
    });
  };
  window.__awmMira.onedrive = O;
}
"""


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

    async def get(self, path: str) -> dict:
        res = await self._run("window.__awmMira.onedrive.get", path, path=path)
        return res or {}

    async def put(self, path: str, b64: str, mime: str | None = None,
                  parents: bool = False) -> dict:
        res = await self._run("window.__awmMira.onedrive.put", path, b64, mime,
                              bool(parents), path=path)
        return res or {}

    async def mkdir(self, path: str) -> dict:
        res = await self._run("window.__awmMira.onedrive.mkdir", path, path=path)
        return res or {}

    async def rm(self, path: str) -> None:
        await self._run("window.__awmMira.onedrive.rm", path, path=path)

    async def search(self, query: str, limit: int,
                     root: str | None = None) -> list[dict]:
        rows = await self._run(
            "window.__awmMira.onedrive.search", query, limit, root, path=root)
        return rows or []


STORAGE_REGISTRY: dict[str, type[StorageDriver]] = {
    "onedrive": OneDriveDriver,
}
