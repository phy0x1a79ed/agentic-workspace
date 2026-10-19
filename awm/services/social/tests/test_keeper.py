"""Kept seats: the [seat.*] tables, the keeper loop, the mount refusal and the bot tag."""

from __future__ import annotations

import sqlite3

import pytest

from awm.social.connectors.base import Channel

pytestmark = [pytest.mark.unit]

SEAT_TOML = """
[account.bot]
platform = "discord"
token = "x"

[seat.lab]
account = "bot"
channel = "C1"
node = "altair"
scope = "awm/seat-lab"
brief = "Answer the lab."
"""


def _seat(**kw):
    from awm.social.config import SeatConfig
    base = dict(name="lab", account="bot", channel="C1", node="altair",
                project="awm", scope="seat-lab", brief="Answer the lab.")
    return SeatConfig(**{**base, **kw})


class FakeCx:
    def __init__(self):
        self.rows: list[dict] = []
        self.started: list[dict] = []
        self.stopped: list[str] = []
        self.resumed: list[str] = []
        self.dead: dict[str, dict] = {}
        self.down = False
        self.refuse = None
        self._n = 0

    async def list(self, node):
        from awm.social.keeper import CxUnavailable
        if self.down:
            raise CxUnavailable("no answer")
        return [dict(r) for r in self.rows]

    async def start(self, node, spec):
        self.started.append(spec)
        if self.refuse:
            return {"ok": False, "reason": self.refuse}
        self._n += 1
        job = f"job{self._n:05d}"
        self.rows.append({"job": job, "name": spec["name"], "state": "idle",
                          "tempo": "idle", "parent": None, "caller": "mira"})
        return {"ok": True, "job": job}

    async def stop(self, node, job):
        self.stopped.append(job)
        self.rows = [r for r in self.rows if r["job"] != job]
        self.dead.pop(job, None)
        return {"ok": True, "job": job}

    async def resume(self, node, job):
        self.resumed.append(job)
        if job not in self.dead:
            return {"ok": False, "reason": f"job {job} has no state record; it was removed"}
        self.rows.append(self.dead.pop(job))
        return {"ok": True, "job": job}

    def die(self, job):
        """The session ends the way a crash or a retirement leaves it: resumable."""
        self.dead[job] = self.row(job)
        self.rows.remove(self.dead[job])

    def row(self, job):
        return next(r for r in self.rows if r["job"] == job)


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture()
def kept(awm_workspace, tmp_path):
    from awm.social.dao import SocialDAO, init
    from awm.social.keeper import Keeper

    init()
    dao, cx, clock = SocialDAO(), FakeCx(), Clock()

    async def chat(account, channel):
        return Channel(id=channel, name="Lab#general", kind="text")

    k = Keeper(dao, [_seat()], lambda a: ("discord", True) if a == "bot" else None,
               chat, cx=cx, lock_path=tmp_path / "keeper.lock", clock=clock,
               push=lambda node, job, line: k.pushed.append((node, job, line)))
    k.pushed = []
    return k, cx, dao, clock


# -- config -------------------------------------------------------------------

def test_seat_tables_load_beside_the_accounts(tmp_path):
    from awm.social import config
    path = tmp_path / "social.toml"
    path.write_text(SEAT_TOML)
    (seat,) = config.load_seats(path)
    assert (seat.name, seat.account, seat.channel, seat.node) == ("lab", "bot", "C1", "altair")
    assert (seat.project, seat.scope, seat.model) == ("awm", "seat-lab", None)
    assert seat.compact_at == 200_000
    assert [a.name for a in config.load(path)] == ["bot"]


def test_a_seat_may_compact_later(tmp_path):
    from awm.social import config
    path = tmp_path / "social.toml"
    path.write_text(SEAT_TOML + "compact_at = 400000\n")
    (seat,) = config.load_seats(path)
    assert seat.compact_at == 400_000


@pytest.mark.parametrize("bad", [
    '[seat.lab]\naccount = "bot"\n',
    '[seat.lab]\naccount="bot"\nchannel="C1"\nnode="a"\nscope="noslash"\nbrief="b"\n',
    '[seat.lab]\naccount="bot"\nchannel="C1"\nnode="a"\nscope="p/s"\nbrief="b"\ncolour="red"\n',
    '[seat.lab]\naccount="bot"\nchannel="C1"\nnode="a"\nscope="p/s"\nbrief="b"\ncompact_at=150000\n',
    '[seat.lab]\naccount="bot"\nchannel="C1"\nnode="a"\nscope="p/s"\nbrief="b"\ncompact_at="200k"\n',
    '[seat.a]\naccount="bot"\nchannel="C1"\nnode="a"\nscope="p/s"\nbrief="b"\n'
    '[seat.b]\naccount="bot"\nchannel="C1"\nnode="a"\nscope="p/s"\nbrief="b"\n',
])
def test_a_bad_seat_table_is_refused_and_leaves_the_accounts(tmp_path, bad):
    from awm.social import config
    path = tmp_path / "social.toml"
    path.write_text('[account.bot]\nplatform = "discord"\ntoken = "x"\n' + bad)
    with pytest.raises(config.SocialConfigError):
        config.load_seats(path)
    assert [a.name for a in config.load(path)] == ["bot"]


# -- the loop -----------------------------------------------------------------

async def test_a_missing_agent_is_started_pinned_and_holds_the_seat(kept):
    k, cx, dao, _ = kept
    await k.tick()
    (spec,) = cx.started
    assert spec["name"] == "seat:discord/Lab/general" and spec["pin"] is True
    assert spec["remote_control"] is True
    assert (spec["project"], spec["scope"]) == ("awm", "seat-lab")
    system = spec["system_prompt"]
    assert "Answer the lab." in system and "Monitor" in system
    assert "social history account=bot channel=C1" in system
    assert "compacted" in system and "Answer the lab." not in spec["prompt"]
    assert spec["prompt"].startswith("Start by reading the backlog")
    assert spec["autocompact"] == 233_000
    held = dao.holder("bot", "C1")
    assert (held["node"], held["job"], held["session_name"]) == ("altair", "job00001", "seat:discord/Lab/general")
    assert dao.keeper("lab")["job"] == "job00001"
    assert k.status(k.seats[0])["health"] == "running"


async def test_an_alive_agent_is_left_alone_and_keeps_its_mount(kept):
    k, cx, dao, _ = kept
    await k.tick()
    dao.unmount("bot", "C1")
    await k.tick()
    assert len(cx.started) == 1 and not cx.stopped
    assert dao.holder("bot", "C1")["job"] == "job00001"


async def test_a_stopped_agent_is_replaced_then_backs_off(kept):
    k, cx, dao, clock = kept
    await k.tick()
    await cx.stop("altair", "job00001")
    clock.t += 60
    await k.tick()
    assert [s["name"] for s in cx.started] == ["seat:discord/Lab/general"] * 2
    assert dao.holder("bot", "C1")["job"] == "job00002"
    assert k.status(k.seats[0])["restarts"] == 1
    await cx.stop("altair", "job00002")
    clock.t += 30
    await k.tick()
    assert len(cx.started) == 2
    clock.t += 31
    await k.tick()
    assert len(cx.started) == 3


async def test_an_exited_agent_is_resumed_and_told_what_it_missed(kept):
    k, cx, dao, clock = kept
    await k.tick()
    clock.t += 60
    await k.tick()
    seen = clock.t
    cx.die("job00001")
    dao.unmount("bot", "C1")
    clock.t += 60
    await k.tick()
    assert cx.resumed == ["job00001"] and not cx.stopped and len(cx.started) == 1
    assert dao.keeper("lab")["job"] == "job00001"
    assert dao.holder("bot", "C1")["job"] == "job00001"
    status = k.status(k.seats[0])
    assert status["restarts"] == 0 and status["resumes"] == 1
    ((node, job, line),) = k.pushed
    assert (node, job) == ("altair", "job00001")
    assert f"social history account=bot channel=C1 since={seen:.0f}" in line


async def test_the_last_alive_tick_survives_a_new_keeper(kept, tmp_path):
    from awm.social.keeper import Keeper
    k, cx, dao, clock = kept
    await k.tick()
    clock.t += 60
    await k.tick()
    seen = clock.t
    cx.die("job00001")
    pushed = []
    fresh = Keeper(dao, k.seats, k._describe, k._chat, cx=cx,
                   lock_path=tmp_path / "other.lock", clock=clock,
                   push=lambda *a: pushed.append(a))
    clock.t += 3600
    await fresh.tick()
    assert cx.resumed == ["job00001"]
    assert f"since={seen:.0f}" in pushed[0][2]


async def test_a_refused_resume_falls_back_to_a_new_agent(kept):
    k, cx, dao, _ = kept
    await k.tick()
    await cx.stop("altair", "job00001")
    await k.tick()
    assert cx.resumed == ["job00001"]
    assert dao.keeper("lab")["job"] == "job00002"
    assert k.status(k.seats[0])["restarts"] == 1 and not k.pushed


async def test_a_changed_spec_starts_new_instead_of_resuming(kept):
    k, cx, dao, _ = kept
    await k.tick()
    cx.die("job00001")
    k.seats = [_seat(brief="Answer the lab, briefly.")]
    await k.tick()
    assert not cx.resumed and dao.keeper("lab")["job"] == "job00002"
    assert "briefly" in cx.started[-1]["system_prompt"]


async def test_a_seat_that_keeps_dying_on_resume_gets_a_new_agent(kept):
    from awm.social.keeper import MAX_RESUMES
    k, cx, dao, clock = kept
    await k.tick()
    for _ in range(MAX_RESUMES):
        cx.die("job00001")
        clock.t += 1000
        await k.tick()
    assert len(cx.resumed) == MAX_RESUMES and len(cx.started) == 1
    cx.die("job00001")
    await k.tick()
    assert dao.keeper("lab")["job"] == "job00001", "a resume backs off first"
    clock.t += 1000
    await k.tick()
    assert len(cx.resumed) == MAX_RESUMES and dao.keeper("lab")["job"] == "job00002"


async def test_resumes_settle_after_a_healthy_spell(kept):
    k, cx, _, clock = kept
    await k.tick()
    cx.die("job00001")
    await k.tick()
    clock.t += 601
    await k.tick()
    assert k.status(k.seats[0])["resumes"] == 0


async def test_restarts_settle_after_a_healthy_spell(kept):
    k, cx, _, clock = kept
    await k.tick()
    await cx.stop("altair", "job00001")
    await k.tick()
    clock.t += 601
    await k.tick()
    assert k.status(k.seats[0])["restarts"] == 0


async def test_an_api_error_is_stopped_and_replaced(kept):
    k, cx, dao, _ = kept
    await k.tick()
    cx.row("job00001").update(state="failed", needs="API error")
    await k.tick()
    assert cx.stopped == ["job00001"]
    assert dao.keeper("lab")["job"] == "job00002"


async def test_a_login_block_waits_for_a_person(kept):
    k, cx, _, _ = kept
    await k.tick()
    cx.row("job00001").update(state="blocked", needs="login required — run /login")
    await k.tick()
    assert not cx.stopped and len(cx.started) == 1
    assert k.status(k.seats[0])["health"] == "blocked"


async def test_a_changed_brief_recycles_only_between_turns(kept):
    k, cx, dao, _ = kept
    await k.tick()
    k.seats = [_seat(brief="Answer the lab, briefly.")]
    cx.row("job00001")["tempo"] = "active"
    await k.tick()
    assert not cx.stopped
    cx.row("job00001")["tempo"] = "idle"
    await k.tick()
    assert cx.stopped == ["job00001"]
    assert "briefly" in cx.started[-1]["system_prompt"]
    assert dao.holder("bot", "C1")["job"] == "job00002"
    assert k.status(k.seats[0])["restarts"] == 0


async def test_a_seat_started_under_the_old_name_keeps_its_agent(kept):
    from awm.social.keeper import launch_spec, legacy_hash, legacy_name
    k, cx, dao, _ = kept
    seat = k.seats[0]
    old = {**launch_spec(seat, "discord", True, legacy_name(seat)), "node": "altair"}
    cx.rows.append({"job": "old00001", "name": legacy_name(seat), "state": "idle",
                    "parent": None, "caller": "mira"})
    dao.set_keeper("lab", "altair", "old00001", legacy_hash(old, seat))
    await k.tick()
    await k.tick()
    assert not cx.started and not cx.stopped
    assert dao.keeper("lab")["job"] == "old00001"


async def test_a_renamed_chat_renames_new_agents_only(kept):
    k, cx, dao, _ = kept
    await k.tick()

    async def renamed(account, channel):
        return Channel(id=channel, name="Lab#lounge", kind="text")

    k._chat = renamed
    await k.tick()
    assert [s["name"] for s in cx.started] == ["seat:discord/Lab/general"]


@pytest.mark.parametrize("platform,chat,name", [
    ("discord", ("C1", "Hallam Hive#general", "text"), "seat:discord/Hallam Hive/general"),
    ("slack", ("C2", "arbutus", "private"), "seat:slack/arbutus"),
    ("discord", ("D1", "Tony", "dm"), "seat:discord/DM/Tony"),
    ("slack", ("C3", "", "channel"), "seat:slack/C3"),
])
def test_a_seat_is_named_for_its_app_and_chat(platform, chat, name):
    from awm.social.seats import session_name
    assert session_name(platform, Channel(*chat)) == name


async def test_a_start_that_outlived_its_call_is_adopted(kept):
    k, cx, dao, _ = kept
    cx.rows.append({"job": "late0001", "name": "seat:discord/Lab/general", "state": "idle",
                    "parent": None, "caller": "mira"})
    await k.tick()
    assert not cx.started
    assert dao.keeper("lab")["job"] == "late0001"
    assert dao.holder("bot", "C1")["job"] == "late0001"


async def test_a_late_start_is_adopted_only_once_cx_has_recorded_it(kept):
    """cx refuses to stop or resume a job it holds no lineage for, so a seat
    must not be pinned to one."""
    k, cx, dao, _ = kept
    cx.rows.append({"job": "late0001", "name": "seat:discord/Lab/general", "state": "idle",
                    "parent": None, "caller": "mira", "recorded": False})
    await k.tick()
    assert dao.keeper("lab") is None
    assert not cx.started and not cx.stopped, "the name is taken; nothing is stopped"
    cx.row("late0001")["recorded"] = True
    await k.tick()
    assert dao.keeper("lab")["job"] == "late0001"


async def test_a_stopped_job_cx_has_no_record_of_is_replaced_not_resumed(kept):
    k, cx, dao, clock = kept
    await k.tick()
    job = dao.keeper("lab")["job"]
    cx.die(job)
    cx.rows.append({**cx.dead[job], "state": "stopped", "recorded": False})
    clock.t += 61
    await k.tick()
    assert cx.resumed == []
    assert len(cx.started) == 2
    assert dao.keeper("lab")["job"] != job


async def test_a_session_someone_else_parented_is_not_taken(kept):
    k, cx, dao, _ = kept
    cx.rows.append({"job": "kid00001", "name": "seat:discord/Lab/general", "state": "idle",
                    "parent": "abcd1234", "caller": "altair"})
    await k.tick()
    assert not cx.started and dao.keeper("lab") is None


async def test_nothing_happens_when_cx_does_not_answer(kept):
    k, cx, dao, _ = kept
    cx.down = True
    await k.tick()
    assert not cx.started and dao.holder("bot", "C1") is None


async def test_a_refused_start_backs_off(kept):
    k, cx, _, clock = kept
    cx.refuse = "no daemon"
    await k.tick()
    await k.tick()
    assert len(cx.started) == 1
    clock.t += 61
    await k.tick()
    assert len(cx.started) == 2


async def test_a_seat_on_an_unconnected_account_is_skipped(kept):
    k, cx, _, _ = kept
    k.seats = [_seat(account="other")]
    await k.tick()
    assert not cx.started


async def test_a_second_keeper_cannot_take_the_lock(kept, tmp_path):
    from awm.social.keeper import Keeper
    k, cx, dao, _ = kept
    await k.tick()
    other = Keeper(dao, [_seat()], lambda a: ("discord", True), k._chat,
                   cx=cx, lock_path=tmp_path / "keeper.lock")
    cx.rows.clear()
    await other.tick()
    assert len(cx.started) == 1


async def test_prune_frees_a_dead_holder_and_the_keeper_remounts(kept):
    from awm.social import seats
    k, cx, dao, _ = kept
    await k.tick()

    async def none_live(node):
        return set()

    await seats.prune(dao, list_jobs=none_live)
    assert dao.holder("bot", "C1") is None
    assert dao.keeper("lab")["job"] == "job00001"
    await k.tick()
    assert dao.holder("bot", "C1")["job"] == "job00001"


# -- the hub ------------------------------------------------------------------

async def test_mounting_a_kept_seat_is_refused_and_names_the_agent(kept, monkeypatch):
    import awm.social.hub_adapter as hub
    from tests.test_seats import _FakeConn

    k, _, dao, _ = kept
    await k.tick()
    monkeypatch.setattr(hub, "_dao", dao)
    monkeypatch.setattr(hub, "_keeper", k)
    monkeypatch.setattr(hub, "_connectors", {"bot": _FakeConn([("C1", "lab", "channel")])})
    r = await hub._h_mount({"account": "bot", "channel": "C1", "force": True,
                            "_caller_session": {"kind": "bg", "job": "zzzz9999"}},
                           "peer:altair")
    assert not r["ok"] and "job00001@altair" in r["error"]
    assert dao.holder("bot", "C1")["job"] == "job00001"
    (row,) = (await hub._h_seats({"account": "bot"}))["seats"]
    assert row["kept"]["job"] == "job00001" and row["kept"]["seat"] == "lab"


def test_a_bot_sender_is_tagged_in_the_pushed_line():
    from awm.social.connectors.base import InboundMessage
    from awm.social.seats import push_line
    m = InboundMessage(account="bot", platform="discord", channel_id="C1",
                       text="hello", sender_name="john-shaula-agent", sender_bot=True)
    assert push_line(m) == "discord from john-shaula-agent [bot]: hello"
    assert "[bot]" not in push_line(InboundMessage(
        account="bot", platform="discord", channel_id="C1", text="hi", sender_name="john"))


# -- the schema ---------------------------------------------------------------

def test_a_v4_database_gains_the_keepers_table(awm_workspace):
    from awm.persistence.databases import service_db_path
    from awm.social import dao as dao_mod

    path = service_db_path("social")
    path.parent.mkdir(parents=True, exist_ok=True)
    v4 = dao_mod.SCHEMA_SQL[:dao_mod.SCHEMA_SQL.index("CREATE TABLE IF NOT EXISTS social_keepers")]
    conn = sqlite3.connect(path)
    conn.executescript(v4)
    conn.execute("CREATE TABLE schema_version (version INTEGER)")
    conn.execute("INSERT INTO schema_version VALUES (4)")
    conn.execute("INSERT INTO social_mounts (account, channel_id, node, job) "
                 "VALUES ('bot', 'C1', 'altair', 'aaaa1111')")
    conn.commit()
    conn.close()
    dao_mod.init()
    d = dao_mod.SocialDAO()
    d.set_keeper("lab", "altair", "aaaa1111", "h")
    assert d.keeper("lab")["job"] == "aaaa1111"
    assert d.holder("bot", "C1")["job"] == "aaaa1111"
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 6
    d.keeper_seen("lab", 1234.5)
    assert d.keeper("lab")["seen_at"] == "1234.500"
