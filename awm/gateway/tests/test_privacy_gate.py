"""The gateway's private-mark gate: a peer never reaches a private project.

A caller stamped ``peer`` or ``peer:<node>`` (over an edge included) whose call names a
marked project, scope or path is refused before the service runs, with the answer
an unknown name gets. The same holds for the ``/svc`` door, the emit fan-out and
the static mounts. Local and ``user:*`` callers are not gated.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from awm.config import privacy
from awm.gateway import catalog, peer_catalog, private_gate, server
from awm.gateway.hub import rpc
from awm.gateway.hub.registry import ServiceRecord
from awm.gateway.hub.static import serve_static

EDGE_PEER = "peer:capella"
LOCALS = [None, "user:tony", "agent:awm/x"]
PEERS = ["peer", EDGE_PEER, " PEER:Altair "]
EDGE_PEERS = [EDGE_PEER, " PEER:Altair "]


@pytest.fixture()
def projects(awm_workspace):
    root = awm_workspace["projects_dir"]
    (root / "avatar" / "Avatar" / "dev").mkdir(parents=True)
    (root / "avatar" / "PLAN.md").write_text("secret plan")
    (root / "awm" / "svc-a").mkdir(parents=True)
    (root / "awm" / "svc-b").mkdir(parents=True)
    (root / "awm" / "svc-b" / "README.md").write_text("public")
    return root


@pytest.fixture()
def marked(projects):
    """``avatar`` is private, and so is the ``svc-a`` scope of ``awm``."""
    privacy.write_marks([
        {"project": "avatar", "scope": None, "path": str(projects / "avatar")},
        {"project": "awm", "scope": "svc-a", "path": str(projects / "awm" / "svc-a")},
    ])
    return projects


@pytest.fixture()
def corrupt(projects):
    privacy.marks_path().write_text("{not json")
    return projects


# ---------------------------------------------------------------------------
# catalog.dispatch
# ---------------------------------------------------------------------------


class _StubRegistry:
    def __init__(self, records):
        self._records = records

    def service_records(self):
        return list(self._records)


class _FakeChannel:
    def __init__(self):
        self.calls = []

    async def call(self, fn, args, as_=None, timeout=None):
        self.calls.append((fn, args, as_))
        return {"ran": fn}


_PARAMS = [{"name": n, "type": "string"} for n in ("project", "scope", "path", "q")]


@pytest.fixture()
def journal(awm_workspace, monkeypatch):
    """A ``journal`` service with ``fetch`` (read)."""
    spec = {"name": "fetch", "description": "fetch.", "effect": "read",
            "params": _PARAMS}
    rec = ServiceRecord(name="journal", prefix="/svc/journal", kind="service",
                        service_id="sid-journal", api={"functions": [spec]})
    monkeypatch.setattr(catalog, "get_registry", lambda: _StubRegistry([rec]))
    ch = _FakeChannel()
    monkeypatch.setattr(catalog.rpc, "get_control", lambda sid: ch)
    monkeypatch.setattr(peer_catalog, "_snapshot", {})
    return ch


async def _flat(args, as_):
    return json.loads(await catalog.dispatch("journal_fetch", args, as_=as_))


async def _domain(args, as_):
    return json.loads(await catalog.dispatch(
        "journal", {"verb": "fetch", "args": args}, as_=as_))


@pytest.mark.parametrize("as_", EDGE_PEERS)
async def test_a_peer_naming_a_private_project_is_refused_as_unknown(journal, marked, as_):
    with pytest.raises(ValueError, match=r"^Unknown tool: journal_fetch$"):
        await _flat({"project": "avatar"}, as_)
    with pytest.raises(ValueError, match=r"^Unknown verb 'fetch' for domain 'journal'$"):
        await _domain({"project": "avatar"}, as_)
    assert journal.calls == []


@pytest.mark.parametrize("as_", ["peer:hub", "peer"])
async def test_a_peer_without_an_edge_is_refused_whatever_it_names(journal, marked, as_):
    for args in ({"project": "avatar"}, {"project": "awm"}):
        with pytest.raises(ValueError, match=r"^Unknown tool: journal"):
            await _flat(args, as_)
        with pytest.raises(ValueError, match=r"^Unknown tool: journal$"):
            await _domain(args, as_)
    assert journal.calls == []


@pytest.mark.parametrize("as_", LOCALS)
async def test_a_local_caller_reaches_a_private_project(journal, marked, as_):
    assert await _flat({"project": "avatar"}, as_) == {"ran": "fetch"}
    assert await _domain({"project": "avatar", "scope": "Avatar/dev"}, as_) == {"ran": "fetch"}
    assert await _flat({"path": str(marked / "avatar" / "PLAN.md")}, as_) == {"ran": "fetch"}


async def test_a_peer_reaches_an_unmarked_project(journal, marked):
    assert await _flat({"project": "awm"}, EDGE_PEER) == {"ran": "fetch"}
    assert await _domain({"project": "awm", "scope": "svc-b"}, EDGE_PEER) == {"ran": "fetch"}
    assert await _flat({"q": "avatar"}, EDGE_PEER) == {"ran": "fetch"}


async def test_a_scope_mark_hides_that_scope_only(journal, marked):
    with pytest.raises(ValueError):
        await _domain({"project": "awm", "scope": "svc-a"}, EDGE_PEER)
    assert await _domain({"project": "awm", "scope": "svc-b"}, EDGE_PEER) == {"ran": "fetch"}


@pytest.mark.parametrize("path", [
    "{root}/avatar/PLAN.md",
    "{root}/avatar",
    "{root}/awm/svc-a/x.py",
    "projects/avatar/Avatar/dev",
    "{root}/awm/../avatar/PLAN.md",
])
async def test_a_peer_naming_a_private_path_is_refused(journal, marked, path):
    path = path.format(root=marked)
    for key in ("path", "scope"):
        with pytest.raises(ValueError):
            await _flat({key: path}, EDGE_PEER)
    with pytest.raises(ValueError):
        await _domain({"q": "x", "path": path}, EDGE_PEER)
    assert journal.calls == []


async def test_a_path_inside_a_nested_argument_is_refused(journal, marked, monkeypatch):
    monkeypatch.setattr(catalog, "_check_arguments", lambda *a: None)
    with pytest.raises(ValueError):
        await _flat({"meta": {"files": [str(marked / "avatar" / "PLAN.md")]}}, EDGE_PEER)
    with pytest.raises(ValueError):
        await _flat({"projects": ["awm", "avatar"]}, EDGE_PEER)
    assert await _flat({"projects": ["awm"]}, EDGE_PEER) == {"ran": "fetch"}


async def test_a_peer_reaches_a_public_path(journal, marked):
    assert await _flat({"path": str(marked / "awm" / "svc-b" / "README.md")},
                       EDGE_PEER) == {"ran": "fetch"}


async def test_a_corrupt_marks_file_refuses_a_peer_any_project(journal, corrupt):
    for project in ("awm", "avatar", "nope"):
        with pytest.raises(ValueError):
            await _flat({"project": project}, EDGE_PEER)
    assert await _flat({"q": "x"}, EDGE_PEER) == {"ran": "fetch"}
    assert await _flat({"project": "avatar"}, None) == {"ran": "fetch"}


async def test_unmarking_restores_the_peer_on_the_next_call(journal, marked):
    with pytest.raises(ValueError):
        await _flat({"project": "avatar"}, EDGE_PEER)
    privacy.write_marks([])
    assert await _flat({"project": "avatar"}, EDGE_PEER) == {"ran": "fetch"}


# ---------------------------------------------------------------------------
# /invoke and the /svc door
# ---------------------------------------------------------------------------


def test_invoke_answers_a_refusal_with_the_unknown_tool_404(journal, marked):
    client = TestClient(server.app, raise_server_exceptions=False)
    refused = client.post("/invoke", json={"name": "journal_fetch",
                                           "args": {"project": "avatar"}},
                          headers={"X-Awm-As": EDGE_PEER})
    unknown = client.post("/invoke", json={"name": "journal_nope", "args": {}},
                          headers={"X-Awm-As": EDGE_PEER})
    assert refused.status_code == unknown.status_code == 404
    assert refused.json()["detail"] == "Unknown tool: journal_fetch"
    ok = client.post("/invoke", json={"name": "journal_fetch",
                                      "args": {"project": "avatar"}})
    assert ok.status_code == 200


class _Registry:
    def __init__(self, rec):
        self.rec = rec

    def is_empty(self):
        return False

    def longest_match(self, path):
        return self.rec


@pytest.fixture()
def door(awm_workspace, monkeypatch):
    """POST to the ``journal`` service door. A call past the gate reaches the
    service: ``fetch`` is a notification (202), and a ``live`` session open
    is turned down by the service (502)."""
    rec = ServiceRecord(name="journal", prefix="/svc/journal", kind="service",
                        service_id="sid-door")
    ch = rpc.ensure_control("sid-door")
    ch.set_api({"functions": [{"name": "fetch", "no_response": True}],
                "sessions": [{"kind": "live"}]})

    async def turned_down(*a, **kw):
        raise rpc.RpcError("no")

    monkeypatch.setattr(ch, "open_session", turned_down)
    monkeypatch.setattr(server, "_get_hub_registry", lambda: _Registry(rec))
    client = TestClient(server.app, raise_server_exceptions=False)

    def post(body, as_=None, path="/svc/journal/fn/fetch"):
        headers = {"X-Awm-As": as_} if as_ else {}
        return client.post(path, json=body, headers=headers)

    yield post
    rpc.drop_control("sid-door")


@pytest.mark.parametrize("as_", PEERS)
def test_the_door_refuses_a_peer_naming_a_private_project(door, marked, as_):
    resp = door({"project": "avatar"}, as_)
    assert resp.status_code == 404 and resp.json() == {"error": "not found"}
    resp = door({"path": str(marked / "avatar" / "PLAN.md")}, as_)
    assert resp.status_code == 404 and resp.json() == {"error": "not found"}


@pytest.mark.parametrize("as_", ["user:tony", None])
def test_the_door_lets_a_local_caller_through(door, marked, as_):
    assert door({"project": "avatar"}, as_).status_code == 202


def test_the_door_lets_a_peer_reach_an_unmarked_project(door, marked):
    assert door({"project": "awm"}, EDGE_PEER).status_code == 202
    assert door({"project": "awm", "scope": "svc-a"}, EDGE_PEER).status_code == 404


def test_the_door_refuses_a_session_init_naming_a_private_project(door, marked):
    path = "/svc/journal/session/live"
    resp = door({"project": "avatar"}, EDGE_PEER, path=path)
    assert resp.status_code == 404 and resp.json() == {"error": "not found"}
    assert door({"project": "avatar"}, None, path=path).status_code == 502
    assert door({"project": "awm"}, EDGE_PEER, path=path).status_code == 502


def test_the_door_refuses_any_project_when_the_marks_file_is_corrupt(door, corrupt):
    assert door({"project": "awm"}, EDGE_PEER).status_code == 404
    assert door({"project": "awm"}, None).status_code == 202


# ---------------------------------------------------------------------------
# Emit fan-out
# ---------------------------------------------------------------------------


def _post(project, scope="dev"):
    return {"project": project, "scope": scope,
            "post": {"owner_project": project, "body": "hi"}}


async def _emit_round(payload):
    ch = rpc.ControlChannel("sid-emit")
    subs = {as_: ch.add_subscriber("posts", as_=as_) for as_ in (None, "user:tony", EDGE_PEER)}
    ch.handle_emit({"topic": "posts", "payload": payload})
    return {as_: (sub.queue.get_nowait() if not sub.queue.empty() else None)
            for as_, sub in subs.items()}


async def test_emit_drops_a_private_event_for_a_peer_subscriber_only(marked):
    got = await _emit_round(_post("avatar"))
    assert got == {None: _post("avatar"), "user:tony": _post("avatar"), EDGE_PEER: None}


async def test_emit_drops_a_private_scope_event_for_a_peer(marked):
    got = await _emit_round(_post("awm", "svc-a"))
    assert got[EDGE_PEER] is None and got[None] == _post("awm", "svc-a")


async def test_emit_drops_an_event_whose_nested_object_is_private(marked):
    payload = {"kind": "post", "post": {"project": "avatar", "scope": "dev"}}
    assert (await _emit_round(payload))[EDGE_PEER] is None
    payload = {"event": "write", "detail": {"path": str(marked / "avatar" / "PLAN.md")}}
    assert (await _emit_round(payload))[EDGE_PEER] is None


async def test_emit_delivers_an_unmarked_event_to_every_subscriber(marked):
    got = await _emit_round(_post("awm", "svc-b"))
    assert all(v == _post("awm", "svc-b") for v in got.values())


async def test_emit_with_a_corrupt_marks_file_reaches_no_peer(corrupt):
    got = await _emit_round(_post("awm", "svc-b"))
    assert got[EDGE_PEER] is None and got[None] == _post("awm", "svc-b")


# ---------------------------------------------------------------------------
# Static door
# ---------------------------------------------------------------------------


def _get(rec, path, as_=None):
    from starlette.requests import Request

    headers = [(b"x-awm-as", as_.encode())] if as_ else []
    req = Request({"type": "http", "method": "GET", "path": path,
                   "raw_path": path.encode(), "query_string": b"", "headers": headers})
    return req, rec


async def _serve(rec, path, as_=None):
    return await serve_static(*_get(rec, path, as_))


@pytest.fixture()
def files(awm_workspace):
    return ServiceRecord(name="files", prefix="/files", kind="static",
                         static_dir=str(awm_workspace["workspace"]))


async def test_static_refuses_a_peer_a_file_under_a_marked_path(files, marked):
    for as_ in PEERS:
        resp = await _serve(files, "/files/projects/avatar/PLAN.md", as_)
        assert resp.status_code == 404 and resp.body == b"not found"
        resp = await _serve(files, "/files/projects/awm/svc-a", as_)
        assert resp.status_code == 404


@pytest.mark.parametrize("as_", LOCALS)
async def test_static_serves_a_marked_file_locally(files, marked, as_):
    resp = await _serve(files, "/files/projects/avatar/PLAN.md", as_)
    assert resp.status_code == 200 and str(resp.path).endswith("PLAN.md")


async def test_static_serves_a_peer_an_unmarked_file(files, marked):
    resp = await _serve(files, "/files/projects/awm/svc-b/README.md", EDGE_PEER)
    assert resp.status_code == 200


async def test_static_refuses_a_peer_a_symlink_into_a_marked_path(files, marked):
    link = marked / "awm" / "svc-b" / "plan-link.md"
    link.symlink_to(marked / "avatar" / "PLAN.md")
    resp = await _serve(files, "/files/projects/awm/svc-b/plan-link.md", EDGE_PEER)
    assert resp.status_code == 404
    assert (await _serve(files, "/files/projects/awm/svc-b/plan-link.md")).status_code == 200


async def test_static_refuses_a_peer_a_mount_rooted_in_a_marked_path(marked):
    rec = ServiceRecord(name="page", prefix="/ui/avatar", kind="static",
                        static_dir=str(marked / "avatar"))
    (marked / "avatar" / "index.html").write_text("<html/>")
    assert (await _serve(rec, "/ui/avatar/", EDGE_PEER)).status_code == 404
    assert (await _serve(rec, "/ui/avatar/PLAN.md", EDGE_PEER)).status_code == 404
    assert (await _serve(rec, "/ui/avatar/")).status_code == 200


async def test_static_with_a_corrupt_marks_file_serves_a_peer_nothing(files, corrupt):
    resp = await _serve(files, "/files/projects/awm/svc-b/README.md", EDGE_PEER)
    assert resp.status_code == 404
    assert (await _serve(files, "/files/projects/awm/svc-b/README.md")).status_code == 200


# ---------------------------------------------------------------------------
# Static door: records of private work outside the marked paths
# ---------------------------------------------------------------------------


@pytest.fixture()
def root_mount():
    """The ``/files`` door as production mounts it: the whole filesystem."""
    return ServiceRecord(name="files", prefix="/files", kind="static", static_dir="/")


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A gateway home whose ``~/.claude`` holds a plan and a transcript filed
    under the workspace's ``projects`` slug, not under the marked path."""
    home = tmp_path / "home"
    (home / ".claude" / "plans").mkdir(parents=True)
    (home / ".claude" / "plans" / "plan.md").write_text("private plan")
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture()
def records(awm_workspace, projects, home, tmp_path):
    """Files a peer could learn private work from, each outside every mark."""
    ws = awm_workspace["workspace"]
    (ws / ".awm" / "services" / "journal").mkdir(parents=True)
    (ws / ".awm" / "services" / "journal" / "journal.db").write_text("rows")
    slugged = tmp_path / "logs" / privacy.slug(projects / "avatar" / "Avatar" / "dev")
    slugged.mkdir(parents=True)
    (slugged / "s.jsonl").write_text("{}")
    scratch = tmp_path / "scratch.txt"
    scratch.write_text("scratch")
    return {
        "awm_dir": ws / ".awm" / "services" / "journal" / "journal.db",
        "claude": home / ".claude" / "plans" / "plan.md",
        "slug": slugged / "s.jsonl",
        "tmp": scratch,
        "proc": "/proc/self/status",
    }


def _url(path):
    return "/files" + str(path)


@pytest.mark.parametrize("which", ["awm_dir", "claude", "slug", "tmp", "proc"])
async def test_static_refuses_a_peer_the_records_of_private_work(
        root_mount, records, marked, which, monkeypatch):
    if which != "tmp":
        # The fixtures live under /tmp; take it off the list so each rule is
        # shown to refuse on its own.
        monkeypatch.setattr(private_gate, "SYSTEM_DENY_ROOTS",
                            tuple(r for r in private_gate.SYSTEM_DENY_ROOTS if r != "/tmp"))
    for as_ in PEERS:
        resp = await _serve(root_mount, _url(records[which]), as_)
        assert resp.status_code == 404 and resp.body == b"not found"
    assert (await _serve(root_mount, _url(records[which]))).status_code == 200
    assert (await _serve(root_mount, _url(records[which]), "user:tony")).status_code == 200


async def test_static_refuses_a_peer_the_marks_file_itself(root_mount, records, marked):
    resp = await _serve(root_mount, _url(privacy.marks_path()), EDGE_PEER)
    assert resp.status_code == 404


async def test_static_refuses_a_peer_a_symlink_into_claude(root_mount, records, marked):
    link = marked / "awm" / "svc-b" / "plan-link.md"
    link.symlink_to(records["claude"])
    assert (await _serve(root_mount, _url(link), EDGE_PEER)).status_code == 404
    assert (await _serve(root_mount, _url(link))).status_code == 200


async def test_static_still_serves_a_peer_the_workspace_outside_the_marks(
        root_mount, records, marked):
    resp = await _serve(root_mount, _url(marked / "awm" / "svc-b" / "README.md"), EDGE_PEER)
    assert resp.status_code == 200


@pytest.mark.parametrize("which", ["awm_dir", "claude", "slug", "tmp", "proc"])
async def test_static_without_marks_serves_a_peer_as_before(root_mount, records, which):
    resp = await _serve(root_mount, _url(records[which]), EDGE_PEER)
    assert resp.status_code == 200


async def test_a_gate_that_raises_refuses_the_peer(root_mount, records, marked, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(privacy, "hidden_path", boom)
    readme = marked / "awm" / "svc-b" / "README.md"
    assert (await _serve(root_mount, _url(readme), EDGE_PEER)).status_code == 404
    assert (await _serve(root_mount, _url(readme))).status_code == 200
    assert private_gate.refuses(EDGE_PEER, {"path": str(readme)})
    assert not private_gate.refuses(None, {"path": str(readme)})


async def test_static_answers_a_nul_byte_with_404(files, marked):
    assert (await _serve(files, "/files/projects/awm/svc-b/x\x00y", EDGE_PEER)).status_code == 404
    assert (await _serve(files, "/files/projects/awm/svc-b/x\x00y")).status_code == 404


async def test_emit_whose_classification_raises_reaches_locals_only(marked):
    payload = {"event": "write", "path": str(marked / "awm" / "svc-b") + "/x\x00y"}
    got = await _emit_round(payload)
    assert got[EDGE_PEER] is None
    assert got[None] == payload and got["user:tony"] == payload
