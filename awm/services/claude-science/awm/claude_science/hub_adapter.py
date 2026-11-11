"""Hub adapter for the claude-science service — the Claude Science workbench.

Registers with the gateway on the shared ``ServiceAdapter`` loop (register →
ready → serve → reconnect), so a workbench that was previously three hand-rolled
systemd units and a forked copy of awm's own reverse proxy becomes a service
like any other: visible in ``awm services list``, health as a verb, and
reachable from any node in the fleet.

Like ``mic`` with PulseAudio, this service's real job is owning something it did not write.
Unlike every other one, it owns it *at arm's length*: the workbench daemon is
adopted rather than parented, so an awm deploy cannot interrupt a conversation.
See ``daemon`` for that argument in full.

The daemon runs on demand. It starts on the first request, which is a browser hit
through auth's fronts (their ``wake`` URL reaches ``signin``'s ``/_wake``) or a verb
that needs it, and stops after ``CLAUDE_SCIENCE_IDLE_S`` with no request and no
conversation in flight. Starting the service only adopts a daemon that is running.

Three things live under this one supervised process, and all three are things
the gateway registration does not carry:

  - the workbench daemon (adopted or started on demand; outlives us on purpose),
  - the sign-in route behind auth's front — see ``signin``,
  - an MCP bridge handing the workbench a curated slice of awm's verbs — see
    ``mcp_bridge``.

The route and the bridge die with this process, which is right: they are pure
plumbing and cost nothing to rebuild. The daemon does not, which is also right.

Run via ``run.sh`` (which the gateway spawns and respawns):
    python -m awm.claude_science.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from awm.gatewayclient import ServiceAdapter, call, spawn_supervised

from awm.claude_science import (api, conversations, daemon, mcp_bridge, outputs,
                                signin)

log = logging.getLogger("awm.claude_science.hub_adapter")

SUPERVISOR = daemon.Supervisor(
    main_front_port=daemon.FRONT_PORT,
    sandbox_front_port=daemon.SANDBOX_FRONT_PORT,
)


#: ``name`` is the service-internal handle (the function the gateway calls on the control WS);
#: ``tool`` is the published name, ``science_<verb>``. The service is
#: ``claude-science`` but the tool is ``science``: the first-underscore fold
#: would otherwise cut the service name in half (``claude_science_status`` ->
#: domain ``claude``). Daemon-lifecycle verbs are hidden from MCP with
#: ``surfaces``.
#: Where `new` opens a conversation when the caller names no project.
DEFAULT_PROJECT = "Agents"

API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "status",
            "tool": "science_status",
            "description": (
                "Report the Claude Science workbench: whether the daemon is "
                "running, or idle-stopped (a normal state: it starts on the "
                "next request), and at what pid/version/port, whether this "
                "service adopted it or started it, the three install steps (binary, "
                "provisioned data dir, daemon) named separately, both mesh TLS "
                "fronts' listener + TLS state, the MCP bridge's mount and "
                "allowlist size, each conversation with a turn in flight and "
                "any approval it waits on (pending_approval), and the last "
                "output-folder sync."
            ),
            "params": [],
        },
        {
            "name": "start",
            "tool": "science_start",
            "surfaces": ["cli", "http"],
            "description": (
                "Adopt the running workbench daemon, or launch one detached if "
                "none is running. Idempotent. Other verbs that need the "
                "daemon start it themselves. The daemon deliberately outlives "
                "this service, so this is safe to call at any time."
            ),
            "params": [],
            "timeout": 180,
        },
        {
            "name": "stop",
            "tool": "science_stop",
            "surfaces": ["cli", "http"],
            "description": (
                "Stop the workbench daemon through its own socket, ending live "
                "conversations. Idempotent. Stopping the *service* does not do "
                "this — you have to mean it."
            ),
            "params": [],
            "timeout": 180,
        },
        {
            "name": "restart",
            "tool": "science_restart",
            "surfaces": ["cli", "http"],
            "description": (
                "Stop the workbench daemon and start it again — e.g. to pick up "
                "an update that has been staged. Interrupts live conversations."
            ),
            "params": [],
            "timeout": 300,
        },
        {
            "name": "logs",
            "tool": "science_logs",
            "surfaces": ["cli", "http"],
            "description": "Tail the workbench daemon's log.",
            "params": [
                {"name": "tail", "type": "number",
                 "description": "Lines to return (default 200)."},
            ],
        },
        {
            "name": "url",
            "tool": "science_url",
            "description": (
                "Mint a fresh single-use sign-in link for the workbench, valid "
                "about three minutes. The loopback form the binary prints — "
                "useful over an SSH tunnel. For a browser already holding an "
                "awm session, prefer signin_url."
            ),
            "params": [],
        },
        {
            "name": "signin_url",
            "tool": "science_signin_url",
            "description": (
                "The mesh URL that signs a browser into the workbench in one "
                "click. Behind awm's edge session, so it is only usable by "
                "someone who could log into awm in the first place."
            ),
            "params": [],
        },
        {
            "name": "connector",
            "tool": "science_connector",
            "description": (
                "The workbench's local (stdio) MCP servers. With no arguments, "
                "list them and say whether each one actually connects. Given a "
                "name and command, register one — idempotent, and a no-op when "
                "an identical entry is already there. Remote connectors are not "
                "an option on any node in this fleet: the daemon requires a "
                "public https URL and rejects every private address."
            ),
            "params": [
                {"name": "name", "type": "string",
                 "description": "Server name. Omit to list."},
                {"name": "command", "type": "string",
                 "description": "Absolute path to the executable. An absolute "
                                "path is required to bypass the workbench's "
                                "managed conda runtime."},
                {"name": "args", "type": "array",
                 "description": "Arguments (default none)."},
                {"name": "env", "type": "object",
                 "description": "Environment for the server process."},
                {"name": "description", "type": "string",
                 "description": "Shown in the workbench's connector list."},
                {"name": "probe", "type": "boolean",
                 "description": "Connect to each listed server to count its "
                                "tools. Always done for one just registered; "
                                "off by default when listing, as it is slow."},
            ],
            "timeout": 180,
        },
        {
            "name": "grants",
            "tool": "science_grants",
            "description": (
                "The host directories Claude may read or write inside the "
                "workbench's sandbox. With no arguments, list them. Given a "
                "path, grant it — idempotent. Symlinks are refused, as is a "
                "read-only grant nested under an existing read-write one, a "
                "read-write grant overlapping a read-only one, and a "
                "read-write grant too large for the daemon's credential scan: "
                "either would stall every conversation on an approval."
            ),
            "params": [
                {"name": "path", "type": "string",
                 "description": "Absolute host path. Omit to list."},
                {"name": "mode", "type": "string",
                 "description": "'ro' (default) or 'rw'."},
            ],
            "timeout": 120,
        },
        {
            "name": "message",
            "tool": "science_message",
            "description": (
                "Send a message into a running Claude Science conversation, as "
                "the user's next turn. Names a conversation by id, id prefix or "
                "name; with none, the most recently updated one. A busy "
                "conversation queues it. Refused while the conversation awaits "
                "an approval or an answer. Returns a cursor: read from it to "
                "get the reply."
            ),
            "params": [
                {"name": "text", "type": "string", "required": True,
                 "description": "The message to send."},
                {"name": "conversation", "type": "string",
                 "description": "Conversation id, id prefix or name. Omit for "
                                "the latest."},
            ],
            "timeout": 120,
        },
        {
            "name": "new",
            "tool": "science_new",
            "description": (
                "Start a new Claude Science conversation with text as its "
                "first turn, in a project named by name (found or created). "
                "Returns the conversation id, its output folder on the host "
                "and a cursor: read from it to follow the reply. Never retry a "
                "failed call blindly; it may have started the conversation."
            ),
            "params": [
                {"name": "text", "type": "string", "required": True,
                 "description": "The first message: the research request."},
                {"name": "project", "type": "string",
                 "description": f"Project name. Default {DEFAULT_PROJECT!r}."},
                {"name": "model", "type": "string",
                 "description": "Model id. Omit for the workbench default."},
                {"name": "effort", "type": "string",
                 "description": "Effort level. Omit for the workbench default."},
            ],
            "timeout": 120,
        },
        {
            "name": "read",
            "tool": "science_read",
            "description": (
                "Read a Claude Science conversation's transcript from a cursor. "
                "Returns the messages with text, the conversation's status "
                "('processing' means a reply is still coming) and the next "
                "cursor to pass as since. With no since, returns the tail. A "
                "conversation blocked on an approval or a question carries "
                "pending_approval {kind, summary}."
            ),
            "params": [
                {"name": "conversation", "type": "string",
                 "description": "Conversation id, id prefix or name. Omit for "
                                "the latest."},
                {"name": "since", "type": "number",
                 "description": "Cursor from a previous read: the index of the "
                                "first message to return. Omit for the tail."},
                {"name": "limit", "type": "number",
                 "description": "Most messages to fetch (default 20 for the "
                                "tail, 200 from a cursor)."},
                {"name": "include_tools", "type": "boolean",
                 "description": "Keep messages that carry no text, such as "
                                "tool results. Default false."},
            ],
            "timeout": 120,
        },
        {
            "name": "update_check",
            "tool": "science_update_check",
            "surfaces": ["cli", "http"],
            "description": (
                "Ask the binary whether a newer build is available. Reports "
                "only; installing is a deliberate act (`claude-science update`)."
            ),
            "params": [],
            "timeout": 180,
        },
    ],
    "emitters": [],
    "sessions": [],
}


# -- handlers ---------------------------------------------------------------
#
# Every handler hops to a worker thread: they spawn processes and wait on
# subprocesses, and either of those on the event loop would stall the control
# WS and have the gateway take this service for dead.


def _active() -> dict:
    try:
        with api.connect(SUPERVISOR.login_url()) as owner:
            return {"conversations": conversations.active(owner)}
    except Exception as exc:  # noqa: BLE001 — status reports, never fails
        return {"error": str(exc)}


async def _front(name: str) -> dict:
    try:
        return await call("auth", "front", {"service": "claude-science", "name": name}, timeout=5.0)
    except Exception as exc:  # noqa: BLE001 — status reports, never fails
        return {"serving": False, "error": f"auth did not answer: {exc}"}


async def _h_status(args: dict) -> dict:
    snap = await asyncio.to_thread(SUPERVISOR.snapshot)
    install = await asyncio.to_thread(SUPERVISOR.install_state)
    return {
        "daemon": snap,
        "install": install,
        "fronts": {name: await _front(name) for name in ("gui", "sandbox")},
        "mcp_bridge": mcp_bridge.status(),
        "origins": daemon.allowed_origins(daemon.FRONT_PORT),
        "active": await asyncio.to_thread(_active) if snap.get("running") else None,
        "outputs": {"root": str(outputs.OUTPUTS_ROOT), **_LAST_SYNC},
    }


async def _h_start(args: dict) -> dict:
    return await asyncio.to_thread(SUPERVISOR.start)


async def _h_stop(args: dict) -> dict:
    return await asyncio.to_thread(SUPERVISOR.stop)


async def _h_restart(args: dict) -> dict:
    return await asyncio.to_thread(SUPERVISOR.restart)


async def _h_logs(args: dict) -> dict:
    tail = int(args.get("tail") or 200)
    return {"tail": tail, "log": await asyncio.to_thread(SUPERVISOR.logs, tail)}


def _url() -> str:
    SUPERVISOR.ensure_running()
    return SUPERVISOR.login_url()


async def _h_url(args: dict) -> dict:
    return {"url": await asyncio.to_thread(_url)}


async def _h_signin_url(args: dict) -> dict:
    return {"url": f"{daemon.front_origin(daemon.FRONT_PORT)}/_signin",
            "note": "requires an awm session; mints a workbench nonce server-side"}


def _connector(args: dict) -> dict:
    """List or register a local MCP server. Runs on a worker thread."""
    SUPERVISOR.ensure_running()
    with api.connect(SUPERVISOR.login_url()) as owner:
        name = (args.get("name") or "").strip()
        if not name:
            servers = api.list_local_servers(owner)
            if args.get("probe"):
                for srv in servers:
                    srv.update(api.probe_tools(owner, srv["id"]))
            return {"servers": servers, "probed": bool(args.get("probe"))}

        command = (args.get("command") or "").strip()
        if not command:
            raise ValueError("command is required when registering a connector")

        want = {"command": command,
                "args": list(args.get("args") or []),
                "env": dict(args.get("env") or {})}
        have = api.stored_local_server(name)
        unchanged = have is not None and all(
            have.get(k) == v for k, v in want.items())

        if unchanged:
            server = {"id": f"{api.LOCAL_ID_PREFIX}{name}", "name": name}
        else:
            server = api.add_local_server(
                owner, name=name, description=args.get("description"), **want)

        # Registering and connecting are separate facts; report the second one
        # explicitly rather than letting a clean 200 imply it.
        return {"server": server, "changed": not unchanged,
                **api.probe_tools(owner, server.get("id") or
                                  f"{api.LOCAL_ID_PREFIX}{name}")}


def _grants(args: dict) -> dict:
    """List or add a host grant. Runs on a worker thread."""
    SUPERVISOR.ensure_running()
    with api.connect(SUPERVISOR.login_url()) as owner:
        before = api.list_grants(owner)
        path = (args.get("path") or "").strip()
        if not path:
            return {"grants": before}

        mode = (args.get("mode") or "ro").strip()
        if mode not in ("ro", "rw"):
            raise ValueError(f"mode must be 'ro' or 'rw', not {mode!r}")
        if mode == "rw":
            outputs.check_writable_grant(path, before)
        api.add_grant(owner, path=path, mode=mode)
        after = api.list_grants(owner)
        return {"grants": after, "changed": after != before}


async def _h_connector(args: dict) -> dict:
    return await asyncio.to_thread(_connector, args)


async def _h_grants(args: dict) -> dict:
    return await asyncio.to_thread(_grants, args)


def _message(args: dict) -> dict:
    """Post into a conversation. Runs on a worker thread."""
    text = (args.get("text") or "").strip()
    if not text:
        raise ValueError("text is required")
    SUPERVISOR.ensure_running()
    with api.connect(SUPERVISOR.login_url()) as owner:
        target = conversations.resolve_conversation(
            owner, args.get("conversation"))
        sent = conversations.post_message(owner, target["id"], text)
        return {"conversation": conversations.summary(target), **sent}


def _new(args: dict) -> dict:
    """Open a conversation. Runs on a worker thread."""
    text = (args.get("text") or "").strip()
    if not text:
        raise ValueError("text is required")
    SUPERVISOR.ensure_running()
    with api.connect(SUPERVISOR.login_url()) as owner:
        return conversations.open_conversation(
            owner, text,
            project=(args.get("project") or "").strip() or DEFAULT_PROJECT,
            model=args.get("model"), effort=args.get("effort"))


def _read(args: dict) -> dict:
    """Read a conversation's transcript. Runs on a worker thread."""
    since = args.get("since")
    limit = args.get("limit")
    SUPERVISOR.ensure_running()
    with api.connect(SUPERVISOR.login_url()) as owner:
        target = conversations.resolve_conversation(
            owner, args.get("conversation"))
        return conversations.read_messages(
            owner, target["id"],
            since=None if since is None else int(since),
            limit=None if limit is None else int(limit),
            include_tools=bool(args.get("include_tools")))


async def _h_message(args: dict) -> dict:
    return await asyncio.to_thread(_message, args)


async def _h_new(args: dict) -> dict:
    return await asyncio.to_thread(_new, args)


async def _h_read(args: dict) -> dict:
    return await asyncio.to_thread(_read, args)


async def _h_update_check(args: dict) -> dict:
    return await asyncio.to_thread(SUPERVISOR.update_check)


HANDLERS = {
    "status": _h_status,
    "start": _h_start,
    "stop": _h_stop,
    "restart": _h_restart,
    "logs": _h_logs,
    "url": _h_url,
    "signin_url": _h_signin_url,
    "connector": _h_connector,
    "grants": _h_grants,
    "message": _h_message,
    "new": _h_new,
    "read": _h_read,
    "update_check": _h_update_check,
}


# -- the supervision loop ---------------------------------------------------


def _busy() -> list[dict]:
    """The conversations with a turn in flight. Raises when the workbench cannot say."""
    with api.connect(SUPERVISOR.login_url()) as owner:
        return conversations.active(owner)


async def _health_loop() -> None:
    """Stop the workbench daemon once it has been idle long enough. Never exits.

    Watches process liveness rather than an HTTP probe: a slow probe during an
    analysis is not evidence of anything, and stopping on it would interrupt the
    very work this service is arranged to protect. It never starts the daemon;
    a request does.
    """
    log.info("claude-science: idle loop started (interval=%ss, idle limit=%ss)",
             daemon.HEALTH_INTERVAL_S, daemon.IDLE_S)
    while True:
        try:
            await asyncio.sleep(daemon.HEALTH_INTERVAL_S)
            res = await asyncio.to_thread(SUPERVISOR.reconcile, _busy)
            if res.get("action") == "stopped":
                log.info("claude-science: workbench daemon stopped after idling")
        except Exception:  # noqa: BLE001 — never let the loop die
            # CancelledError is a BaseException and so passes through, which is
            # what the supervisor above wants: a *return* from here would read
            # as a defect and be respawned, but a cancellation is a shutdown.
            log.exception("claude-science: supervision pass failed")


_LAST_SYNC: dict = {}


def _sync_outputs() -> dict:
    with api.connect(SUPERVISOR.login_url()) as owner:
        return outputs.sync(owner)


async def _outputs_loop() -> None:
    """Keep the output folders in step with the workbench. Never exits."""
    while True:
        try:
            await asyncio.sleep(outputs.SYNC_INTERVAL_S)
            if not outputs.OUTPUTS_ROOT.is_dir():
                _LAST_SYNC.update(at=time.time(), error="no output root")
                continue
            if not daemon.port_listening(daemon.PORT):
                continue
            report = await asyncio.to_thread(_sync_outputs)
            _LAST_SYNC.clear()
            _LAST_SYNC.update(report, at=time.time(), error=None)
            if report["created"] or report["deleted"] or report["moved"]:
                log.info("claude-science: outputs created=%s moved=%s deleted=%s",
                         report["created"], report["moved"], report["deleted"])
        except Exception as exc:  # noqa: BLE001 — never let the loop die
            _LAST_SYNC.update(at=time.time(), error=str(exc))
            log.exception("claude-science: output-folder sync failed")


async def _on_start() -> None:
    """Adopt a running daemon, raise the sign-in route and the bridge, then loop.

    A stopped daemon stays stopped until a request wakes it. No failure here is
    fatal. The service still registers, so ``status`` can report *why* it is
    broken — which is the visibility the hand-managed units never had.
    """
    if not daemon.installed():
        log.warning("claude-science: binary missing at %s — run install.sh; the "
                    "service will register and report this via status", daemon.BIN)
    else:
        try:
            snap = await asyncio.to_thread(SUPERVISOR.adopt)
            if snap.get("running"):
                log.info("claude-science: daemon adopted pid=%s version=%s port=%s",
                         snap.get("pid"), snap.get("version"), snap.get("port"))
            else:
                log.info("claude-science: no daemon running; the first request starts it")
        except Exception:  # noqa: BLE001
            log.exception("claude-science: adopting a running daemon failed")

    try:
        signin.start(SUPERVISOR)
    except OSError:  # the workbench is still usable with the CLI's sign-in link
        log.exception("claude-science: the sign-in route failed to start")

    # The three are long-lived and fail silently if left to a bare create_task
    # whose handle nobody reads: a bridge that died on its first line looks
    # exactly like one nobody has called yet, and a dead supervision loop looks
    # exactly like a workbench that has not crashed. spawn_supervised logs at
    # ERROR and respawns, so "absent" cannot masquerade as "quiet".
    spawn_supervised("claude-science:mcp-bridge", mcp_bridge.serve)
    spawn_supervised("claude-science:health", _health_loop)
    spawn_supervised("claude-science:outputs", _outputs_loop)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter(
        "claude-science", API_MANIFEST, HANDLERS, on_start=_on_start,
        mount=mcp_bridge.listen(),
    ).run()


if __name__ == "__main__":
    asyncio.run(main())
