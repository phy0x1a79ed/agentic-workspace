"""The seed, against a Penpot small enough to hold in one file.

The property that matters is that a second run changes nothing: the demo is
re-run by hand, by `demo-chain.sh`, and by anyone repairing the box, and a
seed that creates a second file every time makes the vault note point at the
wrong one. So the fake below is not a set of canned replies -- it keeps a
file and applies the changes it is sent, because that is the only way the
skip-if-already-there markers get tested at all.
"""

from __future__ import annotations

import json
import uuid as uuidlib

import httpx
import pytest

from awm.penpot_view import demo as D
from awm.penpot_view import exporter_client as EC

TEAM_ID = uuidlib.UUID("0197f9d2-1a2b-73aa-8b9c-1234567890c0")
BASE_URL = "http://penpot.example"


class FakePenpot:
    """Enough of Penpot to answer the seed, and to remember what it did."""

    def __init__(self, *, team_name: str = "Shared") -> None:
        self.team_name = team_name
        self.projects: list[dict] = []
        self.files: dict[uuidlib.UUID, dict] = {}
        self.calls: list[str] = []
        self.media: list[dict] = []

    # -- the change application, shallow but real ------------------------
    def _apply(self, data: dict, change: dict) -> None:
        kind = str(change["type"])
        if kind == "add-page":
            data["pages"].append(change["id"])
            data["pages-index"][change["id"]] = {
                "id": change["id"], "name": change["name"],
                "objects": {uuidlib.UUID(int=0): {"id": uuidlib.UUID(int=0)}}}
        elif kind == "mod-page":
            data["pages-index"][change["id"]]["name"] = change["name"]
        elif kind == "add-obj":
            page = data["pages-index"][change["page-id"]]
            page["objects"][change["id"]] = dict(change["obj"])
        elif kind == "mod-obj":
            page = data["pages-index"][change["page-id"]]
            shape = page["objects"][change["id"]]
            for op in change["operations"]:
                shape[str(op["attr"])] = op["val"]
        elif kind == "add-component":
            data["components"][change["id"]] = {
                "id": change["id"], "name": change["name"],
                "main-instance-id": change["main-instance-id"],
                "main-instance-page": change["main-instance-page"]}
        else:  # pragma: no cover -- a change the seed does not send
            raise AssertionError(f"unexpected change {kind!r}")

    def handler(self, request: httpx.Request) -> httpx.Response:
        command = request.url.path.rsplit("/", 1)[-1]
        self.calls.append(command)
        if command == "login-with-password":
            return httpx.Response(
                200, text=EC._transit_dumps({EC.Keyword("id"): TEAM_ID}),
                headers={"content-type": "application/transit+json",
                         "set-cookie": "auth-token=t; Path=/"})
        if command == "upload-file-media-object":
            mobj = {EC.Keyword("id"): uuidlib.uuid4(),
                    EC.Keyword("width"): 96, EC.Keyword("height"): 96,
                    EC.Keyword("mtype"): "image/png",
                    EC.Keyword("name"): "Demo swatch"}
            self.media.append(mobj)
            return self._ok(mobj)
        params = EC._transit_loads(request.content.decode())
        if command == "get-teams":
            return self._ok([{EC.Keyword("id"): TEAM_ID,
                              EC.Keyword("name"): self.team_name}])
        if command == "get-projects":
            return self._ok(self.projects)
        if command == "create-project":
            self.projects.append({EC.Keyword("id"): params["id"],
                                  EC.Keyword("name"): params["name"]})
            return self._ok(self.projects[-1])
        if command == "get-project-files":
            return self._ok([{EC.Keyword("id"): fid}
                             for fid in self.files])
        if command == "create-file":
            page = uuidlib.uuid4()
            self.files[params["id"]] = {
                "revn": 0, "vern": 0,
                "data": {"pages": [page],
                         "pages-index": {page: {
                             "id": page, "name": "Page 1",
                             "objects": {uuidlib.UUID(int=0): {}}}},
                         "components": {}}}
            return self._ok({EC.Keyword("id"): params["id"]})
        if command == "get-file":
            return self._ok(self._as_transit(self.files[params["id"]]))
        if command == "update-file":
            file = self.files[params["id"]]
            assert params["vern"] == file["vern"], "vern must match exactly"
            assert params["revn"] <= file["revn"], "revn must not be ahead"
            for change in params["changes"]:
                self._apply(file["data"], change)
            file["revn"] += 1
            return self._ok([])
        raise AssertionError(f"unexpected command {command!r}")  # pragma: no cover

    # -- plumbing --------------------------------------------------------
    def _as_transit(self, value):
        if isinstance(value, dict):
            return {EC.Keyword(k) if isinstance(k, str) else k:
                    self._as_transit(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._as_transit(v) for v in value]
        return value

    def _ok(self, value) -> httpx.Response:
        return httpx.Response(
            200, text=EC._transit_dumps(value),
            headers={"content-type": "application/transit+json"})


@pytest.fixture()
def client():
    fake = FakePenpot()
    c = EC.ExporterClient(base_url=BASE_URL, token="borrowed",
                          transport=httpx.MockTransport(fake.handler))
    c.fake = fake  # type: ignore[attr-defined]
    try:
        yield c
    finally:
        c.close()


def test_the_seed_builds_the_whole_chain_in_four_writes(client):
    result = D.seed(client)
    assert result["created"] == ["geometry", "component", "page", "copy"]
    assert result["file_id"] == str(D.FILE_ID)
    assert result["reuse"]["board_id"] == str(D.COPY_IDS["board"])
    # Four separate update-file calls, not one, so a rejection names which
    # kind of change was wrong.
    assert client.fake.calls.count("update-file") == 5  # + the page rename


def test_running_it_twice_produces_one_file_and_the_same_triples(client):
    first = D.seed(client)
    before = client.fake.calls.count("update-file")
    second = D.seed(client)
    assert second["created"] == []
    assert client.fake.calls.count("update-file") == before
    assert len(client.fake.files) == 1
    assert len(client.fake.projects) == 1
    for key in ("file_id", "source", "reuse"):
        assert first[key] == second[key]


def test_the_image_is_uploaded_once_not_once_per_run(client):
    D.seed(client)
    D.seed(client)
    assert len(client.fake.media) == 1


def test_the_component_id_is_never_the_board_id(client):
    D.seed(client)
    file = client.fake.files[D.FILE_ID]
    component = file["data"]["components"][D.COMPONENT_ID]
    assert component["main-instance-id"] == D.MAIN_IDS["board"]
    assert D.COMPONENT_ID != D.MAIN_IDS["board"]


def test_the_main_instance_is_marked_up_on_the_board_itself(client):
    D.seed(client)
    file = client.fake.files[D.FILE_ID]
    page = file["data"]["pages-index"][file["data"]["pages"][0]]
    board = page["objects"][D.MAIN_IDS["board"]]
    assert board["component-id"] == D.COMPONENT_ID
    assert board["main-instance"] is True
    assert board["shape-ref"] is None


def test_every_copied_shape_points_back_at_the_one_it_copies(client):
    D.seed(client)
    file = client.fake.files[D.FILE_ID]
    objects = file["data"]["pages-index"][D.REUSE_PAGE_ID]["objects"]
    for name, copy_id in D.COPY_IDS.items():
        assert objects[copy_id]["shape-ref"] == D.MAIN_IDS[name]
    root = objects[D.COPY_IDS["board"]]
    assert root["component-root"] is True
    assert root["component-id"] == D.COMPONENT_ID
    # Only the root is the component's; a child that claims to be a root
    # detaches the copy from its main on the next sync.
    assert objects[D.COPY_IDS["ribbon"]].get("component-root") is None


def test_the_copy_is_offset_and_its_text_run_moves_with_it(client):
    D.seed(client)
    data = client.fake.files[D.FILE_ID]["data"]
    src = data["pages-index"][data["pages"][0]]["objects"]
    dst = data["pages-index"][D.REUSE_PAGE_ID]["objects"]
    dx, dy = D.COPY_AT
    assert dst[D.COPY_IDS["board"]]["x"] == src[D.MAIN_IDS["board"]]["x"] + dx
    main_run = src[D.MAIN_IDS["label"]]["position-data"][0]
    copy_run = dst[D.COPY_IDS["label"]]["position-data"][0]
    assert copy_run["x"] == main_run["x"] + dx
    assert copy_run["y"] == main_run["y"] + dy


def test_a_missing_shared_team_says_which_script_creates_it(client):
    client.fake.team_name = "Default"
    with pytest.raises(EC.ExporterError, match="penpot-team.sh"):
        D.seed(client)


def test_the_ids_are_derived_not_random():
    assert D.FILE_ID == uuidlib.uuid5(D.NAMESPACE, "file")
    assert D.MAIN_IDS["board"] == uuidlib.uuid5(D.NAMESPACE, "main/board")


def test_the_demo_image_is_a_png_and_the_same_bytes_every_time():
    first = D.demo_png()
    assert first[:8] == b"\x89PNG\r\n\x1a\n"
    assert first == D.demo_png()
