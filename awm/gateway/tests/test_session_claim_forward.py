"""The calling session crosses a peer redirect as a claim, never as a pid.

A seat verb (`social mount`/`unmount`) runs on the node that owns the account.
That node cannot see the caller's pid, so the proxy following the redirect names
the session from its own record, and the owner's gateway keeps that claim only on
a call that came through its edge.
"""

import json

import pytest

from awm.gateway import mcp_caller
from awm.gateway.server import _stamp_caller

pytestmark = [pytest.mark.smoke]

CLAIM = {"kind": "bg", "job": "2eb61302", "name": "opal desk"}


@pytest.fixture
def sessions(tmp_path):
    rec = {"pid": 4242, "kind": "bg", "jobId": "2eb61302", "name": "opal desk",
           "sessionId": "s"}
    (tmp_path / "4242.json").write_text(json.dumps(rec))
    return str(tmp_path)


def test_a_seat_verb_carries_the_session_claim(sessions):
    args = {"verb": "mount", "args": {"account": "a", "channel": "D1",
                                      "_caller_session": {"job": "forged"}},
            "peer": "mira"}
    out = mcp_caller.forwarded_args("social", args, "4242", sessions_dir=sessions)
    assert "peer" not in out
    assert out["args"]["_caller_session"] == CLAIM
    assert args["args"]["_caller_session"] == {"job": "forged"}


def test_the_flat_shape_carries_it_too(sessions):
    out = mcp_caller.forwarded_args("social_unmount", {"account": "a"}, "4242",
                                    sessions_dir=sessions)
    assert out["_caller_session"] == CLAIM


def test_no_record_means_no_claim_and_a_forged_one_is_dropped(sessions):
    args = {"verb": "mount", "args": {"_caller_session": CLAIM}}
    out = mcp_caller.forwarded_args("social", args, "77", sessions_dir=sessions)
    assert "_caller_session" not in out["args"]


def test_other_verbs_pass_through_untouched(sessions):
    args = {"verb": "send", "args": {"text": "hi"}, "peer": "mira"}
    assert mcp_caller.forwarded_args("social", args, "4242", sessions_dir=sessions) == {
        "verb": "send", "args": {"text": "hi"}}
    other = {"verb": "list_other"}
    assert mcp_caller.forwarded_args("reflection", other, "4242",
                                     sessions_dir=sessions) == other


def test_the_owner_keeps_a_claim_only_from_its_edge():
    local = {"verb": "mount", "args": {"_caller_session": CLAIM}}
    _stamp_caller("social", local, "2488")
    assert "_caller_session" not in local["args"]
    assert local["args"]["_caller_pid"] == 2488

    edge = {"verb": "mount", "args": {"_caller_session": CLAIM, "_caller_pid": 1}}
    _stamp_caller("social", edge, None, as_="peer:altair")
    assert edge["args"] == {"_caller_session": CLAIM}


def test_social_send_is_not_stamped():
    args = {"verb": "send", "args": {"_caller_session": CLAIM}}
    _stamp_caller("social", args, "2488")
    assert args["args"] == {"_caller_session": CLAIM}
