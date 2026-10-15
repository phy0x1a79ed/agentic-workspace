"""Cost-based admission for game calls, metered in engine script milliseconds.

Every command reports what it cost the engine (the broker profiles it), and
each connection key draws that cost from its own token bucket. A command always
runs to completion -- nothing in Factorio can interrupt one -- so an expensive
command drives its bucket into debt and that key's next commands wait for the
refill. A caller is delayed, never refused, until its own deadline.

When the server falls below its UPS floor, only keys holding at least half a
burst are admitted: quiet seats keep working while the heavy ones wait.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, str(default)))


RATE_MS_PER_S = _env_float("AWM_FACTORIO_SEAT_MS_PER_S", 50.0)
BURST_MS = _env_float("AWM_FACTORIO_SEAT_BURST_MS", 250.0)
UPS_FLOOR = _env_float("AWM_FACTORIO_UPS_FLOOR", 55.0)
CMD_BUDGET_MS = _env_float("AWM_FACTORIO_CMD_BUDGET_MS", 15.0)
CMD_BUDGET_MAX_MS = _env_float("AWM_FACTORIO_CMD_BUDGET_MAX_MS", 50.0)
UPS_WINDOW_S = 5.0
RATE_WINDOW_S = 10.0
HOLD_LOG_S = 1.0


class Throttled(RuntimeError):
    """The caller's deadline passed while its key was held. Nothing ran."""


class _Key:
    def __init__(self, burst: float, now: float):
        self.level = burst
        self.stamp = now
        self.waiting = 0
        self.inflight = 0
        self.commands = 0
        self.over_budget = 0
        self.spent = deque()          # (t, cost_ms) inside RATE_WINDOW_S
        self.last_hold: dict | None = None


class Throttle:
    def __init__(self, rate_ms_per_s: float = RATE_MS_PER_S,
                 burst_ms: float = BURST_MS, ups_floor: float = UPS_FLOOR,
                 clock=time.monotonic, sleep=time.sleep):
        self.rate = rate_ms_per_s
        self.burst = burst_ms
        self.ups_floor = ups_floor
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._keys: dict[str, _Key] = {}
        self._samples: deque = deque()   # (wall, tick)
        self._paused = False

    def _key(self, key: str, now: float) -> _Key:
        k = self._keys.get(key)
        if k is None:
            k = self._keys[key] = _Key(self.burst, now)
        k.level = min(self.burst, k.level + (now - k.stamp) * self.rate)
        k.stamp = now
        return k

    def ups(self) -> float | None:
        """Updates per second over the sample window; None when unknown or paused."""
        with self._lock:
            if self._paused or len(self._samples) < 2:
                return None
            (t0, k0), (t1, k1) = self._samples[0], self._samples[-1]
        if t1 - t0 < 1.0:
            return None
        return (k1 - k0) / (t1 - t0)

    def note_tick(self, tick: int, paused: bool) -> None:
        now = self._clock()
        with self._lock:
            self._paused = paused
            if self._samples and tick < self._samples[-1][1]:
                self._samples.clear()          # a new world
            self._samples.append((now, tick))
            while self._samples and now - self._samples[0][0] > UPS_WINDOW_S:
                self._samples.popleft()

    def forget_ups(self) -> None:
        with self._lock:
            self._samples.clear()

    def _threshold(self) -> tuple[float, str]:
        ups = self.ups()
        if ups is not None and ups < self.ups_floor:
            return self.burst / 2, f"server at {ups:.0f} UPS (< {self.ups_floor:.0f})"
        return 0.0, "script-time debt"

    def admit(self, key: str, deadline: float) -> float:
        """Block until ``key`` may send. Returns seconds held."""
        start = self._clock()
        with self._lock:
            self._key(key, start).waiting += 1
        try:
            while True:
                threshold, reason = self._threshold()
                now = self._clock()
                with self._lock:
                    k = self._key(key, now)
                    if k.level > threshold:
                        k.inflight += 1
                        held = now - start
                        if held >= HOLD_LOG_S:
                            k.last_hold = {"at": time.time(), "held_s": round(held, 2),
                                           "reason": reason}
                        return held
                    wait = (threshold - k.level) / self.rate + 0.01
                if now + wait > deadline:
                    with self._lock:
                        k.last_hold = {"at": time.time(), "held_s": round(now - start, 2),
                                       "reason": reason, "gave_up": True}
                    raise Throttled(
                        f"held by the game-call throttle ({reason}): {key} owes "
                        f"{max(0.0, threshold - k.level):.0f} ms of script time and "
                        f"refills at {self.rate:.0f} ms/s. Nothing ran. Send fewer "
                        "or cheaper commands; factorio_load shows the meters.")
                self._sleep(min(wait, 1.0, max(0.0, deadline - now)))
        finally:
            with self._lock:
                self._keys[key].waiting -= 1

    def charge(self, key: str, cost_ms: float | None) -> None:
        now = self._clock()
        cost = max(0.0, cost_ms or 0.0)
        with self._lock:
            k = self._key(key, now)
            k.inflight = max(0, k.inflight - 1)
            k.level -= cost
            k.commands += 1
            k.spent.append((now, cost))
            while k.spent and now - k.spent[0][0] > RATE_WINDOW_S:
                k.spent.popleft()

    def note_over_budget(self, key: str) -> None:
        with self._lock:
            self._key(key, self._clock()).over_budget += 1

    def release(self, key: str) -> None:
        """Undo an admit whose command never reached the engine."""
        with self._lock:
            if key in self._keys:
                self._keys[key].inflight = max(0, self._keys[key].inflight - 1)

    def snapshot(self) -> dict:
        ups = self.ups()
        now = self._clock()
        with self._lock:
            keys = {}
            for name, k in sorted(self._keys.items()):
                self._key(name, now)
                ms = sum(c for t, c in k.spent if now - t <= RATE_WINDOW_S)
                keys[name] = {
                    "ms_per_s": round(ms / RATE_WINDOW_S, 2),
                    "level_ms": round(k.level, 1),
                    "debt_ms": round(max(0.0, -k.level), 1),
                    "queued": k.waiting,
                    "inflight": k.inflight,
                    "commands": k.commands,
                    "over_budget": k.over_budget,
                    "last_hold": k.last_hold,
                }
            paused = self._paused
        return {
            "ups": None if ups is None else round(ups, 1),
            "paused": paused,
            "limits": {"seat_ms_per_s": self.rate, "burst_ms": self.burst,
                       "ups_floor": self.ups_floor, "cmd_budget_ms": CMD_BUDGET_MS,
                       "cmd_budget_max_ms": CMD_BUDGET_MAX_MS},
            "keys": keys,
        }


def command_budget(requested) -> float:
    """The per-command budget a caller asked for, clamped to the hard maximum."""
    if requested is None:
        return CMD_BUDGET_MS
    return max(CMD_BUDGET_MS, min(float(requested), CMD_BUDGET_MAX_MS))


def over_budget_message(cost_ms: float, budget_ms: float) -> str:
    return (
        f"OVER_BUDGET: THIS COMMAND RAN; DO NOT RETRY. It used {cost_ms:.1f} ms of "
        f"engine script time against a {budget_ms:.0f} ms budget (one tick is "
        f"16.7 ms). Its effects stand and its output is attached. Split the work: "
        f"survey with factorio_scan, or pass budget_ms (max {CMD_BUDGET_MAX_MS:.0f}) "
        f"for a write you know is heavy.")
