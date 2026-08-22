"""Notifier interface for the approval engine.

In the original ``virtual-auth`` daemon the engine posted green/yellow embeds
(and Approve/Deny buttons) to Discord. In the awm 2fa service that surface is
replaced by the gateway verb contract (``2fa_pending`` / ``2fa_approve`` / …),
so the engine runs with a **no-op** notifier: every ``notify_*`` call is inert.

The method set is kept identical to the original ``DiscordNotifier`` so the
ported ``engine.py`` calls it unchanged — re-wiring real Discord notifications
onto the awm discord service is a deferred follow-up that only has to swap this
implementation.
"""

from __future__ import annotations

from typing import Iterable

from .duo import Transaction


class Notifier:
    """No-op notifier. Every hook the engine calls is a silent pass."""

    @property
    def enabled(self) -> bool:
        return False

    def notify_single(self, tx: Transaction) -> None:
        ...

    def notify_held(self, tx: Transaction) -> None:
        ...

    def notify_text(self, text: str, color: int = 0) -> None:
        ...

    def notify_resolved(self, tx: Transaction | None, urgid: str,
                        answer: str, by: str) -> None:
        ...

    def notify_expired(self, txs: Iterable[Transaction]) -> None:
        ...


# A single shared instance is enough — the no-op notifier holds no state.
NULL_NOTIFIER = Notifier()
