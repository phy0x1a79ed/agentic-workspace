"""Per-platform drivers that operate a logged-in web session via in-page fetch.

Each driver runs JavaScript in its Opera page (through :class:`CDPPage`) so every
request rides the live session's own cookies + tokens — the daemon never
reconstructs credentials. A small idempotent *prelude* installs the platform's
helpers on ``window.__awmMira`` (survives until the page reloads, and is
re-asserted on every call so a reload self-heals); op methods then call those
helpers and normalise the result.

Normalised message dict (what the server returns + pushes):
    platform, channel_id, channel_name, message_id, thread_id,
    sender_id, sender_name, text, ts, marker

``marker`` is a per-conversation high-water key (Slack ``ts`` / Teams message
``id``); both are fixed-width so plain string comparison is monotonic.

Discovered live on mira (see the daemon README "How the drivers work"):
  * Slack — xoxc token from ``localConfig_v2``, same-origin ``fetch('/api/<m>')``
    on the ``app.slack.com`` tab (the ``d`` cookie rides automatically).
  * Teams — bootstrap ``POST /api/authsvc/v1.0/authz`` with the cached
    ``api.spaces.skype.com`` token → ``{skypeToken, regionGtms.chatService}``;
    all ops hit ``{chatService}/v1/users/ME/conversations…`` with the header
    ``Authentication: skypetoken=<tok>``.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Any

from mira_api.cdp import CDPPage

log = logging.getLogger("mira_api.drivers")


def _call(helper: str, *args: Any) -> str:
    """Build a JS expression invoking a window.__awmMira helper with JSON args."""
    arglist = ", ".join(json.dumps(a) for a in args)
    return f"(async()=>{{ return JSON.stringify(await {helper}({arglist})); }})()"


class Driver(ABC):
    platform: str = "base"
    #: substring identifying this platform's Opera page target
    target_needle: str = ""
    #: idempotent JS installing window.__awmMira.<platform>
    PRELUDE: str = ""

    def __init__(self, page: CDPPage) -> None:
        self.page = page

    async def _run(self, helper: str, *args: Any) -> Any:
        """Assert the prelude, then call ``helper(*args)`` and parse its JSON."""
        expr = self.PRELUDE + "\n" + _call(helper, *args)
        return await self.page.evaluate_json(expr)

    @abstractmethod
    async def identity(self) -> dict: ...

    @abstractmethod
    async def list_channels(self) -> list[dict]: ...

    @abstractmethod
    async def list_conversations(self) -> list[dict]:
        """Conversations to watch for inbound — ``[{id, name, kind}]``."""

    @abstractmethod
    async def fetch_messages(self, channel: str, oldest: str | None,
                             limit: int, *, before: str | None = None) -> list[dict]:
        """Normalised messages for ``channel``, newest-first from the platform.

        ``oldest`` is the forward lower bound the inbound watcher uses for deltas
        (messages *after* this marker). ``before`` is the backward upper bound for
        on-demand history paging (messages *older than* this marker); platforms
        without a usable cursor (Teams) ignore it.
        """

    @abstractmethod
    async def send(self, channel: str, text: str,
                   thread: str | None = None) -> dict: ...


# --------------------------------------------------------------------------- #
# Slack                                                                        #
# --------------------------------------------------------------------------- #

SLACK_PRELUDE = r"""
window.__awmMira = window.__awmMira || {};
if (!window.__awmMira.slack) {
  const S = {};
  S._token = function () {
    let lc;
    try { lc = JSON.parse(localStorage.getItem('localConfig_v2')); }
    catch (e) { return null; }
    const teams = (lc && lc.teams) || {};
    const m = location.pathname.match(/\/client\/([^/]+)/);
    const tid = m && m[1];
    if (tid && teams[tid] && (teams[tid].token || '').startsWith('xoxc-'))
      return teams[tid].token;
    for (const k in teams)
      if ((teams[k].token || '').startsWith('xoxc-')) return teams[k].token;
    return null;
  };
  S._call = async function (method, params) {
    const tok = S._token();
    if (!tok) throw new Error('no xoxc token in localConfig_v2');
    const fd = new FormData();
    fd.append('token', tok);
    for (const k in (params || {})) fd.append(k, params[k]);
    const r = await fetch('/api/' + method, {method: 'POST', body: fd, credentials: 'include'});
    const j = await r.json();
    if (!j.ok) throw new Error('slack ' + method + ': ' + (j.error || r.status));
    return j;
  };
  S.identity = async function () {
    const j = await S._call('auth.test', {});
    return {id: j.user_id, name: j.user, team: j.team, url: j.url};
  };
  S.listChannels = async function () {
    const j = await S._call('conversations.list',
      {types: 'public_channel,private_channel', exclude_archived: 'true', limit: '500'});
    return (j.channels || []).map(c => ({id: c.id, name: c.name || '',
      kind: c.is_private ? 'private' : 'channel'}));
  };
  S.listConversations = async function () {
    const j = await S._call('users.conversations',
      {types: 'im,mpim,private_channel,public_channel', exclude_archived: 'true', limit: '500'});
    return (j.channels || []).map(c => ({id: c.id,
      name: c.name || (c.user ? ('dm:' + c.user) : ''),
      kind: c.is_im ? 'dm' : (c.is_mpim ? 'group' : (c.is_private ? 'private' : 'channel'))}));
  };
  S.history = async function (channel, oldest, limit, latest) {
    const p = {channel: channel, limit: String(limit || 50)};
    if (oldest) p.oldest = oldest;   // forward bound (after this ts)
    if (latest) p.latest = latest;   // backward bound (before this ts)
    const j = await S._call('conversations.history', p);
    return (j.messages || []).map(m => ({ts: m.ts, user: m.user || '',
      text: m.text || '', subtype: m.subtype || '', thread_ts: m.thread_ts || ''}));
  };
  S.send = async function (channel, text, thread) {
    const p = {channel: channel, text: text};
    if (thread) p.thread_ts = thread;
    const j = await S._call('chat.postMessage', p);
    return {message_id: j.ts, channel_id: j.channel || channel, ts: j.ts};
  };
  window.__awmMira.slack = S;
}
"""


class SlackDriver(Driver):
    platform = "slack"
    target_needle = "app.slack.com"
    PRELUDE = SLACK_PRELUDE

    async def identity(self) -> dict:
        return await self._run("window.__awmMira.slack.identity")

    async def list_channels(self) -> list[dict]:
        chans = await self._run("window.__awmMira.slack.listChannels")
        return chans or []

    async def list_conversations(self) -> list[dict]:
        convs = await self._run("window.__awmMira.slack.listConversations")
        return convs or []

    async def fetch_messages(self, channel: str, oldest: str | None,
                             limit: int, *, before: str | None = None) -> list[dict]:
        rows = await self._run(
            "window.__awmMira.slack.history", channel, oldest, limit, before)
        out = []
        for m in rows or []:
            if m.get("subtype"):  # joins/edits/system/bot echoes
                continue
            ts = m.get("ts", "")
            out.append({
                "platform": "slack",
                "channel_id": channel,
                "channel_name": "",
                "message_id": ts,
                "thread_id": m.get("thread_ts", "") or "",
                "sender_id": m.get("user", "") or "",
                "sender_name": m.get("user", "") or "",
                "text": m.get("text", "") or "",
                "ts": ts,
                "marker": ts,
            })
        return out

    async def send(self, channel: str, text: str,
                   thread: str | None = None) -> dict:
        return await self._run("window.__awmMira.slack.send", channel, text, thread)


# --------------------------------------------------------------------------- #
# Teams                                                                        #
# --------------------------------------------------------------------------- #

TEAMS_PRELUDE = r"""
window.__awmMira = window.__awmMira || {};
if (!window.__awmMira.teams) {
  const T = {};
  T._auth = null;  // {skype, base, exp}
  T._spaces = function () {
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (!/accesstoken/i.test(k)) continue;
      let o; try { o = JSON.parse(localStorage.getItem(k)); } catch (e) { continue; }
      if (o.credentialType === 'AccessToken' &&
          /api\.spaces\.skype\.com/.test(o.target || '')) return o.secret;
    }
    return null;
  };
  T.ensureAuth = async function (force) {
    const now = Date.now() / 1000;
    if (!force && T._auth && T._auth.exp - 120 > now) return T._auth;
    const sp = T._spaces();
    if (!sp) throw new Error('no api.spaces.skype.com token in MSAL cache');
    const r = await fetch('https://teams.microsoft.com/api/authsvc/v1.0/authz',
      {method: 'POST', headers: {Authorization: 'Bearer ' + sp}});
    if (!r.ok) throw new Error('authz ' + r.status);
    const az = await r.json();
    const skype = (az.tokens || {}).skypeToken;
    const base = (az.regionGtms || {}).chatService;
    const ttl = (az.tokens || {}).expiresIn || 3600;
    if (!skype || !base) throw new Error('authz missing skypeToken/chatService');
    T._auth = {skype: skype, base: base, exp: now + ttl};
    return T._auth;
  };
  T._req = async function (path, opts) {
    let a = await T.ensureAuth(false);
    const mk = (a) => Object.assign({'Authentication': 'skypetoken=' + a.skype},
      (opts && opts.headers) || {});
    let r = await fetch(a.base + path, Object.assign({}, opts, {headers: mk(a)}));
    if (r.status === 401 || r.status === 403) {
      a = await T.ensureAuth(true);
      r = await fetch(a.base + path, Object.assign({}, opts, {headers: mk(a)}));
    }
    return r;
  };
  T._strip = function (html) {
    if (!html) return '';
    let s = String(html).replace(/<[^>]+>/g, '');
    const e = {'&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"',
               '&#39;': "'", '&nbsp;': ' '};
    return s.replace(/&amp;|&lt;|&gt;|&quot;|&#39;|&nbsp;/g, m => e[m]);
  };
  T._isReal = function (m) {
    const t = m.messagetype || '';
    return t === 'Text' || t === 'RichText/Html';
  };
  T.identity = async function () {
    // Graph /me (the page's Graph token authorises /me) gives the stable oid.
    let g = null;
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (!/accesstoken/i.test(k)) continue;
      let o; try { o = JSON.parse(localStorage.getItem(k)); } catch (e) { continue; }
      if (o.credentialType === 'AccessToken' &&
          /graph\.microsoft\.com/.test(o.target || '') &&
          /(openid|profile|User\.Read)/.test(o.target || '')) { g = o.secret; break; }
    }
    if (g) {
      const r = await fetch('https://graph.microsoft.com/v1.0/me',
        {headers: {Authorization: 'Bearer ' + g}});
      if (r.ok) { const b = await r.json();
        return {id: '8:orgid:' + b.id, name: b.displayName || b.userPrincipalName || '',
                raw_id: b.id}; }
    }
    return {id: '', name: '', raw_id: ''};
  };
  T._mapConv = function (c) {
    const id = c.id || '';
    let kind = 'chat';
    if (id.indexOf('@thread.tacv2') >= 0) kind = 'channel';
    else if (id.indexOf('meeting_') >= 0) kind = 'meeting';
    else if (/_.*@unq\.gbl\.spaces/.test(id)) kind = 'dm';
    return {id: id, type: c.type || '',
      name: (c.threadProperties && c.threadProperties.topic) || '', kind: kind};
  };
  T.listConversations = async function () {
    const r = await T._req(
      '/v1/users/ME/conversations?view=msnp24Equivalent&pageSize=200&startTime=1', {});
    if (!r.ok) throw new Error('list ' + r.status);
    const b = await r.json();
    return (b.conversations || [])
      .filter(c => (c.id || '').indexOf('19:') === 0)  // drop 48:notifications/calllogs
      .map(T._mapConv);
  };
  T.listChannels = T.listConversations;
  T._mapMsg = function (m, channel) {
    return {message_id: m.id || '', marker: m.id || '',
      channel_id: channel, channel_name: '',
      thread_id: '', sender_id: m.from || '', sender_name: m.imdisplayname || '',
      text: T._strip(m.content || ''), ts: m.composetime || m.originalarrivaltime || '',
      messagetype: m.messagetype || ''};
  };
  T.messages = async function (channel, limit) {
    const r = await T._req('/v1/users/ME/conversations/' +
      encodeURIComponent(channel) + '/messages?pageSize=' + (limit || 20), {});
    if (!r.ok) throw new Error('messages ' + r.status);
    const b = await r.json();
    return (b.messages || []).filter(T._isReal).map(m => T._mapMsg(m, channel));
  };
  T.send = async function (channel, text, thread) {
    const body = {content: text, messagetype: 'RichText/Html', contenttype: 'text',
      clientmessageid: String(Date.now()) + String(Math.floor(Math.random() * 1e6))};
    const r = await T._req('/v1/users/ME/conversations/' +
      encodeURIComponent(channel) + '/messages',
      {method: 'POST', headers: {'Content-Type': 'application/json'},
       body: JSON.stringify(body)});
    let j = null; try { j = await r.json(); } catch (e) {}
    if (!r.ok) throw new Error('send ' + r.status + ' ' + (j && j.message || ''));
    // ng.msg returns OriginalArrivalTime; the durable id is the clientmessageid
    // we minted (the server echoes it back on the inbound copy).
    return {message_id: (j && (j.id || j.OriginalArrivalTime)) || body.clientmessageid,
      channel_id: channel, ts: (j && j.OriginalArrivalTime) || '',
      clientmessageid: body.clientmessageid};
  };
  window.__awmMira.teams = T;
}
"""


class TeamsDriver(Driver):
    platform = "teams"
    target_needle = "teams.microsoft.com"
    PRELUDE = TEAMS_PRELUDE

    async def identity(self) -> dict:
        return await self._run("window.__awmMira.teams.identity")

    async def list_channels(self) -> list[dict]:
        convs = await self._run("window.__awmMira.teams.listChannels")
        return convs or []

    async def list_conversations(self) -> list[dict]:
        convs = await self._run("window.__awmMira.teams.listConversations")
        return convs or []

    async def fetch_messages(self, channel: str, oldest: str | None,
                             limit: int, *, before: str | None = None) -> list[dict]:
        # Teams' ng.msg /conversations/{id}/messages takes only pageSize — no
        # usable cursor — so both `oldest` and `before` are ignored (limit-only).
        rows = await self._run("window.__awmMira.teams.messages", channel, limit)
        out = []
        for m in rows or []:
            out.append({
                "platform": "teams",
                "channel_id": channel,
                "channel_name": m.get("channel_name", ""),
                "message_id": m.get("message_id", ""),
                "thread_id": m.get("thread_id", "") or "",
                "sender_id": m.get("sender_id", "") or "",
                "sender_name": m.get("sender_name", "") or "",
                "text": m.get("text", "") or "",
                "ts": m.get("ts", "") or "",
                "marker": m.get("marker", "") or "",
            })
        return out

    async def send(self, channel: str, text: str,
                   thread: str | None = None) -> dict:
        return await self._run("window.__awmMira.teams.send", channel, text, thread)


REGISTRY: dict[str, type[Driver]] = {
    "slack": SlackDriver,
    "teams": TeamsDriver,
}
