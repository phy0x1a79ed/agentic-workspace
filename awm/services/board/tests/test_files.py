"""Files on a card: the host's store, the door's routes, the guard on what leaves and the quarantine."""

from __future__ import annotations

import hashlib
import os
import stat

import httpx
import pytest

from awm.board import hub_adapter, transfer
from awm.board.files import FileStore, TooLarge, clean_name
from awm.board.http import create_app
from awm.board.parties import NotFound

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def filing(board, tmp_path):
    board.files = FileStore(tmp_path / "files", max_bytes=1024, total_bytes=4096)
    return board


def ask(board, party, to="collins"):
    return board.post(party, kind="request", recipient=to, title="Run the thing", body="please")


# -- the store and the board --------------------------------------------------


def test_a_file_is_stored_once_announced_on_the_thread_and_listed(filing, tony, collins):
    card = ask(filing, tony)
    out = filing.attach(tony, card["id"], name="../notes.txt", data=b"hello", sha256=sha(b"hello"),
                        agent="opal", node="capella")
    meta = out["file"]
    assert (meta["name"], meta["size"], meta["sha256"]) == ("notes.txt", 5, sha(b"hello"))
    assert meta["sender"]["swarm"] == "tony" and meta["agent"] == "opal"
    assert meta["file_id"] in out["message"]["body"] and out["message"]["origin"] == "file"
    assert filing.messages(collins, card["id"])[-1]["body"] == out["message"]["body"]
    assert [f["file_id"] for f in filing.card_files(collins, card["id"])] == [meta["file_id"]]
    found, path = filing.card_file(collins, card["id"], meta["file_id"])
    assert path.read_bytes() == b"hello" and found == meta


def test_a_wrong_hash_an_empty_file_and_an_oversize_file_are_refused(filing, tony):
    card = ask(filing, tony)
    with pytest.raises(ValueError, match="does not match"):
        filing.attach(tony, card["id"], name="a", data=b"x", sha256=sha(b"y"))
    with pytest.raises(ValueError, match="empty"):
        filing.attach(tony, card["id"], name="a", data=b"", sha256=sha(b""))
    with pytest.raises(TooLarge):
        filing.attach(tony, card["id"], name="a", data=b"x" * 1025, sha256=sha(b"x" * 1025))
    assert filing.card_files(tony, card["id"]) == []


def test_the_store_has_a_total_cap(filing, tony):
    card = ask(filing, tony)
    for n in range(4):
        data = bytes([n]) * 1000
        filing.attach(tony, card["id"], name=f"f{n}", data=data, sha256=sha(data))
    with pytest.raises(TooLarge, match="full"):
        filing.attach(tony, card["id"], name="f5", data=b"z" * 200, sha256=sha(b"z" * 200))


def test_a_party_that_cannot_see_the_card_sees_none_of_its_files(filing, tony, make_party):
    mock = make_party("mock")
    card = ask(filing, tony)
    meta = filing.attach(tony, card["id"], name="a", data=b"x", sha256=sha(b"x"))["file"]
    for call in (lambda: filing.card_files(mock, card["id"]),
                 lambda: filing.card_file(mock, card["id"], meta["file_id"]),
                 lambda: filing.attach(mock, card["id"], name="b", data=b"y", sha256=sha(b"y"))):
        with pytest.raises(NotFound):
            call()


def test_prune_forgets_old_files_and_their_bytes(filing, tony):
    card = ask(filing, tony)
    meta = filing.attach(tony, card["id"], name="a", data=b"x", sha256=sha(b"x"))["file"]
    assert filing.files.prune(days=-1) == 1
    assert filing.files.get(card["id"], meta["file_id"]) is None
    assert list(filing.files.blobs.iterdir()) == []


def test_names_are_one_safe_segment():
    assert clean_name("../../etc/passwd") == "passwd"
    assert clean_name(".bashrc") == "bashrc"
    assert clean_name("a b$(rm).sh") == "a b__rm_.sh"
    assert clean_name("") == "file"


# -- the door -----------------------------------------------------------------


async def test_the_door_takes_raw_bytes_and_serves_them_back(filing, parties, events, tony):
    _, token = parties.add("caviar", "caviar", "foreign")
    card = filing.post(tony, kind="message", recipient="caviar", title="files", body="")
    app = create_app(filing, parties, events)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://b",
                                 headers={"Authorization": f"Bearer {token}"}) as c:
        r = await c.post(f"/board/cards/{card['id']}/files", params={"name": "r.md", "sha256": sha(b"recipe")},
                         content=b"recipe")
        assert r.status_code == 200, r.text
        file_id = r.json()["file"]["file_id"]
        r = await c.post(f"/board/cards/{card['id']}/files", params={"name": "r.md", "sha256": sha(b"x")},
                         content=b"y")
        assert r.status_code == 400
        r = await c.post(f"/board/cards/{card['id']}/files", params={"name": "big", "sha256": "0" * 64},
                         content=b"x" * 2000)
        assert r.status_code == 413
        listing = (await c.get(f"/board/cards/{card['id']}/files")).json()
        assert [f["file_id"] for f in listing] == [file_id]
        r = await c.get(f"/board/cards/{card['id']}/files/{file_id}")
        assert r.content == b"recipe" and r.headers["x-board-sha256"] == sha(b"recipe")
        assert r.headers["content-type"] == "application/octet-stream"
        assert r.headers["x-content-type-options"] == "nosniff"
        assert (await c.get(f"/board/cards/{card['id']}/files/{'0' * 32}")).status_code == 404


# -- the guard on what leaves -------------------------------------------------


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def write(path, data=b"plain notes\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_a_plain_file_is_allowed_with_its_hash(home):
    verdict = transfer.check(str(write(home / "work" / "recipe.md")))
    assert verdict["allowed"] and verdict["sha256"] == sha(b"plain notes\n") and verdict["reasons"] == []


@pytest.mark.parametrize("relative", [
    ".awm-secrets/caviar-board-token.json", ".ssh/config", "agentic_workspace/.awm/env",
    "projects/avatar/Avatar/data/models/x/readme.txt", "work/data/models/notes.txt",
    "work/.env", "work/server.pem", "work/my-token.json", "work/id_ed25519",
    "work/rig.glb", "work/char.vrm", "work/clip.vrma", "work/scene.blend", "work/state.db",
])
def test_denied_places_names_and_kinds_are_refused(home, relative):
    verdict = transfer.check(str(write(home / relative)))
    assert verdict["allowed"] is False and verdict["reasons"], relative
    assert "sha256" not in verdict and "data" not in verdict


def test_a_symlink_into_a_denied_place_is_refused(home):
    secret = write(home / ".awm-secrets" / "x.txt")
    link = home / "work" / "innocent.txt"
    link.parent.mkdir(parents=True)
    link.symlink_to(secret)
    assert transfer.check(str(link))["allowed"] is False


@pytest.mark.parametrize("content", [
    b"-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n",
    b"token = ghp_" + b"a" * 36,
    b"AWM_BOARD_TOKEN=abcdefghijklmnop",
    b"Authorization: Bearer " + b"q" * 40,
])
def test_credential_shaped_content_is_refused(home, content):
    verdict = transfer.check(str(write(home / "work" / "notes.txt", content)))
    assert verdict["allowed"] is False and "credential" in verdict["reasons"][0]


def test_a_secret_value_from_the_environment_is_refused(home, monkeypatch):
    monkeypatch.setenv("SOME_SERVICE_TOKEN", "zzsecretvalue123456")
    verdict = transfer.check(str(write(home / "work" / "notes.txt", b"x zzsecretvalue123456 y")))
    assert verdict["allowed"] is False and "environment" in verdict["reasons"][0]


def test_size_cap_relative_paths_and_directories_are_refused(home, monkeypatch):
    monkeypatch.setenv("AWM_BOARD_FILE_MAX", "4")
    assert transfer.check(str(write(home / "work" / "big.txt", b"12345")))["allowed"] is False
    assert transfer.check("work/big.txt")["allowed"] is False
    assert transfer.check(str(home / "work"))["allowed"] is False


def test_the_denylist_can_only_grow(home, monkeypatch):
    monkeypatch.setenv("AWM_BOARD_FILE_DENY", "**/private/**")
    assert transfer.check(str(write(home / "private" / "a.txt")))["allowed"] is False
    assert transfer.check(str(write(home / ".ssh" / "a.txt")))["allowed"] is False


# -- the verbs: send with an allow, fetch into quarantine ---------------------


@pytest.fixture
def relay(filing, parties, events, home, tmp_path, monkeypatch):
    """The adapter's verbs against a real board through the door, as tony on this node."""
    _, token = parties.add("tony", "tony", "domestic")
    app = create_app(filing, parties, events)
    real = hub_adapter.BoardClient

    def client(url, tok, **kw):
        return real(url, tok, transport=httpx.ASGITransport(app=app), **kw)

    monkeypatch.setattr(hub_adapter, "BoardClient", client)
    monkeypatch.setenv("AWM_BOARD_URL", "http://board")
    monkeypatch.setenv("AWM_BOARD_TOKEN", token)
    monkeypatch.setenv("AWM_BOARD_QUARANTINE", str(tmp_path / "quarantine"))
    monkeypatch.setattr(hub_adapter, "_who", lambda args: {"agent": "opal", "node": "capella"})
    return filing


async def test_send_needs_the_checked_hash_and_fetch_lands_read_only(relay, tony, home):
    card = ask(relay, tony)
    path = str(write(home / "work" / "recipe.md", b"step 1\n"))
    checked = await hub_adapter.file_check({"path": path})
    assert checked["allowed"] and "data" not in checked
    refused = await hub_adapter.file_send({"card_id": card["id"], "path": path, "allow": "0" * 64})
    assert refused["ok"] is False and "allow" in refused["error"]
    sent = await hub_adapter.file_send({"card_id": card["id"], "path": path, "allow": checked["sha256"]})
    assert sent["ok"], sent
    file_id = sent["file"]["file_id"]
    listed = await hub_adapter.files({"card_id": card["id"]})
    assert [f["file_id"] for f in listed["files"]] == [file_id]
    got = await hub_adapter.file_fetch({"card_id": card["id"], "file_id": file_id})
    assert got["ok"], got
    assert got["path"].endswith(f"{card['id']}/{file_id}/recipe.md")
    with open(got["path"], "rb") as fh:
        assert fh.read() == b"step 1\n"
    mode = stat.S_IMODE(os.stat(got["path"]).st_mode)
    assert mode == 0o444 and not mode & 0o111
    assert stat.S_IMODE(os.stat(os.path.dirname(got["path"])).st_mode) == 0o700
    assert "never execute" in got["notice"]
    again = await hub_adapter.file_fetch({"card_id": card["id"], "file_id": file_id})
    assert again["ok"] and again["path"] == got["path"]


async def test_a_file_that_changed_after_the_check_is_refused(relay, tony, home):
    card = ask(relay, tony)
    path = write(home / "work" / "recipe.md", b"v1")
    checked = await hub_adapter.file_check({"path": str(path)})
    path.write_bytes(b"v2")
    out = await hub_adapter.file_send({"card_id": card["id"], "path": str(path), "allow": checked["sha256"]})
    assert out["ok"] is False


async def test_a_denied_file_is_never_sent(relay, tony, home):
    card = ask(relay, tony)
    path = str(write(home / ".awm-secrets" / "t.json", b"{}"))
    out = await hub_adapter.file_send({"card_id": card["id"], "path": path, "allow": sha(b"{}")})
    assert out["ok"] is False and out["reasons"]
    assert relay.card_files(tony, card["id"]) == []


async def test_file_verbs_refuse_a_caller_from_another_node(relay, tony, home):
    path = str(write(home / "work" / "a.txt"))
    assert (await hub_adapter.file_check({"path": path}, as_="peer:altair"))["ok"] is False
    out = await hub_adapter.file_fetch({"card_id": "a" * 32, "file_id": "b" * 32}, as_="peer")
    assert out["ok"] is False


async def test_fetch_takes_only_minted_ids(relay):
    out = await hub_adapter.file_fetch({"card_id": "../../x", "file_id": "b" * 32})
    assert out["ok"] is False
