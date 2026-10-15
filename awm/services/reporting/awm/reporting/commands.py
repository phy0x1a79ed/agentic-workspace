"""The Discord commands the front door consumes from ``social``: ``/reject`` and ``/new-agent``.

Social has already checked the sender against its operators table, so a command
that reaches this module comes from Tony. Each command is answered in the
channel it came from, with the outcome or the reason it failed.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Awaitable, Callable

from awm import gatewayclient

from awm.reporting import config
from awm.reporting.federation import AmbiguousRef, FedError, Federation
from awm.reporting.intake import send_social, short_ref
from awm.reporting.sessions import Cx

log = logging.getLogger("awm.reporting.commands")

SOCIAL_TOPIC = "command"
DEFAULT_REASON = "rejected by the recipient"
RENAME_HINT = ("You were started from Discord as {name}, a placeholder name. Once you know what this "
               "session is for, rename yourself to a short name for that work (2-4 words) with "
               "reflection(verb=\"rename\", args={{name: \"<name>\"}}).")


def _new_agent_prompt(name: str, prompt: str) -> str:
    hint = RENAME_HINT.format(name=name)
    return f"{prompt}\n\n{hint}" if prompt else f"{hint} Wait for Tony's first instruction."


class Commands:
    def __init__(self, fed: Federation, cx: Cx, *,
                 reply: Callable[[str, str, str], Awaitable[None]] = send_social) -> None:
        self.fed = fed
        self.cx = cx
        self._reply = reply
        self._running: set[asyncio.Task] = set()
        self.subscription: gatewayclient.SupervisedSubscription | None = None

    async def run(self) -> None:
        """Consume ``social``'s command topic. Never returns."""
        self.subscription = gatewayclient.SupervisedSubscription(
            "reporting/social.command",
            lambda: gatewayclient.subscribe_maybe_peer(
                gatewayclient.peer_env("AWM_SOCIAL_PEER"), "social", SOCIAL_TOPIC),
            self.handle)
        await self.subscription.run()

    async def handle(self, event: Any) -> None:
        """React to one command event. Commands the door does not own are ignored."""
        if not isinstance(event, dict) or not event.get("operator"):
            return
        command = event.get("command")
        if command == "reject":
            await self._answered(event, self._reject(event))
        elif command == "new_agent":
            task = asyncio.create_task(self._answered(event, self._new_agent(event)))
            self._running.add(task)
            task.add_done_callback(self._running.discard)

    async def _answered(self, event: dict, work: Awaitable[str]) -> None:
        """Run a command and send its outcome back. A failure is an outcome too."""
        try:
            text = await work
        except Exception as exc:  # noqa: BLE001 — Tony hears of it either way
            log.warning("reporting: command %s failed: %s", event.get("command"), exc)
            text = f"{event.get('command')} failed: {exc}"
        await self._tell(event, text)

    async def _tell(self, event: dict, text: str) -> None:
        account = event.get("account")
        user = event.get("user_id")
        channel = event.get("reply_to") or (f"dm:{user}" if user else event.get("channel_id"))
        if not account or not channel:
            log.warning("reporting: cannot answer a command with no account or channel")
            return
        try:
            await self._reply(str(account), str(channel), text)
        except Exception as exc:  # noqa: BLE001
            log.warning("reporting: could not answer a command: %s", exc)

    # -- /reject ---------------------------------------------------------------

    async def _reject(self, event: dict) -> str:
        ref, _, extra = str(event.get("ref") or "").strip().partition(" ")
        reason = str(event.get("reason") or "").strip() or extra.strip() or DEFAULT_REASON
        if not ref:
            return "usage: /reject <ref> [reason]"
        try:
            card = await self.fed.resolve(ref)
        except AmbiguousRef as exc:
            return str(exc)
        if card is None:
            return f"no card matches {ref}"
        try:
            await self.fed.reject(card["id"], reason)
        except FedError as exc:
            return f"could not reject {short_ref(card['id'])}: {exc}"
        return f"Rejected {short_ref(card['id'])} ({card.get('title') or 'no title'}): {reason}"

    # -- /new-agent ------------------------------------------------------------

    async def _new_agent(self, event: dict) -> str:
        project, scope = config.new_agent_target()
        name = f"agent-{uuid.uuid4().hex[:4]}"
        spec = {"project": project, "scope": scope, "name": name, "remote_control": True,
                "prompt": _new_agent_prompt(name, str(event.get("prompt") or "").strip())}
        started = await self.cx.start(spec)
        if not started.get("ok"):
            return f"could not start an agent: {started.get('reason') or started.get('error') or started}"
        job = started.get("job")
        if started.get("link"):
            return f"{name} ({project}/{scope}, job {job}): {started['link']}"
        return (f"started {job}, link not ready yet: "
                f"{started.get('link_reason') or 'Remote Control did not report a link'}")
