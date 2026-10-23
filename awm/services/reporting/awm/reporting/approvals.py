"""Arbutus admin approvals: an agent asks through ``request_human``, an admin answers in a Slack thread.

The agent names a bundle it wrote for ``~/.ssh/awm-admin-approve``. The front door
freezes a copy under ``~/.ssh/admin-approvals/pending/<ref>/``, where the admin-gate
hook keeps every Claude session out, and posts the bundle to the approval channel.
An admin's ``approve`` or ``deny`` in that post's thread decides it. Approving runs
the approve script on the frozen copy, with the admin's name as the approver. No
verb grants an approval: only a message event from an admin's own hand does, and
social flags as ``relayed`` any message it sent through a person's account.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from awm import gatewayclient

from awm.reporting import config
from awm.reporting.intake import send_social

log = logging.getLogger("awm.reporting.approvals")

SOCIAL_TOPIC = "message"
NAME = re.compile(r"^[A-Za-z0-9._-]+$")
EXPIRES_S = 24 * 3600
# Only these words decide: a casual "ok, checking" in the thread must not approve.
APPROVE = {"approve", "approved"}
DENY = {"deny", "denied", "reject"}
PENDING, APPROVED, DENIED, EXPIRED, REFUSED, FAILED = (
    "pending", "approved", "denied", "expired", "refused", "failed")
RUN_TIMEOUT_S = 60.0


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_bundle(text: str) -> dict[str, Any]:
    """The bundle's reason, minutes, ssh targets and tf changes. The approve script is the real check."""
    out: dict[str, Any] = {"reason": "", "minutes": "", "ssh": [], "tf": []}
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        fields = [f for f in line.split("\t") if f]
        key = fields[0]
        if key in ("reason", "minutes") and len(fields) == 2 and not out[key]:
            out[key] = fields[1]
        elif key == "ssh" and len(fields) == 2:
            out["ssh"].append(fields[1])
        elif key == "tf" and len(fields) == 3:
            out["tf"].append(f"{fields[1]} {fields[2]}")
        else:
            raise ValueError(f"malformed bundle line: {line!r}")
    if not out["reason"]:
        raise ValueError("the bundle names no reason")
    if not out["minutes"].isdigit() or not 1 <= int(out["minutes"]) <= 480:
        raise ValueError(f"minutes must be 1 to 480, not {out['minutes']!r}")
    return out


def _slack(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def request_text(req: dict) -> str:
    lines = [f"*Admin approval request* `{req['ref']}` from {_slack(req['agent'])} on {req['node']}",
             f"> {_slack(req['reason'])}",
             f"For {req['minutes']} minutes:"]
    lines += [f"• admin ssh window on `{_slack(t)}`" for t in req["ssh"]]
    lines += [f"• terraform `{_slack(c)}`" for c in req["tf"]]
    if not req["ssh"] and not req["tf"]:
        lines.append("• nothing beyond the record itself")
    lines.append(f"Bundle `{req['name']}`, sha256 `{req['sha256'][:12]}`.")
    lines.append("Tony or Ryan: reply `approve` or `deny` in this thread.")
    return "\n".join(lines)


def _word(text: str) -> str:
    first = (text or "").strip().split(maxsplit=1)
    return re.sub(r"[^a-z]", "", first[0].lower()) if first else ""


async def run_approve(name: str, bundles: Path, approved_by: str) -> tuple[int, str]:
    env = {**os.environ, "ARB_BUNDLES": str(bundles), "ARB_APPROVED_BY": approved_by}
    proc = await asyncio.create_subprocess_exec(
        str(config.admin_approve_cmd()), name, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), RUN_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "the approve script did not finish in time"
    return proc.returncode or 0, out.decode(errors="replace").strip()


def _send_line(job: str, text: str) -> None:
    from awm.claudedaemon.job import send_line

    send_line(job, text)


class Approvals:
    """The pending requests on this node and the handler for their Slack threads."""

    def __init__(self, *, post: Callable[..., Awaitable[dict]] = send_social,
                 run: Callable[[str, Path, str], Awaitable[tuple[int, str]]] = run_approve,
                 tell: Callable[[str, str], None] = _send_line,
                 on_decided: Callable[[int, str], Awaitable[None]] | None = None) -> None:
        self._post = post
        self._run = run
        self._tell = tell
        self.on_decided = on_decided
        self._lock = asyncio.Lock()
        self.subscription: gatewayclient.SupervisedSubscription | None = None

    # -- requests ------------------------------------------------------------

    def _dir(self, ref: str) -> Path:
        return config.approval_pending_dir() / ref

    def _save(self, req: dict) -> None:
        path = self._dir(req["ref"]) / "request.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(req, indent=1))
        tmp.replace(path)

    def _requests(self) -> list[dict]:
        root = config.approval_pending_dir()
        out = []
        for path in sorted(root.glob("*/request.json")) if root.is_dir() else []:
            try:
                out.append(json.loads(path.read_text()))
            except (OSError, ValueError) as exc:
                log.warning("reporting: unreadable approval request %s: %s", path, exc)
        return out

    async def open(self, bundle: str, *, agent: str, node: str, job: str) -> dict:
        """Freeze the named bundle, post it, and record the request. Grants nothing."""
        target = config.approval_channel()
        if target is None:
            raise ValueError("this node has no approval channel: set AWM_APPROVAL_CHANNEL")
        if not NAME.match(bundle or ""):
            raise ValueError("bundle must name a bundle file: letters, digits, '.', '_' and '-'")
        src = config.approval_bundles_dir() / f"{bundle}.bundle"
        if not src.is_file():
            raise ValueError(f"no bundle at {src}")
        parsed = parse_bundle(src.read_text())
        ref = secrets.token_hex(3)
        frozen = self._dir(ref)
        frozen.mkdir(mode=0o700, parents=True)
        os.chmod(frozen.parent, 0o700)
        shutil.copyfile(src, frozen / src.name)
        account, channel = target
        req = {"ref": ref, "name": bundle, "sha256": _sha256(frozen / src.name), **parsed,
               "agent": agent, "node": node, "job": job, "task": None,
               "account": account, "channel": channel, "post_ts": "",
               "created": time.time(), "state": PENDING}
        try:
            sent = await self._post(account, channel, request_text(req), quiet=True)
        except Exception:
            shutil.rmtree(frozen, ignore_errors=True)
            raise
        req["post_ts"] = str(sent.get("ts") or sent.get("message_id") or "")
        req["channel"] = str(sent.get("channel_id") or channel)
        self._save(req)
        return req

    def attach(self, ref: str, task_id: int) -> None:
        req = json.loads((self._dir(ref) / "request.json").read_text())
        req["task"] = task_id
        self._save(req)

    # -- answers -------------------------------------------------------------

    async def run(self) -> None:
        """Consume social's message topic. Never returns."""
        self.subscription = gatewayclient.SupervisedSubscription(
            "reporting/social.message",
            lambda: gatewayclient.subscribe_maybe_peer(
                gatewayclient.peer_env("AWM_SOCIAL_PEER"), "social", SOCIAL_TOPIC),
            self.handle)
        await self.subscription.run()

    async def handle(self, event: Any) -> None:
        """One message event: decide the request whose thread it answers, if any."""
        if not isinstance(event, dict) or not event.get("thread_id"):
            return
        word = _word(str(event.get("text") or ""))
        if word not in APPROVE | DENY:
            return
        async with self._lock:
            req = next((r for r in self._requests()
                        if r.get("post_ts") == event["thread_id"]
                        and r.get("channel") == event.get("channel_id")
                        and r.get("account") == event.get("account")), None)
            if req is None:
                return
            text = await self._decide(req, event, word in APPROVE)
        if text:
            await self._reply(req, text)

    async def _decide(self, req: dict, event: dict, approve: bool) -> str | None:
        """Decide one request and return the thread reply, or None for a message to ignore."""
        if event.get("relayed") or event.get("sender_bot"):
            log.warning("reporting: ignored a relayed or bot %r on approval %s", event.get("text"), req["ref"])
            return None
        admin = config.approval_admins().get(str(event.get("sender_id") or ""))
        if admin is None:
            return (f"Only an Arbutus admin can decide this. {_slack(str(event.get('sender_name') or ''))} "
                    f"(`{event.get('sender_id')}`) is not on the list.")
        if req["state"] != PENDING:
            return f"`{req['ref']}` is already {req['state']}."
        if time.time() - req["created"] > EXPIRES_S:
            return await self._close(req, EXPIRED, admin, "This request expired. The agent can ask again.")
        frozen = self._dir(req["ref"]) / f"{req['name']}.bundle"
        if not frozen.is_file() or _sha256(frozen) != req["sha256"]:
            return await self._close(req, REFUSED, admin, "The frozen bundle changed after it was posted. Nothing was approved.")
        if not approve:
            return await self._close(req, DENIED, admin, f"Denied by {admin}.")
        rc, output = await self._run(req["name"], frozen.parent, f"{admin} via Slack")
        if rc != 0:
            return await self._close(req, FAILED, admin, f"The approve script failed (exit {rc}):\n```{_slack(output)}```")
        return await self._close(req, APPROVED, admin, f"Approved by {admin}.\n```{_slack(output)}```")

    async def _close(self, req: dict, state: str, admin: str, text: str) -> str:
        req.update(state=state, decided_by=admin, decided_at=time.time())
        self._save(req)
        line = (f"Admin approval {req['name']} (ref {req['ref']}) is {state}, by {admin} in Slack. "
                + ("Run scripts/tf --approval " + req["name"] + " for its terraform changes."
                   if state == APPROVED else "Nothing was granted."))
        if req.get("job"):
            try:
                await asyncio.to_thread(self._tell, req["job"], line)
            except Exception as exc:  # noqa: BLE001 — a closed session misses the line
                log.info("reporting: %s did not take the approval line: %s", req["job"], exc)
        if req.get("task") and self.on_decided is not None:
            task = asyncio.ensure_future(self.on_decided(int(req["task"]), line))
            task.add_done_callback(lambda t: t.exception() and log.warning(
                "reporting: approval %s could not move its card: %s", req["ref"], t.exception()))
        return text

    async def _reply(self, req: dict, text: str) -> None:
        try:
            await self._post(req["account"], req["channel"], text, thread=req["post_ts"])
        except Exception as exc:  # noqa: BLE001 — the decision stands either way
            log.warning("reporting: could not answer approval %s in its thread: %s", req["ref"], exc)
