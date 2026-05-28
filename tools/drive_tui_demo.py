#!/usr/bin/env python3
"""Drive the friend-side TUI: connect, send a few exec frames, send a chat
message, wait briefly, send bye. Used to exercise tui.rs end-to-end."""
import asyncio
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse

import aiomqtt

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_op import Session, load_env_file, ENV_FILE  # noqa: E402


class Args:
    def __init__(self, env):
        self.name = "tuitest"
        self.mqtt_url = env["EMQX_URL"]
        self.mqtt_user = env["EMQX_USER"]
        self.mqtt_pass = env["EMQX_PASS"]
        self.cf_turn_id = env.get("CF_TURN_TOKEN_ID")
        self.cf_turn_token = env.get("CF_TURN_API_TOKEN")
        self.no_turn = False
        self.timeout = 15.0
        self.connect_timeout = 25.0
        self.mqtt_relay = False
        self.scope = "demo-scope"
        self.operator_name = "drive_tui_demo"


async def amain():
    env = load_env_file(ENV_FILE)
    args = Args(env)

    url = urlparse(args.mqtt_url)
    client_id = f"operator-{uuid.uuid4().hex[:8]}"
    async with aiomqtt.Client(
        hostname=url.hostname,
        port=url.port or 8084,
        username=args.mqtt_user,
        password=args.mqtt_pass,
        identifier=client_id,
        keepalive=30,
        tls_params=aiomqtt.TLSParameters(),
        transport="websockets",
        websocket_path=url.path or "/mqtt",
    ) as mqtt:
        session = Session(args, mqtt, stream_to_stdio=True)
        await session.setup_pc()
        sig_task = asyncio.create_task(session.signaling_listener())
        try:
            await session.send_offer()
            await asyncio.wait_for(session.channel_open.wait(), timeout=args.connect_timeout)
            print("[driver] data channel open", file=sys.stderr)

            # Greet first — drives the TUI header's scope field.
            session.send_hello(args.scope, args.operator_name)
            # Long hold so the verification harness can capture a "live +
            # scope" snapshot before any exec arrives. Bump if you want
            # more headroom for the human-eye snapshot.
            await asyncio.sleep(8.0)

            # 1. plain exec
            rc, _, _ = await session.exec_one("echo hello from mira; uname -n", 5.0)
            print(f"[driver] echo rc={rc}", file=sys.stderr)

            # 2. chat → friend TUI
            session.send_chat("hi from the operator, this is a TUI test")
            await asyncio.sleep(0.3)
            session.send_chat("running one more command then disconnecting")
            await asyncio.sleep(0.3)

            # 3. another exec — non-zero exit
            rc, _, _ = await session.exec_one("ls /nope-does-not-exist", 5.0)
            print(f"[driver] ls rc={rc}", file=sys.stderr)

            # 4. multi-line output
            rc, _, _ = await session.exec_one("for i in 1 2 3; do echo line $i; done", 5.0)
            print(f"[driver] loop rc={rc}", file=sys.stderr)

            await asyncio.sleep(0.5)
            await session.send_bye()
        finally:
            sig_task.cancel()
            try:
                await sig_task
            except Exception:
                pass
            await session.close()


if __name__ == "__main__":
    asyncio.run(amain())
