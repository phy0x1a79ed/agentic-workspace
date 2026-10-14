"""A cancelled call kills the process groups it started and stops its handler."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time

import pytest

from awm.gatewayclient import cancel
from awm.gatewayclient.adapter import ServiceAdapter


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_gone(pgid: int, within: float = 5.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not _group_alive(pgid):
            return True
        time.sleep(0.05)
    return False


def test_cancel_kills_the_whole_process_group():
    token = cancel.CancelToken()
    seen: dict = {}

    def work():
        with cancel.bound(token):
            try:
                cancel.run(["bash", "-c", "sleep 30 & sleep 30"])
            except cancel.Cancelled as exc:
                seen["exc"] = exc

    t = threading.Thread(target=work)
    t.start()
    deadline = time.monotonic() + 5
    while not token._procs and time.monotonic() < deadline:
        time.sleep(0.02)
    pgid = next(iter(token._procs)).pid
    token.cancel()
    t.join(5)
    assert isinstance(seen.get("exc"), cancel.Cancelled)
    assert _wait_gone(pgid), "the grandchild sleep must die with the group"


def test_a_cancelled_token_refuses_new_work_but_shielded_cleanup_runs():
    token = cancel.CancelToken()
    token.cancel()
    with cancel.bound(token):
        with pytest.raises(cancel.Cancelled):
            cancel.run(["true"])
        with cancel.shielded():
            assert cancel.run(["true"]).returncode == 0


def test_no_token_behaves_like_subprocess_run():
    r = cancel.run(["bash", "-c", "echo out; echo err >&2; exit 3"])
    assert (r.returncode, r.stdout, r.stderr) == (3, "out\n", "err\n")


class _WS:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(json.loads(data))


def test_a_cancel_frame_reaches_a_sync_handler_and_sends_no_reply():
    seen: dict = {}

    def handler(args):
        try:
            cancel.run(["sleep", "30"])
        except cancel.Cancelled:
            seen["cancelled"] = True
            raise

    async def drive():
        ad = ServiceAdapter("svc", {"functions": [{"name": "f"}]}, {"f": handler})
        ws = _WS()
        token = cancel.CancelToken()
        task = asyncio.create_task(ad._handle_call(
            ws, {"kind": "call", "id": "7", "fn": "f", "args": {}}, token))
        ad._calls["7"] = (task, token)
        while not token._procs:
            await asyncio.sleep(0.02)
        ad._cancel_call("7")
        ad._cancel_call("unknown")
        await asyncio.wait_for(task, 5)
        for _ in range(100):
            if seen:
                break
            await asyncio.sleep(0.05)
        return ws, ad

    ws, ad = asyncio.run(drive())
    assert seen == {"cancelled": True}
    assert ws.sent == []
    assert ad._calls == {}
