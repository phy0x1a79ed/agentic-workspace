"""``social send`` with a file: the verb's checks, the forward to an owner node,
and each connector's upload."""

from __future__ import annotations

import base64

import pytest

pytestmark = [pytest.mark.unit]


async def _noop(_m):
    return None


def _build(**kw):
    from awm.social.config import AccountConfig
    from awm.social.connectors import build
    return build(AccountConfig(**kw), _noop)


@pytest.fixture()
def plan(tmp_path):
    p = tmp_path / "plan.md"
    p.write_text("# Plan\n")
    return str(p)


class _Conn:
    platform = "slack"
    sends_files = True
    max_file_bytes = 100

    def __init__(self):
        self.sent = []

    async def send(self, channel, text, *, thread=None, files=None):
        self.sent.append((channel, text, thread, files))
        return {"message_id": "", "files": [{"id": "F1"}]}


@pytest.fixture()
def conn(monkeypatch):
    from awm.social import hub_adapter as hub
    c = _Conn()
    monkeypatch.setitem(hub._connectors, "bot", c)
    return c


async def _send(**args):
    from awm.social import hub_adapter as hub
    return await hub.HANDLERS["send"]({"account": "bot", "channel": "D1", **args})


# -- the verb -----------------------------------------------------------------

async def test_file_attaches_under_its_own_name(conn, plan):
    r = await _send(file=plan, text="the plan", thread="1.0")
    assert r["ok"] and r["files"] == [{"id": "F1"}]
    assert conn.sent == [("D1", "the plan", "1.0",
                          [{"filename": "plan.md", "path": plan}])]


async def test_text_is_optional_with_a_file(conn, plan):
    await _send(file=plan)
    assert conn.sent[0][1] == ""
    with pytest.raises(ValueError, match="text, a file, or both"):
        await _send()


async def test_a_folder_a_missing_file_and_an_oversize_file_refuse(conn, plan, tmp_path):
    with pytest.raises(ValueError, match="is a folder"):
        await _send(file=str(tmp_path))
    with pytest.raises(FileNotFoundError):
        await _send(file=str(tmp_path / "gone.md"))
    big = tmp_path / "big.md"
    big.write_bytes(b"x" * 101)
    with pytest.raises(ValueError, match="101 bytes, over the 100"):
        await _send(file=str(big))
    assert conn.sent == []


async def test_gmail_and_teams_refuse_files(monkeypatch, plan):
    from awm.social import hub_adapter as hub
    gmail = _build(name="g", platform="gmail", token="pw", address="a@b.c")
    teams = _build(name="t", platform="teams", token="", source="mira",
                   mira_url="https://h:7822", mira_token="tok")
    for c in (gmail, teams):
        monkeypatch.setitem(hub._connectors, "bot", c)
        with pytest.raises(RuntimeError, match="does not take files through social"):
            await _send(file=plan)


async def test_a_borrower_carries_the_file_inside_the_call(monkeypatch, plan):
    from awm import gatewayclient as gc
    from awm.social import hub_adapter as hub
    calls = []

    async def call_peer(peer, service, fn, args=None, **kw):
        calls.append((peer, fn, args))
        return {"ok": True}

    monkeypatch.setattr(gc, "peer_env", lambda var: "mira")
    monkeypatch.setattr(gc, "call_peer", call_peer)
    monkeypatch.setattr(hub, "_connectors", {})
    await hub.HANDLERS["send"]({"account": "slack-bot", "channel": "D1",
                                "text": "t", "file": plan})
    b64 = base64.b64encode(b"# Plan\n").decode()
    assert calls == [("mira", "send", {"account": "slack-bot", "channel": "D1",
                                       "text": "t",
                                       "files": [{"filename": "plan.md", "b64": b64}]})]


def test_the_manifest_hops_the_file():
    from awm.social import hub_adapter as hub
    fn = next(f for f in hub.API_MANIFEST["functions"] if f["name"] == "send")
    params = {p["name"]: p for p in fn["params"]}
    assert params["file"]["file"] == "in"
    assert not params["text"]["required"]


def test_file_bytes_reads_every_form(plan):
    from awm.social.connectors import file_bytes, file_size
    forms = [{"path": plan}, {"b64": base64.b64encode(b"# Plan\n").decode()},
             {"text": "# Plan\n"}]
    for f in forms:
        assert file_bytes(f) == b"# Plan\n"
        assert file_size(f) == 7


# -- connectors ---------------------------------------------------------------

def test_discord_attaches_a_path(plan):
    from awm.social.connectors.discord_conn import _discord_files
    (f,) = _discord_files([{"filename": "plan.md", "path": plan}])
    assert f.filename == "plan.md" and f.fp.read() == b"# Plan\n"


class _Upload:
    def __init__(self, sink, status):
        self.sink, self.status = sink, status

    def post(self, url, data):
        self.sink.append((url, data))
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _bot(monkeypatch, error=None):
    from awm.social.connectors.slack_bot_conn import SlackApiError
    conn = _build(name="opal", platform="slack", token="xoxb-1", app_token="xapp-1")
    calls, posted = [], []

    async def call(method, **params):
        calls.append((method, params))
        if error and method == error[0]:
            raise SlackApiError(method, error[1])
        if method == "files.getUploadURLExternal":
            return {"upload_url": "https://files.slack.com/up/1", "file_id": "F1"}
        if method == "conversations.open":
            return {"channel": {"id": "D9"}}
        if method == "users.info":
            return {"user": {"name": "tony"}}
        return {"files": [{"id": "F1", "title": "plan.md", "permalink": "https://p"}]}

    monkeypatch.setattr(conn, "_call", call)
    monkeypatch.setattr(conn, "_session", lambda: _Upload(posted, 200))
    return conn, calls, posted


async def test_slack_bot_uploads_in_three_steps(monkeypatch, plan):
    conn, calls, posted = _bot(monkeypatch)
    r = await conn.send("D1", "the plan", thread="1.0",
                        files=[{"filename": "plan.md", "path": plan}])
    assert [m for m, _ in calls] == ["files.getUploadURLExternal",
                                     "files.completeUploadExternal"]
    assert calls[0][1] == {"filename": "plan.md", "length": "7"}
    assert posted == [("https://files.slack.com/up/1", b"# Plan\n")]
    done = calls[1][1]
    assert done["channel_id"] == "D1" and done["initial_comment"] == "the plan"
    assert done["thread_ts"] == "1.0"
    assert r["files"] == [{"id": "F1", "title": "plan.md", "permalink": "https://p"}]


async def test_slack_bot_opens_the_dm_for_a_user_id(monkeypatch, plan):
    conn, calls, _ = _bot(monkeypatch)
    r = await conn.send("U0TONY", "", files=[{"filename": "plan.md", "path": plan}])
    assert r["channel_id"] == "D9"
    assert calls[-1][1]["channel_id"] == "D9"


async def test_slack_bot_without_files_write_refuses(monkeypatch, plan):
    conn, _, posted = _bot(monkeypatch,
                           error=("files.getUploadURLExternal", "missing_scope"))
    with pytest.raises(RuntimeError, match="files:write"):
        await conn.send("D1", "x", files=[{"filename": "plan.md", "path": plan}])
    assert posted == []


async def test_slack_user_uploads_through_the_sdk(monkeypatch, plan):
    conn = _build(name="tony", platform="slack", token="xoxp-1", app_token="xapp-1")
    seen = {}

    class _Web:
        async def files_upload_v2(self, **kw):
            seen.update(kw)
            return {"files": [{"id": "F2", "title": "plan.md"}]}

    monkeypatch.setattr(conn, "_web_client", lambda: _Web())
    r = await conn.send("D1", "the plan", files=[{"filename": "plan.md", "path": plan}])
    assert seen["file_uploads"] == [{"content": b"# Plan\n", "filename": "plan.md",
                                     "title": "plan.md"}]
    assert seen["channel"] == "D1" and seen["initial_comment"] == "the plan"
    assert r["files"][0]["id"] == "F2"


async def test_mira_posts_the_file_as_base64(monkeypatch, plan):
    conn = _build(name="mira", platform="slack", token="", source="mira",
                  mira_url="https://h:7822", mira_token="tok")
    posts = []

    async def post(path, body, timeout=30):
        posts.append((path, body))
        return {"ok": True, "channel_id": "D1", "files": [{"id": "F3"}]}

    monkeypatch.setattr(conn, "_post", post)
    assert conn.sends_files and conn.max_file_bytes == 8 * 1024 * 1024
    r = await conn.send("D1", "the plan", thread="1.0",
                        files=[{"filename": "plan.md", "path": plan}])
    assert posts == [("/v1/slack/upload", {
        "channel": "D1", "text": "the plan", "thread": "1.0",
        "files": [{"filename": "plan.md",
                   "b64": base64.b64encode(b"# Plan\n").decode()}]})]
    assert r["files"] == [{"id": "F3"}]
