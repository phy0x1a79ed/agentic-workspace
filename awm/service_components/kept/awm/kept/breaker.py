"""The persistent breaker on one kept session's restart attempts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Attempts that did not reach healthy, within `DECAY_S`, before the breaker trips.
BUDGET = 4
DECAY_S = 3600.0
#: The wait after the 1st, 2nd, 3rd and later attempt.
BACKOFF_S = (60.0, 120.0, 300.0, 600.0)
#: A tripped breaker clears itself after this long, or when an operator clears it.
HOLD_S = 6 * 3600.0
#: A session alive this long after an attempt counts as healthy again.
SETTLE_S = 600.0


@dataclass
class Breaker:
    attempts: list[tuple[float, str]] = field(default_factory=list)
    tripped_at: float | None = None
    reason: str = ""
    next_try: float = 0.0

    def tripped(self, now: float) -> bool:
        if self.tripped_at is None:
            return False
        if now - self.tripped_at >= HOLD_S:
            self.clear()
            return False
        return True

    def recent(self, now: float, kind: str | None = None) -> int:
        return sum(1 for at, k in self.attempts
                   if now - at < DECAY_S and (kind is None or k == kind))

    def attempt(self, now: float, kind: str, reason: str = "") -> None:
        """Count one resume or fresh start, and set the wait before the next."""
        self.attempts = [(at, k) for at, k in self.attempts if now - at < DECAY_S]
        self.attempts.append((now, kind))
        n = len(self.attempts)
        self.next_try = now + BACKOFF_S[min(n, len(BACKOFF_S)) - 1]
        if n >= BUDGET:
            self.tripped_at = now
            self.reason = reason or f"{n} attempts in {DECAY_S / 3600:.0f} h did not stay up"

    def healthy(self, now: float, alive_since: float | None) -> bool:
        """Forget the attempts once the session has stayed up `SETTLE_S`.

        True when this call cleared something, so the keeper can say so.
        """
        if not self.attempts or alive_since is None or now - alive_since < SETTLE_S:
            return False
        if alive_since < max(at for at, _ in self.attempts):
            return False
        self.clear()
        return True

    def clear(self) -> None:
        self.attempts.clear()
        self.tripped_at = None
        self.reason = ""
        self.next_try = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"attempts": [[at, k] for at, k in self.attempts], "tripped_at": self.tripped_at,
                "reason": self.reason, "next_try": self.next_try}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Breaker":
        data = data or {}
        return cls(attempts=[(float(at), str(k)) for at, k in data.get("attempts") or []],
                   tripped_at=data.get("tripped_at"), reason=data.get("reason") or "",
                   next_try=float(data.get("next_try") or 0.0))
