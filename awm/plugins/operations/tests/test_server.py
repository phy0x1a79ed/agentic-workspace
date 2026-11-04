from __future__ import annotations

import asyncio
import time

from fastapi.testclient import TestClient

from awm.operations import cli
from awm.operations.approver.chain import passkey_challenge
from awm.operations.hub_adapter import control_socket
from awm.operations.approver.server import create_app

from conftest import enrol


def signed_get(client, dev, path):
    ts = int(time.time())
    return client.get(path, headers={"X-Awm-Device": dev.id, "X-Awm-Ts": str(ts),
                                     "X-Awm-Sig": dev.sign({"device": dev.id, "path": path,
                                                            "ts": ts})})


def test_http_flow_and_headers(env):
    ap, store, _ = env
    visits = []

    async def usage(page):
        visits.append(page)

    client = TestClient(create_app(ap, board_origins=["https://vault.test"], usage=usage))
    r = client.get("/api/keys")
    assert r.json()["rp_id"] == "approve.test"
    assert "frame-ancestors 'self' https://vault.test" in r.headers["content-security-policy"]
    assert 'publickey-credentials-get=(self "https://vault.test")' in r.headers["permissions-policy"]

    dev, pk = enrol(ap)
    req = {"batch": [{"action": "note_new", "args": {"title": "from http"}}]}
    opts = client.post("/api/passkey/options", json={"prev": dev.head, "request": req}).json()
    assert opts["userVerification"] == "required"
    m, s = dev.message("authorize", req, int(time.time()))
    r = client.post("/api/act", json={"message": m, "sig": s,
                                      "assertion": pk.get(passkey_challenge(dev.head, req))})
    assert r.status_code == 200, r.text
    dev.take(r.json()["ack"])
    tid = r.json()["result"]["batch"][0]["result"]["task_id"]

    r = client.post("/api/act", json={"message": m, "sig": s})
    assert r.status_code == 409 and r.json()["code"] == "stale_head"

    m, s = dev.message("note_new", {"title": "no window"}, int(time.time()))
    r = client.post("/api/act", json={"message": m, "sig": s})
    assert r.status_code == 401 and r.json()["code"] == "need_passkey"

    assert signed_get(client, dev, f"/api/card/{tid}").json()["title"] == "from http"
    bad = client.get(f"/api/card/{tid}", headers={"X-Awm-Device": dev.id, "X-Awm-Ts": "0",
                                                  "X-Awm-Sig": "AA"})
    assert bad.status_code == 403
    assert signed_get(client, dev, "/api/twofa").json()["devices"] == [{"name": "phone"}]


async def test_control_socket_prints_an_enrol_link(env, tmp_path, monkeypatch, capsys):
    ap, _, _ = env
    monkeypatch.setenv("AWM_APPROVER_DIR", str(tmp_path))
    server = await control_socket(ap, tmp_path / "control.sock")
    assert (tmp_path / "control.sock").stat().st_mode & 0o077 == 0
    rc = await asyncio.to_thread(cli.main, ["enrol-link"])
    server.close()
    url = capsys.readouterr().out.strip()
    assert rc == 0 and url.startswith("https://approve.test/enrol#")
    enrol(ap, token=url.split("#", 1)[1])
