"""Which kinds of caller may call which verbs on the shared vault.

The public edge's allow-list is *not* what enforces this. A mesh node's edge
runs no profile and never consults `policy`, so the gate has to be in the
handler, and these tests are what say so.

Five kinds, told apart from the edge's `as_` and the gateway's stamps:

- host: loopback with no session pid, the host's shell and services, and the
  edge's `slice_resolve` lookup. Everything but the push verbs.
- host_session: a session on the vault's own host. Refused the vault, so
  agents work it from other nodes, where their writes are labelled.
- peer_service and peer_agent: a door call from another node, without and
  with a session claim. The note API and slices, never the lifecycle.
- person: a signed-in browser through the edge. Only the public face.
"""

from __future__ import annotations

import asyncio

import pytest

from awm.trilium import etapi, hub_adapter, instances

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


@pytest.fixture(autouse=True)
def _off_the_real_vault(tmp_path, monkeypatch):
    """Point the singleton at a throwaway path, and refuse the ETAPI client.

    These tests invoke the verbs to prove the *gate*, not the work. Several
    lifecycle verbs create directories before they fail, and against the live
    singleton that scaffolds `live/` where `git worktree add` then refuses.
    Everything past the gate fails the same way on the refused client, which
    `_admitted` tolerates.
    """
    monkeypatch.setattr(instances, "VAULT",
                        instances.Vault(scope=tmp_path / "vault"))

    def _no_vault() -> None:
        raise etapi.EtapiError("no vault in this test")
    monkeypatch.setattr(etapi, "client", _no_vault)


#: (args, as_) for one caller of each kind.
CALLERS = {
    "host": ({}, None),
    "host_session": ({"_caller_pid": 4242}, None),
    "peer_service": ({}, "peer:mizar"),
    "peer_agent": ({"_caller_session": {"session_id": "s", "node": "mizar"}}, "peer:mizar"),
    "person": ({}, "user:tony"),
}

PUBLIC = ["status", "snapshots", "url"]
LIFECYCLE = ["start", "stop", "restart", "init", "logs", "board_install",
             "snapshot", "export", "restore", "slice_resolve"]
VAULT = ["note_upsert", "note_get", "note_children", "note_search",
         "note_create", "note_update", "note_delete", "note_move", "note_clone",
         "note_place", "attrs_get", "attr_set", "attr_delete", "attachment_put",
         "attachment_get", "slice_expose", "slice_list", "slice_revoke"]
PUSH = ["board_apply", "ops_render"]

#: The kinds each group admits. Every other kind is refused.
ADMITS = {
    "public": (PUBLIC, set(CALLERS)),
    "lifecycle": (LIFECYCLE, {"host"}),
    "vault": (VAULT, {"host", "peer_service", "peer_agent"}),
}


def test_the_groups_are_the_whole_surface():
    """A verb added later fails at import until `ACCESS` names it, and here
    until someone has decided which group it is."""
    assert set(PUBLIC) | set(LIFECYCLE) | set(VAULT) | set(PUSH) == set(hub_adapter.HANDLERS)


def _admitted(verb: str, kind: str) -> bool:
    args, as_ = CALLERS[kind]
    try:
        asyncio.run(hub_adapter.HANDLERS[verb](dict(args), as_))
    except PermissionError as e:
        if " caller: " in str(e):
            return False
    except Exception:  # noqa: BLE001 — no vault here; only the caller gate matters
        pass
    return True


@pytest.mark.parametrize("kind", CALLERS)
@pytest.mark.parametrize("group", ADMITS)
def test_each_kind_of_caller_reaches_its_groups_only(group, kind):
    verbs, admitted = ADMITS[group]
    for verb in verbs:
        assert _admitted(verb, kind) == (kind in admitted), verb


def test_status_sheds_the_operators_half_for_a_signed_in_caller():
    """Pids, ports and absolute paths are an operator's business. On a public
    host they are gratuitous, and a path is a small map of the machine."""
    through_edge = asyncio.run(hub_adapter.HANDLERS["status"]({}, "user:tony"))
    from_console = asyncio.run(hub_adapter.HANDLERS["status"]({}, None))
    assert "source" not in through_edge and "source" in from_console
    for key in ("pid", "port", "scope", "data_dir", "log"):
        assert key not in through_edge["vault"], key
        assert key in from_console["vault"], key


@pytest.mark.parametrize("verb", PUSH)
@pytest.mark.parametrize("args,as_", [
    *CALLERS.values(),
    ({"_caller_session": {"session_id": "s", "node": "alcor"}}, "peer:alcor"),
])
def test_a_push_verb_refuses_all_but_alcors_services(verb, args, as_):
    with pytest.raises(PermissionError):
        asyncio.run(hub_adapter.HANDLERS[verb](dict(args), as_))


def test_a_push_verb_admits_alcors_service_through_the_door(monkeypatch):
    class Board:
        def apply(self, args):
            return {"mark": 7}
    monkeypatch.setattr(hub_adapter, "_board", lambda: Board())
    assert asyncio.run(hub_adapter.HANDLERS["board_apply"]({}, "peer:alcor")) == {"mark": 7}
