"""One bot account's synced history, as the connector sees it.

The adapter hands a :class:`History` to a connector whose ``keeps_history`` is
set; the connector records what it receives and syncs, and never touches SQL.
"""

from __future__ import annotations

from awm.social.connectors.base import InboundMessage


class History:
    def __init__(self, dao, account: str) -> None:  # noqa: ANN001
        self._dao = dao
        self.account = account

    def record(self, m: InboundMessage, *, own: bool = False,
               edited: bool = False) -> None:
        self._dao.upsert_message(self.account, {
            "channel_id": m.channel_id,
            "message_id": m.message_id,
            "thread_id": m.thread_id,
            "sender_id": m.sender_id,
            "sender_name": m.sender_name,
            "text": m.text,
            "attachments": [
                {"idx": a.idx, "filename": a.filename, "mime": a.mime,
                 "size": a.size, "url": a.url}
                for a in m.attachments],
            "ts": m.ts,
            "own": own,
        }, edited=edited)

    def mark_deleted(self, channel_id: str, message_id: str) -> None:
        self._dao.mark_deleted(self.account, channel_id, message_id)

    def synced_ts(self, channel_id: str) -> str | None:
        return self._dao.synced_ts(self.account, channel_id)

    def active_threads(self, channel_id: str, since_ts: str) -> list[str]:
        return self._dao.active_threads(self.account, channel_id, since_ts)

    def set_synced(self, channel_id: str, ts: str, channel_name: str = "") -> None:
        self._dao.set_synced(self.account, channel_id, ts, channel_name)
