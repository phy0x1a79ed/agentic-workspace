#!/usr/bin/env python3
"""probe operator — connect to a named probe via EMQX MQTT signaling and a
WebRTC data channel, send Exec frames, stream stdio back.

Modes:
    python3 probe_op.py <name> exec '<cmd>'   one-shot, exits with friend's code
    python3 probe_op.py <name>                 interactive REPL

EMQX creds resolved from CLI > env > tools/.env.emqx (in that order).
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import signal
import sys
import tempfile
import urllib.request
import uuid
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import aiomqtt
from aiortc import (
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.sdp import candidate_from_sdp

logger = logging.getLogger("operator")

ENV_FILE = Path(__file__).resolve().parent / ".env.probe"
CF_TURN_ENDPOINT = "https://rtc.live.cloudflare.com/v1/turn/keys/{key_id}/credentials/generate"
CF_TURN_TTL_SECS = 600


def load_env_file(path: Path) -> dict:
    if not path.is_file():
        return {}
    env = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("name", help="probe name")
    sub = p.add_subparsers(dest="mode")
    exec_p = sub.add_parser("exec", help="run one command and exit")
    exec_p.add_argument("cmd", help="shell command (passed to sh -c)")
    sub.add_parser("daemon", help="hold the channel open, accept commands over a unix socket")
    send_p = sub.add_parser("send", help="submit one command to a running daemon")
    send_p.add_argument("cmd", help="shell command (passed to sh -c)")
    sub.add_parser("stop", help="ask a running daemon to shut down")
    p.add_argument("--mqtt-url", default=None)
    p.add_argument("--mqtt-user", default=None)
    p.add_argument("--mqtt-pass", default=None)
    p.add_argument("--cf-turn-id", default=None,
                   help="Cloudflare TURN key ID (or CF_TURN_TOKEN_ID env)")
    p.add_argument("--cf-turn-token", default=None,
                   help="Cloudflare API token (or CF_TURN_API_TOKEN env)")
    p.add_argument("--no-turn", action="store_true",
                   help="Skip TURN minting even if creds are present (STUN only)")
    p.add_argument("--mqtt-relay", action="store_true",
                   help="Route frames through the MQTT broker instead of WebRTC. "
                        "Use on UDP-blocked friends (HPC, restrictive corp). "
                        "Probe binary must also be started with --mqtt-relay.")
    p.add_argument("--scope", default=None,
                   help="Vagrant scope name to display in the friend's TUI header.")
    p.add_argument("--operator-name", default=None,
                   help="Operator display name sent in the Hello frame.")
    p.add_argument("--timeout", type=float, default=30.0,
                   help="exec deadline in seconds")
    p.add_argument("--connect-timeout", type=float, default=20.0,
                   help="seconds to wait for data channel to open")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    env = load_env_file(ENV_FILE)
    args.mqtt_url = args.mqtt_url or os.environ.get("EMQX_URL") or env.get("EMQX_URL")
    args.mqtt_user = args.mqtt_user or os.environ.get("EMQX_USER") or env.get("EMQX_USER")
    args.mqtt_pass = args.mqtt_pass or os.environ.get("EMQX_PASS") or env.get("EMQX_PASS")
    args.cf_turn_id = args.cf_turn_id or os.environ.get("CF_TURN_TOKEN_ID") or env.get("CF_TURN_TOKEN_ID")
    args.cf_turn_token = args.cf_turn_token or os.environ.get("CF_TURN_API_TOKEN") or env.get("CF_TURN_API_TOKEN")

    # send/stop are pure local IPC clients — no broker creds needed.
    if args.mode not in ("send", "stop"):
        missing = [
            name for name, val in [
                ("EMQX_URL", args.mqtt_url),
                ("EMQX_USER", args.mqtt_user),
                ("EMQX_PASS", args.mqtt_pass),
            ] if not val
        ]
        if missing:
            p.error(
                f"missing EMQX config: {', '.join(missing)}\n"
                f"set in env or {ENV_FILE} (see .env.probe.example)"
            )
    return args


def mint_cf_turn(key_id: str, api_token: str, ttl: int = CF_TURN_TTL_SECS, timeout: float = 10.0) -> list[dict]:
    """POST to Cloudflare; return a list of RTCIceServer-shape dicts.

    CF returns one server object with mixed stun:/turn:/turns: URLs and a
    single username+credential pair. webrtc-rs rejects entries that mix
    stun: with turn: (credentials are required for turn but forbidden for
    stun), so we split into two entries: one stun-only without credentials,
    one turn-only with the CF credentials.

    Raises urllib.error.HTTPError / URLError on failure — caller decides
    whether to fall back to STUN-only.
    """
    req = urllib.request.Request(
        CF_TURN_ENDPOINT.format(key_id=key_id),
        data=json.dumps({"ttl": ttl}).encode(),
        headers={
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
            # CF's edge 403s the default "Python-urllib/X.Y" UA; spoofing a
            # named client is the documented workaround.
            "User-Agent": "probe-operator/0.0.1",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read())
    raw = body["iceServers"]
    stun_urls = [u for u in raw["urls"] if u.startswith("stun:")]
    turn_urls = [u for u in raw["urls"] if u.startswith(("turn:", "turns:"))]
    servers: list[dict] = []
    if stun_urls:
        servers.append({"urls": stun_urls})
    if turn_urls:
        servers.append({
            "urls": turn_urls,
            "username": raw["username"],
            "credential": raw["credential"],
        })
    return servers


class Session:
    def __init__(self, args: argparse.Namespace, mqtt: aiomqtt.Client,
                 stream_to_stdio: bool = True):
        self.args = args
        self.mqtt = mqtt
        self.pc: RTCPeerConnection | None = None
        self.channel = None
        self.channel_open = asyncio.Event()
        self.connection_dead = asyncio.Event()
        self.next_id = 0
        self.in_flight: dict = {}
        # When False, stdout/stderr frames are only buffered into the in_flight
        # entry (caller reads them via exec_one's return value). Tests use this
        # so they can assert on the bytes without scraping a subprocess pipe.
        self.stream_to_stdio = stream_to_stdio
        self._published_candidates: set[str] = set()
        self.chat_callback = None

    async def setup_pc(self):
        ice_servers, turn_payload = self._build_ice_servers()
        if turn_payload is not None:
            await self._publish_turn(turn_payload)
        config = RTCConfiguration(iceServers=ice_servers)
        self.pc = RTCPeerConnection(configuration=config)
        self.channel = self.pc.createDataChannel("ctrl")

        @self.channel.on("open")
        def _on_open():
            logger.info("data channel open")
            self.channel_open.set()

        @self.channel.on("message")
        def _on_msg(message):
            self._on_frame(message)

        @self.channel.on("close")
        def _on_close():
            logger.info("data channel closed")
            self.connection_dead.set()

        @self.pc.on("connectionstatechange")
        def _on_state():
            logger.info("pc state: %s", self.pc.connectionState)
            if self.pc.connectionState in ("failed", "closed", "disconnected"):
                self.connection_dead.set()
                self.channel_open.set()  # unblock waiters

    def _build_ice_servers(self) -> tuple[list[RTCIceServer], dict | None]:
        """Return (ice_servers, turn_payload).

        turn_payload is the JSON dict to publish on the from-operator/turn
        topic (so the friend can use the same servers), or None when running
        STUN-only.
        """
        fallback = [RTCIceServer(urls=["stun:stun.l.google.com:19302"])]
        if self.args.no_turn:
            logger.info("TURN minting disabled (--no-turn)")
            return fallback, None
        if not (self.args.cf_turn_id and self.args.cf_turn_token):
            logger.info("CF_TURN creds not present; STUN-only")
            return fallback, None
        try:
            servers = mint_cf_turn(self.args.cf_turn_id, self.args.cf_turn_token)
        except Exception as e:
            logger.warning("TURN mint failed (%s); falling back to STUN-only", e)
            return fallback, None
        total_urls = sum(len(s["urls"]) for s in servers)
        user_prefix = next(
            (s["username"][:8] for s in servers if s.get("username")), ""
        )
        logger.info(
            "minted CF TURN credential (entries=%d, urls=%d, user=%s...)",
            len(servers), total_urls, user_prefix,
        )
        ice = [
            RTCIceServer(
                urls=s["urls"],
                username=s.get("username"),
                credential=s.get("credential"),
            )
            for s in servers
        ]
        # Wire shape: list of server objects matching aiortc/webrtc-rs schema.
        payload = {"iceServers": servers}
        return ice, payload

    async def _publish_turn(self, payload: dict):
        topic = f"probe/{self.args.name}/from-operator/turn"
        await self.mqtt.publish(topic, json.dumps(payload).encode(), qos=1)
        logger.info("published TURN config to %s", topic)

    def _on_frame(self, message):
        if isinstance(message, bytes):
            text = message.decode("utf-8", errors="ignore")
        else:
            text = message
        try:
            frame = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("non-json frame: %r", text)
            return
        ftype = frame.get("type")
        fid = frame.get("id")
        entry = self.in_flight.get(fid)
        if ftype == "stdout":
            data = base64.b64decode(frame["data"])
            sink = entry.get("on_stdout") if entry else None
            if entry is not None:
                entry["stdout"].extend(data)
            if sink is not None:
                sink(data)
            elif self.stream_to_stdio:
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
        elif ftype == "stderr":
            data = base64.b64decode(frame["data"])
            sink = entry.get("on_stderr") if entry else None
            if entry is not None:
                entry["stderr"].extend(data)
            if sink is not None:
                sink(data)
            elif self.stream_to_stdio:
                sys.stderr.buffer.write(data)
                sys.stderr.buffer.flush()
        elif ftype == "exit":
            if entry is not None:
                entry["code"] = frame.get("code") if frame.get("code") is not None else 0
                entry["event"].set()
        elif ftype == "error":
            msg = frame.get("message", "")
            if entry is not None:
                entry["stderr"].extend(f"[probe error] {msg}\n".encode())
                entry["code"] = 127
                entry["event"].set()
            elif self.stream_to_stdio:
                sys.stderr.write(f"[probe error] {msg}\n")
        elif ftype == "chat":
            msg = frame.get("message", "")
            if self.chat_callback:
                self.chat_callback(msg)
            elif self.stream_to_stdio:
                sys.stderr.write(f"[friend] {msg}\n")
        else:
            logger.warning("unexpected frame type: %s", ftype)

    async def send_offer(self):
        """Publish the SDP offer as soon as setLocalDescription returns.

        aiortc's setLocalDescription gathers candidates internally before it
        returns, so by the time we publish, the SDP already embeds them.
        That means we can drop the old extra `await gathering complete`
        step — it was a no-op on top of setLocalDescription. The friend
        gets the offer the moment we have one to send.
        """
        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)
        sdp = self.pc.localDescription.sdp
        # Record candidates already embedded in the offer so we never
        # republish them on the trickle topic.
        for line in sdp.splitlines():
            stripped = line.strip()
            if stripped.startswith("a=candidate:"):
                self._published_candidates.add(stripped[2:])
        payload = {"type": "offer", "sdp": sdp}
        topic = f"probe/{self.args.name}/from-operator/sdp"
        await self.mqtt.publish(topic, json.dumps(payload).encode(), qos=1)
        logger.info("published SDP offer to %s (gathering=%s)",
                    topic, self.pc.iceGatheringState)

    async def signaling_listener(self):
        await self.mqtt.subscribe(f"probe/{self.args.name}/from-friend/+")
        async for message in self.mqtt.messages:
            topic = message.topic.value
            leaf = topic.rsplit("/", 1)[-1]
            try:
                payload = json.loads(message.payload.decode())
            except Exception as e:
                logger.warning("bad payload on %s: %s", topic, e)
                continue
            if leaf == "sdp":
                if payload.get("type") != "answer":
                    logger.warning("unexpected sdp type from friend: %s",
                                   payload.get("type"))
                    continue
                desc = RTCSessionDescription(sdp=payload["sdp"], type="answer")
                await self.pc.setRemoteDescription(desc)
                logger.info("set remote SDP answer")
            elif leaf == "ice":
                cand_str = payload.get("candidate", "")
                if cand_str.startswith("candidate:"):
                    cand_str = cand_str[len("candidate:"):]
                try:
                    cand = candidate_from_sdp(cand_str)
                except Exception as e:
                    logger.warning("bad ice candidate %r: %s", cand_str, e)
                    continue
                cand.sdpMid = payload.get("sdpMid")
                cand.sdpMLineIndex = payload.get("sdpMLineIndex")
                try:
                    await self.pc.addIceCandidate(cand)
                except Exception as e:
                    logger.warning("addIceCandidate failed: %s", e)
            elif leaf == "bye":
                logger.info("friend sent bye: %s", payload.get("reason"))
                self.connection_dead.set()
                return
            else:
                logger.warning("unknown leaf: %s", leaf)

    def _alloc_id(self) -> int:
        self.next_id += 1
        return self.next_id

    async def exec_one(self, cmd: str, timeout: float,
                       on_stdout: Optional[Callable[[bytes], None]] = None,
                       on_stderr: Optional[Callable[[bytes], None]] = None,
                       ) -> tuple[int, bytes, bytes]:
        """Send an Exec frame and await the exit. Returns (code, stdout, stderr).

        stdout/stderr are always buffered into the returned bytes. If
        per-exec sinks are passed, each chunk is forwarded there as it
        arrives; otherwise — if ``stream_to_stdio`` is True (default) —
        chunks are written to sys.stdout/stderr. Sinks take precedence so
        the daemon can fan output to a single client without dumping it on
        its own terminal.
        """
        if not self.channel_open.is_set():
            msg = b"[operator] channel not open\n"
            if on_stderr is not None:
                on_stderr(msg)
            elif self.stream_to_stdio:
                sys.stderr.buffer.write(msg)
            return 4, b"", msg
        fid = self._alloc_id()
        entry = {
            "event": asyncio.Event(),
            "code": 1,
            "stdout": bytearray(),
            "stderr": bytearray(),
            "on_stdout": on_stdout,
            "on_stderr": on_stderr,
        }
        self.in_flight[fid] = entry
        frame = json.dumps({"type": "exec", "id": fid, "cmd": cmd})
        self.channel.send(frame)
        try:
            await asyncio.wait_for(entry["event"].wait(), timeout=timeout)
        except asyncio.TimeoutError:
            msg = f"[operator] timeout after {timeout}s\n".encode()
            entry["stderr"].extend(msg)
            if self.stream_to_stdio:
                sys.stderr.buffer.write(msg)
            entry["code"] = 124
        finally:
            self.in_flight.pop(fid, None)
        return entry["code"], bytes(entry["stdout"]), bytes(entry["stderr"])

    def send_chat(self, message: str):
        """Send a Chat frame to the friend over the data channel."""
        if self.channel and self.channel.readyState == "open":
            frame = json.dumps({"type": "chat", "message": message})
            self.channel.send(frame)

    def send_hello(self, scope: str | None, operator: str | None):
        """Send a Hello frame as the first post-open frame.

        Carries human-readable session metadata for the friend's TUI
        header (vagrant scope, operator display name). Fire-and-forget;
        no ack expected. See binary/PROTOCOL.md for the full spec.
        """
        if self.channel and self.channel.readyState == "open":
            frame = json.dumps({"type": "hello", "scope": scope, "operator": operator})
            self.channel.send(frame)

    async def send_bye(self):
        topic = f"probe/{self.args.name}/from-operator/bye"
        try:
            await self.mqtt.publish(
                topic,
                json.dumps({"reason": "operator finished"}).encode(),
                qos=1,
            )
        except Exception:
            pass

    async def close(self):
        if self.pc:
            await self.pc.close()


class RelaySession:
    """MQTT-relay equivalent of Session — no WebRTC, frames ride on
    `probe/<name>/from-{role}/data` topics.

    Mirrors the public surface of Session (channel_open, connection_dead,
    exec_one, send_bye, close) so repl() and exec mode don't have to care
    which transport they're talking to.
    """

    def __init__(self, args: argparse.Namespace, mqtt: aiomqtt.Client,
                 stream_to_stdio: bool = True):
        self.args = args
        self.mqtt = mqtt
        self.channel_open = asyncio.Event()
        self.connection_dead = asyncio.Event()
        self.next_id = 0
        self.in_flight: dict = {}
        self.stream_to_stdio = stream_to_stdio
        self._reader_task: asyncio.Task | None = None
        self.chat_callback = None

    async def setup_pc(self):
        # Subscribe to the friend's data + bye topics. No PC to build —
        # the moment we have a subscription we can publish exec frames.
        await self.mqtt.subscribe(f"probe/{self.args.name}/from-friend/+")
        self.channel_open.set()
        logger.info("mqtt-relay subscribed; channel_open set")

    async def signaling_listener(self):
        async for message in self.mqtt.messages:
            topic = message.topic.value
            leaf = topic.rsplit("/", 1)[-1]
            if leaf == "data":
                try:
                    frame = json.loads(message.payload.decode())
                except Exception as e:
                    logger.warning("bad data frame on %s: %s", topic, e)
                    continue
                self._on_frame_dict(frame)
            elif leaf == "bye":
                try:
                    payload = json.loads(message.payload.decode())
                except Exception:
                    payload = {}
                logger.info("friend sent bye: %s", payload.get("reason"))
                self.connection_dead.set()
                return
            elif leaf in ("sdp", "ice"):
                # friend should not be sending these in relay mode; ignore
                logger.warning("ignoring %s on %s in relay mode", leaf, topic)
            else:
                logger.warning("unknown leaf: %s", leaf)

    def _on_frame_dict(self, frame: dict):
        ftype = frame.get("type")
        fid = frame.get("id")
        entry = self.in_flight.get(fid)
        if ftype == "stdout":
            data = base64.b64decode(frame["data"])
            sink = entry.get("on_stdout") if entry else None
            if entry is not None:
                entry["stdout"].extend(data)
            if sink is not None:
                sink(data)
            elif self.stream_to_stdio:
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
        elif ftype == "stderr":
            data = base64.b64decode(frame["data"])
            sink = entry.get("on_stderr") if entry else None
            if entry is not None:
                entry["stderr"].extend(data)
            if sink is not None:
                sink(data)
            elif self.stream_to_stdio:
                sys.stderr.buffer.write(data)
                sys.stderr.buffer.flush()
        elif ftype == "exit":
            if entry is not None:
                entry["code"] = frame.get("code") if frame.get("code") is not None else 0
                entry["event"].set()
        elif ftype == "error":
            msg = frame.get("message", "")
            if entry is not None:
                entry["stderr"].extend(f"[probe error] {msg}\n".encode())
                entry["code"] = 127
                entry["event"].set()
            elif self.stream_to_stdio:
                sys.stderr.write(f"[probe error] {msg}\n")
        elif ftype == "chat":
            msg = frame.get("message", "")
            if self.chat_callback:
                self.chat_callback(msg)
            elif self.stream_to_stdio:
                sys.stderr.write(f"[friend] {msg}\n")
        else:
            logger.warning("unexpected frame type: %s", ftype)

    async def send_offer(self):
        # No-op in relay mode (kept for API parity with Session).
        return

    def _alloc_id(self) -> int:
        self.next_id += 1
        return self.next_id

    async def exec_one(self, cmd: str, timeout: float,
                       on_stdout: Optional[Callable[[bytes], None]] = None,
                       on_stderr: Optional[Callable[[bytes], None]] = None,
                       ) -> tuple[int, bytes, bytes]:
        fid = self._alloc_id()
        entry = {
            "event": asyncio.Event(),
            "code": 1,
            "stdout": bytearray(),
            "stderr": bytearray(),
            "on_stdout": on_stdout,
            "on_stderr": on_stderr,
        }
        self.in_flight[fid] = entry
        payload = json.dumps({"type": "exec", "id": fid, "cmd": cmd}).encode()
        topic = f"probe/{self.args.name}/from-operator/data"
        await self.mqtt.publish(topic, payload, qos=1)
        try:
            await asyncio.wait_for(entry["event"].wait(), timeout=timeout)
        except asyncio.TimeoutError:
            msg = f"[operator] timeout after {timeout}s\n".encode()
            entry["stderr"].extend(msg)
            if self.stream_to_stdio:
                sys.stderr.buffer.write(msg)
            entry["code"] = 124
        finally:
            self.in_flight.pop(fid, None)
        return entry["code"], bytes(entry["stdout"]), bytes(entry["stderr"])

    def send_chat(self, message: str):
        """Send a Chat frame to the friend over MQTT relay."""
        frame = json.dumps({"type": "chat", "message": message}).encode()
        topic = f"probe/{self.args.name}/from-operator/data"
        asyncio.ensure_future(self.mqtt.publish(topic, frame, qos=1))

    def send_hello(self, scope: str | None, operator: str | None):
        """Send a Hello frame as the first post-open frame (relay path)."""
        frame = json.dumps({"type": "hello", "scope": scope, "operator": operator}).encode()
        topic = f"probe/{self.args.name}/from-operator/data"
        asyncio.ensure_future(self.mqtt.publish(topic, frame, qos=1))

    async def send_bye(self):
        topic = f"probe/{self.args.name}/from-operator/bye"
        try:
            await self.mqtt.publish(
                topic,
                json.dumps({"reason": "operator finished"}).encode(),
                qos=1,
            )
        except Exception:
            pass

    async def close(self):
        # Nothing to tear down — the aiomqtt context exits cleanly.
        return


async def repl(session: Session):
    sys.stderr.write(f"[connected to {session.args.name}] Ctrl-D to exit\n")
    loop = asyncio.get_event_loop()
    while True:
        if session.connection_dead.is_set():
            sys.stderr.write("[operator] connection lost\n")
            return
        try:
            line = await loop.run_in_executor(None, _read_input, "> ")
        except (EOFError, KeyboardInterrupt):
            sys.stderr.write("\n")
            return
        line = line.strip()
        if not line:
            continue
        if line in (":q", ":quit", "exit"):
            return
        # bytes are also streamed live; discard the tuple
        await session.exec_one(line, session.args.timeout)


def _read_input(prompt: str) -> str:
    sys.stderr.write(prompt)
    sys.stderr.flush()
    return input()


def _socket_path(name: str) -> Path:
    """Per-(name, uid) unix-socket path. Prefer XDG_RUNTIME_DIR (tmpfs,
    cleared on logout), fall back to the system tempdir."""
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return Path(base) / f"probe-{name}-{os.getuid()}.sock"


async def _daemon_alive(path: Path) -> bool:
    """True iff *some* peer is currently accepting connections at path."""
    if not path.exists():
        return False
    try:
        _, writer = await asyncio.open_unix_connection(str(path))
    except (OSError, ConnectionRefusedError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    return True


async def run_daemon(session, args) -> int:
    """Hold the WebRTC/MQTT-relay channel open and serve exec/stop requests
    over a unix socket. Returns when SIGINT/SIGTERM fires, the friend Byes,
    or a client requests `stop`. Caller handles send_bye + session close."""
    path = _socket_path(args.name)
    if await _daemon_alive(path):
        sys.stderr.write(
            f"[daemon] another daemon already serving probe '{args.name}' at {path}\n"
        )
        return 5
    try:
        path.unlink()
    except FileNotFoundError:
        pass

    stop_event = asyncio.Event()

    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            line = await reader.readline()
            if not line:
                return
            try:
                req = json.loads(line)
            except json.JSONDecodeError as e:
                writer.write(json.dumps(
                    {"type": "error", "message": f"bad json: {e}"}
                ).encode() + b"\n")
                await writer.drain()
                return
            rtype = req.get("type")
            if rtype == "stop":
                writer.write(json.dumps({"type": "ack"}).encode() + b"\n")
                await writer.drain()
                stop_event.set()
                return
            if rtype != "exec":
                writer.write(json.dumps(
                    {"type": "error", "message": f"unknown request type: {rtype!r}"}
                ).encode() + b"\n")
                await writer.drain()
                return
            cmd = req.get("cmd", "")
            timeout = float(req.get("timeout", args.timeout))

            def make_sink(kind: str):
                def cb(data: bytes):
                    payload = {"type": kind, "data": base64.b64encode(data).decode()}
                    try:
                        writer.write(json.dumps(payload).encode() + b"\n")
                    except Exception:
                        pass
                return cb

            rc, _, _ = await session.exec_one(
                cmd, timeout,
                on_stdout=make_sink("stdout"),
                on_stderr=make_sink("stderr"),
            )
            try:
                await writer.drain()
            except Exception:
                pass
            writer.write(json.dumps({"type": "exit", "code": rc}).encode() + b"\n")
            try:
                await writer.drain()
            except Exception:
                pass
        except Exception as e:
            logger.warning("daemon client handler error: %s", e)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    server = await asyncio.start_unix_server(handle_client, path=str(path))
    try:
        os.chmod(str(path), 0o600)
    except OSError:
        pass

    loop = asyncio.get_event_loop()
    installed_sigs: list[int] = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
            installed_sigs.append(sig)
        except (NotImplementedError, RuntimeError):
            pass

    sys.stderr.write(f"[daemon] probe '{args.name}' listening on {path}\n")

    async def dead_watcher():
        await session.connection_dead.wait()
        sys.stderr.write("[daemon] friend connection dead; shutting down\n")
        stop_event.set()

    watcher_task = asyncio.create_task(dead_watcher())
    try:
        await stop_event.wait()
    finally:
        for sig in installed_sigs:
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, RuntimeError):
                pass
        server.close()
        try:
            await server.wait_closed()
        except Exception:
            pass
        watcher_task.cancel()
        try:
            await watcher_task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    return 0


async def run_client(args) -> int:
    """Thin unix-socket client. Submits one request to a running daemon and
    proxies stdout/stderr/exit back to the calling shell."""
    path = _socket_path(args.name)
    if not path.exists():
        sys.stderr.write(f"no daemon for probe '{args.name}' (expected {path})\n")
        return 4
    try:
        reader, writer = await asyncio.open_unix_connection(str(path))
    except (OSError, ConnectionRefusedError) as e:
        sys.stderr.write(f"no daemon for probe '{args.name}': {e}\n")
        return 4

    if args.mode == "stop":
        req = {"type": "stop"}
    else:
        req = {"type": "exec", "cmd": args.cmd, "timeout": args.timeout}
    writer.write(json.dumps(req).encode() + b"\n")
    try:
        await writer.drain()
    except Exception as e:
        sys.stderr.write(f"[send] daemon hung up before request: {e}\n")
        return 4

    rc = 1
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue
            ftype = frame.get("type")
            if ftype == "stdout":
                sys.stdout.buffer.write(base64.b64decode(frame["data"]))
                sys.stdout.buffer.flush()
            elif ftype == "stderr":
                sys.stderr.buffer.write(base64.b64decode(frame["data"]))
                sys.stderr.buffer.flush()
            elif ftype == "exit":
                rc = int(frame.get("code", 1))
                break
            elif ftype == "ack":
                rc = 0
                break
            elif ftype == "error":
                sys.stderr.write(f"[daemon error] {frame.get('message', '')}\n")
                rc = 5
                break
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
    return rc


async def amain() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="[%(levelname)s %(name)s] %(message)s",
    )

    # send/stop are local unix-socket clients — never touch MQTT.
    if args.mode in ("send", "stop"):
        return await run_client(args)

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
            session = RelaySession(args, mqtt)
        else:
            session = Session(args, mqtt)
        await session.setup_pc()

        sig_task = asyncio.create_task(session.signaling_listener())

        rc = 1
        try:
            await session.send_offer()
            try:
                await asyncio.wait_for(
                    session.channel_open.wait(),
                    timeout=args.connect_timeout,
                )
            except asyncio.TimeoutError:
                sys.stderr.write(
                    f"[operator] data channel did not open within "
                    f"{args.connect_timeout}s\n"
                )
                return 3

            if session.connection_dead.is_set():
                state = getattr(getattr(session, "pc", None), "connectionState", "n/a")
                sys.stderr.write(f"[operator] pc state {state}\n")
                return 4

            # Greet the friend with session metadata so their TUI header
            # can show the scope name + operator name.
            session.send_hello(args.scope, args.operator_name)

            if args.mode == "exec":
                rc, _, _ = await session.exec_one(args.cmd, args.timeout)
            elif args.mode == "daemon":
                rc = await run_daemon(session, args)
            else:
                await repl(session)
                rc = 0

            await session.send_bye()
        finally:
            sig_task.cancel()
            try:
                await sig_task
            except (asyncio.CancelledError, Exception):
                pass
            await session.close()
        return rc


def main():
    try:
        rc = asyncio.run(amain())
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc)


if __name__ == "__main__":
    main()
