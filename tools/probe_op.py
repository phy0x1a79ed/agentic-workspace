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
import sys
import uuid
from pathlib import Path
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

ENV_FILE = Path(__file__).resolve().parent / ".env.emqx"


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
    p.add_argument("--mqtt-url", default=None)
    p.add_argument("--mqtt-user", default=None)
    p.add_argument("--mqtt-pass", default=None)
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
            f"set in env or {ENV_FILE} (see .env.emqx.example)"
        )
    return args


class Session:
    def __init__(self, args: argparse.Namespace, mqtt: aiomqtt.Client):
        self.args = args
        self.mqtt = mqtt
        self.pc: RTCPeerConnection | None = None
        self.channel = None
        self.channel_open = asyncio.Event()
        self.connection_dead = asyncio.Event()
        self.next_id = 0
        self.in_flight: dict = {}

    async def setup_pc(self):
        config = RTCConfiguration(iceServers=[
            RTCIceServer(urls=["stun:stun.l.google.com:19302"]),
        ])
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
            sys.stdout.buffer.write(base64.b64decode(frame["data"]))
            sys.stdout.buffer.flush()
        elif ftype == "stderr":
            sys.stderr.buffer.write(base64.b64decode(frame["data"]))
            sys.stderr.buffer.flush()
        elif ftype == "exit":
            if entry is not None:
                entry["code"] = frame.get("code") if frame.get("code") is not None else 0
                entry["event"].set()
        elif ftype == "error":
            sys.stderr.write(f"[probe error] {frame.get('message')}\n")
            if entry is not None:
                entry["code"] = 127
                entry["event"].set()
        else:
            logger.warning("unexpected frame type: %s", ftype)

    async def send_offer(self):
        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)
        await self._wait_ice_gathering_complete()
        payload = {"type": "offer", "sdp": self.pc.localDescription.sdp}
        topic = f"probe/{self.args.name}/from-operator/sdp"
        await self.mqtt.publish(topic, json.dumps(payload).encode(), qos=1)
        logger.info("published SDP offer to %s", topic)

    async def _wait_ice_gathering_complete(self):
        if self.pc.iceGatheringState == "complete":
            return
        done = asyncio.Event()

        @self.pc.on("icegatheringstatechange")
        def _on_change():
            if self.pc.iceGatheringState == "complete":
                done.set()

        await done.wait()

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

    async def exec_one(self, cmd: str, timeout: float) -> int:
        if not self.channel_open.is_set():
            sys.stderr.write("[operator] channel not open\n")
            return 4
        fid = self._alloc_id()
        entry = {"event": asyncio.Event(), "code": 1}
        self.in_flight[fid] = entry
        frame = json.dumps({"type": "exec", "id": fid, "cmd": cmd})
        self.channel.send(frame)
        try:
            await asyncio.wait_for(entry["event"].wait(), timeout=timeout)
        except asyncio.TimeoutError:
            sys.stderr.write(f"[operator] timeout after {timeout}s\n")
            return 124
        finally:
            self.in_flight.pop(fid, None)
        return entry["code"]

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
        await session.exec_one(line, session.args.timeout)


def _read_input(prompt: str) -> str:
    sys.stderr.write(prompt)
    sys.stderr.flush()
    return input()


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
                sys.stderr.write(
                    f"[operator] pc state {session.pc.connectionState}\n"
                )
                return 4

            if args.mode == "exec":
                rc = await session.exec_one(args.cmd, args.timeout)
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
