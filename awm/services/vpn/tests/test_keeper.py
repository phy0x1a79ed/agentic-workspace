"""When the keeper re-dials a dead exit, and when it stops: every dial spends a Duo push."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit]


class Lives:
    """A watch stand-in: each call is one dial whose tunnel lives the next duration, or raises."""

    def __init__(self, keeper_mod, lives):
        self.lives, self.now, self.dials, self.mod = list(lives), 0.0, 0, keeper_mod

    def clock(self):
        return self.now

    def __call__(self, profile):
        self.dials += 1
        life = self.lives.pop(0) if self.lives else 0.0
        if isinstance(life, Exception):
            raise life
        self.now += life


@pytest.fixture()
def keeper_mod(monkeypatch):
    from awm.vpn import keeper
    monkeypatch.setattr(keeper, "_BACKOFF_S", (0.0, 0.0, 0.0))
    monkeypatch.setattr(keeper.awm_config, "node_name", lambda: "altair")
    return keeper


@pytest.mark.parametrize("lives, dials", [
    ([RuntimeError("Duo timed out")] * 5, 3),
    ([5.0, 5.0, 5.0, 5.0], 3),
    ([5.0, 5.0, 3600.0, 5.0, RuntimeError("no network"), 5.0], 6),
], ids=["failed dials", "young deaths", "a settled tunnel resets the count"])
def test_the_keeper_stops_and_pages_after_three_short_lives_in_a_row(keeper_mod, lives, dials):
    pages: list[str] = []
    watch = Lives(keeper_mod, lives)
    k = keeper_mod.Keeper("ubc", watch=watch, alert=pages.append, clock=watch.clock)

    k.run()

    assert watch.dials == dials
    assert len(pages) == 1 and "`ubc`" in pages[0] and "altair" in pages[0]


def test_a_held_keeper_does_not_redial(keeper_mod):
    pages: list[str] = []
    k = keeper_mod.Keeper("ubc", alert=pages.append, watch=lambda p: k.hold())

    k.run()

    assert pages == []
