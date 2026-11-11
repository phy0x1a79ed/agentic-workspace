"""The session id of ``sessions.md`` R9 and the session name, mirrored from ``awm-proto/src/ids.rs``.

A ``SessionId`` is a two-letter platform code then hex. Its canonical form is the only text
``SessionId.parse`` takes. A harness's own form comes in only through
``SessionId.from_harness_native``, where awm reads that harness's files.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

PLATFORM_CLAUDE = "cc"
PLATFORM_DEEPSEEK = "ds"
PLATFORMS = (PLATFORM_CLAUDE, PLATFORM_DEEPSEEK)
NAME_MAX = 80
_RC_MAX = 64

_HEX = re.compile(r"[0-9a-f]+")
_RC_RUN = re.compile(r"[^A-Za-z0-9._-]+")


class BadSessionId(ValueError):
    """The text is not a canonical session id."""


class BadName(ValueError):
    """The text is not a valid session name."""


@dataclass(frozen=True, order=True)
class SessionId:
    platform: str
    hex: str

    @classmethod
    def parse(cls, text: str) -> SessionId:
        """The id whose canonical form is ``text``: lowercase ``cc`` + 32 hex, or ``ds`` + hex."""
        def bad(why: str) -> BadSessionId:
            return BadSessionId(f"{text!r} is not a canonical session id ({why}); "
                                "the form is cc<32 hex> or ds<hex>")
        if not isinstance(text, str) or len(text) < 2:
            raise bad("too short")
        platform, digits = text[:2], text[2:]
        if platform not in PLATFORMS:
            raise bad("no platform code")
        if not _HEX.fullmatch(digits):
            raise bad("not lowercase hex")
        if platform == PLATFORM_CLAUDE and len(digits) != 32:
            raise bad(f"{len(digits)} hex digits, a cc id has 32")
        return cls(platform, digits)

    @classmethod
    def from_harness_native(cls, platform: str, raw: str) -> SessionId:
        """The id of the harness's own session id ``raw``: a Claude session UUID, or a DeepSeek hex id."""
        try:
            return cls.parse(platform + (raw or "").strip().lower().replace("-", ""))
        except BadSessionId:
            raise BadSessionId(f"{raw!r} is not a {platform} harness session id") from None

    def canonical(self) -> str:
        return self.platform + self.hex

    def harness_native(self) -> str:
        """The form the harness itself uses: a dashed UUID for Claude Code, the hex for DeepSeek."""
        if self.platform != PLATFORM_CLAUDE:
            return self.hex
        h = self.hex
        return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"

    def job_dir(self) -> str | None:
        """The name of the session's ``~/.claude/jobs/`` dir. Only Claude Code sessions have one."""
        return self.hex[:8] if self.platform == PLATFORM_CLAUDE else None

    def __str__(self) -> str:
        return self.canonical()


@dataclass(frozen=True)
class Name:
    """A session's label. It carries no meaning: no routing, no pool state, no id."""
    text: str

    @classmethod
    def new(cls, text: str) -> Name:
        """The name ``text``, trimmed: 1 to 80 characters, no control characters."""
        t = (text or "").strip()
        if not t or len(t) > NAME_MAX or any(ord(c) < 32 or 127 <= ord(c) < 160 for c in t):
            raise BadName(f"a session name is 1 to {NAME_MAX} printable characters, not {text!r}")
        return cls(t)

    def display(self) -> str:
        return self.text

    def rc(self) -> str:
        """The name as Claude Code's ``/rc`` takes it: runs outside ``[A-Za-z0-9._-]`` become one ``-``."""
        out = _RC_RUN.sub("-", self.text).strip("-")[:_RC_MAX].rstrip("-")
        return out or "session"

    def matches(self, text: str) -> bool:
        """Whether ``text`` is this whole name, ignoring case and surrounding space."""
        return self.text.lower() == (text or "").strip().lower()

    def __str__(self) -> str:
        return self.text
