"""Browser microphone → the PulseAudio ``virtmic`` null-sink.

The service half of mic's ``stream`` session. The browser opens
``POST /svc/mic/session/stream`` declaring its capture format, the gateway hands
both sides a direct byte-relayed WebSocket, and every binary frame that arrives
is s16le PCM written straight into a ``pacat`` playing into the sink — so
whatever the phone hears, this machine records.

This used to be an off-host HTTPS listener with a hand-rolled WebSocket, because
``getUserMedia`` needs a secure context and the hub is loopback-only. ``httpsfront``
now puts the whole of awm behind one CA-verified TLS door, so the listener,
its TLS, and its copy of the cert-minting code are gone; what is left is the
part that was always the point.

mic does not own the audio plumbing — PulseAudio and the null-sink belong to the
``virtmic`` service. The dependency is resolved per-stream rather than at boot
(the gateway guarantees no start order between services, and a sink provisioned
once at startup silently disappears when PulseAudio idle-exits), which is why
``virtmic ensure`` runs immediately before ``pacat`` is spawned.

Single-stream policy: a new stream supersedes the old one so two phones can't
mix into the sink. The loser is *told* rather than left to infer it from a
broken pipe.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

log = logging.getLogger("awm.mic.bridge")

#: Which sink to feed. The *name* lives here because mic is what hands it to
#: ``pacat``; creating the sink is virtmic's job.
SINK = os.environ.get("MIC_SINK", "virtmic")

#: The session handler starts as soon as the gateway accepts the open, which is
#: before the browser's socket has necessarily arrived. Without a deadline an
#: abandoned session would hold a live ``pacat`` on the sink forever.
FIRST_FRAME_TIMEOUT_S = 30.0

#: Wide enough for every browser's native rate (Chrome/Firefox pick the device's
#: own, commonly 44.1k or 48k), narrow enough that a garbage value can't reach
#: ``pacat``.
MIN_RATE, MAX_RATE = 8000, 192000


class _Stream:
    """One browser's audio and the ``pacat`` it feeds."""

    def __init__(self, proc: asyncio.subprocess.Process,
                 rate: int, channels: int) -> None:
        self.proc = proc
        self.rate = rate
        self.channels = channels
        self.superseded = asyncio.Event()
        self._reaped = False

    def supersede(self) -> None:
        """Another device took the microphone; this stream ends."""
        self.superseded.set()
        self.kill()

    def kill(self) -> None:
        """Terminate *this* stream's subprocess. Safe to call twice."""
        stdin = self.proc.stdin
        if stdin is not None:
            try:
                stdin.close()
            except Exception:  # noqa: BLE001 — already-broken pipe
                pass
        if self.proc.returncode is None:
            try:
                self.proc.terminate()
            except ProcessLookupError:
                pass
        if not self._reaped:
            self._reaped = True
            try:
                asyncio.get_running_loop().create_task(self.proc.wait())
            except RuntimeError:  # no loop (tests) — nothing to reap against
                pass


class _State:
    """Live service state, read by the ``mic_status`` verb.

    Loop-only: every mutation and every read happens on the service's single
    event loop, so there is no lock. That is a consequence of the move — the old
    listener ran a thread per socket and needed one.
    """

    def __init__(self) -> None:
        self.active_streams = 0
        self.last_pcm_ts: float = 0.0
        self.sink = SINK
        self.current: _Stream | None = None


STATE = _State()


def status() -> dict:
    """Snapshot of the audio path for the status verb."""
    cur = STATE.current
    return {
        "sink": STATE.sink,
        "active_streams": STATE.active_streams,
        "last_pcm_ts": STATE.last_pcm_ts or None,
        "stream_rate": cur.rate if cur else None,
        "stream_channels": cur.channels if cur else None,
    }


# -- the audio path ---------------------------------------------------------


def pacat_argv(sink: str, rate: int, channels: int) -> list[str]:
    """The exact argv the browser's PCM is played into the sink with.

    Named rather than inlined so it can be asserted: this list *is* the audio
    path, and moving mic onto the hub must not perturb it. ``--latency-msec=40``
    in particular is tuned against the sink, not against the transport.
    """
    return [
        "pacat", "--playback", "-d", sink, "--raw",
        "--format=s16le", f"--rate={rate}", f"--channels={channels}",
        "--client-name=awm-mic", "--stream-name=browser-mic",
        "--latency-msec=40",
    ]


def pacat_env() -> dict:
    """The environment ``pacat`` needs to find the user's PulseAudio socket.

    The gateway respawns services under systemd's minimal environment, which has
    no ``XDG_RUNTIME_DIR`` — and without it libpulse cannot locate
    ``$XDG_RUNTIME_DIR/pulse/native`` and fails with "Connection refused" even
    when the daemon is perfectly healthy. That mismatch is invisible from
    ``virtmic_status``, which reports on *its own* repaired environment.

    This is about mic's subprocess environment, not about owning the audio
    plumbing, so it stays here rather than moving to virtmic.
    """
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return env


def parse_init(init: dict[str, Any]) -> tuple[int, int]:
    """Validate the capture format the browser declared when opening the session.

    Knowing the rate *before* any audio arrives is the point of carrying it in
    the open payload rather than as a first message on the audio channel: pacat
    is already running when the first sample lands. A wrong rate is also the
    worst kind of failure — it pitch-shifts everything the machine records with
    no error anywhere — so it is rejected here rather than handed to pacat.
    """
    fmt = init.get("format", "s16le")
    if fmt != "s16le":
        raise ValueError(f"unsupported format {fmt!r}; mic accepts s16le only")
    try:
        rate = int(init.get("sampleRate", 48000))
        channels = int(init.get("channels", 1))
    except (TypeError, ValueError):
        raise ValueError("sampleRate and channels must be integers") from None
    if not MIN_RATE <= rate <= MAX_RATE:
        raise ValueError(
            f"sampleRate {rate} outside [{MIN_RATE}, {MAX_RATE}]")
    if channels not in (1, 2):
        raise ValueError(f"channels {channels} must be 1 or 2")
    return rate, channels


async def _ensure_sink() -> None:
    """Ask virtmic to guarantee the sink exists, right before we feed it.

    Best-effort: if virtmic is unreachable we still start ``pacat``, because the
    sink may well already exist and refusing audio outright would be a worse
    failure than a possibly-dead one.
    """
    try:
        from awm import gatewayclient
        await gatewayclient.call("virtmic", "ensure", timeout=30.0)
    except Exception as exc:  # noqa: BLE001
        log.warning("virtmic ensure failed (%s); starting pacat anyway", exc)


async def start_stream(rate: int, channels: int) -> _Stream:
    """Spawn this stream's ``pacat``, superseding whatever was feeding the sink."""
    await _ensure_sink()
    proc = await asyncio.create_subprocess_exec(
        *pacat_argv(STATE.sink, rate, channels),
        stdin=asyncio.subprocess.PIPE,
        env=pacat_env(),
    )
    stream = _Stream(proc, rate, channels)
    prev, STATE.current = STATE.current, stream
    if prev is not None:
        prev.supersede()
    log.info("pacat started rate=%d ch=%d sink=%s", rate, channels, STATE.sink)
    return stream


# -- the session ------------------------------------------------------------


async def _send(bridge: Any, obj: dict) -> None:
    try:
        await bridge.send(json.dumps(obj))
    except Exception as exc:  # noqa: BLE001 — a dead socket isn't news here
        log.debug("mic control send failed: %s", exc)


async def _close(bridge: Any) -> None:
    try:
        await bridge.close()
    except Exception:  # noqa: BLE001
        pass


async def _recv_loop(bridge: Any, stream: _Stream) -> None:
    """Binary frames are PCM for ``pacat``; text frames are JSON control."""
    import websockets

    deadline: float | None = FIRST_FRAME_TIMEOUT_S
    while True:
        try:
            raw = await asyncio.wait_for(bridge.recv(), timeout=deadline)
        except asyncio.TimeoutError:
            log.info("mic session opened but no audio arrived in %.0fs; closing",
                     FIRST_FRAME_TIMEOUT_S)
            return
        except websockets.ConnectionClosed:
            return
        deadline = None  # only the first frame is deadlined

        if isinstance(raw, (bytes, bytearray)):
            stdin = stream.proc.stdin
            if stdin is None or stream.proc.returncode is not None:
                await _send(bridge, {"type": "error", "message": "pacat exited"})
                return
            try:
                stdin.write(bytes(raw))
                await stdin.drain()
            except (BrokenPipeError, ConnectionResetError, RuntimeError):
                log.info("pacat pipe broke (superseded, or pacat died)")
                return
            STATE.last_pcm_ts = time.time()
            continue

        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(msg, dict) and msg.get("type") == "ping":
            # Echoed so the page can display a real round-trip time. The old
            # protocol echoed it too and then threw the answer away.
            await _send(bridge, {"type": "pong", "t": msg.get("t")})


async def _pump(bridge: Any, stream: _Stream) -> None:
    """Run the receive loop until it ends or another device takes the mic."""
    recv = asyncio.create_task(_recv_loop(bridge, stream), name="mic-recv")
    sup = asyncio.create_task(stream.superseded.wait(), name="mic-superseded")
    try:
        done, _ = await asyncio.wait(
            {recv, sup}, return_when=asyncio.FIRST_COMPLETED)
        if sup in done and recv not in done:
            await _send(bridge, {
                "type": "superseded",
                "message": "another device took the microphone",
            })
        if recv in done and not recv.cancelled() and recv.exception():
            log.warning("mic receive loop failed: %s", recv.exception())
    finally:
        for task in (recv, sup):
            task.cancel()
        await asyncio.gather(recv, sup, return_exceptions=True)


async def run_session(ctx: Any) -> None:
    """Drive one ``stream`` session: browser PCM in, control frames out."""
    # Open the bridge FIRST. The gateway drops the browser's socket if this side
    # hasn't attached within five seconds, and the virtmic call below is allowed
    # thirty — so ensuring the sink first fails precisely when virtmic is doing
    # its job, which is the case the lazy call exists to serve.
    bridge = await ctx.open_bridge()
    try:
        try:
            rate, channels = parse_init(ctx.init or {})
        except ValueError as exc:
            await _send(bridge, {"type": "error", "message": str(exc)})
            return
        try:
            stream = await start_stream(rate, channels)
        except Exception as exc:  # noqa: BLE001
            log.exception("pacat spawn failed")
            await _send(bridge, {"type": "error", "message": f"pacat: {exc}"})
            return

        STATE.active_streams += 1
        try:
            # `ready` — not socket-open — is what turns the page's badge purple.
            # An open socket says nothing about whether pacat exists; during the
            # XDG_RUNTIME_DIR outage the page displayed "48 kHz · live" while
            # nothing was recording at all.
            await _send(bridge, {
                "type": "ready", "sampleRate": rate,
                "channels": channels, "sink": STATE.sink,
            })
            await _pump(bridge, stream)
        finally:
            STATE.active_streams = max(0, STATE.active_streams - 1)
            stream.kill()
            # Release the shared slot only if we still own it — on a fast
            # reconnect the successor has already claimed it.
            if STATE.current is stream:
                STATE.current = None
    finally:
        await _close(bridge)
        log.info("mic session closed")
