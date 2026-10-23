"""Admin approvals: a bundle posted to Slack, decided by an admin's reply in its thread."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from awm.reporting import config
from awm.reporting.approvals import Approvals, parse_bundle

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

TONY, RYAN, STEVEN = "U01", "U02", "U03"
BUNDLE = "reason\tGrow altair's root\nminutes\t60\nssh\tshaula-admin\ntf\tupdate\topenstack_compute_instance_v2.service[\"altair\"]\n"


@pytest.fixture(autouse=True)
def approval_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AWM_APPROVAL_CHANNEL", "slack-bot:C9")
    monkeypatch.setenv("AWM_APPROVAL_ADMINS", f"{TONY}=tony,{RYAN}=ryan")
    monkeypatch.setenv("ARB_BUNDLES", str(tmp_path / "bundles"))
    monkeypatch.setenv("AWM_APPROVAL_PENDING", str(tmp_path / "ssh" / "admin-approvals" / "pending"))
    (tmp_path / "bundles").mkdir()
    (tmp_path / "bundles" / "grow.bundle").write_text(BUNDLE)


class Harness:
    def __init__(self, rc: int = 0) -> None:
        self.posts: list[tuple[str, str, str, str]] = []
        self.quiet: list[bool] = []
        self.runs: list[tuple[str, Path, str]] = []
        self.lines: list[tuple[str, str]] = []
        self.decided: list[tuple[int, str]] = []
        self.rc = rc
        self.approvals = Approvals(post=self.post, run=self.run, tell=self.tell, on_decided=self.on_decided)

    async def post(self, account, channel, text, *, thread="", quiet=False):
        self.posts.append((account, channel, text, thread))
        self.quiet.append(quiet)
        return {"ts": "100.1", "channel_id": channel}

    async def run(self, name, bundles, approved_by):
        self.runs.append((name, bundles, approved_by))
        return self.rc, "Approved grow for 60 minutes"

    def tell(self, job, line):
        self.lines.append((job, line))

    async def on_decided(self, task_id, line):
        self.decided.append((task_id, line))

    async def open(self) -> dict:
        req = await self.approvals.open("grow", agent="infra-agent", node="altair", job="job1")
        self.approvals.attach(req["ref"], 7)
        return req

    async def reply(self, text, sender=TONY, **extra):
        await self.approvals.handle({"account": "slack-bot", "channel_id": "C9", "thread_id": "100.1",
                                     "sender_id": sender, "sender_name": "x", "text": text,
                                     "relayed": False, "sender_bot": False, **extra})

    def state(self, ref: str) -> dict:
        return json.loads((config.approval_pending_dir() / ref / "request.json").read_text())


async def test_open_freezes_and_posts():
    h = Harness()
    req = await h.open()
    frozen = config.approval_pending_dir() / req["ref"] / "grow.bundle"
    assert frozen.read_text() == BUNDLE
    assert oct(frozen.parent.stat().st_mode)[-3:] == "700"
    account, channel, text, thread = h.posts[0]
    assert (account, channel, thread) == ("slack-bot", "C9", "")
    assert h.quiet == [True]
    assert "shaula-admin" in text and "approve" in text and "&lt;" not in text
    assert h.state(req["ref"])["post_ts"] == "100.1"
    assert h.state(req["ref"])["task"] == 7


async def test_admin_approve_runs_on_the_frozen_copy():
    h = Harness()
    req = await h.open()
    await h.reply("Approve, thanks")
    name, bundles, by = h.runs[0]
    assert (name, by) == ("grow", "tony via Slack")
    assert bundles == config.approval_pending_dir() / req["ref"]
    assert h.state(req["ref"])["state"] == "approved"
    assert h.posts[-1][3] == "100.1" and "Approved by tony" in h.posts[-1][2]
    assert h.lines[0][0] == "job1" and "approved" in h.lines[0][1]


async def test_ryan_counts_and_deny_grants_nothing():
    h = Harness()
    req = await h.open()
    await h.reply("deny", sender=RYAN)
    assert h.runs == []
    assert h.state(req["ref"])["state"] == "denied"
    assert "Nothing was granted" in h.lines[0][1]


@pytest.mark.parametrize("extra", [{"relayed": True}, {"sender_bot": True}])
async def test_relayed_or_bot_message_is_ignored(extra):
    h = Harness()
    req = await h.open()
    await h.reply("approve", **extra)
    assert h.runs == [] and len(h.posts) == 1
    assert h.state(req["ref"])["state"] == "pending"


async def test_non_admin_is_told_and_nothing_runs():
    h = Harness()
    req = await h.open()
    await h.reply("approve", sender=STEVEN)
    assert h.runs == []
    assert "not on the list" in h.posts[-1][2]
    assert h.state(req["ref"])["state"] == "pending"


async def test_casual_reply_and_other_threads_are_ignored():
    h = Harness()
    await h.open()
    await h.reply("ok, checking")
    await h.approvals.handle({"account": "slack-bot", "channel_id": "C9", "thread_id": "999.9",
                              "sender_id": TONY, "text": "approve"})
    assert h.runs == [] and len(h.posts) == 1


async def test_second_answer_changes_nothing():
    h = Harness()
    req = await h.open()
    await h.reply("approve")
    await h.reply("approve", sender=RYAN)
    assert len(h.runs) == 1
    assert "already approved" in h.posts[-1][2]
    assert h.state(req["ref"])["decided_by"] == "tony"


async def test_changed_frozen_copy_is_refused():
    h = Harness()
    req = await h.open()
    (config.approval_pending_dir() / req["ref"] / "grow.bundle").write_text(BUNDLE + "ssh\t*\n")
    await h.reply("approve")
    assert h.runs == []
    assert h.state(req["ref"])["state"] == "refused"


async def test_expired_request_is_closed(monkeypatch):
    h = Harness()
    req = await h.open()
    data = h.state(req["ref"])
    data["created"] = time.time() - 2 * 86400
    (config.approval_pending_dir() / req["ref"] / "request.json").write_text(json.dumps(data))
    await h.reply("approve")
    assert h.runs == [] and h.state(req["ref"])["state"] == "expired"


async def test_failed_script_is_reported():
    h = Harness(rc=1)
    req = await h.open()
    await h.reply("approve")
    assert h.state(req["ref"])["state"] == "failed"
    assert "exit 1" in h.posts[-1][2]


async def test_open_refuses_a_bad_name_or_bundle(tmp_path):
    h = Harness()
    with pytest.raises(ValueError):
        await h.approvals.open("../etc", agent="a", node="n", job="")
    with pytest.raises(ValueError):
        await h.approvals.open("missing", agent="a", node="n", job="")
    assert h.posts == []


@pytest.mark.parametrize("text", ["minutes\t60\n", "reason\tx\nminutes\t999\n", "reason\tx\nminutes\t5\ntf\tdelete\n"])
def test_parse_bundle_refuses_what_the_script_refuses(text):
    with pytest.raises(ValueError):
        parse_bundle(text)


def test_admins_default_to_tony_on_the_tony_swarm(monkeypatch):
    monkeypatch.delenv("AWM_APPROVAL_ADMINS")
    assert config.approval_admins() == {"U01Q4CDSTFX": "tony"}
    monkeypatch.setenv("AWM_SWARM", "other")
    monkeypatch.delenv("AWM_APPROVAL_CHANNEL")
    assert config.approval_admins() == {} and config.approval_channel() is None
