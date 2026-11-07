"""No plugin may call the approver's device verbs or the spawn orders. Any plugin may call any
tool, so these verbs refuse a call whose frame names a plugin, and serve the operator's own."""

from __future__ import annotations

import asyncio

import pytest

from awm.operations import hub_adapter


class _Front:
    relay = None

    async def call(self, verb, args):
        return {"ok": True, "verb": verb}


class _Approver:
    def devices(self):
        return [{"name": "phone"}]

    def device_revoke(self, device_id):
        return {"revoked": device_id}


class _Home:
    approver = _Approver()


def _run(out):
    return asyncio.run(out) if asyncio.iscoroutine(out) else out


@pytest.fixture()
def handlers():
    return hub_adapter.handlers(_Front(), _Home())


@pytest.mark.parametrize("verb", hub_adapter.NO_PLUGIN)
def test_a_plugin_caller_is_refused(handlers, verb):
    out = _run(handlers[verb]({"_caller_plugin": "search", "device_id": "d1"}))
    assert out == {"ok": False, "error": f"{verb} is not open to plugin 'search'"}


def test_the_operator_still_calls_them(handlers):
    assert _run(handlers["devices"]({})) == [{"name": "phone"}]
    assert _run(handlers["device_revoke"]({"device_id": "d1"})) == {"revoked": "d1"}
    assert _run(handlers["order_pull"]({"node": "mizar"})) == {"ok": True, "verb": "order_pull"}


def test_other_verbs_stay_open_to_plugins(handlers):
    assert _run(handlers["task_for_session"]({"_caller_plugin": "reflection"}))["ok"] is True
