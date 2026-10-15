"""Slack bot connector — a first-class bot identity over plain HTTPS.

Acts as a Slack app's bot user (``xoxb-`` token), never as a person, so it needs
no browser and no ``slack_sdk``: every Web API call is one aiohttp POST to
``https://slack.com/api/<method>``. Receive is Socket Mode: an ``xapp-``
app-level token opens a websocket that pushes every message in every chat the
bot is a member of, with no public URL. With no ``app_token`` the bot sends but
does not receive.

The bot keeps a synced history (``keeps_history``). Live events write it as they
arrive, edits and deletes included. A delta sync over the bot's memberships
fills whatever arrived while the socket was down: at start, after each
reconnect, every :data:`RESYNC_S`, and on the first message from a chat it has
not synced yet (which is how a newly joined chat gets its backlog).

The person's own Slack (session ``xoxc-`` via the mira browser) stays on
:class:`~awm.social.connectors.slack_conn.SlackConnector`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

import aiohttp

from awm.social.connectors.base import (
    Account, Attachment, Channel, Connector, Identity, InboundMessage,
    OnMessage,
)

log = logging.getLogger("awm.social.connectors.slack_bot")

API_BASE = "https://slack.com/api/"
# Subtypes that are a message someone (or some other bot) posted. Every other
# subtype is a join, topic change, etc.; edits and deletes are handled apart.
_POSTED_SUBTYPES = ("", "file_share", "thread_broadcast", "bot_message")
_MEMBER_TYPES = "public_channel,private_channel,mpim,im"
_MAX_BACKOFF_S = 30.0
RESYNC_S = 600.0
# How far back the first sync of a chat reaches, in messages.
BACKFILL_MAX = 1000


class SlackApiError(RuntimeError):
    """Slack answered ``ok: false``; ``error`` is Slack's error code."""

    def __init__(self, method: str, error: str) -> None:
        super().__init__(f"slack {method}: {error}")
        self.error = error


def _attachments_of(m: dict) -> list[Attachment]:
    out: list[Attachment] = []
    for i, f in enumerate(m.get("files", []) or []):
        url = f.get("url_private_download") or f.get("url_private") or ""
        out.append(Attachment(
            idx=i,
            filename=f.get("name") or f.get("title") or f"attachment-{i}",
            mime=f.get("mimetype", "") or "",
            size=int(f.get("size", 0) or 0),
            url=url,
            ref={"id": f.get("id", ""), "url": url},
        ))
    return out


def _looks_like_user_id(s: str) -> bool:
    return bool(s) and s[0] in ("U", "W") and s[1:].isalnum() and s.isupper()


def _later(a: str | None, b: str | None) -> str | None:
    if not a or not b:
        return a or b
    return a if float(a) >= float(b) else b


class SlackBotConnector(Connector):
    platform = "slack"
    keeps_history = True

    def __init__(self, account: Account, on_message: OnMessage,
                 on_command=None) -> None:
        super().__init__(account, on_message, on_command)
        self.api_base = API_BASE
        self._http: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._self_user = ""
        self._self_bot = ""
        self._self_name = ""
        self._user_names: dict[str, str] = {}
        self._synced: set[str] = set()
        self._resync = asyncio.Event()
        self._sync_lock = asyncio.Lock()
        self._sync_task: asyncio.Task | None = None

    # -- Web API -------------------------------------------------------------

    def _session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession()
        return self._http

    async def _call(self, method: str, *, token: str | None = None,
                    **params) -> dict:
        """POST one Web API method form-encoded; retry on HTTP 429."""
        data = {k: v for k, v in params.items() if v is not None}
        headers = {"Authorization": f"Bearer {token or self.account.token}"}
        while True:
            async with self._session().post(
                    self.api_base + method, data=data, headers=headers) as r:
                if r.status == 429:
                    await asyncio.sleep(float(r.headers.get("Retry-After", "1")))
                    continue
                body = await r.json(content_type=None)
            if not body.get("ok"):
                raise SlackApiError(method, body.get("error", f"http {r.status}"))
            return body

    async def _paged(self, method: str, key: str, *, cap: int | None = None,
                     **params) -> list[dict]:
        out: list[dict] = []
        cursor = None
        while True:
            r = await self._call(method, limit="200", cursor=cursor, **params)
            out.extend(r.get(key, []))
            cursor = (r.get("response_metadata") or {}).get("next_cursor")
            if not cursor or (cap is not None and len(out) >= cap):
                return out[:cap] if cap is not None else out

    async def _user_name(self, user_id: str) -> str:
        if not user_id:
            return ""
        if user_id not in self._user_names:
            try:
                u = (await self._call("users.info", user=user_id))["user"]
                prof = u.get("profile") or {}
                self._user_names[user_id] = (
                    prof.get("display_name") or u.get("real_name")
                    or u.get("name") or user_id)
            except SlackApiError:
                self._user_names[user_id] = user_id
        return self._user_names[user_id]

    async def _inbound(self, channel: str, m: dict,
                       channel_name: str = "") -> InboundMessage:
        sender = m.get("user") or m.get("bot_id") or ""
        name = await self._user_name(m.get("user", "")) or (
            (m.get("bot_profile") or {}).get("name") or m.get("username") or sender)
        return InboundMessage(
            account=self.account.name,
            platform=self.platform,
            channel_id=channel,
            channel_name=channel_name,
            thread_id=m.get("thread_ts", "") or "",
            sender_id=sender,
            sender_name=name,
            message_id=m.get("ts", ""),
            ts=m.get("ts", ""),
            text=m.get("text", "") or "",
            attachments=_attachments_of(m),
        )

    def _is_own(self, m: dict) -> bool:
        return bool((self._self_bot and m.get("bot_id") == self._self_bot)
                    or (self._self_user and m.get("user") == self._self_user))

    # -- history sync --------------------------------------------------------

    async def _record(self, channel: str, m: dict, channel_name: str = "",
                      *, edited: bool = False) -> InboundMessage:
        msg = await self._inbound(channel, m, channel_name)
        if self.history is not None:
            self.history.record(msg, own=self._is_own(m), edited=edited)
        return msg

    async def _chat_name(self, ch: dict) -> str:
        if ch.get("is_im"):
            return await self._user_name(ch.get("user", ""))
        return ch.get("name", "")

    async def _sync_channel(self, cid: str, name: str = "") -> None:
        """Pull everything in one chat newer than its cursor, threads included."""
        since = self.history.synced_ts(cid)
        parents = await self._paged(
            "conversations.history", "messages", channel=cid, oldest=since,
            cap=None if since else BACKFILL_MAX)
        newest = since
        for m in parents:
            if m.get("subtype", "") not in _POSTED_SUBTYPES:
                continue
            await self._record(cid, m, name)
            newest = _later(newest, m.get("ts"))
            if m.get("reply_count") and _later(m.get("latest_reply"), since) != since:
                replies = await self._paged(
                    "conversations.replies", "messages", channel=cid,
                    ts=m["ts"], oldest=since)
                for r in replies:
                    if r.get("ts") != m["ts"]:
                        await self._record(cid, r, name)
                        newest = _later(newest, r.get("ts"))
        self.history.set_synced(cid, newest or "0", name)
        self._synced.add(cid)

    async def sync(self) -> int:
        """Delta-sync every chat the bot is a member of; returns the chat count."""
        async with self._sync_lock:
            chats = await self._paged("users.conversations", "channels",
                                      types=_MEMBER_TYPES, exclude_archived="true")
            for ch in chats:
                try:
                    await self._sync_channel(ch["id"], await self._chat_name(ch))
                except SlackApiError as exc:
                    log.warning("slack-bot[%s] sync of %s failed: %s",
                                self.account.name, ch["id"], exc)
            return len(chats)

    async def _resync_loop(self) -> None:
        while True:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._resync.wait(), RESYNC_S)
            self._resync.clear()
            try:
                n = await self.sync()
                log.info("slack-bot[%s] synced %d chat(s)", self.account.name, n)
            except Exception as exc:  # noqa: BLE001 — retried next round
                log.warning("slack-bot[%s] sync failed: %s", self.account.name, exc)

    # -- Socket Mode receive -------------------------------------------------

    async def _handle_event(self, event: dict) -> None:
        if event.get("type") != "message":
            return  # app_mention duplicates the message event; message wins
        cid = event.get("channel", "")
        subtype = event.get("subtype", "")
        if subtype == "message_changed":
            if self.history is not None and event.get("message"):
                await self._record(cid, event["message"], edited=True)
            return
        if subtype == "message_deleted":
            if self.history is not None:
                self.history.mark_deleted(cid, event.get("deleted_ts", ""))
            return
        if subtype not in _POSTED_SUBTYPES:
            return
        msg = await self._record(cid, event)
        if self.history is not None:
            if cid in self._synced:
                self.history.set_synced(cid, msg.ts)
            else:
                self._resync.set()  # first sight of this chat: pull its backlog
        if not self._is_own(event):
            await self.on_message(msg)

    async def _run_socket(self) -> None:
        """One Socket Mode connection, until Slack disconnects it."""
        opened = await self._call("apps.connections.open",
                                  token=self.account.app_token)
        async with self._session().ws_connect(opened["url"], heartbeat=30) as ws:
            self._ws = ws
            async for frame in ws:
                if frame.type != aiohttp.WSMsgType.TEXT:
                    break
                env = json.loads(frame.data)
                if env.get("envelope_id"):
                    await ws.send_json({"envelope_id": env["envelope_id"]})
                kind = env.get("type")
                if kind == "hello":
                    log.info("slack-bot[%s] Socket Mode connected", self.account.name)
                    self._resync.set()  # cover whatever arrived while away
                elif kind == "disconnect":
                    break
                elif kind == "events_api":
                    try:
                        await self._handle_event(
                            (env.get("payload") or {}).get("event") or {})
                    except Exception as exc:  # noqa: BLE001 — one bad event never kills the loop
                        log.warning("slack-bot[%s] event handler failed: %s",
                                    self.account.name, exc)
        self._ws = None

    async def start(self) -> None:
        try:
            auth = await self._call("auth.test")
        except Exception as exc:  # noqa: BLE001
            log.error("slack-bot[%s] auth.test failed; account down: %s",
                      self.account.name, exc)
            return
        self._self_user = auth.get("user_id", "")
        self._self_bot = auth.get("bot_id", "")
        self._self_name = auth.get("user", "")
        log.info("slack-bot[%s] authenticated as %s in %s", self.account.name,
                 self._self_name, auth.get("team", ""))

        if self.history is not None:
            self._resync.set()
            self._sync_task = asyncio.create_task(self._resync_loop())

        if not self.account.app_token:
            log.warning("slack-bot[%s] has no app_token (xapp-); send-only",
                        self.account.name)
            await asyncio.Event().wait()
            return

        backoff = 1.0
        while True:
            try:
                await self._run_socket()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("slack-bot[%s] Socket Mode lost (%s); retry in %.1fs",
                            self.account.name, exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF_S)

    # -- verbs ---------------------------------------------------------------

    async def send(
        self, channel: str, text: str, *, thread: str | None = None
    ) -> dict:
        # A user id as channel posts into the bot's DM with that user.
        r = await self._call("chat.postMessage", channel=channel, text=text,
                             thread_ts=thread)
        if self.history is not None and r.get("message"):
            await self._record(r.get("channel", channel), r["message"])
        return {"message_id": r.get("ts", ""),
                "channel_id": r.get("channel", channel), "ts": r.get("ts", "")}

    async def fetch(
        self, channel: str, *, limit: int = 50, before: str | None = None
    ) -> list[InboundMessage]:
        r = await self._call("conversations.history", channel=channel,
                             limit=str(limit), latest=before)
        msgs = [m for m in r.get("messages", [])
                if m.get("subtype", "") in _POSTED_SUBTYPES]
        out = [await self._inbound(channel, m) for m in msgs]
        out.reverse()  # Slack returns newest-first
        return out

    async def list_channels(self, *, include_dms: bool = False) -> list[Channel]:
        types = "public_channel,private_channel"
        if include_dms:
            types += ",im,mpim"
        out: list[Channel] = []
        for ch in await self._paged("conversations.list", "channels",
                                    types=types, exclude_archived="true"):
            kind = ("dm" if ch.get("is_im") else "group" if ch.get("is_mpim")
                    else "private" if ch.get("is_private") else "channel")
            out.append(Channel(id=ch["id"], name=await self._chat_name(ch), kind=kind))
        return out

    async def _resolve_user_id(self, who: str) -> str:
        if _looks_like_user_id(who):
            return who
        needle = who.lstrip("@").lower()
        for u in await self._paged("users.list", "members"):
            prof = u.get("profile") or {}
            names = {u.get("name", ""), u.get("real_name", ""),
                     prof.get("display_name", ""), prof.get("real_name", "")}
            if needle in {n.lower() for n in names if n}:
                return u["id"]
        raise ValueError(f"no slack user named {who!r}")

    async def open_dm(self, user: str) -> Channel:
        uid = await self._resolve_user_id(user)
        r = await self._call("conversations.open", users=uid)
        return Channel(id=r["channel"]["id"], name=await self._user_name(uid),
                       kind="dm")

    async def download_attachments(
        self, channel: str, message_id: str, *, idx: int | None = None
    ) -> list[tuple[str, str, bytes]]:
        r = await self._call("conversations.history", channel=channel,
                             latest=message_id, inclusive="true", limit="1")
        msgs = [m for m in r.get("messages", []) if m.get("ts") == message_id]
        if not msgs:
            raise ValueError(f"message {message_id} not found in {channel}")
        out: list[tuple[str, str, bytes]] = []
        headers = {"Authorization": f"Bearer {self.account.token}"}
        for a in _attachments_of(msgs[0]):
            if idx is not None and a.idx != idx:
                continue
            async with self._session().get(a.url, headers=headers) as resp:
                resp.raise_for_status()
                out.append((a.filename, a.mime, await resp.read()))
        return out

    async def identity(self) -> Identity:
        if not self._self_user:
            auth = await self._call("auth.test")
            self._self_user = auth.get("user_id", "")
            self._self_name = auth.get("user", "")
        return Identity(id=self._self_user, name=self._self_name)

    async def close(self) -> None:
        if self._sync_task is not None:
            self._sync_task.cancel()
            self._sync_task = None
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        if self._http is not None and not self._http.closed:
            await self._http.close()
