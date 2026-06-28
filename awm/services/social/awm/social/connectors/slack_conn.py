"""Slack connector — wraps ``slack_sdk`` for one account, in two auth modes.

**API mode** (a bot ``xoxb-`` or user ``xoxp-`` OAuth token): send + channel
listing go through :class:`~slack_sdk.web.async_client.AsyncWebClient`; receive
goes through Socket Mode (:class:`~slack_sdk.socket_mode.aiohttp.SocketModeClient`),
which REQUIRES a separate app-level ``app_token`` (``xapp-``). With no ``app_token``
the connector still sends but never receives.

**Session mode** (a web-session ``xoxc-`` token + the ``d`` cookie — the same pair
the Slack web/desktop client uses): this acts as the *user* without an app. The
SDK's default host (``slack.com/api``) accepts the pair when the cookie rides in a
``Cookie: d=<value>`` header. Socket Mode is app-scoped and unavailable here, so
receive is a **poll loop** over ``conversations.history`` (modelled on the gmail
connector's baseline-then-delta discipline). The creds are short-lived, so a
``creds_cmd`` (e.g. ``ssh mira awm-slack-creds``) fetches a fresh ``{token,cookie}``
at start and again on an auth failure — nothing secret need live on disk. Session
mode is selected when ``creds_cmd`` is set or the inline token is an ``xoxc-``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from awm.social.connectors.base import (
    Account, Channel, Connector, Identity, InboundMessage, OnMessage,
)

log = logging.getLogger("awm.social.connectors.slack")

# Session-mode receive tuning.
SESSION_POLL_INTERVAL_S = 20.0
SESSION_CONV_TYPES = "im,mpim,private_channel,public_channel"
SESSION_HISTORY_LIMIT = 50
# Auth errors that mean "the session creds are stale, re-fetch and retry".
_REFRESHABLE = ("invalid_auth", "not_authed", "token_revoked", "token_expired")


class SlackConnector(Connector):
    platform = "slack"

    def __init__(self, account: Account, on_message: OnMessage) -> None:
        super().__init__(account, on_message)
        self._web = None       # AsyncWebClient (send / list)
        self._socket = None     # SocketModeClient (receive, API mode)
        self._self_id = ""      # our own user id, to drop echoes
        # Session mode: creds_cmd pull, or an inline xoxc- token + cookie.
        self._session = bool(account.creds_cmd) or account.token.startswith("xoxc-")
        self._token = account.token        # may be "" until creds_cmd runs
        self._cookie = account.cookie or ""

    # -- credentials --------------------------------------------------------

    def _web_client(self):
        from slack_sdk.web.async_client import AsyncWebClient

        if self._web is None:
            if self._session:
                # The 'd' cookie value is %-encoded — pass it verbatim.
                self._web = AsyncWebClient(
                    token=self._token,
                    headers={"Cookie": f"d={self._cookie}"})
            else:
                self._web = AsyncWebClient(token=self._token)
        return self._web

    async def _fetch_creds(self) -> None:
        """Run ``creds_cmd`` → refresh the live token + cookie. Raises on failure.

        Resets the cached web client so the next call rebuilds with fresh creds.
        Logs nothing secret.
        """
        cmd = self.account.creds_cmd
        if not cmd:
            raise RuntimeError("no creds_cmd to refresh session credentials")
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        if proc.returncode != 0:
            msg = err.decode(errors="replace").strip()[:200]
            raise RuntimeError(f"creds_cmd exited {proc.returncode}: {msg}")
        try:
            data = json.loads(out.decode())
        except ValueError as exc:
            raise RuntimeError(f"creds_cmd output was not JSON: {exc}") from exc
        token = (data.get("token") or "").strip()
        cookie = (data.get("cookie") or "").strip()
        if not token or not cookie:
            raise RuntimeError("creds_cmd JSON missing token/cookie")
        self._token, self._cookie = token, cookie
        self._web = None  # force rebuild

    async def _auth_retry(self, fn):
        """Call ``fn()``; on a refreshable auth error re-fetch creds and retry once.

        ``fn`` must re-resolve the web client itself (it calls ``_web_client()``),
        so the retry picks up the rebuilt client. A no-creds_cmd account just
        re-raises — there is nothing to refresh.
        """
        from slack_sdk.errors import SlackApiError

        try:
            return await fn()
        except SlackApiError as exc:
            err = ""
            try:
                err = (exc.response or {}).get("error", "") or ""
            except Exception:  # noqa: BLE001
                err = ""
            if self.account.creds_cmd and err in _REFRESHABLE:
                log.info("slack[%s] auth error %r; refreshing creds",
                         self.account.name, err)
                await self._fetch_creds()
                return await fn()
            raise

    # -- API-mode (Socket Mode) receive ------------------------------------

    async def _handle_request(self, client, req) -> None:  # noqa: ANN001
        from slack_sdk.socket_mode.response import SocketModeResponse

        # Always ack first so Slack doesn't redeliver.
        if req.envelope_id:
            await client.send_socket_mode_response(
                SocketModeResponse(envelope_id=req.envelope_id))
        if req.type != "events_api":
            return
        event = (req.payload or {}).get("event") or {}
        if event.get("type") != "message":
            return
        # Skip message subtypes (edits, joins, bot echoes) and our own messages.
        if event.get("subtype"):
            return
        sender = event.get("user") or ""
        if sender and sender == self._self_id:
            return
        try:
            inbound = InboundMessage(
                account=self.account.name,
                platform=self.platform,
                channel_id=event.get("channel", ""),
                channel_name="",
                thread_id=event.get("thread_ts", "") or "",
                sender_id=sender,
                sender_name=sender,
                message_id=event.get("ts", ""),
                ts=event.get("ts", ""),
                text=event.get("text", "") or "",
            )
            await self.on_message(inbound)
        except Exception as exc:  # noqa: BLE001 — one bad event never kills the loop
            log.warning("slack[%s] event handler failed: %s",
                        self.account.name, exc)

    # -- session-mode (poll) receive ---------------------------------------

    async def _list_conversations(self) -> list[str]:
        resp = await self._auth_retry(lambda: self._web_client().users_conversations(
            types=SESSION_CONV_TYPES, exclude_archived=True, limit=200))
        return [c.get("id", "") for c in resp.get("channels", []) if c.get("id")]

    async def _emit_session_message(self, cid: str, m: dict) -> None:
        if m.get("subtype"):  # joins/edits/system/bot — not a plain message
            return
        sender = m.get("user") or ""
        if sender and sender == self._self_id:  # echo of our own send
            return
        try:
            inbound = InboundMessage(
                account=self.account.name,
                platform=self.platform,
                channel_id=cid,
                channel_name="",
                thread_id=m.get("thread_ts", "") or "",
                sender_id=sender,
                sender_name=sender,
                message_id=m.get("ts", ""),
                ts=m.get("ts", ""),
                text=m.get("text", "") or "",
            )
            await self.on_message(inbound)
        except Exception as exc:  # noqa: BLE001 — one bad msg never kills the loop
            log.warning("slack[%s] session msg handler failed: %s",
                        self.account.name, exc)

    async def _poll_once(self, baseline: dict[str, str], first: bool) -> None:
        """One session-receive cycle.

        ``baseline`` maps channel id → last-seen ts. A channel seen for the first
        time (initial run, or a conversation that appeared later) is baselined to
        its current latest ts and NOT replayed — only strictly-newer messages on
        already-known channels are emitted (mirrors gmail's baseline-UID rule).
        """
        convs = await self._list_conversations()
        if first:
            log.info("slack[%s] session receive: polling %d conversations every %.0fs",
                     self.account.name, len(convs), SESSION_POLL_INTERVAL_S)
        for cid in convs:
            if cid not in baseline:
                resp = await self._auth_retry(
                    lambda cid=cid: self._web_client().conversations_history(
                        channel=cid, limit=1))
                msgs = resp.get("messages", [])
                baseline[cid] = msgs[0].get("ts", "") if msgs else f"{time.time():.6f}"
                continue
            resp = await self._auth_retry(
                lambda cid=cid: self._web_client().conversations_history(
                    channel=cid, oldest=baseline[cid], limit=SESSION_HISTORY_LIMIT))
            newest = baseline[cid]
            for m in reversed(resp.get("messages", [])):  # API is newest-first
                ts = m.get("ts", "")
                if not ts or ts <= baseline[cid]:
                    continue  # oldest= is inclusive → drop the boundary message
                if ts > newest:
                    newest = ts
                await self._emit_session_message(cid, m)
            baseline[cid] = newest

    async def _run_session_receive(self) -> None:
        baseline: dict[str, str] = {}
        first = True
        backoff = 1.0
        while True:
            try:
                await self._poll_once(baseline, first)
                first = False
                backoff = 1.0
                await asyncio.sleep(SESSION_POLL_INTERVAL_S)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("slack[%s] session poll error (%s); retry in %.1fs",
                            self.account.name, exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        try:
            from slack_sdk.web.async_client import AsyncWebClient  # noqa: F401
        except ImportError as exc:
            log.error("slack_sdk not installed; slack[%s] disabled: %s",
                      self.account.name, exc)
            return

        # Session mode with a pull command: fetch creds before the first call.
        if self._session and self.account.creds_cmd:
            try:
                await self._fetch_creds()
            except Exception as exc:  # noqa: BLE001
                log.error("slack[%s] creds_cmd failed; account down: %s",
                          self.account.name, exc)
                return

        # Resolve our own user id once so we can drop echoes of our own sends.
        try:
            auth = await self._auth_retry(lambda: self._web_client().auth_test())
            self._self_id = auth.get("user_id", "") or ""
            log.info("slack[%s] authenticated as %s (%s mode)",
                     self.account.name, auth.get("user", self._self_id),
                     "session" if self._session else "api")
        except Exception as exc:  # noqa: BLE001
            log.error("slack[%s] auth_test failed; account down: %s",
                      self.account.name, exc)
            return

        if self._session:
            await self._run_session_receive()
            return

        if not self.account.app_token:
            log.warning(
                "slack[%s] has no app_token (xapp-); send works but Socket Mode "
                "receive is disabled", self.account.name)
            # Send-only account: nothing to run a receive loop for. Park until
            # cancelled so the task stays alive and send() keeps working.
            await asyncio.Event().wait()
            return

        from slack_sdk.socket_mode.aiohttp import SocketModeClient

        backoff = 1.0
        while True:
            try:
                self._socket = SocketModeClient(
                    app_token=self.account.app_token,
                    web_client=self._web_client(),
                )
                self._socket.socket_mode_request_listeners.append(
                    self._handle_request)
                await self._socket.connect()
                log.info("slack[%s] Socket Mode connected", self.account.name)
                backoff = 1.0
                # connect() returns once the WS is up; the SDK keeps it alive.
                # Park until cancelled; a hard drop raises out of connect on the
                # next cycle and we reconnect with backoff.
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("slack[%s] Socket Mode lost (%s); retry in %.1fs",
                            self.account.name, exc, backoff)
                await self.close()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def send(
        self, channel: str, text: str, *, thread: str | None = None
    ) -> dict:
        kwargs = {"channel": channel, "text": text}
        if thread:
            kwargs["thread_ts"] = thread
        resp = await self._auth_retry(
            lambda: self._web_client().chat_postMessage(**kwargs))
        return {
            "message_id": resp.get("ts", ""),
            "channel_id": resp.get("channel", channel),
            "ts": resp.get("ts", ""),
        }

    async def list_channels(self) -> list[Channel]:
        resp = await self._auth_retry(lambda: self._web_client().conversations_list(
            types="public_channel,private_channel", limit=200))
        out: list[Channel] = []
        for ch in resp.get("channels", []):
            out.append(Channel(
                id=ch.get("id", ""),
                name=ch.get("name", ""),
                kind="channel",
            ))
        return out

    async def identity(self) -> Identity:
        auth = await self._auth_retry(lambda: self._web_client().auth_test())
        return Identity(
            id=auth.get("user_id", ""), name=auth.get("user", ""))

    async def close(self) -> None:
        if self._socket is not None:
            try:
                await self._socket.close()
            except Exception as exc:  # noqa: BLE001
                log.debug("slack[%s] close error: %s", self.account.name, exc)
            self._socket = None
