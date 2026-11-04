from __future__ import annotations

import json
import subprocess

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from awm.operations.approver.chain import next_head, passkey_challenge, unlock_challenge
from awm.operations.approver.core import ApproverError
from awm.operations.approver.keys import p1363_to_der
from verify_grant import GrantInvalid, canon, load_public_key, unb64u, verify_grant, verify_order

from conftest import authorize, enrol
from softauthn import SoftDevice

PUBKEY = None


def ssh_pubkey(tmp_path) -> str:
    k = tmp_path / "user"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(k)], check=True)
    return (tmp_path / "user.pub").read_text().strip()


def now(clock) -> int:
    return int(clock())


# -- enrolment ----------------------------------------------------------------------


def test_enrol_link_is_single_use(env):
    ap, _, _ = env
    link = ap.enrol_link()
    assert link["url"].startswith("https://approve.test/enrol#")
    dev, _ = enrol(ap, token=link["token"])
    assert dev.id and len(dev.head) == 64
    with pytest.raises(ApproverError) as e:
        ap.enrol_begin(link["token"], "again")
    assert e.value.code == "bad_link"


def test_enrol_link_expires(env, clock):
    ap, _, _ = env
    token = ap.enrol_link()["token"]
    clock.t += 16 * 60
    with pytest.raises(ApproverError):
        ap.enrol_begin(token, "late")


def test_ack_verifies_with_alcors_key(env, keys):
    ap, _, _ = env
    link = ap.enrol_link()
    from softauthn import SoftPasskey
    pk, dev = SoftPasskey("approve.test", "https://approve.test"), SoftDevice()
    b = ap.enrol_begin(link["token"], "x")
    out = ap.enrol_finish(link["token"], b["reg_id"], pk.create(b["options"]), dev.pub, "x")
    ack = dict(out["ack"])
    sig = unb64u(ack.pop("sig"))
    pub = serialization.load_der_public_key(unb64u(out["ack_pub"]))
    pub.verify(p1363_to_der(sig), canon(ack), ec.ECDSA(hashes.SHA256()))


# -- the chain ----------------------------------------------------------------------


async def test_batch_request_runs_each_action(env, clock):
    ap, store, _ = env
    dev, pk = enrol(ap)
    out = await authorize(ap, dev, pk, now(clock), batch=[
        {"action": "note_new", "args": {"title": "Look at the tunnel"}},
        {"action": "note_new", "args": {"title": "Second idea"}}])
    results = out["result"]["batch"]
    assert [r["ok"] for r in results] == [True, True]
    tid = results[0]["result"]["task_id"]
    assert store.task_get(tid)["section"] == "proposed"


async def test_request_needs_a_passkey_bound_to_the_head(env, clock):
    ap, _, _ = env
    dev, pk = enrol(ap)
    req = {"batch": [{"action": "note_new", "args": {"title": "a"}}]}
    m, s = dev.message("authorize", req, now(clock))
    with pytest.raises(ApproverError) as e:
        await ap.act(m, s, None)
    assert e.value.code == "need_passkey"
    other = {"batch": [{"action": "note_new", "args": {"title": "b"}}]}
    with pytest.raises(ApproverError) as e:
        await ap.act(m, s, pk.get(passkey_challenge(dev.head, other)))
    assert e.value.code == "bad_passkey"


async def test_replay_is_refused(env, clock):
    ap, _, _ = env
    dev, pk = enrol(ap)
    req = {"batch": [{"action": "note_new", "args": {"title": "a"}}]}
    m, s = dev.message("authorize", req, now(clock))
    a = pk.get(passkey_challenge(dev.head, req))
    out = await ap.act(m, s, a)
    assert out["ack"]["head"] == next_head(m["prev"], m, unb64u(s))
    with pytest.raises(ApproverError) as e:
        await ap.act(m, s, a)
    assert e.value.code == "stale_head"


async def test_swapped_message_is_refused(env, clock):
    ap, _, _ = env
    dev, pk = enrol(ap)
    await authorize(ap, dev, pk, now(clock), window={"kinds": ["close", "reject"],
                                                      "cards": "any", "minutes": 10})
    m, s = dev.message("close", {"task_id": "AAAAAAAA"}, now(clock))
    swapped = {**m, "action": "reject"}
    with pytest.raises(ApproverError) as e:
        await ap.act(swapped, s)
    assert e.value.code == "bad_signature"


async def test_stale_head_and_second_action_before_ack(env, clock):
    ap, store, _ = env
    dev, pk = enrol(ap)
    await authorize(ap, dev, pk, now(clock), window={"kinds": ["note_new"], "cards": "any",
                                                      "minutes": 10})
    acked = dev.head
    m1, s1 = dev.message("note_new", {"title": "one"}, now(clock))
    await ap.act(m1, s1)              # the ack never reaches the device
    m2, s2 = dev.message("note_new", {"title": "two"}, now(clock), prev=acked)
    with pytest.raises(ApproverError) as e:
        await ap.act(m2, s2)
    assert e.value.code == "stale_head"


async def test_out_of_window_action_needs_a_passkey_and_keeps_the_head(env, clock):
    ap, store, _ = env
    dev, pk = enrol(ap)
    note = await authorize(ap, dev, pk, now(clock), batch=[
        {"action": "note_new", "args": {"title": "a"}}])
    tid = note["result"]["batch"][0]["result"]["task_id"]
    await authorize(ap, dev, pk, now(clock), window={"kinds": ["close"], "cards": [tid],
                                                      "minutes": 10})
    head = dev.head
    m, s = dev.message("reject", {"task_id": tid}, now(clock))
    with pytest.raises(ApproverError) as e:
        await ap.act(m, s)
    assert e.value.code == "need_passkey"
    assert ap.devices()[0]["seq"] == dev.seq and dev.head == head
    m, s = dev.message("close", {"task_id": "ZZZZZZZZ"}, now(clock))
    with pytest.raises(ApproverError):
        await ap.act(m, s)
    m, s = dev.message("close", {"task_id": tid}, now(clock))
    out = await ap.act(m, s)
    dev.take(out["ack"])
    assert out["result"]["ok"] and store.task_get(tid)["status"] == "done"


async def test_window_ends(env, clock):
    ap, _, _ = env
    dev, pk = enrol(ap)
    await authorize(ap, dev, pk, now(clock), window={"kinds": ["note_new"], "cards": "any",
                                                      "minutes": 1})
    clock.t += 61
    m, s = dev.message("note_new", {"title": "late"}, now(clock))
    with pytest.raises(ApproverError) as e:
        await ap.act(m, s)
    assert e.value.code == "need_passkey"


@pytest.mark.parametrize("signal", ["network", "rate", "new device"])
async def test_risk_closes_the_window(env, clock, signal):
    ap, _, _ = env
    dev, pk = enrol(ap)
    await authorize(ap, dev, pk, now(clock), window={"kinds": ["note_new"], "cards": "any",
                                                      "minutes": 60}, ip="203.0.113.5")
    ip = "203.0.113.5"
    if signal == "network":
        ip = "198.51.100.7"
    elif signal == "new device":
        enrol(ap, "laptop")
    elif signal == "rate":
        for i in range(4):
            m, s = dev.message("note_new", {"title": str(i)}, now(clock))
            dev.take((await ap.act(m, s, ip=ip))["ack"])
    m, s = dev.message("note_new", {"title": "x"}, now(clock))
    with pytest.raises(ApproverError) as e:
        await ap.act(m, s, ip=ip)
    assert e.value.code == "need_passkey"
    assert ap.windows(dev.id)[0]["closed_reason"] == signal
    await authorize(ap, dev, pk, now(clock), batch=[{"action": "note_new", "args": {"title": "y"}}],
                    ip=ip)


async def test_unknown_address_never_counts_as_trusted(env, clock):
    ap, _, _ = env
    dev, pk = enrol(ap)
    out = await authorize(ap, dev, pk, now(clock), window={"kinds": ["note_new"], "cards": "any",
                                                            "minutes": 5})
    assert out["result"]["window"]["network_check"] is False
    out = await authorize(ap, dev, pk, now(clock), window={"kinds": ["note_new"], "cards": "any",
                                                            "minutes": 5}, ip="203.0.113.5")
    m, s = dev.message("note_new", {"title": "x"}, now(clock))
    with pytest.raises(ApproverError):
        await ap.act(m, s, ip=None)


# -- drop recovery (R65 item 4) ---------------------------------------------------------


async def _window(ap, dev, pk, clock):
    await authorize(ap, dev, pk, now(clock), window={"kinds": ["note_new"], "cards": "any",
                                                      "minutes": 30})


def _resume_sig(dev, acked, ts):
    return dev.sign({"device": dev.id, "acked_head": acked, "ts": ts})


async def test_resume_resends_a_lost_ack(env, clock):
    ap, _, _ = env
    dev, pk = enrol(ap)
    await _window(ap, dev, pk, clock)
    m, s = dev.message("note_new", {"title": "a"}, now(clock))
    first = await ap.act(m, s)
    out = await ap.resume(dev.id, dev.head, now(clock), _resume_sig(dev, dev.head, now(clock)),
                          {"message": m, "sig": s})
    assert out["state"] == "acked" and out["ack"] == first["ack"]
    assert out["result"]["ok"]


async def test_resume_takes_a_lost_message(env, clock):
    ap, store, _ = env
    dev, pk = enrol(ap)
    await _window(ap, dev, pk, clock)
    m, s = dev.message("note_new", {"title": "lost"}, now(clock))
    clock.t += 30
    out = await ap.resume(dev.id, dev.head, now(clock), _resume_sig(dev, dev.head, now(clock)),
                          {"message": m, "sig": s})
    assert out["state"] == "taken"
    assert store.task_get(out["result"]["result"]["task_id"])["title"] == "lost"


async def test_resume_drops_an_in_flight_action_after_60_s(env, clock):
    ap, _, _ = env
    dev, pk = enrol(ap)
    await _window(ap, dev, pk, clock)
    m, s = dev.message("note_new", {"title": "old"}, now(clock))
    clock.t += 61
    out = await ap.resume(dev.id, dev.head, now(clock), _resume_sig(dev, dev.head, now(clock)),
                          {"message": m, "sig": s})
    assert out["state"] == "in_sync"
    assert ap.devices()[0]["seq"] == dev.seq


async def test_mismatch_locks_until_a_passkey(env, clock):
    ap, _, _ = env
    dev, pk = enrol(ap)
    bogus = "ab" * 32
    out = await ap.resume(dev.id, bogus, now(clock), _resume_sig(dev, bogus, now(clock)))
    assert out["state"] == "locked"
    nonce = out["nonce"]
    m, s = dev.message("note_new", {"title": "x"}, now(clock))
    with pytest.raises(ApproverError) as e:
        await ap.act(m, s)
    assert e.value.code == "locked"
    with pytest.raises(ApproverError):
        await ap.unlock(dev.id, pk.get(unlock_challenge(dev.id, nonce)), dev.sign({"x": 1}))
    out = await ap.unlock(dev.id, pk.get(unlock_challenge(dev.id, nonce)),
                          dev.sign({"device": dev.id, "nonce": nonce}))
    dev.take(out["ack"])
    assert dev.seq == 0
    await authorize(ap, dev, pk, now(clock), batch=[{"action": "note_new", "args": {"title": "ok"}}])


async def test_resume_needs_the_device_key(env, clock):
    ap, _, _ = env
    dev, _ = enrol(ap)
    with pytest.raises(ApproverError):
        await ap.resume(dev.id, "ab" * 32, now(clock), SoftDevice().sign({"device": dev.id}))
    assert not ap.devices()[0]["locked"]


# -- approval requests and grants (fleet.md R97) ----------------------------------------------


async def test_admin_ssh_grant_and_certificate(env, clock, keys, tmp_path):
    ap, store, _ = env
    dev, pk = enrol(ap)
    pub = ssh_pubkey(tmp_path)
    req = await ap.approval_request("admin_ssh", reason="fix sshd", minutes=30,
                                    payload={"pubkey": pub, "nodes": ["capella", "altair"]},
                                    requester_session="cc-123")
    card = store.task_get(req["task_id"])
    assert (card["section"], card["status"]) == ("needs_you", "approval")
    out = await authorize(ap, dev, pk, now(clock), batch=[
        {"action": "approve", "args": {"request_id": req["request_id"]}}])
    assert out["result"]["batch"][0]["ok"], out
    got = ap.approval_get(req["request_id"])
    assert got["status"] == "approved"
    gpub = load_public_key(keys.grant_pub_b64u())
    body = verify_grant(got["grant"], gpub, kind="admin_ssh", node="altair", now=clock())
    assert body["not_after"] - body["not_before"] == 30 * 60
    assert body["approved_by_device"] == dev.id
    with pytest.raises(GrantInvalid):
        verify_grant(got["grant"], gpub, node="deneb", now=clock())
    tampered = json.loads(json.dumps(got["grant"]))
    tampered["grant"]["not_after"] += 1
    with pytest.raises(GrantInvalid):
        verify_grant(tampered, gpub, now=clock())
    assert got["ssh_cert"] == got["cert"]
    cert = tmp_path / "user-cert.pub"
    cert.write_text(got["cert"] + "\n")
    shown = subprocess.run(["ssh-keygen", "-L", "-f", str(cert)], capture_output=True,
                           text=True, check=True).stdout
    assert "admin@altair" in shown and "admin@capella" in shown
    assert body["grant_id"] in shown
    assert store.task_get(req["task_id"])["status"] == "approved"


async def test_deny_wait_cancel_and_expiry(env, clock):
    ap, store, _ = env
    dev, pk = enrol(ap)
    payload = {"session_id": "cc-1", "from": "low", "to": "high"}
    r1 = await ap.approval_request("trust_raise", node="mizar", reason="r", payload=payload)
    await authorize(ap, dev, pk, now(clock), batch=[
        {"action": "deny", "args": {"request_id": r1["request_id"]}}])
    got = await ap.approval_wait(r1["request_id"], 1)
    assert got["status"] == "denied" and got["grant"] is None
    r2 = await ap.approval_request("trust_raise", node="mizar", reason="r", payload=payload,
                                   requester_session="cc" + "1" * 32)
    for who in (None, "cc" + "2" * 32):
        with pytest.raises(ApproverError) as e:
            await ap.approval_cancel(r2["request_id"], who)
        assert e.value.code == "not_requester"
    await ap.approval_cancel(r2["request_id"], "cc" + "1" * 32)
    assert store.task_get(r2["task_id"])["status"] == "cancelled"
    r3 = await ap.approval_request("trust_raise", node="mizar", reason="r", payload=payload)
    clock.t += 24 * 3600 + 1
    assert ap.approval_get(r3["request_id"])["status"] == "expired"
    await ap.sweep()
    assert store.task_get(r3["task_id"])["status"] == "expired"
    clock.t = clock.t  # decided requests stay decided
    with pytest.raises(ApproverError):
        await ap.decide(r3["request_id"], True, dev.id)


async def test_vault_hold_needs_no_payload(env, clock):
    ap, store, _ = env
    dev, pk = enrol(ap)
    r = await ap.approval_request("vault_hold", node="deneb", reason="checks tripped")
    await authorize(ap, dev, pk, now(clock), batch=[
        {"action": "approve", "args": {"request_id": r["request_id"]}}])
    assert ap.approval_get(r["request_id"])["status"] == "approved"


async def test_request_validation(env):
    ap, _, _ = env
    for kind, kw in [("nope", {}), ("admin_ssh", {"payload": {"pubkey": "x", "nodes": ["a"]}}),
                     ("admin_ssh", {"payload": {"pubkey": "ssh-ed25519 AAAA", "nodes": []}}),
                     ("trust_raise", {"minutes": 5, "payload": {"session_id": 1, "from": 1,
                                                                "to": 2}})]:
        with pytest.raises(ApproverError):
            await ap.approval_request(kind, reason="r", **kw)


async def test_waiting_request_wakes_on_decision(env, clock):
    import asyncio
    ap, _, _ = env
    dev, pk = enrol(ap)
    r = await ap.approval_request("browser_signin", reason="r",
                                  payload={"seat": "s1", "site": "github"})
    waiter = asyncio.create_task(ap.approval_wait(r["request_id"], 30))
    await asyncio.sleep(0.01)
    await authorize(ap, dev, pk, now(clock), batch=[
        {"action": "approve", "args": {"request_id": r["request_id"]}}])
    got = await asyncio.wait_for(waiter, 2)
    assert got["status"] == "approved"


# -- orders ------------------------------------------------------------------------------------


async def test_spawn_issues_an_order_the_peer_verifies(env, clock, keys):
    ap, store, _ = env
    dev, pk = enrol(ap)
    out = await authorize(ap, dev, pk, now(clock), batch=[
        {"action": "note_new", "args": {"title": "Rework the tunnel"}}])
    tid = out["result"]["batch"][0]["result"]["task_id"]
    out = await authorize(ap, dev, pk, now(clock), batch=[
        {"action": "spawn", "args": {"task_id": tid, "peer": "deneb"}}])
    res = out["result"]["batch"][0]
    assert res["ok"], res
    assert store.task_get(tid)["section"] == "working"
    envs = await ap.order_pull("deneb")
    gpub = load_public_key(keys.grant_pub_b64u())
    body = verify_order(envs[0], gpub, node="deneb", now=clock())
    assert body["kind"] == "spawn" and body["payload"]["task_id"] == tid
    with pytest.raises(GrantInvalid):
        verify_order(envs[0], gpub, node="altair", now=clock())
    assert ap.order_done(body["order_id"], True, "started")["recorded"]
    assert await ap.order_pull("deneb") == []


async def test_order_button_respects_allowed_orders(env, clock):
    ap, _, _ = env
    dev, pk = enrol(ap)
    out = await authorize(ap, dev, pk, now(clock), batch=[
        {"action": "order", "args": {"kind": "zotero_sync", "node": "deneb"}},
        {"action": "order", "args": {"kind": "zotero_sync", "node": "altair"}},
        {"action": "order", "args": {"kind": "reboot", "node": "deneb"}}])
    oks = [r["ok"] for r in out["result"]["batch"]]
    assert oks == [True, False, False]
    assert len(await ap.order_pull("deneb", ["zotero_sync"])) == 1


async def test_order_pull_long_polls(env):
    import asyncio
    ap, _, _ = env
    waiter = asyncio.create_task(ap.order_pull("deneb", wait_s=5))
    await asyncio.sleep(0.01)
    ap.issue_order("zotero_sync", "deneb", {})
    assert len(await asyncio.wait_for(waiter, 2)) == 1


# -- the 2fa strip (R137) ------------------------------------------------------------------------


async def test_twofa_actions_reach_the_2fa_verbs(env, clock):
    ap, _, tools = env
    dev, pk = enrol(ap)
    out = await authorize(ap, dev, pk, now(clock), batch=[
        {"action": "twofa_burst", "args": {"device": "phone", "count": 2}},
        {"action": "twofa_burst", "args": {"device": "phone", "count": 3}},
        {"action": "twofa_approve_all", "args": {"device": "phone"}},
        {"action": "twofa_approve", "args": {"device": "phone", "urgid": "u1"}},
        {"action": "twofa_deny", "args": {"device": "phone", "urgid": "u2"}}])
    assert [r["ok"] for r in out["result"]["batch"]] == [True, False, True, True, True]
    assert [c[0] for c in tools.twofa_calls] == ["burst", "approve_all", "approve", "deny"]
    state = await ap.twofa_state()
    assert state["pending"]["phone"][0]["urgid"] == "u1"


# -- a second device, by approval from a trusted one (R64 item 1) ----------------------------------


async def test_second_device_enrols_by_approval(env, clock):
    ap, _, _ = env
    dev, pk = enrol(ap)
    ask = await ap.enrol_ask("laptop")
    assert ap.enrol_ask_status(ask["request_id"], ask["poll_secret"])["token"] is None
    await authorize(ap, dev, pk, now(clock), batch=[
        {"action": "approve", "args": {"request_id": ask["request_id"]}}])
    st = ap.enrol_ask_status(ask["request_id"], ask["poll_secret"])
    assert st["status"] == "approved" and st["token"]
    assert ap.enrol_ask_status(ask["request_id"], ask["poll_secret"])["token"] is None
    with pytest.raises(ApproverError):
        ap.enrol_ask_status(ask["request_id"], "wrong")
    laptop, _ = enrol(ap, "laptop", token=st["token"])
    assert [d["enrolled_by"] for d in ap.devices()] == ["link", ask["request_id"]]


async def test_revoked_device_stops(env, clock):
    ap, _, _ = env
    dev, pk = enrol(ap)
    ap.device_revoke(dev.id)
    m, s = dev.message("note_new", {"title": "x"}, now(clock))
    with pytest.raises(ApproverError) as e:
        await ap.act(m, s)
    assert e.value.code == "unknown_device"


# -- portal login (fleet.md R84) ----------------------------------------------------------------


def test_portal_login_grant(env, clock, keys):
    ap, _, _ = env
    dev, pk = enrol(ap)
    b = ap.login_begin("vault", "deneb", "https://vault.test/after")
    from awm.operations.approver.core import Approver
    ch = Approver._login_challenge(b["nonce"], "vault", "deneb", "https://vault.test/after")
    out = ap.login_finish(b["nonce"], pk.get(ch))
    body = verify_grant(out["grant"], load_public_key(keys.grant_pub_b64u()),
                        kind="portal_login", node="deneb", now=clock())
    assert body["approved_by_device"] == dev.id
    with pytest.raises(ApproverError):
        ap.login_finish(b["nonce"], pk.get(ch))


async def test_plugin_commits_grant_names_every_commit(env, clock):
    """One passkey approves every plugin commit of one change (install.md R116 item 2)."""
    ap, _, _ = env
    dev, pk = enrol(ap)
    a, b = "a" * 40, "b" * 40
    with pytest.raises(ApproverError):
        await ap.approval_request("plugin_commits", reason="bad", payload={
            "change": "c1", "plugins": [{"plugin": "zotero", "commit": "abc"}]})
    with pytest.raises(ApproverError):
        await ap.approval_request("plugin_commits", reason="dup", payload={
            "change": "c1", "plugins": [{"plugin": "zotero", "commit": a},
                                        {"plugin": "zotero", "commit": b}]})
    r = await ap.approval_request("plugin_commits", reason="zotero sync and a new engcyc",
                                  payload={"change": "c1", "plugins": [
                                      {"plugin": "zotero", "commit": b, "from": a},
                                      {"plugin": "engcyc", "commit": a,
                                       "source": "git@example:engcyc.git"}]})
    await authorize(ap, dev, pk, now(clock), batch=[
        {"action": "approve", "args": {"request_id": r["request_id"]}}])
    body = ap.approval_get(r["request_id"])["grant"]["grant"]
    assert body["kind"] == "plugin_commits"
    assert body["commits"] == [f"engcyc@{a}", f"zotero@{b}"]


async def test_a_relay_minted_request_id_is_kept(env, clock):
    ap, _, _ = env
    r = await ap.approval_request("vault_hold", node="deneb", reason="r", request_id="q" + "A" * 12)
    assert r["request_id"] == "q" + "A" * 12
    with pytest.raises(ApproverError):
        await ap.approval_request("vault_hold", node="deneb", reason="r", request_id="q" + "A" * 12)
    with pytest.raises(ApproverError):
        await ap.approval_request("vault_hold", node="deneb", reason="r", request_id="bad")
