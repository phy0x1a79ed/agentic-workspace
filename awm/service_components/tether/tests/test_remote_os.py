"""The owner's kind of computer is declared, never guessed, and never forgotten.

An agent that guesses hands a Windows user a line PowerShell cannot run, or
later sends bash to a PowerShell prompt. So `invite` refuses without `os`,
answers with that machine's launcher only, and every view of the session after
it carries the declaration beside what the owner's client actually reports.
"""

from __future__ import annotations

import pytest

from awm.tether import remote_os

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


@pytest.mark.parametrize("given", [None, "", "Windows", "freebsd box"])
def test_an_unknown_os_is_refused_with_the_question_to_ask(given):
    refused = remote_os.refusal(given)
    assert "Ask the owner explicitly" in refused
    # WSL is offered as its own answer, or its users say "Windows".
    assert "Windows PowerShell, WSL, Linux or Mac?" in refused
    assert "guess" in refused


@pytest.mark.parametrize("given", ["wsl", "linux", "macos", "Mac"])
def test_a_unix_flavour_is_refused_with_the_value_to_send(given):
    assert 'send `os: "unix"`' in remote_os.refusal(given)


@pytest.mark.parametrize("given", ["windows", "unix"])
def test_a_known_os_is_not_refused(given):
    assert remote_os.refusal(given) is None


def test_each_os_gets_its_own_launcher():
    sh = "curl -fsSL https://relay.test/tether | sh -s acre fox kettle"
    win = remote_os.launcher_line("windows", sh)
    assert "[scriptblock]" in win and "| sh" not in win
    assert remote_os.launcher_line("unix", sh) == sh


def test_a_session_nobody_declared_says_to_ask():
    assert "Ask the owner" in remote_os.describe(None)["warning"]


def test_only_a_windows_report_disagrees_with_unix_and_vice_versa():
    assert "mismatch" not in remote_os.describe("unix", "macos")
    assert "mismatch" not in remote_os.describe("unix", "linux")
    assert remote_os.describe("unix", "windows")["mismatch"] is True
    assert remote_os.describe("windows", "macos")["mismatch"] is True
