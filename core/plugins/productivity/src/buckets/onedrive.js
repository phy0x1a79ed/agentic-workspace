// Installs window.__awmDrive in a signed-in *.sharepoint.com tab: OneDrive and SharePoint
// through the SharePoint REST API, same-origin, so the session's cookies ride along. Paths are
// server-relative (/personal/<x>/Documents/..., /teams/<x>/Shared Documents/...). A write
// carries the X-RequestDigest from _api/contextinfo. A failure's message starts with ENOENT,
// EISDIR, ENOTDIR, EINVAL or EAUTH when it is one of those.
window.__awmDrive = window.__awmDrive || {};
if (window.__awmDrive._v !== 1) {
  const O = {_v: 1};
  O.b64 = function (buf) {
    const bytes = new Uint8Array(buf);
    let bin = '';
    for (let i = 0; i < bytes.length; i += 0x8000)
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(bin);
  };
  O.unb64 = function (b64) {
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return bytes;
  };
  O.err = (code, msg) => new Error(code + ': ' + msg);
  O.isCode = (e, code) => String(e && e.message).startsWith(code + ':');
  O.segs = path => String(path || '').split('/').filter(Boolean);
  O.parent = function (path) { const s = O.segs(path); s.pop(); return '/' + s.join('/'); };
  // A decodedurl='...' literal: quotes doubled for OData, then percent-encoded with the
  // slashes kept, so '#', '?', '%' and '&' in a name survive.
  O.lit = path => encodeURIComponent(String(path).replace(/'/g, "''")).replace(/%2F/g, '/');
  // The site collection that owns a path: /teams/<x>, /sites/<x> and /personal/<x> have their own _api.
  O.webBase = function (path) {
    const s = O.segs(path);
    const mp = (s[0] || '').toLowerCase();
    return s.length >= 2 && ['teams', 'sites', 'personal'].includes(mp) ? '/' + s[0] + '/' + s[1] : '';
  };
  O.libDepth = path => O.segs(O.webBase(path)).length + 1;
  O.api = path => location.origin + O.webBase(path).split('/').map(encodeURIComponent).join('/') + '/_api';
  O.hdr = {'Accept': 'application/json;odata=nometadata'};
  // redirect:'manual' turns an expired session's bounce to sign-in into a response we can name.
  O.fetch = (url, opts) => fetch(url, Object.assign({credentials: 'include', redirect: 'manual'}, opts || {}));
  O.fail = async function (r, what, path) {
    if (r.type === 'opaqueredirect' || r.status === 0)
      return O.err('EAUTH', what + ' ' + path + ': the SharePoint session redirected to sign-in');
    if (r.status === 401 || r.status === 403)
      return O.err('EAUTH', what + ' ' + path + ': HTTP ' + r.status + ' (no access, or the session needs signing in again)');
    let text = '';
    try { text = (await r.text()).slice(0, 300); } catch (e) { /* the status says enough */ }
    if (r.status === 404 || /does not exist|FileNotFound|DirectoryNotFound|-2147024894|-2130575338/.test(text))
      return O.err('ENOENT', what + ' ' + path + ': not found');
    return new Error(what + ' ' + path + ': HTTP ' + r.status + ' ' + text);
  };
  O.getJson = async function (url, what, path) {
    const r = await O.fetch(url, {headers: O.hdr});
    if (!r.ok) throw await O.fail(r, what, path);
    return r.json();
  };
  O.digest = async function (path) {
    const r = await O.fetch(O.api(path) + '/contextinfo', {method: 'POST', headers: O.hdr});
    if (!r.ok) throw await O.fail(r, 'contextinfo', path);
    return (await r.json()).FormDigestValue;
  };
  O.post = (url, digest, body, type) => O.fetch(url, {method: 'POST', body,
    headers: Object.assign({'X-RequestDigest': digest, 'Accept': 'application/json;odata=nometadata'}, type ? {'Content-Type': type} : {})});
  O.folderUrl = path => O.api(path) + "/web/GetFolderByServerRelativePath(decodedurl='" + O.lit(path) + "')";
  O.fileUrl = path => O.api(path) + "/web/GetFileByServerRelativePath(decodedurl='" + O.lit(path) + "')";
  // A folder's metadata, or null. SharePoint answers a missing folder 200 with Exists:false.
  O.folder = async function (path) {
    const r = await O.fetch(O.folderUrl(path), {headers: O.hdr});
    if (!r.ok) {
      const e = await O.fail(r, 'stat', path);
      if (O.isCode(e, 'ENOENT')) return null;
      throw e;
    }
    const d = await r.json();
    return d.Exists === false ? null : d;
  };
  O.file = async function (path) {
    const r = await O.fetch(O.fileUrl(path), {headers: O.hdr});
    if (r.ok) return r.json();
    const e = await O.fail(r, 'stat', path);
    if (O.isCode(e, 'ENOENT')) return null;
    throw e;
  };
  // Personal OneDrive lives on the tenant's -my host, team sites on the plain one.
  O.origin = function (path) {
    const tenant = location.origin.replace(/-my\.sharepoint\.com$/i, '.sharepoint.com');
    return (O.segs(path)[0] || '').toLowerCase() === 'personal' ? tenant.replace(/\.sharepoint\.com$/i, '-my.sharepoint.com') : tenant;
  };
  O.webUrl = function (path, isDir) {
    const enc = p => p.split('/').map(encodeURIComponent).join('/');
    if (!isDir) return O.origin(path) + enc(path) + '?web=1';
    const lib = '/' + O.segs(path).slice(0, O.libDepth(path)).join('/');
    return O.origin(path) + enc(lib) + '/Forms/AllItems.aspx?id=' + encodeURIComponent(path);
  };
  O.entry = (d, isDir) => ({name: d.Name || '', path: d.ServerRelativeUrl || '', is_dir: isDir,
    size: isDir ? 0 : Number(d.Length || 0) || 0, mime: '', modified: d.TimeLastModified || '',
    id: d.UniqueId || '', web_url: O.webUrl(d.ServerRelativeUrl || '', isDir)});

  // The signed-in person's OneDrive documents folder, for a bucket with no root.
  O.home = async function () {
    const j = await O.getJson(location.origin + '/_api/SP.UserProfiles.PeopleManager/GetMyProperties', 'home', '/');
    if (!j.PersonalUrl) throw O.err('ENOENT', 'the profile names no personal OneDrive');
    return decodeURIComponent(new URL(j.PersonalUrl).pathname).replace(/\/$/, '') + '/Documents';
  };
  O.ls = async function (path) {
    if (!(await O.folder(path))) {
      if (await O.file(path)) throw O.err('ENOTDIR', path + ' is a file');
      throw O.err('ENOENT', path + ': not found');
    }
    const fj = await O.getJson(O.folderUrl(path) + '/Folders', 'ls', path);
    const xj = await O.getJson(O.folderUrl(path) + '/Files', 'ls', path);
    return (fj.value || []).filter(d => d.Name !== 'Forms').map(d => O.entry(d, true))
      .concat((xj.value || []).map(f => O.entry(f, false)));
  };
  O.stat = async function (path) {
    const f = await O.file(path);
    if (f) return O.entry(f, false);
    const d = await O.folder(path);
    if (d) return O.entry(d, true);
    throw O.err('ENOENT', path + ': not found');
  };
  O.get = async function (path, maxBytes) {
    const f = await O.file(path);
    if (!f) {
      if (await O.folder(path)) throw O.err('EISDIR', path + ' is a folder');
      throw O.err('ENOENT', path + ': not found');
    }
    if (Number(f.Length || 0) > maxBytes)
      throw O.err('EINVAL', path + ' is ' + f.Length + ' bytes; the browser path carries ' + maxBytes + ' at most. Open ' + O.webUrl(path, false) + ' instead');
    const r = await O.fetch(O.fileUrl(path) + '/$value', {});
    if (!r.ok) throw await O.fail(r, 'get', path);
    return {filename: f.Name || O.segs(path).pop() || 'file', mime: r.headers.get('Content-Type') || '', b64: O.b64(await r.arrayBuffer())};
  };
  O.mkdir = async function (path) {
    const segs = O.segs(path);
    const lib = O.libDepth(path);
    if (segs.length < lib) throw O.err('EINVAL', path + ' is above a document library');
    let cur = '/' + segs.slice(0, lib).join('/');
    let d = await O.folder(cur);
    if (!d) throw O.err('ENOENT', cur + ': no such document library');
    let digest = null;
    for (let i = lib; i < segs.length; i++) {
      cur += '/' + segs[i];
      d = await O.folder(cur);
      if (d) continue;
      if (await O.file(cur)) throw O.err('ENOTDIR', cur + ' is a file');
      digest = digest || await O.digest(path);
      const r = await O.post(O.api(path) + "/web/folders/AddUsingPath(decodedurl='" + O.lit(cur) + "')", digest);
      d = r.ok ? await r.json() : await O.folder(cur);
      if (!d) throw await O.fail(r, 'mkdir', cur);
    }
    return O.entry(d, true);
  };
  O.put = async function (path, b64, parents) {
    const name = O.segs(path).pop();
    const parent = O.parent(path);
    if (parents) await O.mkdir(parent);
    const digest = await O.digest(path);
    const r = await O.post(O.folderUrl(parent) + "/Files/add(url='" + O.lit(name) + "',overwrite=true)", digest, O.unb64(b64));
    if (r.ok) return O.entry(await r.json(), false);
    const e = await O.fail(r, 'put', path);
    if (O.isCode(e, 'ENOENT') && !(await O.folder(parent)))
      throw O.err('ENOENT', 'parent folder ' + parent + ' does not exist (pass parents to create it)');
    throw e;
  };
  // To the recycle bin, never a hard delete.
  O.rm = async function (path) {
    if (O.segs(path).length <= O.libDepth(path)) throw O.err('EINVAL', 'refusing to remove a site or document library: ' + path);
    const digest = await O.digest(path);
    let r = await O.post(O.fileUrl(path) + '/recycle()', digest);
    if (r.ok) return {};
    const e = await O.fail(r, 'rm', path);
    if (!O.isCode(e, 'ENOENT') || !(await O.folder(path))) throw e;
    r = await O.post(O.folderUrl(path) + '/recycle()', digest);
    if (!r.ok) throw await O.fail(r, 'rm', path);
    return {};
  };
  // Move or copy within one site collection; the caller has cleared the target.
  O.transfer = async function (src, dst, isDir, copy) {
    if (O.origin(src) !== O.origin(dst) || O.webBase(src).toLowerCase() !== O.webBase(dst).toLowerCase())
      throw O.err('EINVAL', 'mv and cp stay inside one site: ' + src + ' -> ' + dst);
    const op = (copy ? 'Copy' : 'Move') + (isDir ? 'Folder' : 'File') + 'ByPath';
    const res = p => ({__metadata: {type: 'SP.ResourcePath'}, DecodedUrl: O.origin(p) + p});
    const options = {__metadata: {type: 'SP.MoveCopyOptions'}, KeepBoth: false, ResetAuthorAndCreatedOnCopy: false, ShouldBypassSharedLocks: true};
    if (!copy) options.RetainEditorAndModifiedOnMove = true;
    const url = O.api(src) + '/SP.MoveCopyUtil.' + op + (isDir ? '()' : '(overwrite=@a1)?@a1=false');
    const r = await O.post(url, await O.digest(src), JSON.stringify({srcPath: res(src), destPath: res(dst), options}), 'application/json;odata=verbose');
    if (!r.ok) throw await O.fail(r, copy ? 'cp' : 'mv', src + ' -> ' + dst);
    return {};
  };
  O.search = async function (query, limit, root) {
    const q = String(query || '') + ' Path:"' + O.origin(root) + root + '*"';
    const url = O.api(root) + "/search/query?querytext='" + encodeURIComponent(q.replace(/'/g, "''")) + "'&rowlimit=" + limit +
      "&selectproperties='Title,Path,FileExtension,Size,LastModifiedTime,UniqueId,IsContainer'";
    const j = await O.getJson(url, 'search', root);
    const rows = (((j.PrimaryQueryResult || {}).RelevantResults || {}).Table || {}).Rows || [];
    return rows.map(row => {
      const c = {};
      for (const cell of (row.Cells || [])) c[cell.Key] = cell.Value;
      let path = c.Path || '';
      try { path = decodeURIComponent(new URL(path).pathname); } catch (e) { /* keep the raw Path */ }
      const isDir = String(c.IsContainer).toLowerCase() === 'true';
      return {name: c.Title || O.segs(path).pop() || '', path, is_dir: isDir, size: Number(c.Size || 0) || 0,
        mime: isDir ? '' : (c.FileExtension || ''), modified: c.LastModifiedTime || '', id: c.UniqueId || '', web_url: c.Path || ''};
    });
  };
  window.__awmDrive = O;
}
