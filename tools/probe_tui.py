#!/usr/bin/env python3
"""probe_tui — terminal UI for vagrant-shell operator.

Connects to a probe, wraps SimpleAgent logic, and displays a split-pane
chat window for driving exec sessions and sending chat messages.

Usage:
    python3 probe_tui.py <name>
"""
from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from dataclasses import dataclass
from urllib.parse import urlparse

import aiomqtt
from prompt_toolkit import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.widgets import TextArea
from prompt_toolkit.styles import Style

from probe_op import RelaySession, Session, parse_args

logger = logging.getLogger("tui")

# --- Event types ---
SYSTEM = "system"
AGENT_ACTION = "agent_action"
EXEC_STDOUT = "exec_stdout"
EXEC_STDERR = "exec_stderr"
EXEC_EXIT = "exec_exit"
CHAT_FROM_USER = "chat_from_user"
CHAT_FROM_PROBE = "chat_from_probe"
CHAT_FROM_AGENT = "chat_from_agent"
ERROR = "error"


@dataclass
class AgentEvent:
    type: str
    text: str


KNOWN_SHELL_VERBS = frozenset({
    'exec', 'run', 'check', 'cat', 'ls', 'uname', 'whoami', 'id', 'pwd',
    'which', 'env', 'echo', 'grep', 'find', 'ps', 'top', 'df', 'du', 'free',
    'mount', 'lsof', 'netstat', 'ss', 'ip', 'ifconfig', 'ping', 'curl',
    'wget', 'ssh', 'scp', 'make', 'gcc', 'python', 'node', 'npm', 'cargo',
    'rustc', 'go', 'docker', 'kubectl', 'helm', 'git', 'vim', 'nano', 'less',
    'more', 'head', 'tail', 'sort', 'wc', 'tee', 'xargs', 'tar', 'zip',
    'unzip', 'gzip', 'gunzip', 'bzip2', 'xz', 'zcat', 'chmod', 'chown',
    'mv', 'cp', 'rm', 'mkdir', 'rmdir', 'touch', 'ln', 'mount', 'umount',
    'systemctl', 'journalctl', 'service', 'apt', 'yum', 'dnf', 'pacman',
    'brew', 'pip', 'conda',
})


class SimpleAgent:
    """Agent that routes user messages to exec or chat based on content."""

    def __init__(self, session: Session | RelaySession):
        self.session = session
        self.events: asyncio.Queue[AgentEvent] = asyncio.Queue()
        session.chat_callback = self._on_chat_from_probe

    def _on_chat_from_probe(self, message: str):
        self.events.put_nowait(AgentEvent(CHAT_FROM_PROBE, message))

    async def start(self):
        """Run auto-diagnostics on session start."""
        await self.events.put(AgentEvent(SYSTEM, "Running auto-diagnostics..."))
        await self.events.put(AgentEvent(AGENT_ACTION, "exec: uname -a"))
        code, stdout, stderr = await self.session.exec_one("uname -a", timeout=10)
        if stdout:
            await self.events.put(AgentEvent(EXEC_STDOUT, stdout.decode(errors="replace").rstrip()))
        if stderr:
            await self.events.put(AgentEvent(EXEC_STDERR, stderr.decode(errors="replace").rstrip()))
        await self.events.put(AgentEvent(EXEC_EXIT, f"exit {code}"))
        if code != 0:
            await self.events.put(AgentEvent(ERROR, "Auto-diagnostics failed"))
        await self.events.put(AgentEvent(SYSTEM, "Auto-diagnostics complete. Ready."))

    async def handle_message(self, msg: str):
        """Route a user message to exec or chat based on first word."""
        first_word = msg.split()[0].lower() if msg.strip() else ""
        if first_word in KNOWN_SHELL_VERBS:
            await self._exec(msg)
        else:
            await self._chat(msg)

    async def _exec(self, cmd: str):
        await self.events.put(AgentEvent(AGENT_ACTION, f"exec: {cmd}"))
        try:
            code, stdout, stderr = await self.session.exec_one(cmd, timeout=30)
            if stdout:
                await self.events.put(AgentEvent(EXEC_STDOUT, stdout.decode(errors="replace").rstrip()))
            if stderr:
                await self.events.put(AgentEvent(EXEC_STDERR, stderr.decode(errors="replace").rstrip()))
            await self.events.put(AgentEvent(EXEC_EXIT, f"exit {code}"))
        except Exception as e:
            await self.events.put(AgentEvent(ERROR, f"exec failed: {e}"))

    async def _chat(self, message: str):
        self.session.send_chat(message)
        await self.events.put(AgentEvent(CHAT_FROM_USER, message))
        await self.events.put(AgentEvent(CHAT_FROM_AGENT, "Message sent to friend"))


STYLE = Style.from_dict({
    'scrollback':      'bg:#1a1a2e #e0e0e0',
    'input':           'bg:#16213e #e0e0e0',
    'status':          'bg:#0f3460 #e0e0e0 bold',
    'system':          '#8a8a8a italic',
    'agent_action':    '#f0c040 bold',
    'exec_stdout':     '#e0e0e0',
    'exec_stderr':     '#ff6b6b',
    'exec_exit':       '#8a8a8a',
    'chat_from_user':  '#51cf66 bold',
    'chat_from_probe': '#22bcf8 bold',
    'chat_from_agent': '#f0c040',
    'error':           '#ff4444 bold',
})

EVENT_STYLE = {
    SYSTEM: 'class:system',
    AGENT_ACTION: 'class:agent_action',
    EXEC_STDOUT: 'class:exec_stdout',
    EXEC_STDERR: 'class:exec_stderr',
    EXEC_EXIT: 'class:exec_exit',
    CHAT_FROM_USER: 'class:chat_from_user',
    CHAT_FROM_PROBE: 'class:chat_from_probe',
    CHAT_FROM_AGENT: 'class:chat_from_agent',
    ERROR: 'class:error',
}

EVENT_PREFIX = {
    SYSTEM: '[system] ',
    AGENT_ACTION: '',
    EXEC_STDOUT: '',
    EXEC_STDERR: '',
    EXEC_EXIT: '',
    CHAT_FROM_USER: 'You: ',
    CHAT_FROM_PROBE: 'Friend: ',
    CHAT_FROM_AGENT: 'Agent: ',
    ERROR: '[error] ',
}


class TuiApp:
    """Prompt_toolkit split-pane chat TUI."""

    def __init__(self, agent: SimpleAgent, name: str):
        self.agent = agent
        self.name = name
        self._formatted_lines: list[tuple[str, str]] = []
        self._done = False

        self.scrollback = Window(
            content=FormattedTextControl(text=self._get_formatted_text),
            wrap_lines=True,
            style='class:scrollback',
        )

        self.input_field = TextArea(
            style='class:input',
            height=3,
            prompt='> ',
            multiline=False,
            focus_on_click=True,
        )

        self.status_bar = Window(
            content=FormattedTextControl(
                text=[('class:status', f' Connected to {name} | Ctrl-D to exit ')]
            ),
            height=1,
            style='class:status',
        )

        self.layout = Layout(
            HSplit([self.scrollback, self.input_field, self.status_bar])
        )

        kb = KeyBindings()

        @kb.add('enter')
        def _submit(event):
            self._submit()

        @kb.add('c-d')
        def _exit(event):
            self._done = True
            event.app.exit()

        @kb.add('c-c')
        def _int(event):
            self._done = True
            event.app.exit()

        self.app = Application(
            layout=self.layout,
            key_bindings=kb,
            style=STYLE,
            full_screen=True,
            mouse_support=True,
        )

        self._poll_task: asyncio.Task | None = None

    def _get_formatted_text(self):
        result = []
        for style_class, text in self._formatted_lines:
            result.append((style_class, text + '\n'))
        if not result:
            result.append(('class:system', 'Starting...\n'))
        return result

    def _submit(self):
        text = self.input_field.text.strip()
        if not text:
            return
        self.input_field.text = ''
        asyncio.ensure_future(self.agent.handle_message(text))

    def _append_event(self, ev: AgentEvent):
        style_class = EVENT_STYLE.get(ev.type, '')
        prefix = EVENT_PREFIX.get(ev.type, '')
        self._formatted_lines.append((style_class, f"{prefix}{ev.text}"))
        self.app.invalidate()

    async def _poll_events(self):
        while not self._done:
            try:
                ev = await asyncio.wait_for(self.agent.events.get(), timeout=0.2)
                self._append_event(ev)
            except asyncio.TimeoutError:
                continue

    async def run(self):
        self._poll_task = asyncio.create_task(self._poll_events())
        try:
            await self.app.run_async()
        finally:
            self._done = True
            if self._poll_task:
                self._poll_task.cancel()
                try:
                    await self._poll_task
                except (asyncio.CancelledError, Exception):
                    pass


async def amain() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="[%(levelname)s %(name)s] %(message)s",
    )

    url = urlparse(args.mqtt_url)
    hostname = url.hostname
    if not hostname:
        sys.stderr.write(f"bad EMQX URL: {args.mqtt_url}\n")
        return 2
    port = url.port or (8084 if url.scheme in ("wss", "https") else 8883)
    path = url.path or "/mqtt"
    use_wss = url.scheme in ("wss", "https")

    client_id = f"operator-{uuid.uuid4().hex[:8]}"

    client_kwargs = dict(
        hostname=hostname,
        port=port,
        username=args.mqtt_user,
        password=args.mqtt_pass,
        identifier=client_id,
        keepalive=30,
        tls_params=aiomqtt.TLSParameters(),
    )
    if use_wss:
        client_kwargs["transport"] = "websockets"
        client_kwargs["websocket_path"] = path

    async with aiomqtt.Client(**client_kwargs) as mqtt:
        if args.mqtt_relay:
            session = RelaySession(args, mqtt, stream_to_stdio=False)
        else:
            session = Session(args, mqtt, stream_to_stdio=False)
        await session.setup_pc()

        sig_task = asyncio.create_task(session.signaling_listener())

        try:
            await session.send_offer()
            try:
                await asyncio.wait_for(
                    session.channel_open.wait(),
                    timeout=args.connect_timeout,
                )
            except asyncio.TimeoutError:
                sys.stderr.write(
                    f"[tui] data channel did not open within "
                    f"{args.connect_timeout}s\n"
                )
                return 3

            if session.connection_dead.is_set():
                state = getattr(getattr(session, "pc", None), "connectionState", "n/a")
                sys.stderr.write(f"[tui] pc state {state}\n")
                return 4

            transport = 'WebRTC' if not args.mqtt_relay else 'MQTT-relay'
            agent = SimpleAgent(session)
            tui = TuiApp(agent, args.name)

            agent.events.put_nowait(
                AgentEvent(SYSTEM, f"Connected to {args.name} via {transport}")
            )

            await agent.start()
            await tui.run()
            await session.send_bye()
        finally:
            sig_task.cancel()
            try:
                await sig_task
            except (asyncio.CancelledError, Exception):
                pass
            await session.close()

    return 0


def main():
    try:
        rc = asyncio.run(amain())
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc)


if __name__ == "__main__":
    main()
