"""Messaging a workbench conversation, against a stub of its loopback API.

The stub models the three routes the verbs ride: the frame listing, a frame's
transcript page (`from`, `limit`, `total`) and the message post. Nothing here
touches a live workbench, so the post body is checked against what the binary's
handler reads (`input_data.request`), not against a running daemon.
"""

from __future__ import annotations

import json

import httpx
import pytest

from awm.claude_science import api, conversations

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def _frame(fid, name, updated, status="completed", hidden=False):
    return {"id": fid, "root_frame_id": fid, "name": name, "status": status,
            "project_id": "proj_1", "updated_at": updated, "is_hidden": hidden}


def _user(text, ts=1000, notice=False):
    msg = {"role": "user", "content": [{"type": "text", "text": text}],
           "_uuid": "u", "_ts": ts}
    if notice:
        msg["_harness_notice"] = True
    return msg


def _assistant_tool(name):
    return {"role": "assistant", "_ts": 2000,
            "content": [{"type": "thinking", "thinking": "..."},
                        {"type": "tool_use", "id": "t", "name": name,
                         "input": {}}]}


def _tool_result():
    return {"role": "user", "_ts": 3000,
            "content": [{"type": "tool_result", "tool_use_id": "t",
                         "content": "ok"}]}


def _assistant_text(text):
    return {"role": "assistant", "_ts": 4000,
            "content": [{"type": "text", "text": text}]}


class Stub:
    """Just enough of the workbench to route the verbs' requests."""

    def __init__(self, frames, transcripts=None, post_status=200):
        self.frames = frames
        self.transcripts = transcripts or {}
        self.post_status = post_status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        q = request.url.params
        if request.method == "GET" and path == "/api/frames":
            assert q["root_only"] == "true"
            return httpx.Response(200, json=self.frames)
        if request.method == "POST" and path == "/api/projects":
            body = json.loads(request.content)
            assert body["find_or_create"] is True
            return httpx.Response(200, json={"project_id": "proj_9", "name": body["name"]})
        if request.method == "POST" and path == "/api/projects/proj_9/request":
            if self.post_status is None:
                raise httpx.ReadTimeout("slow", request=request)
            return httpx.Response(200, json={
                "root_frame_id": "eeee5555-0", "frame_id": "eeee5555-0",
                "status": "accepted"})
        parts = path.strip("/").split("/")
        fid = parts[2] if len(parts) > 2 else None
        if fid not in {f["id"] for f in self.frames}:
            return httpx.Response(404, json={"detail": f"Frame {fid} not found"})
        if request.method == "GET" and len(parts) == 3:
            return httpx.Response(
                200, json=next(f for f in self.frames if f["id"] == fid))
        if request.method == "GET" and parts[3] == "messages":
            msgs = self.transcripts.get(fid, [])
            limit = int(q["limit"]) if "limit" in q else len(msgs)
            start = (min(int(q["from"]), len(msgs)) if "from" in q
                     else max(0, len(msgs) - limit))
            return httpx.Response(200, json={
                "frame_id": fid, "from": start, "total": len(msgs),
                "messages": msgs[start:start + limit]})
        if request.method == "POST" and parts[3] == "message":
            if self.post_status != 200:
                return httpx.Response(
                    self.post_status, json={"detail": "Frame is awaiting input"})
            return httpx.Response(200, json={
                "root_frame_id": fid, "frame_id": fid, "status": "accepted"})
        return httpx.Response(405)

    def owner(self) -> api.Owner:
        return api.Owner("tok", transport=httpx.MockTransport(self))

    def posts(self):
        return [r for r in self.requests if r.method == "POST"]


FRAMES = [
    _frame("aaaa1111-0", "Optimize Core Dataset", "2026-10-07T20:00:00Z"),
    _frame("bbbb2222-0", "Evaluate Pipeline Methods", "2026-10-08T21:00:00Z"),
    _frame("cccc3333-0", "Hidden thing", "2026-10-09T21:00:00Z", hidden=True),
    _frame("dddd4444-0", "Evaluate Other Pipeline", "2026-10-06T21:00:00Z"),
]


# ---------------------------------------------------------------------------
# Which conversation
# ---------------------------------------------------------------------------

def test_no_reference_means_the_latest_visible_conversation():
    with Stub(FRAMES).owner() as owner:
        got = conversations.resolve_conversation(owner)
    assert got["id"] == "bbbb2222-0"


def test_a_conversation_is_found_by_id_prefix_or_name():
    with Stub(FRAMES).owner() as owner:
        by_prefix = conversations.resolve_conversation(owner, "aaaa")
        by_name = conversations.resolve_conversation(owner, "optimize core dataset")
        by_part = conversations.resolve_conversation(owner, "core")
    assert by_prefix["id"] == by_name["id"] == by_part["id"] == "aaaa1111-0"


def test_an_ambiguous_name_is_refused_with_its_candidates():
    """Posting into the wrong conversation is not undone by a retry."""
    with Stub(FRAMES).owner() as owner:
        with pytest.raises(api.WorkbenchError, match="matches 2") as err:
            conversations.resolve_conversation(owner, "evaluate")
    assert "bbbb2222" in str(err.value) and "dddd4444" in str(err.value)


def test_an_unknown_conversation_and_an_empty_workbench_are_errors():
    with Stub(FRAMES).owner() as owner:
        with pytest.raises(api.WorkbenchError, match="no conversation"):
            conversations.resolve_conversation(owner, "nothing like this")
    with Stub([]).owner() as owner:
        with pytest.raises(api.WorkbenchError, match="no conversations"):
            conversations.resolve_conversation(owner)


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------

def test_a_post_carries_the_text_as_input_data_request_and_returns_a_cursor():
    stub = Stub(FRAMES, {"bbbb2222-0": [_user("hi"), _assistant_text("hello")]})
    with stub.owner() as owner:
        out = conversations.post_message(owner, "bbbb2222-0", "run it again")

    (post,) = stub.posts()
    assert post.url.path == "/api/frames/bbbb2222-0/message"
    assert json.loads(post.content) == {"input_data": {"request": "run it again"}}
    assert post.headers["authorization"] == "Bearer tok"
    assert out["result"]["status"] == "accepted"
    assert out["cursor"] == 2


def test_a_refusal_from_the_workbench_surfaces_with_its_detail():
    stub = Stub(FRAMES, {"bbbb2222-0": []}, post_status=409)
    with stub.owner() as owner:
        with pytest.raises(api.WorkbenchError, match="awaiting input") as err:
            conversations.post_message(owner, "bbbb2222-0", "x")
    assert err.value.status == 409


# ---------------------------------------------------------------------------
# Opening
# ---------------------------------------------------------------------------

def test_a_new_conversation_finds_its_project_and_sends_the_first_turn():
    stub = Stub(FRAMES)
    with stub.owner() as owner:
        out = conversations.open_conversation(owner, "survey the data",
                                              project="Agents", effort="high")

    made, sent = stub.posts()
    assert json.loads(made.content) == {"name": "Agents", "find_or_create": True}
    assert json.loads(sent.content) == {"input_data": {"request": "survey the data"},
                                        "effort": "high"}
    assert out == {"conversation": {"id": "eeee5555-0", "project_id": "proj_9",
                                    "project": "Agents"},
                   "status": "accepted", "cursor": 0, "output_folder": None}


def test_a_new_conversation_gets_its_output_folder(tmp_path, monkeypatch):
    seen = []

    def ensure(owner, pid, root):
        seen.append(pid)
        (tmp_path / "agents").mkdir(exist_ok=True)
        return tmp_path / "agents"

    monkeypatch.setattr(conversations.outputs, "ensure_project", ensure)
    stub = Stub(FRAMES)
    with stub.owner() as owner:
        out = conversations.open_conversation(owner, "x", project="Agents",
                                              root=tmp_path)
    assert seen == ["proj_9"]
    assert out["output_folder"] == str(tmp_path / "agents" / "eeee5555")
    assert (tmp_path / "agents" / "eeee5555").is_dir()


def test_a_create_lost_in_flight_is_not_retried_and_says_so():
    stub = Stub(FRAMES, post_status=None)
    with stub.owner() as owner:
        with pytest.raises(api.WorkbenchError, match="may have started"):
            conversations.open_conversation(owner, "x", project="Agents")
    assert [r.url.path for r in stub.posts()] == ["/api/projects",
                                                  "/api/projects/proj_9/request"]


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

TRANSCRIPT = [
    _user("setup notice", notice=True),
    _user("analyse the data"),
    _assistant_tool("python"),
    _tool_result(),
    _assistant_text("done, 3 outliers"),
]


def test_a_read_from_a_cursor_returns_the_rest_and_the_next_cursor():
    stub = Stub(FRAMES, {"bbbb2222-0": TRANSCRIPT})
    with stub.owner() as owner:
        out = conversations.read_messages(owner, "bbbb2222-0", since=1)

    assert [m["index"] for m in out["messages"]] == [1, 4]
    assert out["messages"][-1]["text"] == "done, 3 outliers"
    assert out["messages"][-1]["role"] == "assistant"
    assert out["cursor"] == 5 and out["total"] == 5 and out["more"] is False
    assert out["conversation"]["status"] == "completed"
    assert not stub.posts()


def test_reading_at_the_cursor_returns_nothing_new():
    stub = Stub(FRAMES, {"bbbb2222-0": TRANSCRIPT})
    with stub.owner() as owner:
        out = conversations.read_messages(owner, "bbbb2222-0", since=5)
    assert out["messages"] == [] and out["cursor"] == 5


def test_a_cursor_past_the_end_is_clamped_not_an_error():
    stub = Stub(FRAMES, {"bbbb2222-0": TRANSCRIPT})
    with stub.owner() as owner:
        out = conversations.read_messages(owner, "bbbb2222-0", since=99)
    assert out["messages"] == [] and out["cursor"] == 5


def test_with_no_cursor_a_read_returns_the_tail():
    stub = Stub(FRAMES, {"bbbb2222-0": TRANSCRIPT})
    with stub.owner() as owner:
        out = conversations.read_messages(owner, "bbbb2222-0", limit=2)
    assert [m["index"] for m in out["messages"]] == [4]
    assert out["cursor"] == 5


def test_tool_plumbing_is_dropped_unless_asked_for_but_the_cursor_passes_it():
    stub = Stub(FRAMES, {"bbbb2222-0": TRANSCRIPT})
    with stub.owner() as owner:
        quiet = conversations.read_messages(owner, "bbbb2222-0", since=2)
        loud = conversations.read_messages(owner, "bbbb2222-0", since=2,
                                           include_tools=True)
    assert [m["index"] for m in quiet["messages"]] == [4]
    assert [m["index"] for m in loud["messages"]] == [2, 3, 4]
    assert loud["messages"][0]["tool_calls"] == ["python"]
    assert loud["messages"][1]["tool_results"] == 1
    assert quiet["cursor"] == loud["cursor"] == 5


def test_a_page_short_of_the_end_says_there_is_more():
    stub = Stub(FRAMES, {"bbbb2222-0": TRANSCRIPT})
    with stub.owner() as owner:
        out = conversations.read_messages(owner, "bbbb2222-0", since=0, limit=2)
    assert out["cursor"] == 2 and out["more"] is True


def test_harness_notices_are_marked_and_timestamps_are_iso():
    stub = Stub(FRAMES, {"bbbb2222-0": TRANSCRIPT})
    with stub.owner() as owner:
        out = conversations.read_messages(owner, "bbbb2222-0", since=0)
    first = out["messages"][0]
    assert first["harness_notice"] is True
    assert first["ts"] == "1970-01-01T00:00:01.000+00:00"


def test_reading_an_unknown_conversation_is_a_404_from_the_workbench():
    with Stub(FRAMES).owner() as owner:
        with pytest.raises(api.WorkbenchError) as err:
            conversations.read_messages(owner, "nope")
    assert err.value.status == 404


# ---------------------------------------------------------------------------
# Pending approvals
# ---------------------------------------------------------------------------

GRANT_SCAN = {"kind": "grant_scan", "requestId": "r1", "tool_id": "t1",
              "grant": "ro:/home/tony/agentic_workspace/projects",
              "unscanned_folders": 4462, "scanned_folders": 5000}


def _blocked(fid, requests=None, descendant=False):
    frame = _frame(fid, "Blocked one", "2026-10-10T00:00:00Z",
                   status="processing")
    frame["output_data"] = {"pending_input_requests": requests or []}
    if descendant:
        frame["output_data"]["descendant_awaiting_input"] = True
    return frame


def test_a_grant_scan_wait_is_reported_with_its_folder():
    got = conversations.pending_approval(_blocked("f", [GRANT_SCAN]))
    assert got["kind"] == "grant_scan" and got["count"] == 1
    assert "/home/tony/agentic_workspace/projects" in got["summary"]
    assert "4462" in got["summary"]


def test_a_sub_agent_wait_is_reported_without_details():
    got = conversations.pending_approval(_blocked("f", descendant=True))
    assert got == {"kind": None, "summary": "a sub-agent awaits input",
                   "count": 1}


def test_an_idle_conversation_has_no_pending_approval():
    assert conversations.pending_approval(FRAMES[0]) is None
    assert "pending_approval" not in conversations.summary(FRAMES[0])


def test_a_read_of_a_blocked_conversation_says_what_it_waits_on():
    frames = [*FRAMES, _blocked("ffff6666-0", [GRANT_SCAN])]
    stub = Stub(frames, {"ffff6666-0": TRANSCRIPT})
    with stub.owner() as owner:
        out = conversations.read_messages(owner, "ffff6666-0")
    assert out["conversation"]["pending_approval"]["kind"] == "grant_scan"


def test_a_message_to_a_blocked_conversation_is_refused_before_posting():
    frames = [*FRAMES, _blocked("ffff6666-0", [GRANT_SCAN])]
    stub = Stub(frames, {"ffff6666-0": TRANSCRIPT})
    with stub.owner() as owner:
        with pytest.raises(api.WorkbenchError, match="awaits grant_scan") as err:
            conversations.post_message(owner, "ffff6666-0", "hello?")
    assert err.value.status == 409
    assert not stub.posts()


def test_active_lists_processing_conversations_with_their_waits():
    frames = [*FRAMES, _blocked("ffff6666-0", [GRANT_SCAN])]
    with Stub(frames).owner() as owner:
        got = conversations.active(owner)
    assert [c["id"] for c in got] == ["ffff6666-0"]
    assert got[0]["pending_approval"]["kind"] == "grant_scan"
