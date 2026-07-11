"""Tests for awm.notifications — classifier, report lifecycle, dedupe, expiry.

All on an isolated service DB in a temp dir (mirrors the writing pattern).
"""

from __future__ import annotations

import json
import time

import pytest

pytestmark = [pytest.mark.notifications]


# ---------------------------------------------------------------------------
# Fixtures — isolated service DB in a temp dir
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    services_dir = tmp_path / "services"
    services_dir.mkdir()
    monkeypatch.setenv("AWM_WORKSPACE", str(tmp_path))
    import awm.persistence.databases as dbmod
    monkeypatch.setattr(dbmod, "SERVICES_DIR", services_dir, raising=False)
    import awm.notifications.dao as daomod
    monkeypatch.setattr(daomod, "_initialized", False)
    daomod.init()
    yield tmp_path


@pytest.fixture
def conn():
    from awm.notifications import dao
    c = dao.connect()
    yield c
    c.close()


def _report(conn, **event):
    """Run handle_report synchronously."""
    import asyncio
    from awm.notifications import service
    return asyncio.run(service.handle_report(conn, event))


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


class TestIsQuestion:
    def test_trailing_question_mark(self):
        from awm.notifications.classify import is_question
        assert is_question("Should I proceed with the migration?")

    def test_question_on_last_line(self):
        from awm.notifications.classify import is_question
        assert is_question("Done with part one.\n\nWhich approach do you prefer?")

    def test_markdown_wrapped_question(self):
        from awm.notifications.classify import is_question
        assert is_question("**Should I delete the old table?**")

    def test_interrogative_leadin_without_mark(self):
        from awm.notifications.classify import is_question
        assert is_question("Let me know when the credentials are ready.")
        assert is_question("Please confirm the deploy window.")

    def test_ask_tool_use_wins(self):
        from awm.notifications.classify import is_question
        assert is_question("anything", tool_names=("AskUserQuestion",))
        assert is_question(None, tool_names=("ExitPlanMode",))

    def test_plain_statement_is_not_question(self):
        from awm.notifications.classify import is_question
        assert not is_question("All 34 tests pass. The feature is complete.")
        assert not is_question(None)
        assert not is_question("")

    def test_mid_text_question_ending_in_statement(self):
        from awm.notifications.classify import is_question
        # The final line is the ask-surface; an earlier rhetorical ? isn't.
        assert not is_question("Why did this fail? Because of X.\nFixed it; all done.")


class TestTranscriptRead:
    async def test_reads_last_assistant_text(self, tmp_path):
        from awm.notifications.classify import read_last_assistant
        p = tmp_path / "t.jsonl"
        lines = [
            {"type": "user", "message": {"content": [{"type": "text", "text": "hi"}]}},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "First reply."}]}},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "Should I continue?"},
                {"type": "tool_use", "name": "AskUserQuestion"}]}},
        ]
        p.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
        text, tools = await read_last_assistant(str(p), retries=1)
        assert text == "Should I continue?"
        assert tools == ("AskUserQuestion",)

    async def test_missing_file_is_none(self):
        from awm.notifications.classify import read_last_assistant
        text, tools = await read_last_assistant("/nope/missing.jsonl",
                                                retries=1, delay=0.01)
        assert text is None and tools == ()

    async def test_garbled_lines_skipped(self, tmp_path):
        from awm.notifications.classify import read_last_assistant
        p = tmp_path / "t.jsonl"
        p.write_text('not json\n{"type":"assistant","message":{"content":'
                     '[{"type":"text","text":"ok done"}]}}\n{broken\n')
        text, _ = await read_last_assistant(str(p), retries=1)
        assert text == "ok done"


# ---------------------------------------------------------------------------
# Report lifecycle
# ---------------------------------------------------------------------------


class TestReport:
    def test_turn_end_is_always_idle_with_grace(self, conn, tmp_path):
        # Regrounded: turn_end is plain idle regardless of message content —
        # no NLP question fork. A blocked turn resurfaces via Notification.
        from awm.notifications.service import IDLE_GRACE_S
        p = tmp_path / "t.jsonl"
        p.write_text(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Which port should I use?"}]}}) + "\n")
        delta = _report(conn, harness="claude", event="turn_end",
                        session_id="s1", cwd="/w/p", transcript_path=str(p))
        assert delta["type"] == "raise"
        item = delta["item"]
        assert item["kind"] == "idle"
        assert item["notify_at"] == pytest.approx(item["created_at"] + IDLE_GRACE_S)

    def test_turn_end_idle_has_grace(self, conn):
        from awm.notifications.service import IDLE_GRACE_S
        delta = _report(conn, harness="opencode", event="turn_end",
                        session_id="s2", last_message="All tests pass.")
        item = delta["item"]
        assert item["kind"] == "idle"
        assert item["notify_at"] == pytest.approx(
            item["created_at"] + IDLE_GRACE_S)

    def test_notification_is_needs_you(self, conn):
        delta = _report(conn, harness="claude", event="notification",
                        session_id="s3",
                        message="Claude needs your permission to use Bash")
        assert delta["item"]["kind"] == "needs-you"
        assert "permission" in delta["item"]["detail"]

    def test_dedupe_refreshes_not_duplicates(self, conn):
        d1 = _report(conn, harness="opencode", event="turn_end",
                     session_id="s4", last_message="done A")
        d2 = _report(conn, harness="opencode", event="turn_end",
                     session_id="s4", last_message="done B")
        assert d1["type"] == "raise" and d2["type"] == "update"
        assert d1["item"]["id"] == d2["item"]["id"]
        # created_at/notify_at survive the refresh (push gate not reset)
        assert d2["item"]["created_at"] == d1["item"]["created_at"]
        assert "done B" in d2["item"]["snippet"]

    def test_user_prompt_auto_resolves(self, conn):
        from awm.notifications import service
        raised = _report(conn, harness="claude", event="notification",
                         session_id="s5", message="waiting for your input")
        delta = _report(conn, harness="claude", event="user_prompt",
                        session_id="s5")
        assert delta["type"] == "resolve"
        assert raised["item"]["id"] in delta["ids"]
        out = service.list_items(conn)
        open_items = [i for i in out["items"] if i["resolved_at"] is None]
        assert not open_items
        assert out["sessions"]["s5"]["state"] == "working"
        resolved = [i for i in out["items"] if i["id"] == raised["item"]["id"]]
        assert resolved[0]["resolved_by"] == "user_response"

    def test_session_end_resolves(self, conn):
        from awm.notifications import service
        _report(conn, harness="opencode", event="turn_end",
                session_id="s6", last_message="idle now")
        delta = _report(conn, harness="opencode", event="session_end",
                        session_id="s6")
        assert delta["type"] == "resolve"
        out = service.list_items(conn)
        assert out["sessions"]["s6"]["state"] == "ended"

    def test_error_event(self, conn):
        delta = _report(conn, harness="opencode", event="error",
                        session_id="s7", message="ProviderAuthError")
        assert delta["item"]["kind"] == "error"

    def test_bad_event_rejected_without_raise(self, conn):
        assert _report(conn, harness="x", event="nonsense", session_id="s")["ok"] is False
        assert _report(conn, harness="x", event="turn_end", session_id="")["ok"] is False

    def test_needs_you_and_idle_coexist_then_both_resolve(self, conn):
        _report(conn, harness="claude", event="notification",
                session_id="s8", message="permission?")
        _report(conn, harness="opencode", event="turn_end",
                session_id="s8", last_message="finished")
        from awm.notifications import service
        out = service.list_items(conn)
        assert len([i for i in out["items"] if not i["resolved_at"]]) == 2
        _report(conn, harness="claude", event="user_prompt", session_id="s8")
        out = service.list_items(conn)
        assert not [i for i in out["items"] if not i["resolved_at"]]


# ---------------------------------------------------------------------------
# Sweep + verbs
# ---------------------------------------------------------------------------


class TestSweepAndVerbs:
    def test_stale_session_expires_on_list(self, conn):
        from awm.notifications import service
        _report(conn, harness="claude", event="notification",
                session_id="s9", message="q?")
        # Backdate the session far past the TTL.
        conn.execute("UPDATE sessions SET last_seen = ? WHERE session_id = 's9'",
                     (time.time() - service.STALE_TTL_S - 60,))
        conn.commit()
        out = service.list_items(conn)
        row = [i for i in out["items"] if i["session_id"] == "s9"][0]
        assert row["resolved_at"] is not None
        assert row["resolved_by"] == "expired"

    def test_mark_seen_and_resolve_and_clear(self, conn):
        from awm.notifications import service
        d = _report(conn, harness="claude", event="error",
                    session_id="s10", message="boom")
        iid = d["item"]["id"]
        service.mark_seen(conn, iid)
        out = service.list_items(conn)
        assert [i for i in out["items"] if i["id"] == iid][0]["seen_at"]
        assert service.resolve_items(conn, item_id=iid) == [iid]
        _report(conn, harness="claude", event="error",
                session_id="s11", message="boom2")
        assert service.clear_all(conn)["resolved"]
        assert service.stats(conn)["open_by_kind"] == {}


# ---------------------------------------------------------------------------
# Token / context accounting + EOOT
# ---------------------------------------------------------------------------


def _assistant(model, **usage):
    return json.dumps({"type": "assistant", "message": {
        "model": model, "content": [{"type": "text", "text": "ok"}],
        "usage": usage}}) + "\n"


class TestUsageAccounting:
    def test_accumulate_categories_and_context(self, tmp_path):
        from awm.notifications.classify import accumulate_usage
        p = tmp_path / "t.jsonl"
        p.write_text(
            _assistant("claude-sonnet-5", input_tokens=100, output_tokens=50,
                       cache_read_input_tokens=2000,
                       cache_creation_input_tokens=300)
            + _assistant("claude-sonnet-5", input_tokens=10, output_tokens=5,
                         cache_read_input_tokens=4000,
                         cache_creation={"ephemeral_5m_input_tokens": 40,
                                         "ephemeral_1h_input_tokens": 60},
                         cache_creation_input_tokens=100))
        u = accumulate_usage(str(p), 0)
        assert u["add"]["tok_out"] == 55
        assert u["add"]["tok_cache_read"] == 6000
        # first line had only the total (→ 5m bucket), second split 40/60
        assert u["add"]["tok_cache_write_5m"] == 340
        assert u["add"]["tok_cache_write_1h"] == 60
        # context = last turn's in + cache_read + cache_creation total
        assert u["context_tokens"] == 10 + 4000 + 100
        assert u["model"] == "claude-sonnet-5"
        assert u["new_offset"] == p.stat().st_size

    def test_incremental_offset_only_reads_new(self, tmp_path):
        from awm.notifications.classify import accumulate_usage
        p = tmp_path / "t.jsonl"
        p.write_text(_assistant("claude-haiku-4-5", input_tokens=1, output_tokens=1))
        first = accumulate_usage(str(p), 0)
        with open(p, "a") as f:
            f.write(_assistant("claude-haiku-4-5", input_tokens=2, output_tokens=9))
        second = accumulate_usage(str(p), first["new_offset"])
        assert second["add"]["tok_out"] == 9  # only the appended line
        assert first["new_offset"] < second["new_offset"]

    def test_partial_trailing_line_not_consumed(self, tmp_path):
        from awm.notifications.classify import accumulate_usage
        p = tmp_path / "t.jsonl"
        # A complete line + a partial (no newline) line.
        p.write_text(_assistant("claude-opus-4-8", input_tokens=1, output_tokens=1)
                     + '{"type":"assistant","message":{"model":"x"')
        u = accumulate_usage(str(p), 0)
        assert u["add"]["tok_out"] == 1
        # offset stops at the newline, leaving the partial for next time
        assert u["new_offset"] < p.stat().st_size

    def test_eoot_math_and_unknown_model(self):
        from awm.notifications.config import eoot, DEFAULT_RATES
        # sonnet: 2/10/2.5/4/0.2, divisor 25
        v = eoot(100, 50, 300, 0, 2000, model="claude-sonnet-5",
                 rates=DEFAULT_RATES, divisor=25.0)
        assert v == pytest.approx((200 + 500 + 750 + 0 + 400) / 25)
        # unknown model → None (can't price → UI shows a dash)
        assert eoot(1, 1, 0, 0, 0, model="gpt-x", rates=DEFAULT_RATES,
                    divisor=25.0) is None


class TestFleetRoster:
    def test_list_fleet_shape_and_metrics(self, conn, tmp_path):
        from awm.notifications import service
        p = tmp_path / "t.jsonl"
        p.write_text(_assistant("claude-sonnet-5", input_tokens=100,
                                 output_tokens=50, cache_read_input_tokens=2000,
                                 cache_creation_input_tokens=300))
        _report(conn, harness="claude", event="session_start",
                session_id="f1", cwd="/w/proj", tmux_session="awm-f1")
        _report(conn, harness="claude", event="turn_end",
                session_id="f1", cwd="/w/proj", transcript_path=str(p))
        out = service.list_fleet(conn)
        row = [s for s in out["sessions"] if s["session_id"] == "f1"][0]
        assert row["attachable"] is True
        assert row["tmux_session"] == "awm-f1"
        assert row["state"] == "idle"
        assert row["context_tokens"] == 2400
        assert row["eoot"] == pytest.approx(74.0)
        assert row["attention"] == 1  # the idle item
        assert "column_order" in out["config"]

    def test_non_tmux_session_not_attachable(self, conn):
        from awm.notifications import service
        _report(conn, harness="opencode", event="turn_end",
                session_id="f2", last_message="done")
        row = [s for s in service.list_fleet(conn)["sessions"]
               if s["session_id"] == "f2"][0]
        assert row["attachable"] is False
        assert row["tmux_session"] is None

    def test_liveness_window_excludes_old(self, conn):
        from awm.notifications import service
        _report(conn, harness="claude", event="turn_end",
                session_id="f3", last_message="done")
        conn.execute("UPDATE sessions SET last_seen = ? WHERE session_id='f3'",
                     (time.time() - 10_000,))
        conn.commit()
        out = service.list_fleet(conn, window_s=100)
        assert not [s for s in out["sessions"] if s["session_id"] == "f3"]

    def test_list_fleet_surfaces_notifications_flag(self, conn):
        from awm.notifications import service
        cfg = service.list_fleet(conn)["config"]
        assert cfg["notifications_enabled"] is True
        assert "status" in cfg["hidden_columns"]  # redundant → hidden by default


class TestSpawnPlaceholder:
    """The 'spawned' fleet event: an instant 'starting' row keyed by tmux name,
    adopted (not duplicated) once the real agent's hook fires."""

    def test_spawned_plants_starting_row(self, conn):
        from awm.notifications import service
        d = _report(conn, harness="claude", event="spawned",
                    session_id="fleet-x-1", tmux_session="fleet-x-1",
                    cwd="/w/proj")
        assert d["ok"] is True
        row = [s for s in service.list_fleet(conn)["sessions"]
               if s["tmux_session"] == "fleet-x-1"][0]
        assert row["state"] == "starting"
        assert row["attachable"] is True

    def test_real_hook_adopts_placeholder_no_dupe(self, conn):
        from awm.notifications import service
        # placeholder (keyed by tmux name) …
        _report(conn, harness="claude", event="spawned",
                session_id="fleet-x-2", tmux_session="fleet-x-2", cwd="/w/proj")
        # … then the real agent boots and reports under its real session id.
        _report(conn, harness="claude", event="session_start",
                session_id="real-uuid", tmux_session="fleet-x-2",
                cwd="/w/proj", title="proj: work")
        rows = [s for s in service.list_fleet(conn)["sessions"]
                if s["tmux_session"] == "fleet-x-2"]
        assert len(rows) == 1                 # placeholder adopted, not duplicated
        assert rows[0]["session_id"] == "real-uuid"
        assert rows[0]["state"] == "working"

    def test_spawned_skips_when_real_row_already_present(self, conn):
        from awm.notifications import service
        # A fast boot: the real hook beats the spawn RPC.
        _report(conn, harness="claude", event="session_start",
                session_id="real-first", tmux_session="fleet-x-3", cwd="/w/proj")
        _report(conn, harness="claude", event="spawned",
                session_id="fleet-x-3", tmux_session="fleet-x-3", cwd="/w/proj")
        rows = [s for s in service.list_fleet(conn)["sessions"]
                if s["tmux_session"] == "fleet-x-3"]
        assert len(rows) == 1                 # no stray 'starting' dupe
        assert rows[0]["session_id"] == "real-first"
