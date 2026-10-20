"""Kept sessions: what to do when a session that must stay up goes down.

A keeper is a loop that holds one Claude Code session per job it keeps: a
social seat, a front door, a seat holder. This library is the part every keeper
shares. It is pure, with no I/O and no clock of its own, so a keeper persists
its state as plain dicts and a test drives it with a fake clock.

- `Outage` is the restart record: when the session went down and why, when it
  came back, what arrived meanwhile and what the keeper did. It renders into
  the one prompt a resumed or fresh session gets, so the session comes back to
  its normal work knowing why it restarted.
- `decide` says what to do this tick: nothing, wait, back off, resume, start
  fresh, or give up.
- `Breaker` bounds the attempts. It counts attempts that did not reach healthy,
  not attempts per hour, so a slow loop still trips it. Waiting for a daemon
  or a node spends nothing.
"""

from __future__ import annotations

from awm.kept.breaker import BACKOFF_S, BUDGET, DECAY_S, HOLD_S, SETTLE_S, Breaker
from awm.kept.decide import Action, Situation, decide
from awm.kept.outage import Outage, transcript_tail

__all__ = ["Action", "BACKOFF_S", "BUDGET", "Breaker", "DECAY_S", "HOLD_S", "Outage",
           "SETTLE_S", "Situation", "decide", "transcript_tail"]
