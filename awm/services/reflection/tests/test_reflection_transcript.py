"""Reading a session's transcript: what it took in, and what it is busy with.

These write real .jsonl files, because the thing under test is a parser for
somebody else's format and a fake of that format would only prove the fake
right. The event shapes here are copied from live transcripts.
"""
from __future__ import annotations

import json

import pytest

pytestmark = [pytest.mark.smoke]

from awm.reflection import transcript


SESSION = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    """A projects dir and a matching per-pid record, both redirected to tmp."""
    from awm.reflection import session_target

    projects = tmp_path / "projects"
    (projects / "-home-tony-somewhere").mkdir(parents=True)
    monkeypatch.setattr(transcript, "PROJECTS_DIR", projects)

    records = tmp_path / "sessions"
    records.mkdir()
    (records / "4242.json").write_text(json.dumps({"sessionId": SESSION}))
    monkeypatch.setattr(session_target, "SESSIONS_DIR", records)

    path = projects / "-home-tony-somewhere" / f"{SESSION}.jsonl"
    path.write_text("")
    return path


def append(path, *entries):
    with path.open("a", encoding="utf-8") as fp:
        for entry in entries:
            fp.write(json.dumps(entry) + "\n")


def user_text(text):
    return {"type": "user", "message": {"role": "user",
                                        "content": [{"type": "text", "text": text}]}}


def tool_use(call_id):
    return {"type": "assistant",
            "message": {"content": [{"type": "tool_use", "id": call_id,
                                     "name": "Bash"}]}}


def tool_result(call_id):
    return {"type": "user",
            "message": {"content": [{"type": "tool_result",
                                     "tool_use_id": call_id}]}}


def queue(op, content=None):
    entry = {"type": "queue-operation", "operation": op}
    if content is not None:
        entry["content"] = content
    return entry


def queued_command(prompt):
    return {"type": "attachment",
            "attachment": {"type": "queued_command", "prompt": prompt}}


def at(ms):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000.0, timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def prompt_taken_up(text, when_ms=2_000_000):
    """The entry the CLI writes as it takes a prompt up and starts running it.

    Copied from a live compaction: the content is a bare string, not a block
    list, and it is stamped at the instant the command starts.
    """
    return {"type": "user", "timestamp": at(when_ms),
            "message": {"role": "user", "content": text}}


def compact_boundary(when_ms=2_000_000):
    return {"type": "system", "subtype": "compact_boundary",
            "timestamp": at(when_ms), "content": "Conversation compacted",
            "compactMetadata": {"trigger": "manual", "preTokens": 180000,
                                "postTokens": 20000, "durationMs": 120045}}


# -- finding the file ------------------------------------------------------

def test_the_transcript_is_found_by_id_not_by_guessing_the_directory(sessions):
    assert transcript.path_for(SESSION) == sessions


def test_an_unknown_session_has_no_transcript(sessions):
    assert transcript.path_for("no-such-session") is None
    assert transcript.path_for("") is None


# -- consumption -----------------------------------------------------------

def test_a_plain_user_message_is_consumption(sessions):
    tail = transcript.Tail(4242)
    tail.watch("resume please")
    append(sessions, user_text("resume please"))
    tail.poll()
    assert tail.consumed("resume please") is True
    assert tail.landed("resume please") is True


def test_an_enqueue_has_landed_but_is_not_yet_consumed(sessions):
    tail = transcript.Tail(4242)
    tail.watch("resume please")
    append(sessions, queue("enqueue", "resume please"))
    tail.poll()
    assert tail.landed("resume please") is True
    assert tail.queued("resume please") is True
    assert tail.consumed("resume please") is False


def test_remove_is_the_delivery_path_not_a_drop(sessions):
    # The queue hands a line to the running turn by removing it and attaching it
    # as a queued_command. Reading `remove` as a loss is how a delivered resume
    # gets re-sent.
    tail = transcript.Tail(4242)
    tail.watch("resume please")
    append(sessions,
           queue("enqueue", "resume please"),
           queue("remove", "resume please"),
           queued_command("resume please"))
    tail.poll()
    assert tail.consumed("resume please") is True
    assert tail.queued("resume please") is False


def test_dequeue_drains_everything_outstanding(sessions):
    # A dequeue carries no content: it is the whole queue going into the turn
    # that is starting, which is how a followup queued behind /compact arrives.
    tail = transcript.Tail(4242)
    tail.watch("proceed with the plan")
    append(sessions,
           queue("enqueue", "proceed with the plan"),
           queue("dequeue"))
    tail.poll()
    assert tail.consumed("proceed with the plan") is True


def test_text_nobody_asked_about_is_not_retained(sessions):
    tail = transcript.Tail(4242)
    append(sessions, user_text("something else entirely"))
    tail.poll()
    assert tail.consumed("something else entirely") is False


def test_matching_ignores_surrounding_whitespace(sessions):
    tail = transcript.Tail(4242)
    tail.watch("  resume please\n")
    append(sessions, user_text("resume please"))
    tail.poll()
    assert tail.consumed("resume please\n") is True


# -- execution: has the command actually begun? ---------------------------

def test_a_prompt_taken_up_is_the_command_starting(sessions):
    # The live shape: content is a bare string, and this line is stamped at the
    # moment compaction begins — which is the whole signal the resume fires on.
    tail = transcript.Tail(4242)
    tail.watch("/compact")
    append(sessions, prompt_taken_up("/compact"))
    tail.poll()
    assert tail.started("/compact") is True
    assert tail.consumed("/compact") is True


def test_an_enqueued_command_has_not_started(sessions):
    tail = transcript.Tail(4242)
    tail.watch("/compact")
    append(sessions, queue("enqueue", "/compact"))
    tail.poll()
    assert tail.started("/compact") is False
    assert tail.landed("/compact") is True


def test_a_dequeue_alone_does_not_mean_the_command_started(sessions):
    # The trap. A dequeue drains the whole queue into the turn that is starting,
    # and that turn may run an ordinary prompt first and only reach the slash
    # command at its end. Firing a resume here is "any earlier and it hits before
    # the compact".
    tail = transcript.Tail(4242)
    tail.watch("/compact")
    append(sessions, queue("enqueue", "/compact"), queue("dequeue"))
    tail.poll()
    assert tail.consumed("/compact") is True
    assert tail.started("/compact") is False


def test_the_queue_handing_a_line_over_is_not_execution_either(sessions):
    tail = transcript.Tail(4242)
    tail.watch("/compact")
    append(sessions,
           queue("enqueue", "/compact"),
           queue("remove", "/compact"),
           queued_command("/compact"))
    tail.poll()
    assert tail.consumed("/compact") is True
    assert tail.started("/compact") is False


def test_the_start_line_is_seen_even_when_the_queue_handed_it_over_first(sessions):
    tail = transcript.Tail(4242)
    tail.watch("/compact")
    append(sessions, queue("enqueue", "/compact"), queue("dequeue"),
           prompt_taken_up("/compact"))
    tail.poll()
    assert tail.started("/compact") is True


def test_a_compaction_boundary_is_reported(sessions):
    tail = transcript.Tail(4242)
    tail.poll()
    assert tail.compacted() is False
    append(sessions, compact_boundary())
    tail.poll()
    assert tail.compacted() is True


def test_an_earlier_compactions_start_line_is_not_this_command_starting(sessions):
    # The false positive that would fire a resume before the command ran: the
    # session compacted half an hour ago, so its transcript already holds the
    # identical `/compact` start line and a boundary of its own.
    tail = transcript.Tail(4242)
    tail.watch("/compact")
    append(sessions, prompt_taken_up("/compact", when_ms=1_000_000),
           compact_boundary(when_ms=1_100_000))
    tail.poll()
    assert tail.started("/compact", since_ms=1_500_000) is False
    assert tail.compacted(since_ms=1_500_000) is False
    assert tail.started("/compact", since_ms=900_000) is True
    assert tail.compacted(since_ms=900_000) is True


def test_an_undated_start_line_cannot_answer_a_since_question(sessions):
    tail = transcript.Tail(4242)
    tail.watch("/compact")
    append(sessions, {"type": "user",
                      "message": {"role": "user", "content": "/compact"}})
    tail.poll()
    assert tail.started("/compact") is True
    assert tail.started("/compact", since_ms=1) is False


def test_an_unreadable_transcript_knows_nothing_about_starting(sessions):
    sessions.unlink()
    tail = transcript.Tail(4242)
    tail.watch("/compact")
    tail.poll()
    assert tail.started("/compact") is None
    assert tail.compacted() is None


# -- what the session is busy with ----------------------------------------

def test_an_unanswered_tool_call_is_a_foreground_call_in_flight(sessions):
    tail = transcript.Tail(4242)
    append(sessions, tool_use("toolu_1"))
    tail.poll()
    assert tail.tool_call_in_flight() is True


def test_a_tool_call_that_came_back_leaves_nothing_in_flight(sessions):
    tail = transcript.Tail(4242)
    append(sessions, tool_use("toolu_1"), tool_result("toolu_1"))
    tail.poll()
    assert tail.tool_call_in_flight() is False


def test_a_background_task_leaves_nothing_in_flight(sessions):
    # This is the whole point: a Monitor or a backgrounded Bash hands its result
    # straight back and reports later, so the session shows `shell` with a clean
    # tail. Reading `shell` alone as "mid-turn" is what stalled every resume.
    tail = transcript.Tail(4242)
    append(sessions,
           tool_use("toolu_bg"), tool_result("toolu_bg"),
           tool_use("toolu_fg"), tool_result("toolu_fg"))
    tail.poll()
    assert tail.tool_call_in_flight() is False


def test_a_subagents_own_turns_are_not_this_sessions_work(sessions):
    tail = transcript.Tail(4242)
    sidechain = dict(tool_use("toolu_side"), isSidechain=True)
    append(sessions, sidechain)
    tail.poll()
    assert tail.tool_call_in_flight() is False


# -- reading -------------------------------------------------------------

def test_an_unreadable_transcript_answers_dont_know(sessions):
    sessions.unlink()
    tail = transcript.Tail(4242)
    tail.watch("resume please")
    assert tail.poll() is False
    assert tail.readable is False
    assert tail.consumed("resume please") is None
    assert tail.landed("resume please") is None
    assert tail.tool_call_in_flight() is None


def test_polling_is_incremental_across_calls(sessions):
    tail = transcript.Tail(4242)
    tail.watch("second")
    append(sessions, user_text("first"))
    tail.poll()
    assert tail.consumed("second") is False
    append(sessions, user_text("second"))
    tail.poll()
    assert tail.consumed("second") is True


def test_a_half_written_last_line_is_picked_up_once_it_is_complete(sessions):
    tail = transcript.Tail(4242)
    tail.watch("resume please")
    line = json.dumps(user_text("resume please"))
    with sessions.open("a", encoding="utf-8") as fp:
        fp.write(line[:20])
    tail.poll()
    assert tail.consumed("resume please") is False
    with sessions.open("a", encoding="utf-8") as fp:
        fp.write(line[20:] + "\n")
    tail.poll()
    assert tail.consumed("resume please") is True


def test_a_line_that_is_not_json_is_skipped(sessions):
    tail = transcript.Tail(4242)
    tail.watch("resume please")
    with sessions.open("a", encoding="utf-8") as fp:
        fp.write("{not json at all}\n")
        fp.write(json.dumps(user_text("resume please")) + "\n")
    tail.poll()
    assert tail.consumed("resume please") is True


def test_a_first_poll_reaches_back_far_enough_to_see_the_turn_in_progress(sessions):
    # Opened against a session that is already mid-tool-call, which is the usual
    # case: the tail must not start so late that the outstanding call is invisible.
    append(sessions, tool_use("toolu_1"))
    tail = transcript.Tail(4242)
    tail.poll()
    assert tail.tool_call_in_flight() is True


def test_a_clear_mid_wait_is_followed_to_the_new_transcript(sessions, tmp_path,
                                                            monkeypatch):
    # /clear replaces the conversation in place and mints a new session id. The
    # old file stops growing; following it would watch a dead conversation.
    from awm.reflection import session_target

    tail = transcript.Tail(4242)
    tail.watch("resume please")
    tail.poll()

    cleared = "99999999-8888-7777-6666-555555555555"
    (session_target.SESSIONS_DIR / "4242.json").write_text(
        json.dumps({"sessionId": cleared}))
    fresh = transcript.PROJECTS_DIR / "-home-tony-somewhere" / f"{cleared}.jsonl"
    fresh.write_text("")
    append(fresh, user_text("resume please"))

    tail.poll()
    assert tail.consumed("resume please") is True


def test_a_transcript_that_shrinks_is_re_read_from_the_top(sessions):
    tail = transcript.Tail(4242, from_start=True)
    tail.watch("resume please")
    append(sessions, user_text("filler"), user_text("filler"))
    tail.poll()
    sessions.write_text(json.dumps(user_text("resume please")) + "\n")
    tail.poll()
    assert tail.consumed("resume please") is True
