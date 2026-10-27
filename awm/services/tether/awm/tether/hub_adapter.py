"""Hub adapter for the tether service — consent-gated remote assistance.

Registers with the gateway on the shared ``ServiceAdapter`` loop (register →
ready → serve → reconnect), so a tether session is driven from the same surface
as everything else: ``awm tether invite`` in a terminal, ``mcp__awm__tether``
from an agent.

This module is the only Python in the tool and it is thin on purpose. It
declares the verbs and forwards each one to a Rust daemon over a local socket.
Nothing here ever touches session bytes, holds a key, or decides who may
connect — those live in the daemon and the relay, where the vocabulary and the
handshake are one implementation shared by both ends.

Two verbs are answered here rather than forwarded, and for the same reason:
they have to work when the daemon does not. ``logs`` reads the log file, which
is the only account of a daemon that will not start; ``status`` reports what
this host can see of its child before asking the child anything. ``restart``
is answered here too, because it replaces the daemon it would be forwarded to.

Which child this host supervises is the host's role, not a runtime choice: the
operator's node runs the operator daemon, the public host runs the relay. See
``paths.ROLE``.

Run via ``run.sh`` (which the gateway spawns and respawns):
    python -m awm.tether.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter, spawn_supervised

from awm.tether import callers, control, daemon, paths, remote_os, stream

log = logging.getLogger("awm.tether.hub_adapter")

#: What the daemon has told us, and where it went. Held at module level because
#: the watch task, the drain verb and ``status`` all read the same one.
JOURNAL = stream.Journal()

#: How far the stream was recorded, each caller's drain cursor, and the OS each
#: session was invited for. On disk, because the daemon and its sessions
#: outlive this process.
MARK = stream.Mark()
SESSION_LOGS = stream.SessionLogs(mark=MARK)

#: Set once the adapter exists, so the watch task can reach ``emit``. The
#: adapter is constructed in ``main`` and the task is started from ``on_start``,
#: which runs inside it.
ADAPTER: ServiceAdapter | None = None

#: The way back into a session after the agent session that minted it was
#: replaced, which gives it a new identity. Shared by every verb that names one.
ADOPT = {
    "name": "adopt", "type": "boolean",
    "description": "Take over the session `code` names although another agent "
                   "session minted it. Only for a session you minted yourself "
                   "before your own session restarted; never to reach another "
                   "agent's work.",
}

#: Every function carries an explicit ``tool`` name under a ``tether_`` prefix,
#: which is what decides the domain this service appears as: the gateway folds
#: the MCP surface by splitting the projected name on its **first** underscore.
#: The service name is one token for that reason and must stay one token.
API_MANIFEST: dict[str, Any] = {
    "description": (
        "Remote assistance with the owner's consent. You are the operator: you "
        "mint an invite, read the code to the person at the other machine, and "
        "they run one line and answer a prompt before anything connects. They "
        "watch everything you do and either side can cut it. Nothing is "
        "installed on their machine. Both sides keep a record of what was "
        "done, which the owner is shown and agrees to before anything runs. "
        "A machine with no terminal, such as an agent's sandbox, is shown the "
        "prompt with three more words and says yes by running the line again "
        "with `--agree` and those words; the session reads `confirming` "
        "meanwhile. Only whoever read that prompt has the words, so do not "
        "ask for them or guess them. "
        "After `invite`, run the `listen.command` it returns under a "
        "background watcher such as Monitor, and wait for the owner to "
        "connect. Then stop the watcher, unless you were explicitly asked to "
        "keep watching for the owner's input. "
        "A session belongs to the agent session that minted it: every verb "
        "sees and acts on your own sessions only, so `code` is needed only "
        "when you have more than one open. "
        "Know the machine before you invite. Ask the owner explicitly: "
        "Windows PowerShell, WSL, Linux or Mac? Offer WSL as its own option, "
        "because a WSL user may otherwise answer Windows. Then pass "
        "`os: windows` for Windows PowerShell and `os: unix` for WSL, Linux "
        "or Mac, never a guess. Each session then carries its `os` in "
        "`invite`, `status` and `tasks`. Write every command for that shell: "
        "PowerShell on `windows`, POSIX sh on `unix`. When the owner's "
        "client reports a different platform, `os.warning` says so: stop and "
        "ask the owner."
    ),
    "functions": [
        {
            "name": "invite",
            "effect": "secret",
            "tool": "tether_invite",
            "description": (
                "Mint an invite code and open a session for it at the relay. "
                "Requires `os`: ask the owner which kind of computer they "
                "are on first. Returns the words to read out, the one-line "
                "command for that kind of computer only, and `os`, which says "
                "which shell later commands must be written for. Only an "
                "operator can do this; "
                "the owner's side can only redeem. The session expires in "
                "minutes if nobody redeems it, and is destroyed after a few "
                "failed attempts."
            ),
            "params": [
                {"name": "os", "type": "string", "required": True,
                 "if_missing": remote_os.ASK,
                 "description": "`windows` or `unix`. Ask the owner: Windows "
                                "PowerShell, WSL, Linux or Mac? Windows "
                                "PowerShell is `windows` and gets the "
                                "PowerShell launcher. WSL, Linux and Mac are "
                                "`unix` and get the sh launcher. Do not "
                                "guess."},
                {"name": "words", "type": "number",
                 "description": "How many secret words in the code (default "
                                "2), on top of the word that names the "
                                "session. More words cost the owner nothing to "
                                "type."},
            ],
            "timeout": 60,
        },
        {
            "name": "status",
            "effect": "read",
            "tool": "tether_status",
            "description": (
                "Report this host's role, whether its binaries are built and "
                "at what commit, whether the daemon is running, and every live "
                "session with its age, which side is connected, whether it is "
                "`mine`, and its `os`: "
                "the kind of computer declared at invite, the platform the "
                "owner's client reports once connected, the shell to write "
                "commands for, and a `warning` when the two disagree."
            ),
            "params": [],
        },
        {
            "name": "run",
            "effect": "write",
            "tool": "tether_run",
            "description": (
                "Start one command on the owner's machine in a live session. "
                "Returns a task id immediately and does NOT wait: the output "
                "and the exit status arrive on the session stream, so read "
                "them with `drain --until <task>`. The owner watches it "
                "happen; nothing here is hidden from them. Its input is closed "
                "from the start, so a command that reads until end of input "
                "does not wait for a keyboard. It runs in the owner's own "
                "shell, so write it in that shell's syntax: PowerShell on a "
                "`windows` session, POSIX sh on a `unix` one. "
                "The answer repeats the session's `os` and `shell`; read "
                "`status` when you are not sure, and ask the owner rather "
                "than assume."
            ),
            "params": [
                {"name": "command", "type": "string",
                 "description": "The command to run."},
                {"name": "code", "type": "string",
                 "description": "Which of your sessions, named by its slot — "
                                "the number in `status`, or the first word of "
                                "the invite code. Omit when only one of yours "
                                "is live."},
                ADOPT,
                {"name": "limit_s", "type": "number",
                 "description": "Stop the command after this many seconds. "
                                "Bounds one that would never end on a machine "
                                "you are a guest on."},
            ],
            "timeout": 60,
        },
        {
            "name": "shell",
            "effect": "write",
            "tool": "tether_shell",
            "description": (
                "Open a terminal on the owner's machine, rather than running "
                "one command. This is the only way a full-screen program is "
                "usable, and the only way the owner sees it as the screen you "
                "are looking at rather than as escape codes. Type at it with "
                "`keys`, read it with `drain`, and end it with `close`. One "
                "terminal per session: the owner has one screen. The shell is "
                "PowerShell on a `windows` session and the owner's login "
                "shell on a `unix` one, so type what that shell understands."
            ),
            "params": [
                {"name": "cols", "type": "number",
                 "description": "Terminal width (default 120)."},
                {"name": "rows", "type": "number",
                 "description": "Terminal height (default 40)."},
                {"name": "command", "type": "string",
                 "description": "What to run in it. Defaults to the owner's "
                                "own shell."},
                {"name": "code", "type": "string",
                 "description": "Which of your sessions, named by its slot. "
                                "Omit when only one of yours is live."},
                ADOPT,
            ],
            "timeout": 60,
        },
        {
            "name": "keys",
            "effect": "write",
            "tool": "tether_keys",
            "description": (
                "Type at a task. This talks to the PROGRAM, not to the person "
                "— use `send` to say something to them. Confusing the two "
                "types a sentence into somebody's shell."
            ),
            "params": [
                {"name": "task", "type": "number",
                 "description": "Which task, from `run` or `shell`."},
                {"name": "text", "type": "string",
                 "description": "What to type."},
                {"name": "enter", "type": "boolean",
                 "description": "Add a newline after the text."},
                {"name": "data", "type": "string",
                 "description": "Base64 bytes, for keys with no printable "
                                "form. Ctrl-C is `Aw==`. Use instead of "
                                "`text`, not with it."},
                {"name": "code", "type": "string",
                 "description": "Which of your sessions, named by its slot. "
                                "Omit when only one of yours is live."},
                ADOPT,
            ],
            "timeout": 60,
        },
        {
            "name": "resize",
            "effect": "write",
            "tool": "tether_resize",
            "description": (
                "Tell a task its terminal changed size. A full-screen program "
                "redraws itself to fit."
            ),
            "params": [
                {"name": "task", "type": "number", "description": "Which task."},
                {"name": "cols", "type": "number", "description": "New width."},
                {"name": "rows", "type": "number", "description": "New height."},
                {"name": "code", "type": "string",
                 "description": "Which of your sessions, named by its slot. "
                                "Omit when only one of yours is live."},
                ADOPT,
            ],
            "timeout": 60,
        },
        {
            "name": "close",
            "effect": "write",
            "tool": "tether_close",
            "description": (
                "Stop a task. This kills it rather than signalling end of "
                "input. The session carries on."
            ),
            "params": [
                {"name": "task", "type": "number", "description": "Which task."},
                {"name": "code", "type": "string",
                 "description": "Which of your sessions, named by its slot. "
                                "Omit when only one of yours is live."},
                ADOPT,
            ],
            "timeout": 60,
        },
        {
            "name": "tasks",
            "effect": "read",
            "tool": "tether_tasks",
            "description": (
                "What is open right now in a session: each task, what it is, "
                "how long it has been going and how much it has written, and "
                "the session's `os`."
            ),
            "params": [
                {"name": "code", "type": "string",
                 "description": "Which of your sessions, named by its slot. "
                                "Omit when only one of yours is live."},
                ADOPT,
            ],
            "timeout": 60,
        },
        {
            "name": "send",
            "effect": "write",
            "tool": "tether_send",
            "description": (
                "Say a line to the person at the other keyboard. It appears "
                "on their screen. This talks to the PERSON, not to a program "
                "— use `keys` to type at a task. This is how you explain what "
                "you are about to do before you do it, and it reaches them "
                "while they are still deciding whether to let you in."
            ),
            "params": [
                {"name": "text", "type": "string",
                 "description": "What to say."},
                {"name": "code", "type": "string",
                 "description": "Which of your sessions, named by its slot. "
                                "Omit when only one of yours is live."},
                ADOPT,
            ],
            "timeout": 60,
        },
        {
            "name": "cut",
            "effect": "write",
            "tool": "tether_cut",
            "description": (
                "End a session from this side. The owner's client notices and "
                "exits. Either side can do this at any time."
            ),
            "params": [
                {"name": "code", "type": "string",
                 "description": "Which of your sessions, named by its slot. "
                                "Omit when only one of yours is live."},
                ADOPT,
                {"name": "reason", "type": "string",
                 "description": "What to tell the owner. It appears on their "
                                "screen instead of a socket that went quiet."},
            ],
            "timeout": 60,
        },
        {
            "name": "drain",
            "effect": "write",
            "tool": "tether_drain",
            "description": (
                "Read what has happened in your sessions: commands and their "
                "output, how each ended, and both sides of the conversation. "
                "Answered from this host's own buffer, so it still works while "
                "the daemon is restarting. With no cursor it means 'what is "
                "new since I last asked', so you can call it bare. Use "
                "`until` with a task id from `run` to wait for that command to "
                "finish instead of polling for it."
            ),
            "params": [
                {"name": "cursor", "type": "number",
                 "description": "Read from here. Omit to continue from your "
                                "own last read."},
                {"name": "wait", "type": "number",
                 "description": "Seconds to wait for something to arrive "
                                "(default 0, at most 90)."},
                {"name": "until", "type": "number",
                 "description": "Return as soon as this task id has ended."},
                {"name": "limit", "type": "number",
                 "description": "Most events to return (default 500)."},
                {"name": "code", "type": "string",
                 "description": "Only this one of your sessions, named by its "
                                "slot. Omit for all of yours."},
                ADOPT,
                {"name": "all", "type": "boolean",
                 "description": "Every session on this host, not only yours. "
                                "For the node's owner auditing it; an operator "
                                "never needs it."},
                {"name": "task", "type": "number",
                 "description": "Only this task."},
                {"name": "types", "type": "string",
                 "description": "Comma-separated kinds to keep, matched by "
                                "prefix: `owner.` for what the person said, "
                                "`task.` for what ran."},
                {"name": "raw", "type": "boolean",
                 "description": "Keep output as base64 rather than decoding it "
                                "to text. For bytes that are not text, such as "
                                "a terminal's escape sequences."},
            ],
            "timeout": 120,
        },
        {
            "name": "logs",
            "effect": "read",
            "tool": "tether_logs",
            "description": "Tail this host's tether daemon log.",
            "params": [
                {"name": "tail", "type": "number",
                 "description": "Lines to return (default 200)."},
            ],
        },
        {
            "name": "restart",
            "effect": "write",
            "tool": "tether_restart",
            "description": (
                "Replace this host's tether daemon with a fresh one. The daemon "
                "outlives restarts of this service and of the gateway, so this "
                "is the only way to restart it on purpose; a new release "
                "replaces it on its own once no session is live. Ends every "
                "live session, so it is refused while one is unless `force`."
            ),
            "params": [
                {"name": "force", "type": "boolean",
                 "description": "Restart even though sessions are live. They "
                                "end, and each owner has to redeem a new "
                                "invite."},
            ],
            "timeout": 90,
        },
    ],
    "emitters": [
        {
            "topic": "session",
            "description": (
                "Fires once per session event: a phase change, a task starting, "
                "a chunk of its output, how it ended, and either side of the "
                "conversation. Payload is the event, carrying {type, seq, at, "
                "slot} and its own fields — projected on /svc/tether/emit/"
                "session. Emitting is best effort, so a subscriber that must "
                "not miss anything reconciles against `drain` using the `seq` "
                "it last saw. The invite phrase is in no event: the slot is "
                "public, the phrase never leaves the two machines."
            ),
        },
    ],
    "sessions": [],
}

#: The verbs that act on a session, and so mean nothing on the relay host.
#: ``drain`` is not among them: it is answered here and truthfully returns an
#: empty buffer on a host that carries sessions rather than driving them.
SESSION_VERBS = ("invite", "run", "shell", "keys", "resize", "close",
                 "tasks", "send", "cut")

CHILD = daemon.for_role()


# -- handlers ---------------------------------------------------------------


def _unreachable(exc: Exception) -> dict[str, Any]:
    """The same answer for every verb the daemon could not be asked."""
    return {
        "ok": False,
        "role": paths.ROLE,
        "daemon": "unavailable",
        "error": str(exc),
        "hint": f"no tether daemon is answering on this host; "
                f"`awm tether status` says why and `awm tether logs` shows it",
    }


def _forward(verb: str, timeout: float):
    async def handler(args: dict) -> dict:
        if paths.ROLE == paths.RELAY and verb in SESSION_VERBS:
            return {
                "ok": False,
                "role": paths.ROLE,
                "error": "this host is the relay; it carries sessions and "
                         "mints none. Run this verb on the operator's node.",
            }
        try:
            return await control.call(verb, callers.strip(args), timeout=timeout)
        except control.DaemonUnavailable as exc:
            return _unreachable(exc)
    return handler


#: What an operator runs to hear the owner connect, and to hear the owner speak
#: when asked to keep watching. Leaves out ``task.output``, whose chunks would
#: flood a watcher, and every other session's events, which are not this
#: operator's business.
LISTEN_THEN = (
    "Run the command under a background watcher, such as Monitor, and wait "
    "for the owner to connect. It prints this session's events only. A "
    "`session.phase` line with `phase: open` means the owner connected: stop "
    "the watcher then, unless you were explicitly asked to keep watching for "
    "the owner's input. A `phase: ended` line means the session is over: stop "
    "the watcher. While you keep watching, each line is a doorbell, not the "
    "record: call `drain` with `types: owner.` to read what the owner said, "
    "then answer with `send`. A `listen.reconnected` line means events may "
    "have been missed, so call `drain`."
)

#: How long `invite` waits for its own mint to come round on the stream, so it
#: can name the session id the watcher filters on.
MINT_WAIT_S = 5.0


def _listen(slot: int | None, sid: str | None) -> dict[str, str]:
    where = f"session={sid}" if sid else f"slot={slot}"
    return {
        "command": f"awm services listen tether session --match owner.,session. "
                   f"--where {where}",
        "then": LISTEN_THEN,
    }


async def _await_owner(slot: int) -> str | None:
    """The session id of the mint ``slot`` was just given, once it is known.

    None when the stream is down, since the mint cannot arrive over it then.
    """
    if not JOURNAL.head()["connected"]:
        return None
    deadline = asyncio.get_running_loop().time() + MINT_WAIT_S
    while slot in MARK.claims:
        left = deadline - asyncio.get_running_loop().time()
        if left <= 0:
            return None
        await JOURNAL.wait(left)
    return MARK.current(slot)


async def invite(args: dict, as_: str | None = None) -> dict:
    """Mint a session for one declared kind of computer, and say so.

    Refused before reaching the daemon when the OS is missing or unknown: a
    session minted on a guess is the failure this exists to prevent. The
    caller is recorded as the session's owner before the answer goes back.
    """
    forward = _forward("invite", 60)
    if paths.ROLE == paths.RELAY:
        return await forward(args)
    os_name = args.get("os")
    refused = remote_os.refusal(os_name)
    if refused:
        return {"ok": False, "error": refused}
    caller = callers.of(args, as_)
    # Every event of the mint about to happen comes after this one.
    floor = JOURNAL.head()["next"]
    answer = await forward({k: v for k, v in args.items() if k != "os"})
    if not isinstance(answer, dict) or answer.get("ok") is False or "error" in answer:
        return answer
    choice = remote_os.CHOICES[os_name]
    line = remote_os.launcher_line(os_name, answer.get("command") or "")
    slot = answer.get("slot")
    sid = None
    if isinstance(slot, int):
        MARK.declared[slot] = {"os": os_name,
                          "word": str(answer.get("code") or "").split(" ")[0]}
        sid = MARK.claim(slot, caller.id, caller.label, floor) or await _await_owner(slot)
    return {
        **answer,
        "session": sid,
        "command": line,
        "read_out": f"{choice['open']}, then answer the prompt it shows you: "
                    f"{line}",
        "os": remote_os.describe(os_name),
        "listen": _listen(slot, sid),
    }


def _os_of(session: dict) -> dict[str, Any]:
    declared = MARK.declared.get(session.get("slot"), {}).get("os")
    reported = (session.get("owner") or {}).get("os")
    return remote_os.describe(declared, reported)


async def _live_sessions() -> list[dict]:
    try:
        report = await control.call("status", {}, timeout=30)
    except control.DaemonUnavailable:
        return []
    return [s for s in report.get("sessions") or [] if s.get("phase") != "ended"]


def _slot_named(code: Any, sessions: list[dict]) -> int | None:
    """The slot ``code`` names: its number, or the word the owner was read."""
    first = str(code).split()[0] if str(code).split() else ""
    if first.isdigit():
        return int(first)
    for s in sessions:
        word = MARK.declared.get(s.get("slot"), {}).get("word")
        code_word = str(s.get("code") or "").split(" ")[0]
        if first in (word, code_word):
            return s.get("slot")
    return None


def _owner_of(slot: int) -> dict | None:
    """Who minted the session on ``slot`` now, or None when nobody recorded it.

    A claim still waiting for its mint is the newest session on that slot, so
    it answers before the owner of whatever mint the stream showed last.
    """
    if slot in MARK.claims:
        return MARK.claims[slot]
    sid = MARK.current(slot)
    return MARK.owners.get(sid) if sid else None


def _take(slot: int, caller: callers.Caller) -> None:
    sid = MARK.current(slot)
    if sid is not None and slot not in MARK.claims:
        MARK.owners[sid] = {"by": caller.id, "label": caller.label}
        MARK.save()


def _admit(caller: callers.Caller, slot: int, adopt: bool) -> str | None:
    """Why ``caller`` may not use the session on ``slot``, or None if it may.

    A session minted before owners were recorded is nobody's yet. A caller
    who names one takes it, so the first operator to name a session it was
    already using keeps it from then on.
    """
    owner = _owner_of(slot)
    if caller.owns(owner):
        return None
    if owner is None or adopt:
        if caller.id is not None:
            _take(slot, caller)
        return None
    return (f"session {slot} belongs to another operator ({owner.get('label')}); "
            f"this agent session can only see and act on sessions it minted. "
            f"Mint your own with `invite`.")


def _yours(caller: callers.Caller, slot: int) -> bool:
    owner = _owner_of(slot)
    return caller.owns(owner) or (caller.id is None and owner is None)


async def _target(caller: callers.Caller, args: dict) -> tuple[dict, str | None]:
    """``args`` aimed at one of the caller's sessions, or why there is none.

    The daemon is always handed an explicit slot, because its own default, the
    only live session, may be somebody else's.
    """
    sessions = await _live_sessions()
    adopt = callers.truthy(args.get("adopt"))
    code = args.get("code")
    if code:
        slot = _slot_named(code, sessions)
        if slot is None:
            return args, f"`{code}` names no live session of yours"
        refused = _admit(caller, slot, adopt)
        return {**args, "code": str(slot)}, refused
    mine = [s["slot"] for s in sessions if _yours(caller, s.get("slot"))]
    if len(mine) == 1:
        return {**args, "code": str(mine[0])}, None
    if mine:
        return args, (f"you have {len(mine)} sessions open: name one with "
                      f"`code`, one of {', '.join(map(str, mine))}")
    unowned = [s["slot"] for s in sessions if _owner_of(s.get("slot")) is None]
    if unowned:
        return args, (f"no session minted by this agent session is open. Session "
                      f"{', '.join(map(str, unowned))} has no recorded operator, "
                      f"because it was minted before sessions had owners: if it "
                      f"is yours, name it with `code` and it becomes yours.")
    others = f" ({len(sessions)} belonging to other operators)" if sessions else ""
    return args, (f"no session minted by this agent session is open on this "
                  f"host{others}; mint one with `invite`")


def _scoped(verb: str, timeout: float, *, os_detail: str | None = None):
    """Forward ``verb`` to the caller's own session, then say which machine it is.

    ``os_detail`` is None for verbs that do not report the machine, ``brief``
    for the fields a command needs, and ``full`` for everything.
    """
    forward = _forward(verb, timeout)

    async def handler(args: dict, as_: str | None = None) -> dict:
        if paths.ROLE == paths.RELAY:
            return await forward(args)
        caller = callers.of(args, as_)
        aimed, refused = await _target(caller, callers.strip(args))
        if refused:
            return {"ok": False, "error": refused}
        aimed.pop("adopt", None)
        answer = await forward(aimed)
        if os_detail is None or not isinstance(answer, dict) \
                or answer.get("ok") is False or "error" in answer:
            return answer
        sessions = await _live_sessions()
        slot = _slot_named(aimed.get("code") or "", sessions)
        session = next((s for s in sessions if s.get("slot") == slot), None)
        if session is None:
            return answer
        about = _os_of(session)
        if os_detail == "brief":
            about = {k: about[k] for k in ("declared", "reported", "shell",
                                           "mismatch", "warning")
                     if k in about}
        return {**answer, "os": about}
    return handler


def _release_report() -> dict[str, Any]:
    """What version this host is pinned to, and what it is actually running.

    A developer pointing ``AWM_TETHER_BIN`` at their own build runs no fetch, so
    there is no VERSION to compare — say that plainly rather than report a
    permanent mismatch. Otherwise an installed version behind the pin means the
    last fetch did not finish, and the fetch log's last line says why.
    """
    pinned = paths.pinned_release()
    if paths.BIN_OVERRIDE:
        return {"pinned": pinned, "source": f"AWM_TETHER_BIN={paths.BIN_OVERRIDE}"}
    installed = paths.installed_release()
    report: dict[str, Any] = {
        "pinned": pinned,
        "installed": installed,
        "in_sync": pinned == installed,
    }
    if not report["in_sync"]:
        tail = daemon.tail(paths.FETCH_LOG, 1)
        if tail:
            report["last_fetch"] = tail[-1]
    return report


async def status(args: dict, as_: str | None = None) -> dict:
    """What this host knows, then what its daemon knows.

    Answered locally first so it still says something useful when the daemon is
    the thing that is wrong — which is the only time anybody reads it closely.
    """
    report: dict[str, Any] = {
        "ok": True,
        "child": await asyncio.to_thread(CHILD.snapshot),
        # The release this host is pinned to versus the one it actually fetched,
        # so a fetch that failed on start shows as a version that lags the pin
        # rather than as a binary nobody can explain.
        "release": _release_report(),
        # Where this host's own buffer stands, which is what `drain` reads and
        # is not the same thing as the daemon's. A caller comparing the two can
        # see whether the watch connection is keeping up.
        "stream": JOURNAL.head(),
    }
    if paths.ROLE == paths.RELAY:
        # The relay's own status is behind its bearer and reachable only over
        # the network it serves. What this host can say is that it is running.
        return report | {"role": paths.ROLE, "sessions": []}
    try:
        report |= await control.call("status", {}, timeout=30)
    except control.DaemonUnavailable as exc:
        report |= _unreachable(exc)
        return report
    sessions = report.get("sessions") or []
    gone = set(MARK.declared) - {s.get("slot") for s in sessions}
    if gone:
        for slot in gone:
            MARK.declared.pop(slot, None)
        MARK.save()
    caller = callers.of(args, as_)
    report["sessions"] = [_as_seen_by(caller, s) for s in sessions]
    return report


def _as_seen_by(caller: callers.Caller, session: dict) -> dict:
    """One session's report, with whose it is and, if not the caller's, no phrase."""
    slot = session.get("slot")
    owner = _owner_of(slot) if isinstance(slot, int) else None
    seen = {**session, "os": _os_of(session),
            "session": MARK.current(slot) if isinstance(slot, int) else None,
            "mine": _yours(caller, slot) if isinstance(slot, int) else False}
    if not seen["mine"]:
        seen["code"] = str(session.get("code") or "").split(" ")[0]
        seen["operator"] = owner.get("label") if owner else None
    return seen


def _decoded(events: list[dict], raw: bool) -> list[dict]:
    """Turn chunks back into something a person can read.

    Output travels as bytes because a terminal's stream is escape sequences and
    a command's can split a character across a chunk boundary. Consecutive
    chunks of the same task and stream are joined before decoding, so a
    character that was split in transit is whole again by the time anyone sees
    it. That healing is only possible here, where the pieces are together.
    """
    import base64

    out: list[dict] = []
    for event in events:
        if event.get("type") != "task.output":
            out.append(event)
            continue
        last = out[-1] if out else None
        if (
            last is not None
            and last.get("type") == "task.output"
            and last.get("task") == event.get("task")
            and last.get("stream") == event.get("stream")
        ):
            last["_chunks"].append(event.get("data") or "")
            last["bytes"] = last.get("bytes", 0) + event.get("bytes", 0)
            continue
        joined = dict(event)
        joined["_chunks"] = [event.get("data") or ""]
        out.append(joined)

    for event in out:
        chunks = event.pop("_chunks", None)
        if chunks is None:
            continue
        # Decoded one chunk at a time and joined as bytes. Each chunk was
        # encoded on its own and carries its own padding, so joining the
        # encoded strings and decoding once would drop everything after the
        # first chunk that did not land on a three-byte boundary.
        blob = b""
        for chunk in chunks:
            try:
                blob += base64.b64decode(chunk)
            except Exception:  # noqa: BLE001 — one bad chunk is not the whole stream
                continue
        if raw:
            event["data"] = base64.b64encode(blob).decode("ascii")
            continue
        event.pop("data", None)
        event["text"] = blob.decode("utf-8", errors="replace")
    return out


async def drain(args: dict, as_: str | None = None) -> dict:
    """What has happened in the caller's sessions, from where it last looked.

    Answered here rather than forwarded, which is the point rather than a
    shortcut: the moment somebody most wants to know what a session did is the
    moment its daemon has just died. So a session named by ``code`` is
    resolved against this host's own records, not the daemon's table.
    """
    def number(name: str, default: int | None = None) -> int | None:
        value = args.get(name)
        if value is None:
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    caller = callers.of(args, as_)
    who = caller.id or as_ or "-"
    cursor = number("cursor")
    if cursor is None:
        # A caller with no cursor of its own starts where the one cursor every
        # caller used to share had got to, rather than at the start of the ring.
        cursor = MARK.cursors.get(who, MARK.cursors.get(as_ or "-"))
    limit = max(1, min(number("limit", 500) or 500, 2000))
    hold = max(0.0, min(float(number("wait", 0) or 0), 90.0))
    until = number("until")
    slot = None
    code = args.get("code")
    if code:
        slot = _slot_named(code, await _live_sessions())
        if slot is None:
            return {"ok": False, "error": f"`{code}` names no session: give its "
                                          f"slot number, as `status` shows it"}
    if callers.truthy(args.get("all")):
        sessions = None
    elif slot is not None:
        refused = _admit(caller, slot, callers.truthy(args.get("adopt")))
        if refused:
            return {"ok": False, "error": refused}
        sessions = {MARK.current(slot)}
    else:
        sessions = _sessions_of(caller)
    types = tuple(t.strip() for t in str(args.get("types") or "").split(",") if t.strip())

    if hold:
        found, _, _ = JOURNAL.since(cursor, limit=limit, slot=slot,
                                    task=number("task"), types=types,
                                    sessions=sessions)
        if not found:
            await JOURNAL.wait(hold, until=until, sessions=sessions)

    events, nxt, gap = JOURNAL.since(cursor, limit=limit, slot=slot,
                                     task=number("task"), types=types,
                                     sessions=sessions)
    if MARK.cursors.get(who) != nxt:
        MARK.cursors[who] = nxt
        MARK.save()
    head = JOURNAL.head()
    return {
        "ok": True,
        "epoch": head["epoch"],
        "cursor": nxt,
        "more": nxt < head["next"],
        "gap": gap,
        "events": _decoded(events, bool(args.get("raw"))),
    }


def _sessions_of(caller: callers.Caller) -> set[str]:
    """Every session id this host knows ``caller`` minted, ended ones included."""
    known = {g["sid"] for gens in MARK.gens.values() for g in gens}
    mine = {sid for sid in known if caller.owns(MARK.owners.get(sid))}
    if caller.id is None:
        mine |= known - set(MARK.owners)
    return mine


async def logs(args: dict) -> dict:
    lines = args.get("tail")
    try:
        lines = int(lines) if lines is not None else 200
    except (TypeError, ValueError):
        lines = 200
    return {
        "ok": True,
        "path": str(paths.DAEMON_LOG),
        "lines": daemon.tail(paths.DAEMON_LOG, lines),
    }


async def restart(args: dict) -> dict:
    force = args.get("force") in (True, "true", "True", "1", 1)
    answer = await asyncio.to_thread(CHILD.restart, force=force)
    return {"ok": answer.get("ok", answer.get("action") == "started"), **answer}


# Every forwarded verb is the same forward, so the table is built rather than
# written out: a handler that diverged from its manifest entry is a bug nobody
# would see until the verb was called.
HANDLERS: dict[str, Any] = {
    fn["name"]: _forward(fn["name"], float(fn.get("timeout", 30)))
    for fn in API_MANIFEST["functions"]
}
for _verb in ("keys", "resize", "close", "send", "cut"):
    HANDLERS[_verb] = _scoped(_verb, 60)
HANDLERS["invite"] = invite
HANDLERS["run"] = _scoped("run", 60, os_detail="brief")
HANDLERS["shell"] = _scoped("shell", 60, os_detail="brief")
HANDLERS["tasks"] = _scoped("tasks", 60, os_detail="full")
HANDLERS["status"] = status
HANDLERS["logs"] = logs
HANDLERS["drain"] = drain
HANDLERS["restart"] = restart


async def _emit(topic: str, payload: dict) -> None:
    """Announce one event, if anything is listening. Never raises."""
    if ADAPTER is None:
        return
    await ADAPTER.emit(topic, payload)


async def _keep_watching() -> None:
    """Hold the daemon's event stream open for the life of this process."""
    await stream.watch_forever(JOURNAL, SESSION_LOGS, _emit, MARK)


async def _keep_child_running() -> None:
    """Start the child, and keep starting it. Never returns.

    Run under ``spawn_supervised`` rather than awaited from ``on_start``: the
    gateway reaps a service that is slow to become ready, so the only thing
    startup may do is arrange for this to happen, not wait for it.
    """
    while True:
        try:
            await asyncio.to_thread(CHILD.reconcile)
        except Exception:  # noqa: BLE001 — a bad tick must not end the loop
            log.exception("tether: could not reconcile the %s child", paths.ROLE)
        await asyncio.sleep(daemon.TICK_S)


#: The supervision task, held so it can be inspected and stopped. Held rather
#: than discarded on principle, even though ``spawn_supervised`` is what makes
#: a dropped handle survivable.
SUPERVISION: asyncio.Task | None = None

#: The event stream's task, held for the same reason.
WATCHING: asyncio.Task | None = None


def _on_start() -> None:
    """Arrange for the child to run, and return.

    Returning ``None`` is load-bearing. The adapter awaits whatever ``on_start``
    hands back if it is awaitable, and a Task is — so returning the supervision
    task would block initialisation on a loop written never to finish, and every
    inbound call would sit behind a gate that never opens.
    """
    global SUPERVISION, WATCHING
    SUPERVISION = spawn_supervised("tether-child", _keep_child_running)
    # The events the child reports, on their way to the log, the topic and the
    # buffer `drain` reads. Supervised the same way and for the same reason: a
    # loop written never to finish must not be awaited from here.
    WATCHING = spawn_supervised("tether-watch", _keep_watching)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log.info("tether: role=%s binaries=%s", paths.ROLE, paths.BIN_DIR)
    global ADAPTER
    ADAPTER = ServiceAdapter("tether", API_MANIFEST, HANDLERS,
                             on_start=_on_start)
    try:
        await ADAPTER.run()
    finally:
        SESSION_LOGS.close()
        MARK.save()
        CHILD.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
