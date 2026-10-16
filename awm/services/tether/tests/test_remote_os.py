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
    monkeypatch.setattr(hub_adapter, "DECLARED", {})
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


@pytest.mark.parametrize("given", [None, "", "unix", "Windows", "mac"])
async def test_an_invite_without_a_known_os_is_refused_before_minting(daemon, given):
    args = {} if given is None else {"os": given}

    answer = await hub_adapter.HANDLERS["invite"](args)

    assert answer["ok"] is False
    assert "Ask the owner" in answer["error"]
    assert "guess" in answer["error"]
    assert daemon["calls"] == [], "a session was minted on a guess"


async def test_the_gateway_refusal_for_a_missing_os_says_to_ask():
    """On the live surface the adapter's required-argument check answers before
    the handler runs, so the advice has to ride on that error too."""
    ad = ServiceAdapter("tether", hub_adapter.API_MANIFEST, hub_adapter.HANDLERS)
    ad._init_done.set()
    with pytest.raises(ValueError) as refused:
        await ad._dispatch("invite", {}, None)
    assert "missing required argument(s): os" in str(refused.value)
    assert "Ask the owner" in str(refused.value)


@pytest.mark.parametrize("os_name, line", [
    ("windows", WIN), ("wsl", SH), ("linux", SH), ("macos", SH)])
async def test_an_invite_returns_only_that_machines_launcher(daemon, os_name, line):
    answer = await hub_adapter.HANDLERS["invite"]({"os": os_name, "words": 2})

    assert answer["command"] == line
    assert answer["read_out"].endswith(line)
    other = SH if line == WIN else WIN
    assert other not in str(answer)
    assert answer["os"]["declared"] == os_name
    assert daemon["calls"] == [("invite", {"words": 2})]


async def test_status_shows_the_declared_os_and_flags_a_mismatch(daemon):
    await hub_adapter.HANDLERS["invite"]({"os": "wsl"})

    waiting = (await hub_adapter.status({}))["sessions"][0]["os"]
    assert waiting["declared"] == "wsl"
    assert "/mnt/c" in waiting["notes"]
    assert "reported" not in waiting and "mismatch" not in waiting

    # WSL runs the Linux client, so a Linux report agrees with it.
    daemon["owner"] = {"who": "ann", "host": "box", "os": "linux", "build": "x"}
    agreed = (await hub_adapter.status({}))["sessions"][0]["os"]
    assert agreed["reported"] == "linux"
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
    assert named["os"]["declared"] == "windows"
    assert named["os"]["label"] == "Windows"


async def test_macos_is_told_apart_from_linux():
    mac = remote_os.describe("macos", "macos")
    assert "BSD" in mac["notes"] and "zsh" in mac["notes"]
    assert remote_os.describe("linux", "macos")["mismatch"] is True
