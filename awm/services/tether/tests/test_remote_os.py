"""The owner's kind of computer is declared, never guessed, and never forgotten.

An agent that guesses hands a Windows user a line PowerShell cannot run, or
later sends bash to a PowerShell prompt. So `invite` refuses without `os`,
answers with that machine's launcher only, and every view of the session after
it carries the declaration beside what the owner's client actually reports.
"""

from __future__ import annotations

import pytest

from awm.gatewayclient.adapter import ServiceAdapter
from awm.tether import hub_adapter, paths, remote_os

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

SH = "curl -fsSL https://relay.test/tether | sh -s acre fox kettle"
WIN = "& ([scriptblock]::Create((irm https://relay.test/tether/win))) acre fox kettle"


@pytest.fixture
def daemon(monkeypatch):
    """A daemon that mints slot 7 and then reports it, owner and all."""
    monkeypatch.setattr(paths, "ROLE", paths.OPERATOR)
    state = {"owner": None, "calls": []}

    async def call(verb, args, timeout):
        state["calls"].append((verb, dict(args)))
        if verb == "invite":
            return {"ok": True, "slot": 7, "code": "acre fox kettle",
                    "command": SH, "read_out": f"Run this: {SH}"}
        if verb == "status":
            return {"ok": True, "role": "operator", "sessions": [
                {"slot": 7, "code": "acre fox kettle", "phase": "open",
                 "owner": state["owner"]}]}
        if verb == "run":
            return {"ok": True, "task": 1, "kind": "command", "cursor": 3}
        if verb == "tasks":
            return {"ok": True, "tasks": []}
        raise AssertionError(verb)

    monkeypatch.setattr(hub_adapter.control, "call", call)
    return state


@pytest.mark.parametrize("given", [None, "", "Windows", "freebsd box"])
async def test_an_invite_without_a_known_os_is_refused_before_minting(daemon, given):
    args = {} if given is None else {"os": given}

    answer = await hub_adapter.HANDLERS["invite"](args)

    assert answer["ok"] is False
    assert "Ask the owner explicitly" in answer["error"]
    # WSL is offered as its own answer, or its users say "Windows".
    assert "Windows PowerShell, WSL, Linux or Mac?" in answer["error"]
    assert "guess" in answer["error"]
    assert daemon["calls"] == [], "a session was minted on a guess"


@pytest.mark.parametrize("given", ["wsl", "linux", "macos", "Mac"])
async def test_a_unix_flavour_is_refused_with_the_value_to_send(daemon, given):
    answer = await hub_adapter.HANDLERS["invite"]({"os": given})

    assert answer["ok"] is False
    assert 'send `os: "unix"`' in answer["error"]
    assert daemon["calls"] == []


async def test_the_gateway_refusal_for_a_missing_os_says_to_ask():
    """On the live surface the adapter's required-argument check answers before
    the handler runs, so the advice has to ride on that error too."""
    ad = ServiceAdapter("tether", hub_adapter.API_MANIFEST, hub_adapter.HANDLERS)
    ad._init_done.set()
    with pytest.raises(ValueError) as refused:
        await ad._dispatch("invite", {}, None)
    assert "missing required argument(s): os" in str(refused.value)
    assert "Windows PowerShell, WSL, Linux or Mac?" in str(refused.value)


@pytest.mark.parametrize("os_name, line", [("windows", WIN), ("unix", SH)])
async def test_an_invite_returns_only_that_machines_launcher(daemon, os_name, line):
    answer = await hub_adapter.HANDLERS["invite"]({"os": os_name, "words": 2})

    assert answer["command"] == line
    assert answer["read_out"].endswith(line)
    other = SH if line == WIN else WIN
    assert other not in str(answer)
    assert answer["os"]["declared"] == os_name
    assert daemon["calls"] == [("invite", {"words": 2})]


async def test_status_shows_the_declared_os_and_flags_a_mismatch(daemon):
    await hub_adapter.HANDLERS["invite"]({"os": "unix"})

    waiting = (await hub_adapter.status({}))["sessions"][0]["os"]
    assert waiting == {"declared": "unix", "shell": "POSIX sh"}

    # WSL runs the Linux client, so Linux and macOS reports both agree.
    daemon["owner"] = {"who": "ann", "host": "box", "os": "linux", "build": "x"}
    for platform in ("linux", "macos"):
        daemon["owner"]["os"] = platform
        agreed = (await hub_adapter.status({}))["sessions"][0]["os"]
        assert agreed["reported"] == platform
        assert "mismatch" not in agreed

    daemon["owner"]["os"] = "windows"
    wrong = (await hub_adapter.status({}))["sessions"][0]["os"]
    assert wrong["mismatch"] is True
    assert "windows" in wrong["warning"] and "ask the owner" in wrong["warning"]


async def test_a_session_nobody_declared_says_to_ask(daemon):
    report = (await hub_adapter.status({}))["sessions"][0]["os"]
    assert report["declared"] is None
    assert "Ask the owner" in report["warning"]


async def test_run_and_tasks_carry_the_sessions_os(daemon):
    await hub_adapter.HANDLERS["invite"]({"os": "windows"})

    ran = await hub_adapter.HANDLERS["run"]({"command": "Get-Date"})
    assert ran["task"] == 1
    assert ran["os"] == {"declared": "windows", "shell": "PowerShell"}

    named = await hub_adapter.HANDLERS["tasks"]({"code": "acre fox kettle"})
    assert named["os"] == {"declared": "windows", "shell": "PowerShell"}

    # Once the owner is in and their client disagrees, the short form says so
    # too, so `shell` is never read alone.
    daemon["owner"] = {"who": "ann", "host": "box", "os": "linux", "build": "x"}
    ran = await hub_adapter.HANDLERS["run"]({"command": "uname", "code": "7"})
    assert ran["os"]["reported"] == "linux"
    assert ran["os"]["mismatch"] is True


def test_only_a_windows_report_disagrees_with_unix_and_vice_versa():
    assert "mismatch" not in remote_os.describe("unix", "macos")
    assert remote_os.describe("unix", "windows")["mismatch"] is True
    assert remote_os.describe("windows", "macos")["mismatch"] is True
    assert "mismatch" not in remote_os.describe("windows", "windows")
