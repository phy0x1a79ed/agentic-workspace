// Installs window.__awmChat in a signed-in Slack, Teams or Discord tab. Every request rides the
// page's own session: Slack's xoxc token, Teams' MSAL token, Discord's webpack-held token.
// Nothing here sends a message.
window.__awmChat = window.__awmChat || {};
if (window.__awmChat._v !== 1) {
  const C = {_v: 1};

  C.b64 = function (buf) {
    const bytes = new Uint8Array(buf);
    let bin = '';
    for (let i = 0; i < bytes.length; i += 0x8000)
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(bin);
  };
  C.fetchFile = async function (url, init) {
    const r = await fetch(url, init || {credentials: 'include'});
    if (!r.ok) throw new Error('GET ' + url.split('?')[0] + ': ' + r.status);
    return C.b64(await r.arrayBuffer());
  };
  C.waitFor = async function (selector, ms) {
    const end = Date.now() + ms;
    for (;;) {
      const el = document.querySelector(selector);
      if (el) return el;
      if (Date.now() > end) throw new Error('no ' + selector + ' on the page after ' + ms / 1000 + 's');
      await new Promise(r => setTimeout(r, 250));
    }
  };
  // Put the files into the open chat's compose box, as a paste or a drop. Never presses send.
  C.stage = async function (site, files) {
    const s = C[site];
    const box = await C.waitFor(s.composer, 30000);
    const dt = new DataTransfer();
    for (const f of files) {
      const bin = atob(f.b64);
      const u = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i);
      dt.items.add(new File([u], f.name, {type: f.mime || 'application/octet-stream'}));
    }
    box.focus();
    if (s.how === 'drop') {
      for (const type of ['dragenter', 'dragover', 'drop'])
        box.dispatchEvent(new DragEvent(type, {dataTransfer: dt, bubbles: true, cancelable: true}));
    } else {
      box.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true}));
    }
    await new Promise(r => setTimeout(r, 2000));
    return {via: s.how, files: files.length};
  };

  // -- Slack: same-origin /api calls with the xoxc token from localConfig_v2 ----------------
  const S = {how: 'paste', composer: '[data-qa="message_input"] [contenteditable="true"], .ql-editor[contenteditable="true"]'};
  S.token = function () {
    let lc;
    try { lc = JSON.parse(localStorage.getItem('localConfig_v2')); } catch (e) { lc = null; }
    const teams = (lc && lc.teams) || {};
    const m = location.pathname.match(/\/client\/([^/]+)/);
    const mine = m && teams[m[1]];
    if (mine && (mine.token || '').startsWith('xoxc-')) return {team: m[1], token: mine.token};
    for (const k in teams)
      if ((teams[k].token || '').startsWith('xoxc-')) return {team: k, token: teams[k].token};
    throw new Error('no xoxc token in localConfig_v2; is Slack signed in?');
  };
  S.call = async function (method, params) {
    const fd = new FormData();
    fd.append('token', S.token().token);
    for (const k in (params || {})) fd.append(k, params[k]);
    const r = await fetch('/api/' + method, {method: 'POST', body: fd, credentials: 'include'});
    const j = await r.json();
    if (!j.ok) throw new Error('slack ' + method + ': ' + (j.error || r.status));
    return j;
  };
  S.paged = async function (method, params, key, pages) {
    let out = [], cursor = '';
    for (let i = 0; i < pages; i++) {
      const j = await S.call(method, Object.assign({limit: '200'}, params, cursor ? {cursor} : {}));
      out = out.concat(j[key] || []);
      cursor = (j.response_metadata || {}).next_cursor || '';
      if (!cursor) break;
    }
    return out;
  };
  S.people = async function () {
    if (!S._people) {
      S._people = {};
      for (const u of await S.paged('users.list', {}, 'members', 10))
        S._people[u.id] = (u.profile || {}).display_name || u.real_name || u.name || u.id;
    }
    return S._people;
  };
  S.list = async function () {
    const who = await S.people();
    const chans = await S.paged('users.conversations', {types: 'im,mpim,private_channel,public_channel', exclude_archived: 'true'}, 'channels', 5);
    return chans.map(c => ({id: c.id, name: c.is_im ? (who[c.user] || c.user) : (c.name || ''),
      kind: c.is_im ? 'dm' : c.is_mpim ? 'group' : c.is_private ? 'private' : 'channel'}));
  };
  S.messages = async function (id, limit) {
    const who = await S.people();
    const j = await S.call('conversations.history', {channel: id, limit: String(limit)});
    return (j.messages || []).filter(m => !m.subtype || m.subtype === 'file_share').reverse().map(m => ({
      id: m.ts, from: who[m.user] || m.user || '', text: m.text || '', ts: new Date(parseFloat(m.ts) * 1000).toISOString(),
      thread: m.thread_ts || '',
      attachments: (m.files || []).map((f, i) => ({idx: i, filename: f.name || f.title || 'attachment-' + i, mime: f.mimetype || '', size: f.size || 0}))}));
  };
  S.download = async function (id, ts, idx) {
    const j = await S.call('conversations.history', {channel: id, latest: ts, oldest: ts, inclusive: 'true', limit: '1'});
    const m = (j.messages || [])[0];
    if (!m) throw new Error('slack message ' + ts + ' not found');
    const out = [];
    for (const [i, f] of (m.files || []).entries()) {
      if (idx != null && i !== idx) continue;
      const url = f.url_private_download || f.url_private;
      if (url) out.push({filename: f.name || f.title || 'attachment-' + i, mime: f.mimetype || '', b64: await C.fetchFile(url)});
    }
    return out;
  };
  S.url = async function (id) {
    return 'https://app.slack.com/client/' + S.token().team + '/' + id;
  };
  C.slack = S;

  // -- Teams: the chat service, with a skype token minted from the MSAL api.spaces token ----
  const T = {how: 'drop', composer: '[data-tid="ckeditor"][contenteditable="true"], [role="textbox"][contenteditable="true"]'};
  T.msal = function (audience) {
    for (const store of [localStorage, sessionStorage]) {
      for (let i = 0; i < store.length; i++) {
        const k = store.key(i);
        if (!/accesstoken/i.test(k)) continue;
        let o;
        try { o = JSON.parse(store.getItem(k)); } catch (e) { continue; }
        if (o.credentialType === 'AccessToken' && audience.test(o.target || '')) return o;
      }
    }
    throw new Error('no ' + audience.source + ' token in the MSAL cache; is Teams signed in?');
  };
  T.auth = async function (force) {
    const now = Date.now() / 1000;
    if (!force && T._auth && T._auth.exp - 120 > now) return T._auth;
    const sp = T.msal(/api\.spaces\.skype\.com/);
    const r = await fetch(location.origin + '/api/authsvc/v1.0/authz', {method: 'POST', headers: {Authorization: 'Bearer ' + sp.secret}});
    if (!r.ok) throw new Error('teams authz: ' + r.status);
    const az = await r.json();
    const tok = az.tokens || {};
    const base = (az.regionGtms || {}).chatService;
    if (!tok.skypeToken || !base) throw new Error('teams authz gave no skypeToken or chatService');
    T._auth = {skype: tok.skypeToken, base, me: (sp.homeAccountId || '').split('.')[0], exp: now + (tok.expiresIn || 3600)};
    return T._auth;
  };
  T.get = async function (path) {
    let a = await T.auth(false);
    let r = await fetch(a.base + path, {headers: {Authentication: 'skypetoken=' + a.skype}});
    if (r.status === 401 || r.status === 403) {
      a = await T.auth(true);
      r = await fetch(a.base + path, {headers: {Authentication: 'skypetoken=' + a.skype}});
    }
    if (!r.ok) throw new Error('teams ' + path.split('?')[0] + ': ' + r.status);
    return r.json();
  };
  T.text = function (html) {
    const d = document.createElement('div');
    d.innerHTML = html || '';
    return d.textContent || '';
  };
  T.files = function (m) {
    let files = [];
    try { files = JSON.parse((m.properties || {}).files || '[]'); } catch (e) { files = []; }
    return files;
  };
  T.raw = async function (id, limit) {
    const b = await T.get('/v1/users/ME/conversations/' + encodeURIComponent(id) + '/messages?pageSize=' + limit);
    return (b.messages || []).filter(m => m.messagetype === 'Text' || m.messagetype === 'RichText/Html');
  };
  T.list = async function () {
    const a = await T.auth(false);
    const b = await T.get('/v1/users/ME/conversations?view=msnp24Equivalent&pageSize=200&startTime=1');
    // 48:notes is Tony's chat with himself.
    return (b.conversations || []).filter(c => (c.id || '').startsWith('19:') || c.id === '48:notes').map(c => {
      const id = c.id, last = c.lastMessage || {};
      if (id === '48:notes') return {id, kind: 'dm', name: 'Tony (you)'};
      const kind = id.includes('@thread.tacv2') ? 'channel' : id.includes('meeting_') ? 'meeting' : id.includes('@unq.gbl.spaces') ? 'dm' : 'group';
      const theirs = last.from && a.me && !last.from.includes(a.me) ? last.imdisplayname : '';
      return {id, kind, name: (c.threadProperties || {}).topic || theirs || ''};
    });
  };
  T.messages = async function (id, limit) {
    return (await T.raw(id, limit)).reverse().map(m => ({
      id: m.id, from: m.imdisplayname || m.from || '', text: T.text(m.content), ts: m.composetime || m.originalarrivaltime || '', thread: '',
      attachments: T.files(m).map((f, i) => ({idx: i, filename: f.title || f.fileName || 'attachment-' + i, mime: '', size: f.fileSize || 0}))}));
  };
  T.download = async function (id, messageId, idx) {
    const m = (await T.raw(id, 50)).find(m => m.id === String(messageId));
    if (!m) throw new Error('teams message ' + messageId + ' is not among the chat\'s last 50');
    const out = [];
    for (const [i, f] of T.files(m).entries()) {
      if (idx != null && i !== idx) continue;
      const url = f.objectUrl || f.fileUrl;
      if (url) out.push({filename: f.title || f.fileName || 'attachment-' + i, mime: '', b64: await C.fetchFile(url)});
    }
    return out;
  };
  T.url = async function (id) {
    return location.origin + '/l/chat/' + encodeURIComponent(id) + '/0';
  };
  C.teams = T;

  // -- Discord: its REST API with the token its webpack modules hold ------------------------
  const D = {how: 'paste', composer: '[role="textbox"][data-slate-editor="true"]'};
  D.token = function () {
    if (D._token) return D._token;
    const chunks = window.webpackChunkdiscord_app;
    if (chunks) {
      chunks.push([[Symbol()], {}, req => {
        for (const m of Object.values(req.c || {})) {
          for (const e of Object.values((m && m.exports) || {})) {
            try {
              const t = e && typeof e.getToken === 'function' && e.getToken();
              if (typeof t === 'string' && t) D._token = t;
            } catch (err) { /* a module that throws is not the token store */ }
          }
        }
      }]);
      chunks.pop();
    }
    if (!D._token) {
      // Discord hides window.localStorage; a fresh iframe's copy still reads the saved token.
      try {
        const f = document.documentElement.appendChild(document.createElement('iframe'));
        const t = f.contentWindow.localStorage.getItem('token');
        f.remove();
        if (t) D._token = JSON.parse(t);
      } catch (e) { /* no token saved */ }
    }
    if (!D._token) throw new Error('no Discord token in the page; is Discord signed in?');
    return D._token;
  };
  D.get = async function (path) {
    const r = await fetch('/api/v9' + path, {headers: {Authorization: D.token()}});
    if (!r.ok) throw new Error('discord ' + path.split('?')[0] + ': ' + r.status);
    return r.json();
  };
  D.person = u => (u && (u.global_name || u.username)) || '';
  D.list = async function () {
    return (await D.get('/users/@me/channels')).map(c => ({id: c.id, kind: c.type === 1 ? 'dm' : 'group',
      name: c.name || (c.recipients || []).map(D.person).join(', ')}));
  };
  D.messages = async function (id, limit) {
    return (await D.get('/channels/' + id + '/messages?limit=' + Math.min(limit, 100))).reverse().map(m => ({
      id: m.id, from: D.person(m.author), text: m.content || '', ts: m.timestamp || '', thread: '',
      attachments: (m.attachments || []).map((a, i) => ({idx: i, filename: a.filename, mime: a.content_type || '', size: a.size || 0}))}));
  };
  D.download = async function (id, messageId, idx) {
    const m = (await D.get('/channels/' + id + '/messages?around=' + messageId + '&limit=1'))[0];
    if (!m || m.id !== String(messageId)) throw new Error('discord message ' + messageId + ' not found');
    const out = [];
    for (const [i, a] of (m.attachments || []).entries())
      if (idx == null || i === idx) out.push({filename: a.filename, mime: a.content_type || '', b64: await C.fetchFile(a.url, {})});
    return out;
  };
  D.url = async id => 'https://discord.com/channels/@me/' + id;
  C.discord = D;

  window.__awmChat = C;
}
