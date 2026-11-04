"""Intake hands operations the posts and messages addressed to this swarm, once each."""

from awm.board.intake import Intake


class Ops:
    def __init__(self):
        self.calls = []

    async def __call__(self, verb, args):
        self.calls.append((verb, args))
        return {"ok": True}


async def test_posts_and_messages_to_this_swarm_reach_operations_once(board, events, tony, collins,
                                                                      tmp_path):
    ops = Ops()
    board.post(collins, kind="request", recipient="tony", title="old", body="before intake")
    intake = Intake(events, tony, ops, tmp_path / "cursor.json")
    assert await intake.step() == 0, "the first start skips the history"
    card = board.post(collins, kind="request", recipient="tony", title="Run it", body="please")
    board.post(tony, kind="request", recipient="collins", title="ours", body="x")
    board.post(collins, kind="request", recipient="sammy", title="not to us", body="x")
    board.message(collins, card["id"], "any news?")
    board.message(tony, card["id"], "our own reply")
    await intake.step()
    verbs = [(v, a.get("title") or a.get("body")) for v, a in ops.calls]
    assert verbs == [("intake_post", "Run it"), ("intake_message", "any news?")]
    post = ops.calls[0][1]
    assert post["board_card"] == card["id"] and post["sender"].startswith("collins/john")
    assert await intake.step() == 0 and len(ops.calls) == 2


async def test_a_failed_call_keeps_the_cursor(board, events, tony, collins, tmp_path):
    async def down(verb, args):
        raise ConnectionError("gateway down")

    intake = Intake(events, tony, down, tmp_path / "cursor.json")
    await intake.step()
    board.post(collins, kind="request", recipient="tony", title="Run it", body="please")
    try:
        await intake.step()
    except ConnectionError:
        pass
    ops = Ops()
    intake.call = ops
    await intake.step()
    assert [v for v, _ in ops.calls] == ["intake_post"]
