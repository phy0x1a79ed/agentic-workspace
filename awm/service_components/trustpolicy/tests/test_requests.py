"""Grant request callers against a fake approver."""

from __future__ import annotations

import base64

import pytest

from awm.trustpolicy import requests as rq

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


class FakeApprover:
    def __init__(self, states):
        self.states = list(states)
        self.calls = []

    def __call__(self, verb, args):
        self.calls.append((verb, args))
        if verb == "approval_request":
            return {"request_id": "r1", "task_id": "t1"}
        if verb == "approval_wait":
            state = self.states.pop(0)
            assert args["wait_s"] <= 55
            rec = {"status": state}
            if state == "approved":
                rec["grant"] = {"grant": {"kind": "x"}, "ssh_cert": "ssh-ed25519-cert-v01@openssh.com AAAA cert"}
            return rec
        return {}


def test_minutes_bounds():
    for bad in (5, 24 * 60 + 1):
        with pytest.raises(ValueError):
            rq.request("invite", node="a", reason="r", minutes=bad, call=FakeApprover([]))


def test_a_reason_is_required():
    with pytest.raises(ValueError, match="reason"):
        rq.request("invite", node="a", reason=" ", call=FakeApprover([]))


def test_waits_through_pending_then_returns():
    fake = FakeApprover(["pending", "approved"])
    rec = rq.request("trust_raise", node="home", reason="r", call=fake)
    assert rec["status"] == "approved"
    assert [v for v, _ in fake.calls] == ["approval_request", "approval_wait", "approval_wait"]


def test_denied_raises():
    with pytest.raises(rq.GrantDenied, match="denied"):
        rq.request("invite", node="home", reason="r", call=FakeApprover(["denied"]))


def test_one_admin_grant_covers_every_node_of_a_run(tmp_path):
    key = tmp_path / "id_admin"
    key.write_text("private")
    (tmp_path / "id_admin.pub").write_text("ssh-ed25519 AAAA awm-admin\n")
    fake = FakeApprover(["approved"])
    cert = rq.admin_cert(["deneb", "alcor"], "apply the fleet config", key=key, call=fake)
    assert cert.read_text().startswith("ssh-ed25519-cert-v01@openssh.com")
    filed = fake.calls[0][1]
    assert filed["kind"] == "admin_ssh"
    assert filed["payload"] == {"pubkey": "ssh-ed25519 AAAA awm-admin", "nodes": ["alcor", "deneb"]}
    assert filed["minutes"] == 15


def test_trust_raise_payload():
    fake = FakeApprover(["approved"])
    rq.trust_raise("s1", "cooperative", "trusted", node="home", reason="r", call=fake)
    assert fake.calls[0][1]["payload"] == {"session_id": "s1", "from": "cooperative", "to": "trusted"}


def test_grant_key_reads_node_config(tmp_path):
    (tmp_path / "grant-key.pub").write_text(base64.b64encode(bytes(32)).decode())
    assert rq.grant_key(tmp_path) == bytes(32)
    (tmp_path / "grant-key.pub").write_text(base64.b64encode(bytes(31)).decode())
    with pytest.raises(ValueError):
        rq.grant_key(tmp_path)
