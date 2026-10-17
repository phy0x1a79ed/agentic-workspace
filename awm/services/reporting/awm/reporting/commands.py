"""The Discord commands the front door consumes from ``social``: ``/reject`` and ``/new-agent``.

Social has already checked the sender against its operators table, so a command
that reaches this module comes from Tony. Each command is answered in the
channel it came from, with the outcome or the reason it failed. A command whose
ack social kept (``ack_ref``) has that ack replaced by the answer.
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
REPLY_HINT = ("Tony asked from Discord. Answer him there with social(verb=\"send\", "
              "args={{account: \"{account}\", channel: \"{channel}\", text: \"<text>\"}}).")
# The answer quotes this much of the prompt and attaches the rest as a file.
PREVIEW_LINES = 3
PREVIEW_CHARS = 300
PROMPT_FILE = "prompt.txt"


def _new_agent_prompt(name: str, prompt: str, answer_to: tuple[str, str] | None) -> str:
    hint = RENAME_HINT.format(name=name)
    if answer_to:
        hint += " " + REPLY_HINT.format(account=answer_to[0], channel=answer_to[1])
    return f"{prompt}\n\n{hint}" if prompt else f"{hint} Wait for Tony's first instruction."


def _answer_to(event: dict) -> tuple[str, str] | None:
    """The account and channel a command is answered in, or None without both."""
    account = event.get("account")
    user = event.get("user_id")
    channel = event.get("reply_to") or (f"dm:{user}" if user else event.get("channel_id"))
    return (str(account), str(channel)) if account and channel else None


def _quoted(prompt: str) -> tuple[str, list[dict]]:
    """A block-quoted preview of ``prompt``, and the whole prompt as a file when
    the preview leaves some of it out."""
    lines = prompt.splitlines()
    if len(lines) <= PREVIEW_LINES and len(prompt) <= PREVIEW_CHARS:
        return "\n".join(f"> {line}" for line in lines), []
    preview = "\n".join(lines[:PREVIEW_LINES])[:PREVIEW_CHARS].rstrip() + "…"
    return "\n".join(f"> {line}" for line in preview.splitlines()), [
        {"filename": PROMPT_FILE, "text": prompt}]


class Commands:
    def __init__(self, fed: Federation, cx: Cx, *,
                 reply: Callable[..., Awaitable[None]] = send_social) -> None:
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

    async def _answered(self, event: dict, work: Awaitable[str | tuple[str, list[dict]]]) -> None:
        """Run a command and send its outcome back, with any files the outcome
        carries. A failure is an outcome too."""
        files: list[dict] = []
        try:
            outcome = await work
            text, files = outcome if isinstance(outcome, tuple) else (outcome, [])
        except Exception as exc:  # noqa: BLE001 — Tony hears of it either way
            log.warning("reporting: command %s failed: %s", event.get("command"), exc)
            text = f"{event.get('command')} failed: {exc}"
        await self._tell(event, text, files)

    async def _tell(self, event: dict, text: str, files: list[dict] | None = None) -> None:
        answer_to = _answer_to(event)
        if answer_to is None:
            log.warning("reporting: cannot answer a command with no account or channel")
            return
        try:
            await self._reply(*answer_to, text,
                              ack_ref=str(event.get("ack_ref") or ""), files=files or [])
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

    async def _new_agent(self, event: dict) -> tuple[str, list[dict]]:
        project, scope = config.new_agent_target()
        name = f"agent-{uuid.uuid4().hex[:4]}"
        prompt = str(event.get("prompt") or "").strip()
        spec = {"project": project, "scope": scope, "name": name, "remote_control": True,
                "started_by": "Tony from Discord",
                "prompt": _new_agent_prompt(name, prompt, _answer_to(event))}
        started = await self.cx.start(spec)
        job = started.get("job")
        if not started.get("ok"):
            outcome = f"could not start an agent: {started.get('reason') or started.get('error') or started}"
        elif started.get("link"):
            outcome = f"{name} ({project}/{scope}, job {job}): {started['link']}"
        else:
            outcome = (f"started {job}, link not ready yet: "
                       f"{started.get('link_reason') or 'Remote Control did not report a link'}")
        if not prompt:
            return outcome, []
        # The answer replaces the ack, so it carries the prompt the agent was started with.
        quote, files = _quoted(prompt)
        return f"{quote}\n{outcome}", files
