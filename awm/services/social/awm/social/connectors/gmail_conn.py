"""Gmail connector — send via SMTP, receive via IMAP, using a Google App Password.

Auth is a 16-char **App Password** (``token``) + the mailbox ``address`` — no
OAuth server, no Gmail API client. The account requires 2-Step Verification on
the Google account so an App Password can be issued; the password itself logs in
to both ``smtp.gmail.com`` (send) and ``imap.gmail.com`` (receive).

The stdlib ``smtplib`` / ``imaplib`` clients are blocking, so every network call
is offloaded with :func:`asyncio.to_thread` — the connector never blocks the
service's event loop. Receive is a poll loop: on first connect it records the
current max IMAP UID as a baseline (so it never replays old mail), then each
cycle fetches messages with a UID strictly greater than the baseline. It reads
without setting ``\\Seen`` (BODY.PEEK), so polling never marks your mail read.

Mapping onto the connector ABC:
  - ``channel`` = a recipient email address (``send``) or, on inbound, the
    sender's address.
  - ``thread`` = an RFC822 ``Message-ID`` to reply into (sets In-Reply-To /
    References); optional.
  - ``list_channels`` = the account's IMAP mailboxes/labels.

Outbound subject: if ``text`` begins with a ``Subject: ...`` line it is used as
the subject and the remainder (after one optional blank line) is the body;
otherwise the first line becomes the subject and the whole text is the body.
"""

from __future__ import annotations

import asyncio
import email
import email.utils
import imaplib
import logging
import smtplib
from email.message import EmailMessage

from awm.social.connectors.base import (
    Account, Attachment, Channel, Connector, Identity, InboundMessage,
    OnMessage,
)

log = logging.getLogger("awm.social.connectors.gmail")

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465  # implicit TLS (SMTP_SSL)
IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
POLL_INTERVAL_S = 30.0
# Gmail's virtual mailbox holding every message (archived or not) — the right
# scope for search + by-Message-ID lookup, which INBOX alone would miss.
ALL_MAIL = "[Gmail]/All Mail"


def _split_subject(text: str) -> tuple[str, str]:
    """Split an outbound body into (subject, body).

    An explicit ``Subject: ...`` first line wins (the body is everything after
    it, with one optional blank separator line dropped). Otherwise the first
    non-empty line becomes the subject (truncated) and the whole text is the
    body.
    """
    lines = text.split("\n")
    if lines and lines[0].lower().startswith("subject:"):
        subject = lines[0][len("subject:"):].strip()
        rest = lines[1:]
        if rest and rest[0].strip() == "":
            rest = rest[1:]
        return subject or "(no subject)", "\n".join(rest)
    first = next((ln.strip() for ln in lines if ln.strip()), "(no subject)")
    return first[:78], text


def _body_text(msg: email.message.Message) -> str:
    """Best-effort plain-text body of a received message."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                payload = part.get_payload(decode=True)
                if payload is not None:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
        return ""
    payload = msg.get_payload(decode=True)
    if payload is None:
        return msg.get_payload() if isinstance(msg.get_payload(), str) else ""
    charset = msg.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace")


def _is_attachment_part(part: email.message.Message) -> bool:
    """A MIME part counts as a downloadable attachment if it carries a filename
    or an explicit ``Content-Disposition: attachment``."""
    if part.get_filename():
        return True
    disp = str(part.get("Content-Disposition", "")).lower()
    return disp.startswith("attachment")


def _extract_attachments(msg: email.message.Message, uid: str) -> list[Attachment]:
    """Build :class:`Attachment` metadata for a parsed message's file parts.

    Each attachment's :attr:`Attachment.idx` is its ordinal among the message's
    file parts (0-based) — the same ordinal :func:`_attachment_bytes` re-derives
    on download, so ``download_attachments(idx=…)`` lines up.
    """
    out: list[Attachment] = []
    if not msg.is_multipart():
        return out
    ordinal = 0
    for part in msg.walk():
        if not _is_attachment_part(part):
            continue
        payload = part.get_payload(decode=True)
        out.append(Attachment(
            idx=ordinal,
            filename=part.get_filename() or f"attachment-{ordinal}",
            mime=part.get_content_type(),
            size=len(payload) if payload else 0,
            ref={"uid": str(uid), "ord": ordinal},
        ))
        ordinal += 1
    return out


def _attachment_bytes(
    msg: email.message.Message, idx: int | None
) -> list[tuple[str, str, bytes]]:
    """Pull ``(filename, mime, bytes)`` for a message's attachments.

    ``idx`` restricts to a single attachment by its ordinal (see
    :func:`_extract_attachments`); ``None`` returns them all.
    """
    out: list[tuple[str, str, bytes]] = []
    ordinal = 0
    for part in msg.walk():
        if not _is_attachment_part(part):
            continue
        if idx is None or ordinal == idx:
            payload = part.get_payload(decode=True) or b""
            out.append((part.get_filename() or f"attachment-{ordinal}",
                        part.get_content_type(), payload))
        ordinal += 1
    return out


def _row_from_msg(msg: email.message.Message, uid: str | int) -> dict:
    """Normalise a parsed message into the dict the connector maps to an
    :class:`InboundMessage` (shared by the poll loop, fetch, and search)."""
    from_addr = email.utils.parseaddr(msg.get("From", ""))[1]
    return {
        "channel_id": from_addr,
        "channel_name": "INBOX",
        "sender_id": from_addr,
        "sender_name": str(msg.get("From", "")),
        "message_id": msg.get("Message-ID", "") or str(uid),
        "ts": msg.get("Date", ""),
        "text": _body_text(msg),
        "subject": str(msg.get("Subject", "")),
        "attachments": _extract_attachments(msg, str(uid)),
    }


class GmailConnector(Connector):
    platform = "gmail"

    def __init__(self, account: Account, on_message: OnMessage,
                 on_command=None) -> None:
        super().__init__(account, on_message, on_command)
        self._addr = account.address or ""
        self._pw = account.token
        self._baseline = 0  # highest IMAP UID seen; 0 = not yet established

    # -- IMAP receive -------------------------------------------------------

    def _imap_login(self) -> imaplib.IMAP4_SSL:
        m = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        m.login(self._addr, self._pw)
        return m

    def _poll_sync(self, baseline: int) -> tuple[list[dict], int]:
        """One blocking IMAP poll cycle. Returns (new messages, new baseline).

        With ``baseline == 0`` it only establishes the baseline (the current max
        UID) and returns no messages, so existing mail is never replayed.
        """
        m = self._imap_login()
        try:
            m.select("INBOX", readonly=True)
            typ, data = m.uid("search", None, "ALL")
            if typ != "OK":
                return [], baseline
            uids = [int(x) for x in (data[0].split() if data and data[0] else [])]
            if not uids:
                return [], baseline
            max_uid = max(uids)
            if baseline == 0:
                return [], max_uid  # first run: just set the baseline
            fresh = sorted(u for u in uids if u > baseline)
            out: list[dict] = []
            for uid in fresh:
                typ, fetched = m.uid("fetch", str(uid), "(BODY.PEEK[])")
                if typ != "OK" or not fetched or not fetched[0]:
                    continue
                raw = fetched[0][1]
                msg = email.message_from_bytes(raw)
                out.append(_row_from_msg(msg, uid))
            return out, (max(fresh) if fresh else baseline)
        finally:
            try:
                m.logout()
            except Exception:  # noqa: BLE001
                pass

    def _history_sync(
        self, channel: str, limit: int, before: int | None
    ) -> list[dict]:
        """Blocking IMAP fetch of existing mail (for on-demand history).

        Returns the last ``limit`` messages (oldest→newest). ``before`` is an
        optional exclusive UID upper bound (page backwards). If ``channel`` looks
        like an address, restricts to mail ``FROM`` that sender; otherwise the
        whole INBOX. Reads with BODY.PEEK so it never marks mail read.
        """
        m = self._imap_login()
        try:
            m.select("INBOX", readonly=True)
            if channel and "@" in channel:
                typ, data = m.uid("search", None, "FROM", channel)
            else:
                typ, data = m.uid("search", None, "ALL")
            if typ != "OK":
                return []
            uids = sorted(
                int(x) for x in (data[0].split() if data and data[0] else []))
            if before:
                uids = [u for u in uids if u < before]
            uids = uids[-max(1, limit):]  # last N, already oldest→newest
            out: list[dict] = []
            for uid in uids:
                typ, fetched = m.uid("fetch", str(uid), "(BODY.PEEK[])")
                if typ != "OK" or not fetched or not fetched[0]:
                    continue
                raw = fetched[0][1]
                msg = email.message_from_bytes(raw)
<<<<<<< HEAD
                from_addr = email.utils.parseaddr(msg.get("From", ""))[1]
                out.append({
                    "channel_id": from_addr,
                    "channel_name": "INBOX",
                    "sender_id": from_addr,
                    "sender_name": str(msg.get("From", "")),
                    "message_id": msg.get("Message-ID", "") or str(uid),
                    "ts": msg.get("Date", ""),
                    "text": _body_text(msg),
                    "subject": str(msg.get("Subject", "")),
                })
=======
                out.append(_row_from_msg(msg, uid))
>>>>>>> feat/svc-social
            return out
        finally:
            try:
                m.logout()
            except Exception:  # noqa: BLE001
                pass

<<<<<<< HEAD
    async def history(
=======
    # -- search + attachment download (blocking IMAP) -----------------------

    def _search_sync(self, query: str, limit: int) -> list[dict]:
        """Blocking Gmail search over All Mail via IMAP ``X-GM-RAW``.

        ``X-GM-RAW`` accepts the full Gmail query syntax (``from:``, ``subject:``,
        ``has:attachment``, …). Returns the newest ``limit`` matches, newest-first.
        """
        m = self._imap_login()
        try:
            typ, _ = m.select(ALL_MAIL, readonly=True)
            if typ != "OK":
                m.select("INBOX", readonly=True)
            # X-GM-RAW takes one quoted string; escape embedded quotes/backslashes.
            q = '"' + str(query).replace("\\", "\\\\").replace('"', '\\"') + '"'
            typ, data = m.uid("search", None, "X-GM-RAW", q)
            if typ != "OK":
                return []
            uids = sorted(
                int(x) for x in (data[0].split() if data and data[0] else []))
            uids = uids[-max(1, limit):]  # newest N
            out: list[dict] = []
            for uid in reversed(uids):  # newest-first
                typ, fetched = m.uid("fetch", str(uid), "(BODY.PEEK[])")
                if typ != "OK" or not fetched or not fetched[0]:
                    continue
                msg = email.message_from_bytes(fetched[0][1])
                out.append(_row_from_msg(msg, uid))
            return out
        finally:
            try:
                m.logout()
            except Exception:  # noqa: BLE001
                pass

    def _download_sync(
        self, message_id: str, idx: int | None
    ) -> list[tuple[str, str, bytes]]:
        """Blocking re-fetch of one message (by RFC822 Message-ID, or raw UID
        fallback) → its attachments' bytes. Reads with BODY.PEEK (never marks
        the mail read)."""
        m = self._imap_login()
        try:
            typ, _ = m.select(ALL_MAIL, readonly=True)
            if typ != "OK":
                m.select("INBOX", readonly=True)
            raw: bytes | None = None
            typ, data = m.uid("search", None, "HEADER", "Message-ID", message_id)
            uids = (data[0].split() if typ == "OK" and data and data[0] else [])
            if uids:
                target = uids[-1].decode() if isinstance(uids[-1], bytes) else uids[-1]
                typ, fetched = m.uid("fetch", target, "(BODY.PEEK[])")
                if typ == "OK" and fetched and fetched[0]:
                    raw = fetched[0][1]
            if raw is None and str(message_id).isdigit():
                # Fallback: message_id was a bare UID (fetch used str(uid)).
                typ, fetched = m.uid("fetch", str(message_id), "(BODY.PEEK[])")
                if typ == "OK" and fetched and fetched[0]:
                    raw = fetched[0][1]
            if raw is None:
                raise RuntimeError(
                    f"gmail[{self.account.name}] message {message_id!r} not found")
            msg = email.message_from_bytes(raw)
            return _attachment_bytes(msg, idx)
        finally:
            try:
                m.logout()
            except Exception:  # noqa: BLE001
                pass

    async def search(
        self, query: str, *, limit: int = 50, channel: str | None = None
    ) -> list[InboundMessage]:
        if not self._addr:
            raise RuntimeError(
                f"gmail[{self.account.name}] no address configured")
        # Scope to one correspondent by folding `channel` (an address) into the
        # Gmail query when given.
        q = query
        if channel and "@" in channel:
            q = f"from:{channel} {query}".strip()
        rows = await asyncio.to_thread(self._search_sync, q, int(limit))
        return [self._inbound(d) for d in rows]

    async def download_attachments(
        self, channel: str, message_id: str, *, idx: int | None = None
    ) -> list[tuple[str, str, bytes]]:
        if not self._addr:
            raise RuntimeError(
                f"gmail[{self.account.name}] no address configured")
        return await asyncio.to_thread(self._download_sync, message_id, idx)

    def _inbound(self, d: dict) -> InboundMessage:
        return InboundMessage(
            account=self.account.name,
            platform=self.platform,
            channel_id=d["channel_id"],
            channel_name=d["channel_name"],
            thread_id="",
            sender_id=d["sender_id"],
            sender_name=d["sender_name"],
            message_id=d["message_id"],
            ts=d["ts"],
            text=d["text"],
            attachments=d.get("attachments", []),
            raw={"subject": d["subject"]},
        )

    async def fetch(
>>>>>>> feat/svc-social
        self, channel: str = "", *, limit: int = 50, before: str | None = None
    ) -> list[InboundMessage]:
        if not self._addr:
            raise RuntimeError(
                f"gmail[{self.account.name}] no address configured")
        before_uid: int | None = None
        if before:
            try:
                before_uid = int(before)
            except (TypeError, ValueError):
                before_uid = None  # non-UID cursor: ignore, return most recent
        rows = await asyncio.to_thread(
            self._history_sync, channel, limit, before_uid)
<<<<<<< HEAD
        return [InboundMessage(
            account=self.account.name,
            platform=self.platform,
            channel_id=d["channel_id"],
            channel_name=d["channel_name"],
            thread_id="",
            sender_id=d["sender_id"],
            sender_name=d["sender_name"],
            message_id=d["message_id"],
            ts=d["ts"],
            text=d["text"],
            raw={"subject": d["subject"]},
        ) for d in rows]
=======
        return [self._inbound(d) for d in rows]
>>>>>>> feat/svc-social

    async def start(self) -> None:
        if not self._addr:
            log.error("gmail[%s] no address configured; account down",
                      self.account.name)
            return
        # Validate credentials up front; a bad app password / disabled IMAP is a
        # permanent failure (return, don't spin).
        try:
            self._baseline = (await asyncio.to_thread(self._poll_sync, 0))[1]
            log.info("gmail[%s] connected as %s (baseline uid=%d)",
                     self.account.name, self._addr, self._baseline)
        except Exception as exc:  # noqa: BLE001
            log.error("gmail[%s] IMAP login failed; account down: %s",
                      self.account.name, exc)
            return

        backoff = 1.0
        while True:
            try:
                await asyncio.sleep(POLL_INTERVAL_S)
                msgs, self._baseline = await asyncio.to_thread(
                    self._poll_sync, self._baseline)
                backoff = 1.0
                for d in msgs:
                    inbound = self._inbound(d)
                    try:
                        await self.on_message(inbound)
                    except Exception as exc:  # noqa: BLE001 — one bad msg never kills the loop
                        log.warning("gmail[%s] on_message failed: %s",
                                    self.account.name, exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("gmail[%s] poll error (%s); retry in %.1fs",
                            self.account.name, exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    # -- SMTP send ----------------------------------------------------------

    def _send_sync(self, channel: str, text: str, thread: str | None) -> dict:
        subject, body = _split_subject(text)
        msg = EmailMessage()
        msg["From"] = self._addr
        msg["To"] = channel
        msg["Subject"] = subject
        msg_id = email.utils.make_msgid()
        msg["Message-ID"] = msg_id
        if thread:
            msg["In-Reply-To"] = thread
            msg["References"] = thread
        msg.set_content(body)
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as s:
            s.login(self._addr, self._pw)
            s.send_message(msg)
        return {
            "message_id": msg_id,
            "channel_id": channel,
            "ts": email.utils.formatdate(),
        }

    async def send(
        self, channel: str, text: str, *, thread: str | None = None
    ) -> dict:
        return await asyncio.to_thread(self._send_sync, channel, text, thread)

    async def list_channels(self, *, include_dms: bool = False) -> list[Channel]:
        # Gmail has no DM concept — mailboxes are the only "channels"; the flag
        # is accepted for ABC parity and ignored.
        def _list() -> list[Channel]:
            m = self._imap_login()
            try:
                typ, data = m.list()
                out: list[Channel] = []
                if typ == "OK":
                    for raw in data:
                        line = raw.decode() if isinstance(raw, bytes) else str(raw)
                        # ``(\HasNoChildren) "/" "INBOX"`` → take the quoted name
                        name = line.split(' "/" ')[-1].strip().strip('"')
                        if name:
                            out.append(Channel(id=name, name=name, kind="mailbox"))
                return out
            finally:
                try:
                    m.logout()
                except Exception:  # noqa: BLE001
                    pass
        return await asyncio.to_thread(_list)

    async def identity(self) -> Identity:
        return Identity(id=self._addr,
                        name=self.account.display_name or self._addr)

    async def close(self) -> None:
        # Each blocking call opens + closes its own connection; nothing persists.
        return None
