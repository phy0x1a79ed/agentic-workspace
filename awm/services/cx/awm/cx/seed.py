"""Starting one warm session.

The launch itself, and the reason it is refused when no Claude Code daemon is
running, live in `awm.claudedaemon.launch`; read its docstring before changing
anything here. This module adds what is particular to the pool: the name a warm
session carries, the directory it starts in, and the test that a new session is
one the pool may hand out.
"""

from __future__ import annotations

import logging
import random

from awm.claudedaemon import launch
from awm.claudedaemon.launch import LAUNCH_TIMEOUT_S, POLL_S, Refused  # noqa: F401
from awm.cx import config, sessions

log = logging.getLogger("awm.cx.seed")

#: Sessions are named so they read as tooling in `claude agents` and nobody
#: deletes one thinking it is abandoned work.
NOUNS = (
    "otter heron badger vole finch stoat lynx marmot quokka teal ibis civet "
    "tapir egret gannet dunlin serval kestrel oryx saiga jerboa numbat quoll "
    "dingo fossa gerbil marten pika ratel shrew skink tanager vireo weka xerus "
    "yapok zorilla auklet bittern chough"
).split()

_UNIT_PREFIX = "awm-cx-seed"


def precondition() -> str | None:
    """Why seeding must not happen right now, or None if it may."""
    if config.want() == 0:
        return "the pool is switched off (AWM_CX_WANT=0)"
    refusal = launch.daemon_refusal(config.roster_path())
    if refusal:
        return refusal
    if sessions.binary_version() is None:
        return f"no claude binary at {config.claude_bin()}"
    seed_dir = config.seed_dir()
    if (seed_dir / "CLAUDE.md").exists():
        return (f"the seed directory {seed_dir} holds a CLAUDE.md, which every "
                "session moved out of it would carry into someone's project")
    return None


async def seed_one() -> str:
    """Start one warm session and return its short id.

    Raises `Refused` if the precondition fails, `TimeoutError` if no new
    session appears.
    """
    refusal = precondition()
    if refusal:
        raise Refused(refusal)

    name = f"{config.name_prefix()}{random.choice(NOUNS)}>"
    s = await launch.launch(
        cwd=config.seed_dir(),
        name=name,
        flags=config.seed_flags(),
        env=config.seed_env(),
        claude=config.claude_bin(),
        unit_prefix=_UNIT_PREFIX,
        roster_path=config.roster_path(),
        jobs_dir=config.jobs_dir(),
        accept=lambda new: sessions.claimable(new, version=sessions.binary_version()),
        label="warm",
    )
    log.info("cx: seeded %s as %s", s.name, s.short)
    return s.short


def _launch_argv(name: str) -> tuple[list[str], str, dict[str, str]]:
    """The command that starts one warm session, how to describe it, and the
    env the command itself needs."""
    return launch.build_argv(
        claude=config.claude_bin(), name=name, flags=config.seed_flags(),
        cwd=config.seed_dir(), env=config.seed_env(), unit_prefix=_UNIT_PREFIX)


_user_manager_env = launch.user_manager_env
