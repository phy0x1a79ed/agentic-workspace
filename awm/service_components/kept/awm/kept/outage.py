"""The restart record of one outage of a kept session."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

#: What a daemon.log cause means, in the words a session reads.
CAUSES = {
    "killed": "the Claude Code daemon on its node was killed without warning "
              "(an OOM kill, a SIGKILL or a power loss)",
    "signal": "the Claude Code daemon on its node was stopped by a signal",
    "upgrade": "the Claude Code daemon on its node restarted to upgrade",
    "idle_exit": "the Claude Code daemon on its node exited while idle",
    "reboot": "its node rebooted",
}


@dataclass
class Outage:
    down_at: float
    cause: str
    detail: str = ""
    up_at: float | None = None
    missed: int | None = None
    action: str | None = None
    alerted_at: float | None = None
    recovery_alerted: bool = False

    def describe_cause(self) -> str:
        text = CAUSES.get(self.cause, self.cause)
        return f"{text} ({self.detail})" if self.detail else text

    def render(self, *, node: str, what_now: str) -> str:
        """The one line a session gets when it comes back from this outage.

        ``what_now`` tells it how to catch up, for example which command reads
        the messages it missed.
        """
        when = f"from {_hhmm(self.down_at)}"
        if self.up_at is not None:
            when += f" to {_hhmm(self.up_at)} ({_span(self.up_at - self.down_at)})"
        how = {"resumed": "It was resumed with its own conversation.",
               "fresh": "It is a new session. The tail of the previous one follows."}.get(
            self.action or "", "")
        missed = ""
        if self.missed:
            missed = f" {self.missed} message{'s' if self.missed != 1 else ''} arrived meanwhile."
        return " ".join(part for part in (
            f"keeper: this session was down {when} on {node} because {self.describe_cause()}.",
            how, missed.strip(), what_now,
            "Do not announce the restart in the chat.") if part)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Outage | None":
        if not data:
            return None
        known = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        return cls(**known)


def _hhmm(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%MZ")


def _span(seconds: float) -> str:
    minutes = max(0, round(seconds / 60))
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60} min"


def transcript_tail(lines: Iterable[str], turns: int = 6, limit: int = 6000) -> str:
    """The last ``turns`` user and assistant texts of a Claude Code transcript.

    ``lines`` are the transcript's JSONL lines. Tool calls and results are left
    out: a fresh session needs what was said, not what was run.
    """
    said: list[str] = []
    for raw in lines:
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        role = entry.get("type")
        if role not in ("user", "assistant"):
            continue
        content = (entry.get("message") or {}).get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(part.get("text", "") for part in content
                             if isinstance(part, dict) and part.get("type") == "text")
        else:
            text = ""
        text = text.strip()
        if text:
            said.append(f"[{role}] {text}")
    out = "\n\n".join(said[-turns:])
    return out[-limit:]
