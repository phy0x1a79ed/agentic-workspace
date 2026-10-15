// In-memory stand-in for the slice of the SharePoint REST API the OneDrive
// prelude calls. Installed over window.fetch in a headless page, so the
// prelude's real JavaScript runs against it.
(function () {
  const F = {};
  F.reset = function (spec) {
    spec = spec || {};
    F.sites = spec.sites || ['/teams/x'];
    F.nodes = {};
    F.calls = [];
    F.auth = 'ok';         // 'ok' | 'redirect' | '403'
    F.range = 'value';     // who honours Range: 'value' ($value) | 'download' | 'none'
    F.uploads = {};        // upload id -> {path, data: Uint8Array}
    F.jobs = {};           // copy job id -> {src, destDir, move, polls}
    F.threshold = 5000;    // children past which /Folders and /Files refuse
    F.pageSize = 5000;     // rows per RenderListDataAsStream page
    F.seq = 0;
    F.links = {};          // path -> [{Url, ShareId, LinkKind}]
    F.grants = {};         // path -> [{email, role, id}]
    F.shareUrls = {};      // share url -> path, for the v2.0 shares API
    F.sharesApi = spec.sharesApi !== false;
    F.denyAnyone = !!spec.denyAnyone;
    F.users = spec.users || {'ann@x.ca': 7};   // email -> site user id
    F.libs = spec.libraries || ['/teams/x/Docs'];
    F.personalUrl = spec.personalUrl || 'https://t-my.sharepoint.com/personal/me_x/';
    F.searchSites = spec.searchSites || [];   // [{Title, Path}]
    F.followed = spec.followed || [];         // [{Name, Uri}]
    for (const lib of F.libs) F.addFolder(lib);
    for (const [p, text] of Object.entries(spec.files || {})) F.addFile(p, text);
    for (const p of (spec.folders || [])) F.addFolder(p);
    for (const [p, uid] of Object.entries(spec.uids || {})) F.nodes[p].uid = uid;
  };
  F.uid = function () { F.seq += 1; return 'uid-' + F.seq; };
  F.addFolder = function (p) {
    const segs = p.split('/').filter(Boolean);
    F.nodes[p] = {kind: 'folder', name: segs[segs.length - 1], uid: F.uid(),
                  modified: '2026-01-01T00:00:00Z'};
  };
  F.addFile = function (p, data) {
    const segs = p.split('/').filter(Boolean);
    const bytes = typeof data === 'string' ? new TextEncoder().encode(data) : data;
    F.nodes[p] = {kind: 'file', name: segs[segs.length - 1], uid: F.uid(), data: bytes,
                  modified: '2026-01-01T00:00:00Z', etag: '"{' + F.uid() + '},1"'};
  };
  F.parent = function (p) { const s = p.split('/').filter(Boolean); s.pop(); return '/' + s.join('/'); };
  F.children = function (p) {
    return Object.keys(F.nodes).filter(k => F.parent(k) === p).sort();
  };
  F.folderJson = function (p) {
    const n = F.nodes[p];
    if (!n || n.kind !== 'folder') return {Exists: false};
    return {Exists: true, Name: n.name, ServerRelativeUrl: p, UniqueId: n.uid,
            TimeLastModified: n.modified, ItemCount: F.children(p).length};
  };
  F.fileJson = function (p) {
    const n = F.nodes[p];
    return {Name: n.name, ServerRelativeUrl: p, UniqueId: n.uid,
            Length: String(n.data.length), TimeLastModified: n.modified, ETag: n.etag};
  };
  F.lit = function (s) { return decodeURIComponent(s).replace(/''/g, "'"); };
  F.json = function (obj, status) {
    return new Response(JSON.stringify(obj), {status: status || 200,
      headers: {'Content-Type': 'application/json'}});
  };
  F.notFound = function (msg) {
    return F.json({'odata.error': {code: '-2130575338, Microsoft.SharePoint.SPException',
      message: {value: msg || 'The file does not exist.'}}}, 404);
  };
  F.concat = function (a, b) {
    const out = new Uint8Array(a.length + b.length);
    out.set(a, 0); out.set(b, a.length);
    return out;
  };
  F.bytes = function (body) { return body ? new Uint8Array(body) : new Uint8Array(0); };
  // Serve n.data, honouring a Range header only when allowed.
  F.serve = function (n, opts, honour) {
    const range = honour && opts.headers && opts.headers['Range'];
    const m = range && range.match(/^bytes=(\d+)-(\d+)$/);
    if (!m) return new Response(n.data, {status: 200,
      headers: {'Content-Type': 'application/octet-stream'}});
    const a = Number(m[1]), b = Math.min(Number(m[2]), n.data.length - 1);
    return new Response(n.data.slice(a, b + 1), {status: 206,
      headers: {'Content-Type': 'application/octet-stream'}});
  };
  F.site = function (p) {
    const s = p.split('/').filter(Boolean);
    return s.length >= 2 ? '/' + s[0] + '/' + s[1] : '';
  };
  F.strip = function (u) { return u.startsWith(location.origin) ? u.slice(location.origin.length) : u; };
  // Copy (or move) the subtree at src to dst. A copy gets fresh ids.
  F.relocate = function (src, dst, move) {
    for (const k of Object.keys(F.nodes).sort()) {
      if (k !== src && !k.startsWith(src + '/')) continue;
      const n = F.nodes[k], to = dst + k.slice(src.length);
      const segs = to.split('/').filter(Boolean);
      F.nodes[to] = Object.assign({}, n, {name: segs[segs.length - 1],
        uid: move ? n.uid : F.uid()});
      if (move) delete F.nodes[k];
    }
  };
  // The ListItemAllFields routes of the item at p.
  F.item = function (p, tail, method, body, opts) {
    if (tail === '/ListItemAllFields/ShareLink' && method === 'POST') {
      const kind = JSON.parse(body).request.settings.linkKind;
      if (F.denyAnyone && (kind === 4 || kind === 5))
        return F.json({'odata.error': {message: {value: 'Anonymous sharing is disabled'}}}, 400);
      const id = F.uid();
      const link = {Url: 'https://t.sharepoint.com/:f:/t/x/' + id, ShareId: id, LinkKind: kind};
      (F.links[p] = F.links[p] || []).push(link);
      F.shareUrls[link.Url] = p;
      return F.json({sharingLinkInfo: link});
    }
    if (tail === '/ListItemAllFields/GetSharingInformation?$expand=permissionsInformation' &&
        method === 'POST')
      return F.json({permissionsInformation: {
        links: [{linkDetails: {Url: '', LinkKind: 1}}].concat((F.links[p] || []).map(l =>
          ({linkDetails: {Url: l.Url, ShareId: l.ShareId, LinkKind: l.LinkKind,
                          IsEditLink: l.LinkKind % 2 === 1, Expiration: ''}}))),
        principals: (F.grants[p] || []).map(g => ({principal: {name: g.email, email: g.email,
          loginName: 'i:0#.f|membership|' + g.email, id: g.id}, role: g.role}))}});
    if (tail === '/ListItemAllFields/UnshareLink' && method === 'POST') {
      const b = JSON.parse(body);
      const before = (F.links[p] || []).length;
      F.links[p] = (F.links[p] || []).filter(l => !(l.ShareId === b.shareId && l.LinkKind === b.linkKind));
      return before === F.links[p].length ? F.json({error: 'no such link'}, 400) : F.json({});
    }
    const ra = tail.match(/^\/ListItemAllFields\/RoleAssignments\/GetByPrincipalId\((\d+)\)$/);
    if (ra && method === 'POST' && opts.headers['X-HTTP-Method'] === 'DELETE') {
      const before = (F.grants[p] || []).length;
      F.grants[p] = (F.grants[p] || []).filter(g => g.id !== Number(ra[1]));
      return before === F.grants[p].length ? F.notFound('no role assignment') : F.json({});
    }
    return null;
  };
  F.handle = async function (url, opts) {
    const method = (opts && opts.method) || 'GET';
    const body = opts && opts.body;
    const dl = url.indexOf('/_layouts/15/download.aspx?SourceUrl=');
    if (dl >= 0) {
      F.calls.push(method + ' download.aspx');
      const n = F.nodes[decodeURIComponent(url.slice(dl + 37))];
      if (!n || n.kind !== 'file') return F.notFound();
      return F.serve(n, opts, F.range === 'download');
    }
    const at = url.indexOf('/_api');
    const web = url.slice(location.origin.length, at);
    const rest = url.slice(at + 5);
    F.calls.push(method + ' ' + web + ' ' + rest);
    if (F.auth === 'redirect')
      return {ok: false, status: 0, type: 'opaqueredirect', text: async () => ''};
    if (F.auth === '403') return F.json({error: 'denied'}, 403);
    if (web && !F.sites.includes(decodeURIComponent(web))) return F.json({error: 'no site'}, 404);
    if (method === 'POST' && rest === '/contextinfo') return F.json({FormDigestValue: 'digest'});
    if (method === 'POST' && opts.headers['X-RequestDigest'] !== 'digest')
      return F.json({error: 'bad digest'}, 403);
    let m = rest.match(/^\/SP\.MoveCopyUtil\.(Move|Copy)(File|Folder)ByPath\((?:overwrite=@a1\)\?@a1=false|\))$/);
    if (m && method === 'POST') {
      const b = JSON.parse(body);
      if (opts.headers['Content-Type'] !== 'application/json;odata=verbose' ||
          b.srcPath.__metadata.type !== 'SP.ResourcePath' ||
          b.options.__metadata.type !== 'SP.MoveCopyOptions')
        return F.json({error: 'MoveCopyUtil wants a verbose, typed body'}, 400);
      const src = F.strip(b.srcPath.DecodedUrl), dst = F.strip(b.destPath.DecodedUrl);
      const n = F.nodes[src];
      if (!n || n.kind !== m[2].toLowerCase()) return F.notFound();
      if (F.site(src) !== F.site(dst)) return F.json({error: 'fake MoveCopyUtil stays in one site'}, 400);
      if (F.nodes[dst]) return F.json({error: 'target exists'}, 400);
      if (!F.nodes[F.parent(dst)]) return F.notFound('target folder missing');
      F.relocate(src, dst, m[1] === 'Move');
      return F.json({});
    }
    if (method === 'POST' && rest === '/site/CreateCopyJobs') {
      const b = JSON.parse(body);
      const src = F.strip(b.exportObjectUris[0]);
      if (!F.nodes[src]) return F.notFound();
      const id = 'job-' + F.uid();
      F.jobs[id] = {src: src, destDir: F.strip(b.destinationUri),
                    move: b.options.IsMoveMode, polls: 0};
      return F.json({value: [{JobId: id, JobQueueUri: 'q', EncryptionKey: 'k'}]});
    }
    if (method === 'POST' && rest === '/site/GetCopyJobProgress') {
      const job = F.jobs[JSON.parse(body).copyJobInfo.JobId];
      if (!job) return F.json({error: 'no such job'}, 400);
      job.polls += 1;
      if (job.polls < 2) return F.json({JobState: 4, Logs: []});
      const name = job.src.split('/').filter(Boolean).pop();
      const dst = job.destDir + '/' + name;
      if (F.nodes[dst] || !F.nodes[job.destDir])
        return F.json({JobState: 0, Logs: [JSON.stringify({Event: 'JobError',
          Message: 'cannot place ' + dst})]});
      F.relocate(job.src, dst, job.move);
      return F.json({JobState: 0, Logs: [JSON.stringify({Event: 'JobEnd'})]});
    }
    m = rest.match(/^\/web\/GetList\(@l\)\/RenderListDataAsStream\?@l='((?:[^']|'')*)'(?:&Paged=TRUE&p_ID=(\d+))?$/);
    if (m && method === 'POST') {
      const prm = JSON.parse(body).parameters;
      const folder = prm.FolderServerRelativeUrl;
      if (!F.nodes[F.lit(m[1])] || !F.nodes[folder]) return F.notFound();
      const kids = F.children(folder);
      const start = Number(m[2] || 0);
      const page = kids.slice(start, start + F.pageSize).map(k => {
        const n = F.nodes[k];
        return {FileLeafRef: n.name, FileRef: k, FSObjType: n.kind === 'folder' ? '1' : '0',
                File_x0020_Size: n.kind === 'file' ? String(n.data.length) : '',
                'Modified.': n.modified, UniqueId: '{' + n.uid + '}'};
      });
      const more = start + F.pageSize < kids.length;
      return F.json({Row: page, NextHref: more ? '?Paged=TRUE&p_ID=' + (start + F.pageSize) : undefined});
    }
    if (method === 'GET' && rest.startsWith('/SP.UserProfiles.PeopleManager/GetMyProperties'))
      return F.json({PersonalUrl: F.personalUrl});
    if (method === 'GET' && rest.startsWith("/search/query?querytext='contentclass:STS_Site'"))
      return F.json({PrimaryQueryResult: {RelevantResults: {Table: {Rows: F.searchSites.map(r =>
        ({Cells: [{Key: 'Title', Value: r.Title}, {Key: 'Path', Value: r.Path}]}))}}}});
    if (method === 'GET' && rest === '/social.following/my/followed(types=4)')
      return F.json({value: F.followed});
    if (method === 'GET' && rest.startsWith('/web/lists?$filter=')) {
      const site = decodeURIComponent(web);
      return F.json({value: F.libs.filter(l => F.site(l) === site).map(l => ({
        Title: l.split('/').pop(), ItemCount: F.children(l).length,
        LastItemModifiedDate: '2026-01-01T00:00:00Z', RootFolder: {ServerRelativeUrl: l}}))});
    }
    if (method === 'POST' && rest === '/SP.Web.ShareObject') {
      const b = JSON.parse(body);
      const p = F.strip(b.url);
      if (!F.nodes[p]) return F.json({StatusCode: -1, ErrorMessage: 'no such item'});
      const role = b.roleValue === 'role:1073741827' ? 2 : 1;
      for (const k of JSON.parse(b.peoplePickerInput)) {
        const email = k.Key.split('|').pop();
        if (!(email in F.users)) return F.json({StatusCode: -1, ErrorMessage: 'cannot resolve ' + email});
        (F.grants[p] = F.grants[p] || []).push({email: email, role: role, id: F.users[email]});
      }
      F.lastShareObject = b;
      return F.json({StatusCode: 0, Url: 'https://t.sharepoint.com' + p});
    }
    m = rest.match(/^\/web\/SiteUsers\/GetByEmail\('((?:[^']|'')*)'\)$/);
    if (m && method === 'GET') {
      const email = F.lit(m[1]);
      return email in F.users ? F.json({Id: F.users[email]}) : F.notFound('no such user');
    }
    m = rest.match(/^\/v2\.0\/shares\/u!([A-Za-z0-9_-]+)\/driveItem$/);
    if (m && method === 'GET') {
      let b64 = m[1].replace(/-/g, '+').replace(/_/g, '/');
      while (b64.length % 4) b64 += '=';
      const p = F.shareUrls[decodeURIComponent(escape(atob(b64)))];
      if (!F.sharesApi || !p) return F.json({error: {code: 'itemNotFound'}}, 404);
      return F.json({name: F.nodes[p].name, webUrl: 'https://t.sharepoint.com' +
        p.split('/').map(encodeURIComponent).join('/')});
    }
    m = rest.match(/^\/web\/Get(Folder|File)ById\('([0-9a-f-]+)'\)$/);
    if (m && method === 'GET') {
      const kind = m[1].toLowerCase();
      const hit = Object.keys(F.nodes).find(k => F.nodes[k].uid === m[2] && F.nodes[k].kind === kind &&
        F.site(k) === decodeURIComponent(web));
      if (!hit) return F.notFound();
      return F.json(kind === 'folder' ? F.folderJson(hit) : F.fileJson(hit));
    }
    m = rest.match(/^\/web\/folders\/AddUsingPath\(decodedurl='((?:[^']|'')*)'\)$/);
    if (m && method === 'POST') {
      const p = F.lit(m[1]);
      if (F.nodes[p]) return F.json({error: 'exists'}, 500);
      if (!F.nodes[F.parent(p)]) return F.notFound('parent missing');
      F.addFolder(p);
      return F.json(F.folderJson(p));
    }
    m = rest.match(/^\/web\/GetFolderByServerRelativePath\(decodedurl='((?:[^']|'')*)'\)(.*)$/);
    if (m) {
      const p = F.lit(m[1]), tail = m[2];
      const n = F.nodes[p];
      if (tail === '') return F.json(F.folderJson(p));
      if (!n || n.kind !== 'folder') return F.notFound('folder missing');
      const itf = F.item(p, tail, method, body, opts);
      if (itf) return itf;
      if ((tail === '/Folders' || tail === '/Files') && F.children(p).length > F.threshold)
        return F.json({'odata.error': {code: '-2147024860, Microsoft.SharePoint.SPQueryThrottledException',
          message: {value: 'The attempted operation is prohibited because it exceeds the list view threshold.'}}}, 500);
      if (tail === '/Folders')
        return F.json({value: F.children(p).filter(k => F.nodes[k].kind === 'folder').map(F.folderJson)});
      if (tail === '/Files')
        return F.json({value: F.children(p).filter(k => F.nodes[k].kind === 'file').map(F.fileJson)});
      if (tail === '/recycle()' && method === 'POST') {
        for (const k of Object.keys(F.nodes)) if (k === p || k.startsWith(p + '/')) delete F.nodes[k];
        return F.json({value: 'recycled'});
      }
      const add = tail.match(/^\/Files\/add\(url='((?:[^']|'')*)',overwrite=true\)$/);
      if (add && method === 'POST') {
        const fp = p + '/' + F.lit(add[1]);
        F.addFile(fp, new Uint8Array(body));
        return F.json(F.fileJson(fp));
      }
    }
    m = rest.match(/^\/web\/GetFileByServerRelativePath\(decodedurl='((?:[^']|'')*)'\)(.*)$/);
    if (m) {
      const p = F.lit(m[1]), tail = m[2];
      const n = F.nodes[p];
      if (!n || n.kind !== 'file') return F.notFound();
      if (tail === '') return F.json(F.fileJson(p));
      const iti = F.item(p, tail, method, body, opts);
      if (iti) return iti;
      if (tail === '/$value') return F.serve(n, opts, F.range === 'value');
      const up = tail.match(/^\/(StartUpload|ContinueUpload|FinishUpload|CancelUpload)\(uploadId=guid'([^']+)'(?:,fileOffset=(\d+))?\)$/);
      if (up && method === 'POST') {
        const [, op, id, off] = up;
        if (op === 'StartUpload') {
          F.uploads[id] = {path: p, data: F.bytes(body)};
          return F.json({value: String(F.uploads[id].data.length)});
        }
        const u = F.uploads[id];
        if (!u || u.path !== p) return F.json({error: 'no such upload session'}, 400);
        if (op === 'CancelUpload') { delete F.uploads[id]; return F.json({}); }
        if (Number(off) !== u.data.length)
          return F.json({error: 'offset ' + off + ' != ' + u.data.length}, 400);
        u.data = F.concat(u.data, F.bytes(body));
        if (op === 'ContinueUpload') return F.json({value: String(u.data.length)});
        delete F.uploads[id];
        n.data = u.data;
        n.etag = '"{' + F.uid() + '},2"';
        return F.json(F.fileJson(p));
      }
      if (tail === '/recycle()' && method === 'POST') { delete F.nodes[p]; return F.json({value: 'x'}); }
    }
    return F.json({error: 'fake SharePoint has no route for ' + method + ' ' + rest}, 400);
  };
  window.__fakeSP = F;
  window.fetch = (url, opts) => F.handle(String(url), opts || {});
  F.reset();
})();
