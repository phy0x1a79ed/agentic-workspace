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
from abc import ABC, abstractmethod
from typing import Any

from mira_api.cdp import CDPPage

log = logging.getLogger("mira_api.storage_drivers")


def _call(helper: str, *args: Any) -> str:
    """Build a JS expression invoking a window.__awmMira helper with JSON args."""
    arglist = ", ".join(json.dumps(a) for a in args)
    return f"(async()=>{{ return JSON.stringify(await {helper}({arglist})); }})()"


class StorageDriver(ABC):
    name: str = "base"
    #: substring identifying this store's Opera page target
    target_needle: str = ""
    #: idempotent JS installing window.__awmMira.<name>
    PRELUDE: str = ""

    def __init__(self, page: CDPPage) -> None:
        self.page = page

    async def _run(self, helper: str, *args: Any) -> Any:
        expr = self.PRELUDE + "\n" + _call(helper, *args)
        return await self.page.evaluate_json(expr)

    @abstractmethod
    async def ls(self, path: str) -> list[dict]: ...

    @abstractmethod
    async def stat(self, path: str) -> dict: ...

    @abstractmethod
    async def get(self, path: str) -> dict:
        """Download one file → ``{filename, mime, b64}`` (base64 over the hop)."""

    @abstractmethod
    async def put(self, path: str, b64: str, mime: str | None = None) -> dict:
        """Upload base64 bytes to ``path`` (create/overwrite) → entry dict."""

    @abstractmethod
    async def rm(self, path: str) -> None: ...

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
if (!window.__awmMira.onedrive || window.__awmMira.onedrive._v !== 1) {
  const O = {};
  O._v = 1;  // bump when changing helpers so a long-lived page self-upgrades
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
  // SharePoint OData string literal: single-quotes are doubled.
  O._sq = function (s) { return String(s).replace(/'/g, "''"); };
  // The _api web base for a server-relative path: a managed-path site collection
  // (/teams/<x>, /sites/<x>, /personal/<x>) owns its own _api; else the root web.
  O._webBase = function (path) {
    const segs = String(path || '').split('/').filter(Boolean);
    const mp = (segs[0] || '').toLowerCase();
    if (segs.length >= 2 && (mp === 'teams' || mp === 'sites' || mp === 'personal'))
      return '/' + segs[0] + '/' + segs[1];
    return '';
  };
  O._api = function (path) { return location.origin + O._webBase(path) + '/_api'; };
  O._json = {headers: {'Accept': 'application/json;odata=nometadata'},
             credentials: 'include'};
  O._digest = async function (path) {
    const r = await fetch(O._api(path) + '/contextinfo',
      {method: 'POST', headers: {'Accept': 'application/json;odata=nometadata'},
       credentials: 'include'});
    if (!r.ok) throw new Error('contextinfo ' + r.status);
    const j = await r.json();
    return j.FormDigestValue ||
      (j.d && j.d.GetContextWebInformation && j.d.GetContextWebInformation.FormDigestValue);
  };
  O._folderCall = function (path) {
    return "/web/GetFolderByServerRelativePath(decodedurl='" + O._sq(path) + "')";
  };
  O._fileCall = function (path) {
    return "/web/GetFileByServerRelativePath(decodedurl='" + O._sq(path) + "')";
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
    const api = O._api(path), fc = O._folderCall(path);
    const fr = await fetch(encodeURI(api + fc + '/Folders'), O._json);
    if (!fr.ok) throw new Error('ls folders ' + fr.status);
    const xr = await fetch(encodeURI(api + fc + '/Files'), O._json);
    if (!xr.ok) throw new Error('ls files ' + xr.status);
    const fj = await fr.json(), xj = await xr.json();
    const out = [];
    for (const d of (fj.value || []))
      if ((d.Name || '') !== 'Forms') out.push(O._folderEntry(d));
    for (const f of (xj.value || [])) out.push(O._fileEntry(f));
    return out;
  };
  O.stat = async function (path) {
    const api = O._api(path);
    let r = await fetch(encodeURI(api + O._fileCall(path)), O._json);
    if (r.ok) return O._fileEntry(await r.json());
    r = await fetch(encodeURI(api + O._folderCall(path)), O._json);
    if (r.ok) return O._folderEntry(await r.json());
    throw new Error('stat ' + r.status);
  };
  O.get = async function (path) {
    const api = O._api(path);
    let name = (String(path).split('/').filter(Boolean).pop()) || 'file';
    const meta = await fetch(encodeURI(api + O._fileCall(path)), O._json);
    if (meta.ok) { const m = await meta.json(); name = m.Name || name; }
    const r = await fetch(encodeURI(api + O._fileCall(path) + '/$value'),
      {credentials: 'include'});
    if (!r.ok) throw new Error('get ' + r.status);
    const buf = await r.arrayBuffer();
    return {filename: name, mime: r.headers.get('Content-Type') || '', b64: O._b64(buf)};
  };
  O.put = async function (path, b64, mime) {
    const api = O._api(path);
    const segs = String(path).split('/').filter(Boolean);
    const name = segs.pop();
    const parent = '/' + segs.join('/');
    const digest = await O._digest(path);
    const call = O._folderCall(parent) + "/Files/add(url='" + O._sq(name) +
      "',overwrite=true)";
    const r = await fetch(encodeURI(api + call), {method: 'POST',
      headers: {'X-RequestDigest': digest, 'Accept': 'application/json;odata=nometadata'},
      body: O._unb64(b64), credentials: 'include'});
    if (!r.ok) throw new Error('put ' + r.status + ' ' + (await r.text()).slice(0, 200));
    return O._fileEntry(await r.json());
  };
  O.rm = async function (path) {
    const api = O._api(path);
    const digest = await O._digest(path);
    const opts = {method: 'POST', headers: {'X-RequestDigest': digest,
      'Accept': 'application/json;odata=nometadata'}, credentials: 'include'};
    let r = await fetch(encodeURI(api + O._fileCall(path) + '/recycle()'), opts);
    if (r.ok) return {ok: true};
    r = await fetch(encodeURI(api + O._folderCall(path) + '/recycle()'), opts);
    if (!r.ok) throw new Error('rm ' + r.status);
    return {ok: true};
  };
  O.search = async function (query, limit, root) {
    const api = location.origin + O._webBase(root || '') + '/_api';
    let q = String(query || '');
    if (root) q = q + ' Path:"' + location.origin + root + '*"';
    const url = api + "/search/query?querytext='" + encodeURIComponent(q) +
      "'&rowlimit=" + (limit || 50) +
      "&selectproperties='Title,Path,FileExtension,Size,LastModifiedTime,UniqueId'";
    const r = await fetch(url, O._json);
    if (!r.ok) throw new Error('search ' + r.status);
    const j = await r.json();
    let rows = [];
    try { rows = j.PrimaryQueryResult.RelevantResults.Table.Rows; } catch (e) { rows = []; }
    return (rows || []).map(row => {
      const c = {};
      for (const cell of (row.Cells || [])) c[cell.Key] = cell.Value;
      const path = c.Path || c.OriginalPath || '';
      // SharePoint search Path is a full url; reduce to a server-relative path.
      let sr = path;
      try { sr = new URL(path).pathname; } catch (e) {}
      return {name: c.Title || (sr.split('/').filter(Boolean).pop()) || '',
        path: decodeURIComponent(sr), is_dir: false,
        size: Number(c.Size || 0) || 0, mime: c.FileExtension || '',
        modified: c.LastModifiedTime || '', id: c.UniqueId || ''};
    });
  };
  window.__awmMira.onedrive = O;
}
"""


class OneDriveDriver(StorageDriver):
    name = "onedrive"
    target_needle = "sharepoint.com"
    PRELUDE = ONEDRIVE_PRELUDE

    async def ls(self, path: str) -> list[dict]:
        rows = await self._run("window.__awmMira.onedrive.ls", path)
        return rows or []

    async def stat(self, path: str) -> dict:
        res = await self._run("window.__awmMira.onedrive.stat", path)
        return res or {}

    async def get(self, path: str) -> dict:
        res = await self._run("window.__awmMira.onedrive.get", path)
        return res or {}

    async def put(self, path: str, b64: str, mime: str | None = None) -> dict:
        res = await self._run("window.__awmMira.onedrive.put", path, b64, mime)
        return res or {}

    async def rm(self, path: str) -> None:
        await self._run("window.__awmMira.onedrive.rm", path)

    async def search(self, query: str, limit: int,
                     root: str | None = None) -> list[dict]:
        rows = await self._run(
            "window.__awmMira.onedrive.search", query, limit, root)
        return rows or []


STORAGE_REGISTRY: dict[str, type[StorageDriver]] = {
    "onedrive": OneDriveDriver,
}
