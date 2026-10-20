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
        self.adopted: list[str] = []
        self.dead: dict[str, dict] = {}
        self.down = False
        self.daemon = {"state": "up", "pid": 1, "since": None, "cause": None}
        self.refuse = None
        self.refuse_resume = None
        self._n = 0

    async def list(self, node):
        from awm.social.keeper import CxUnavailable
        if self.down:
            raise CxUnavailable("no answer")
        return [dict(r) for r in self.rows], dict(self.daemon)

    def _daemon_down(self):
        if self.daemon["state"] == "down":
            return {"ok": False, "reason": "no claude code daemon is running",
                    "code": "daemon_down"}
        return None

    async def start(self, node, spec):
        self.started.append(spec)
        if self._daemon_down():
            return self._daemon_down()
        if self.refuse:
            return {"ok": False, "reason": self.refuse}
        if spec.get("replaces"):
            await self.stop(node, spec["replaces"])
            self.stopped.pop()
        self._n += 1
        job = f"job{self._n:05d}"
        self.rows.append({"job": job, "name": spec["name"], "state": "idle",
                          "tempo": "idle", "parent": None, "caller": "mira"})
        return {"ok": True, "job": job}

    async def adopt(self, node, job):
        self.adopted.append(job)
        return {"ok": True, "job": job}

    def crash(self, cause="killed", since="2026-10-20T00:09:38.163Z"):
        """The daemon dies and takes every worker with it; each stays resumable."""
        self.daemon = {"state": "down", "pid": None, "since": since, "cause": cause}
        for row in list(self.rows):
            self.die(row["job"])

    def revive(self):
        self.daemon = {"state": "up", "pid": 2, "since": None, "cause": None}

    async def stop(self, node, job):
        self.stopped.append(job)
        self.rows = [r for r in self.rows if r["job"] != job]
        self.dead.pop(job, None)
        return {"ok": True, "job": job}

    async def resume(self, node, job):
        if self._daemon_down():
            return self._daemon_down()
        self.resumed.append(job)
        if self.refuse_resume:
            return {"ok": False, "reason": self.refuse_resume}
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

    async def alert(text):
        k.alerts.append(text)

    k = Keeper(dao, [_seat()], lambda a: ("discord", True) if a == "bot" else None,
               chat, cx=cx, lock_path=tmp_path / "keeper.lock", clock=clock,
               push=lambda node, job, line: k.pushed.append((node, job, line)),
               alert=alert)
    k.pushed = []
    k.alerts = []
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
    assert "because its session ended" in line and "resumed with its own" in line


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
    assert not cx.stopped and cx.started[-1]["replaces"] == "job00001"
    assert "it failed (API error)" in cx.started[-1]["prompt"]
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
    assert len(cx.started) == 1
    cx.row("job00001")["tempo"] = "idle"
    await k.tick()
    assert cx.started[-1]["replaces"] == "job00001"
    assert [r["job"] for r in cx.rows] == ["job00002"]
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
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 7
    d.keeper_seen("lab", 1234.5)
    assert d.keeper("lab")["seen_at"] == "1234.500"


# -- the restart record, the breaker and the daemon ----------------------------

def _iso(t):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def _say(dao, channel, ts, own=False):
    dao.upsert_message("bot", {"channel_id": channel, "message_id": f"m{ts}", "ts": f"{ts}",
                               "sender_id": "u", "sender_name": "someone", "text": "hi",
                               "own": own})


async def test_a_seat_waits_for_its_daemon_and_resumes_the_same_job(kept):
    k, cx, dao, clock = kept
    await k.tick()
    clock.t += 60
    await k.tick()
    down = clock.t + 5
    cx.crash(since=_iso(down))
    for _ in range(20):
        clock.t += 60
        await k.tick()
    assert not cx.resumed and len(cx.started) == 1 and not cx.stopped
    status = k.status(k.seats[0])
    assert status["health"] == "waiting" and "daemon" in status["reason"]
    assert status["attempts"] == 0 and status["down_since"] == _iso(down)[:19] + "+00:00"
    _say(dao, "C1", down + 100)
    _say(dao, "C1", down + 200, own=True)
    cx.revive()
    clock.t += 60
    await k.tick()
    assert cx.resumed == ["job00001"] and dao.keeper("lab")["job"] == "job00001"
    line = k.pushed[-1][2]
    assert "killed without warning" in line and "(21 min)" in line
    assert "1 message arrived meanwhile." in line


async def test_a_resume_refused_for_want_of_a_daemon_spends_nothing(kept):
    k, cx, _, clock = kept
    await k.tick()
    cx.die("job00001")
    cx.daemon = {"state": None}
    real = cx.resume

    async def down(node, job):
        return {"ok": False, "reason": "no daemon", "code": "daemon_down"}

    cx.resume = down
    for _ in range(5):
        clock.t += 60
        await k.tick()
    assert k.status(k.seats[0])["attempts"] == 0 and len(cx.started) == 1
    cx.resume = real
    clock.t += 60
    await k.tick()
    assert cx.resumed == ["job00001"]


async def test_the_breaker_trips_once_and_an_operator_clears_it(kept):
    k, cx, dao, clock = kept
    await k.tick()
    cx.refuse = "the worktree is untrusted"
    cx.refuse_resume = "no state record"
    cx.die("job00001")
    for _ in range(10):
        clock.t += 700
        await k.tick()
    status = k.status(k.seats[0])
    assert status["health"] == "broken" and status["broken"]
    tries = len(cx.started)
    clock.t += 700
    await k.tick()
    assert len(cx.started) == tries
    broken = [a for a in k.alerts if "is broken" in a]
    assert len(broken) == 1 and "keep_reset seat=lab" in broken[0]
    k.reset(k.seats[0])
    cx.refuse = None
    await k.tick()
    assert len(cx.started) == tries + 1 and k.status(k.seats[0])["health"] == "running"


async def test_the_breaker_survives_a_restart_of_social(kept, tmp_path):
    from awm.social.keeper import Keeper
    k, cx, dao, clock = kept
    await k.tick()
    cx.die("job00001")
    clock.t += 60
    await k.tick()
    cx.die("job00001")
    fresh = Keeper(dao, k.seats, k._describe, k._chat, cx=cx,
                   lock_path=tmp_path / "other.lock", clock=clock, push=lambda *a: None)
    clock.t += 10
    await fresh.tick()
    assert cx.resumed == ["job00001"], "the backoff of the first resume still holds"
    assert fresh.status(fresh.seats[0])["resumes"] == 1


async def test_one_alert_after_alert_after_and_one_on_recovery(kept):
    k, cx, _, clock = kept
    await k.tick()
    cx.crash(since=_iso(clock.t))
    for _ in range(4):
        clock.t += 60
        await k.tick()
    assert not k.alerts
    for _ in range(5):
        clock.t += 60
        await k.tick()
    (alert,) = k.alerts
    assert "lab on altair is down since" in alert and "not a message from Tony" in alert
    cx.revive()
    clock.t += 60
    await k.tick()
    clock.t += 60
    await k.tick()
    assert len(k.alerts) == 2 and "lab on altair is back after" in k.alerts[1]
    assert "resumed with its own conversation" in k.alerts[1]
    cx.crash(since=_iso(clock.t))
    for _ in range(10):
        clock.t += 60
        await k.tick()
    assert len(k.alerts) == 2, "at most one alert an hour per seat"


async def test_a_seat_back_within_a_tick_alerts_nobody(kept):
    k, cx, _, clock = kept
    await k.tick()
    cx.die("job00001")
    clock.t += 60
    await k.tick()
    clock.t += 600
    await k.tick()
    assert not k.alerts


async def test_rows_another_node_lists_as_not_alive_do_not_count(kept):
    k, cx, dao, clock = kept
    await k.tick()
    cx.row("job00001")["alive"] = False
    clock.t += 60
    await k.tick()
    assert cx.resumed == ["job00001"] or len(cx.started) == 2


async def test_seats_that_stayed_up_hear_what_arrived_while_social_was_down(kept, tmp_path):
    from awm.social.keeper import Keeper
    k, cx, dao, clock = kept
    await k.tick()
    clock.t += 60
    await k.tick()
    seen = clock.t
    _say(dao, "C1", seen + 30)
    clock.t += 120
    pushed = []
    fresh = Keeper(dao, k.seats, k._describe, k._chat, cx=cx,
                   lock_path=tmp_path / "other.lock", clock=clock,
                   push=lambda *a: pushed.append(a))
    await fresh.tick()
    assert not pushed, "the history sync gets a tick first"
    clock.t += 60
    await fresh.tick()
    clock.t += 60
    await fresh.tick()
    ((_, job, line),) = pushed
    assert job == "job00001" and "social restarted, and 1 message" in line
    assert f"since={seen:.0f}" in line


# -- seats of many chats ----------------------------------------------------------

def test_a_seat_may_hold_every_chat_but_some(tmp_path):
    from awm.social import config
    path = tmp_path / "social.toml"
    path.write_text(SEAT_TOML + '''
[seat.main]
account = "bot"
channels = "all"
except = ["C9"]
node = "altair"
scope = "awm/dev"
brief = "Hold them all."
alert_after = 60
''')
    lab, main = config.load_seats(path)
    assert lab.single and not main.single
    assert main.every_chat and main.except_chats == ("C9",) and main.alert_after == 60.0


@pytest.mark.parametrize("bad", [
    'channel = "C1"\nchannels = ["C2"]\n',
    'channels = []\n',
    'channels = ["C1"]\nexcept = ["C2"]\n',
    'channel = "C2"\nexcept = ["C3"]\n',
    'channels = "some"\n',
])
def test_a_bad_chat_list_is_refused(tmp_path, bad):
    from awm.social import config
    path = tmp_path / "social.toml"
    path.write_text('[account.bot]\nplatform = "slack"\ntoken = "x"\n[seat.m]\n'
                    'account="bot"\nnode="a"\nscope="p/s"\nbrief="b"\n' + bad)
    with pytest.raises(config.SocialConfigError):
        config.load_seats(path)


@pytest.fixture()
def many(kept):
    k, cx, dao, clock = kept
    chats = [Channel(id="C1", name="lab", kind="channel"),
             Channel(id="C2", name="random", kind="channel"),
             Channel(id="C9", name="arbutus", kind="private"),
             Channel(id="D1", name="Alice", kind="dm")]

    async def list_chats(account):
        return list(chats)

    k.seats = [_seat(), _seat(name="main", channel="", every_chat=True, except_chats=("C9",),
                              scope="dev")]
    k._list_chats = list_chats
    return k, cx, dao, clock, chats


async def test_a_seat_of_every_chat_mounts_each_and_new_ones(many):
    k, cx, dao, clock, chats = many
    await k.tick()
    main = next(s for s in cx.started if s["name"] == "seat:discord/main")
    assert "every chat of account bot except C9" in main["system_prompt"]
    job = dao.keeper("main")["job"]
    assert {m["channel_id"] for m in dao.mounts() if m["job"] == job} == {"C2", "D1"}
    assert dao.holder("bot", "C1")["job"] == dao.keeper("lab")["job"]
    assert dao.holder("bot", "C9") is None
    assert k.kept("bot", "D2").name == "main" and k.kept("bot", "C9") is None
    chats.append(Channel(id="C3", name="new", kind="channel"))
    await k.tick()
    assert dao.holder("bot", "C3") is None, "the chats are listed every five minutes"
    clock.t += 300
    await k.tick()
    assert dao.holder("bot", "C3")["job"] == job
    assert len(cx.started) == 2, "a new chat does not change the spec"
    assert k.status(k.seats[1])["chats"] == ["C2", "D1", "C3"]


async def test_a_hand_started_holder_is_adopted_with_its_conversation(many):
    k, cx, dao, _, _ = many
    cx.rows.append({"job": "78dc0104", "name": "seat - slack main", "state": "idle",
                    "parent": None, "caller": "altair"})
    out = await k.adopt(k.seats[1], "78dc0104")
    assert out["ok"] and cx.adopted == ["78dc0104"]
    await k.tick()
    assert dao.keeper("main")["job"] == "78dc0104"
    assert not [s for s in cx.started if s["name"] == "seat:discord/main"]
    assert dao.holder("bot", "D1")["job"] == "78dc0104"
