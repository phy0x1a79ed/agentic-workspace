"""deneb issues every machine slot through its door (channels.md R147): a member
node's operator daemon takes a slot, and restores a held one, from ``relay issue``
with no token of its own, and slots are drawn at random (R76)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from awm.channel.relayclient import RelayError

from .conftest import Door, Host, needs_relay

TOKEN_ENV = "AWM_TETHER_ISSUE_TOKEN"
PLUGINS = Path(__file__).resolve().parents[2]
TETHER = PLUGINS.parent / "service_components" / "tether"


@pytest.fixture
async def deneb(tmp_path, relay):
    h = Host(relay, tmp_path / "deneb")
    h.start()
    yield h
    await h.stop()


def issuer_through(door: Door):
    async def call(request: dict):
        return await door("issue", request, 60.0)
    return call


@pytest.mark.integration
@needs_relay
async def test_a_member_node_mints_and_restores_a_slot_through_the_door(deneb, monkeypatch):
    from awm.tether import issuer

    monkeypatch.delenv(TOKEN_ENV, raising=False)
    call = issuer_through(Door("testnode"))

    issued = await issuer.answer({}, call)
    assert 1 <= issued["slot"] <= 999
    assert len(issued["token"]) == 32 and issued["expires_in"] > 0

    # A slot the restarted relay has forgotten, which no session took meanwhile.
    held = {"slot": "418" if issued["slot"] == 417 else "417", "token": "0" * 32, "owner": "1" * 32}
    assert await issuer.answer(held, call) == {"restored": True}
    assert await issuer.answer(held, call) == {"restored": True}, "a slot already back is back"
    other = {**held, "token": "2" * 32}
    assert await issuer.answer(other, call) == {"refused": "taken"}


@pytest.mark.integration
@needs_relay
async def test_slots_through_the_door_are_drawn_at_random(deneb):
    door = Door("testnode")
    slots = sorted([(await door("issue", {}, 60.0))["slot"] for _ in range(8)])
    assert len(set(slots)) == 8
    assert slots != list(range(slots[0], slots[0] + 8)), slots


@pytest.mark.integration
@needs_relay
async def test_only_a_member_nodes_own_service_is_issued_a_slot(deneb):
    from awm.relay import hub_adapter as relay_adapter
    from awm.relay.service import RelayRefused

    with pytest.raises(RelayRefused, match="channel"):
        await relay_adapter.issue({}, "peer:testnode")
    with pytest.raises(RelayRefused, match="never through relay"):
        await relay_adapter.issue({"_caller_trust": "trusted", "_caller_session": "cc" + "a" * 32},
                                  "peer:testnode")
    with pytest.raises(RelayError, match="slot.*token.*owner"):
        await Door("testnode")("issue", {"slot": "12"}, 60.0)


async def test_an_unreachable_relay_is_one_line_for_the_daemon():
    from awm.tether import issuer

    async def refuse(request):
        raise RelayError("deneb's singleton door did not answer for relay")

    with pytest.raises(ValueError, match="^relay issue: deneb's singleton door"):
        await issuer.answer({}, refuse)


def test_the_issuer_module_answers_on_stdout_and_fails_on_stderr(tmp_path):
    """The daemon's side of the protocol: one JSON object in, one out, or a line on stderr."""
    env = {**os.environ, "AWM_HUB_URL": "http://127.0.0.1:9", "AWM_HUB_TOKEN_FILE": str(tmp_path / "none")}
    run = subprocess.run([sys.executable, "-m", "awm.tether.issuer"], input="{}", env=env,
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 1 and run.stdout == ""
    assert run.stderr.startswith("relay issue: "), run.stderr


def test_the_daemon_is_told_its_issuer(monkeypatch):
    from awm.tether import daemon, issuer, paths

    monkeypatch.setattr(paths, "ROLE", paths.OPERATOR)
    assert json.loads(daemon._env()["TETHER_ISSUER"]) == issuer.ARGV
    monkeypatch.setattr(paths, "ROLE", paths.RELAY)
    assert "TETHER_ISSUER" not in daemon._env()


def test_no_member_node_reads_the_issuing_token():
    """Only the public host's relay holds the bearer. The channel plugin and the
    operator side of the tether component never name it."""
    manifest = tomllib.loads((PLUGINS / "channel" / "awm-plugin.toml").read_text())
    assert "secrets" not in manifest["needs"]
    [relay] = [t for t in manifest["needs"]["tool"] if t["name"] == "relay"]
    assert "issue" in relay["verbs"]
    member_side = [*(PLUGINS / "channel" / "awm").rglob("*.py"), *(TETHER / "awm").rglob("*.py")]
    assert member_side
    for path in member_side:
        assert TOKEN_ENV not in path.read_text(), path


def _operator_bin() -> Path | None:
    named = os.environ.get("AWM_TETHER_OPERATOR_BIN")
    if named:
        return Path(named)
    found = sorted(PLUGINS.parents[3].glob("tether/*/target/*/tether-operator"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return found[0] if found else None


OPERATOR_BIN = _operator_bin()


class Gateway:
    """This node's gateway as the issuer reaches it: ``/invoke`` for the channel
    service's token, carried on to deneb through the door stand-in."""

    def __init__(self, loop, door: Door, token: str) -> None:
        import http.server
        import threading

        self.calls: list[dict] = []
        gateway = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                import asyncio

                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                gateway.calls.append(body)
                if self.headers.get("X-Awm-Service-Token") != token or body["name"] != "relay_issue":
                    return self._send(403, {"detail": "not the channel service's call"})
                try:
                    result = asyncio.run_coroutine_threadsafe(
                        door("issue", body["args"], 60.0), loop).result(60)
                except RelayError as exc:
                    return self._send(502, {"detail": str(exc)})
                self._send(200, {"result": json.dumps(result)})

            def _send(self, status, payload):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.mark.integration
@needs_relay
@pytest.mark.skipif(OPERATOR_BIN is None or not OPERATOR_BIN.exists(),
                    reason="needs a tether-operator binary (AWM_TETHER_OPERATOR_BIN)")
async def test_the_operator_daemon_takes_its_slot_through_the_door_with_no_token(
        deneb, relay, tmp_path):
    """The real daemon, its issuer and this node's gateway client, with deneb's
    door and gateway stood in: an invite comes back with a slot deneb issued."""
    import asyncio

    from awm.tether import issuer

    token_file = tmp_path / "channel.token"
    token_file.write_text("the-channel-spawn-token")
    gateway = Gateway(asyncio.get_running_loop(), Door("testnode"), "the-channel-spawn-token")
    sock = tmp_path / "operator.sock"
    env = {k: v for k, v in os.environ.items() if k != TOKEN_ENV and not k.lower().endswith("_proxy")}
    env.update(TETHER_RELAY=relay, TETHER_CONTROL_SOCKET=str(sock),
               TETHER_ISSUER=json.dumps(issuer.ARGV), AWM_HUB_URL=gateway.url,
               AWM_HUB_TOKEN_FILE=str(token_file))
    daemon = subprocess.Popen([str(OPERATOR_BIN)], env=env, stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE)
    try:
        for _ in range(200):
            if sock.exists():
                break
            await asyncio.sleep(0.05)

        async def ask(verb):
            reader, writer = await asyncio.open_unix_connection(str(sock))
            writer.write(json.dumps({"verb": verb, "args": {}}).encode() + b"\n")
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), 60)
            writer.close()
            return json.loads(line)

        assert (await ask("status"))["can_invite"] is True
        invite = await ask("invite")
        assert invite["ok"] is True, invite
        assert 1 <= invite["slot"] <= 999 and invite["code"].split()[0]
        assert [c["name"] for c in gateway.calls] == ["relay_issue"]
        assert gateway.calls[0]["args"] == {}
    finally:
        daemon.terminate()
        _, err = daemon.communicate(timeout=10)
        gateway.close()
    assert TOKEN_ENV.encode() not in err
