from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from awm.approver.chain import passkey_challenge
from awm.approver.core import Approver, Config
from awm.approver.keys import Keys
from awm.taskqueue.store import Store

from softauthn import SoftDevice, SoftPasskey

ORIGIN = "https://approve.test"
SECRET = b"s3cret-shared-with-the-store"


class Clock:
    def __init__(self) -> None:
        self.t = float(int(time.time()))

    def __call__(self) -> float:
        return self.t


class FakeTools:
    def __init__(self, store: Store) -> None:
        self.st = store
        self.twofa_calls: list[tuple[str, dict]] = []

    async def store(self, verb: str, args: dict[str, Any]) -> Any:
        return getattr(self.st, verb)(**args)

    async def twofa(self, verb: str, args: dict[str, Any]) -> Any:
        self.twofa_calls.append((verb, args))
        if verb == "devices":
            return [{"name": "phone"}]
        if verb == "pending":
            return {"phone": [{"urgid": "u1", "app": "UBC"}]}
        return {"ok": True}


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture(scope="session")
def keys(tmp_path_factory) -> Keys:
    return Keys.load_or_create(tmp_path_factory.mktemp("keys"))


@pytest.fixture
def env(tmp_path: Path, clock: Clock, keys: Keys):
    store = Store(tmp_path / "queue.db", action_secret=SECRET, clock=clock)
    tools = FakeTools(store)
    ap = Approver(tmp_path / "approver.db", keys,
                  Config(origin=ORIGIN, allowed_orders={"spawn": ["*"], "new_from": ["*"],
                                                        "zotero_sync": ["deneb"]}, rate_max=5),
                  tools, SECRET, clock=clock)
    return ap, store, tools


def enrol(ap: Approver, name: str = "phone", token: str | None = None,
          ip: str | None = None) -> tuple[SoftDevice, SoftPasskey]:
    token = token or ap.enrol_link()["token"]
    pk, dev = SoftPasskey("approve.test", ORIGIN), SoftDevice()
    begin = ap.enrol_begin(token, name)
    out = ap.enrol_finish(token, begin["reg_id"], pk.create(begin["options"]), dev.pub, name, ip)
    dev.id = out["device"]
    dev.take(out["ack"])
    return dev, pk


async def authorize(ap: Approver, dev: SoftDevice, pk: SoftPasskey, ts: int, *,
                    batch: list | None = None, window: dict | None = None,
                    ip: str | None = None) -> dict:
    req: dict[str, Any] = {}
    if batch is not None:
        req["batch"] = batch
    if window is not None:
        req["window"] = window
    m, s = dev.message("authorize", req, ts)
    out = await ap.act(m, s, pk.get(passkey_challenge(dev.head, req)), ip)
    dev.take(out["ack"])
    return out
