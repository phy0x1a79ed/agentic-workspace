"""The daemon's PTY socket protocol, against a scripted socket.

Moved here from the reflection service when a second consumer appeared. These
tests need no live `claude daemon` and know nothing about whose session may be
reached — that judgement stays with the calling service.
"""
from __future__ import annotations

import json
import struct

import pytest

from awm.claudedaemon import DaemonLane
from awm.claudedaemon import pty as daemon_inject


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def frame(kind: int, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + bytes([kind]) + payload


def ctl(obj: dict) -> bytes:
    return frame(0x01, json.dumps(obj).encode())


def raw(text: str) -> bytes:
    return frame(0x00, text.encode())


class FakeSock:
    """A scripted PTY socket. Serves ``inbound`` once, then times out."""

    def __init__(self, inbound: bytes = b""):
        self._inbound = bytearray(inbound)
        self.sent: list[bytes] = []
        self.closed = False

    def settimeout(self, t):  # noqa: D102
        pass

    def recv(self, n):
        if not self._inbound:
            raise TimeoutError()
        chunk = bytes(self._inbound[:n])
        del self._inbound[:n]
        return chunk

    def sendall(self, data):
        self.sent.append(data)

    def close(self):
        self.closed = True

    # -- assertions helpers ------------------------------------------------

    def frames(self) -> list[tuple[int, bytes]]:
        reader = daemon_inject._FrameReader()
        out = []
        for blob in self.sent:
            out.extend(reader.feed(blob))
        return out


HELLO = ctl({"t": "hello", "replPid": 4242, "version": "2.1.223"}) + ctl({"t": "live"})


def target(**over) -> DaemonLane:
    base = dict(sock="/tmp/fake.sock", auth="tok", session_id="sid-1",
                repl_pid=4242, name="test", cli_version="2.1.223",
                dec_modes=(2004,))
    base.update(over)
    return DaemonLane(**base)


def opener_for(sock: FakeSock):
    return lambda _path: sock


def paste_and_submit(text, tgt, *, enter=True, opener):
    """Write ``text`` down the lane the way a sender does, minus the retrying.

    The caller owns detection, verification and retrying; this component owns
    the protocol. Going through ``open_lane`` rather than poking the connection
    directly keeps the handshake and the identity check in the path, which is
    where several of the tests below expect their refusals to come from.
    """
    with daemon_inject.open_lane(tgt, opener=opener) as conn:
        conn.write(text)
        # Not optional garnish: pumping the socket is what surfaces an
        # `auth-required`, and it is the one verb on the writer protocol a lane
        # may use to say it discarded what it was handed. Skipping it here would
        # make a host that dropped our input look like a clean write.
        conn.check_not_rejected()
        if enter:
            conn.commit()


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------

def test_frame_length_excludes_the_type_byte():
    # The single most expensive detail to get wrong: assuming the length covers
    # the type byte misparses everything after the first frame.
    encoded = daemon_inject.frame(0x00, b"abcd")
    assert encoded == b"\x00\x00\x00\x04" + b"\x00" + b"abcd"
    assert struct.unpack(">I", encoded[:4])[0] == 4


def test_handshake_keeps_frames_that_share_a_read_with_live():
    # One read can carry the greeting, the `live` marker, and the first screen
    # paint. Stopping at `live` without finishing the batch drops the paint, and
    # it is not recoverable — the bytes are gone from the socket.
    sock = FakeSock(ctl({"t": "hello", "replPid": 4242}) + ctl({"t": "live"})
                    + raw("⏵⏵ bypass permissions on"))
    conn = daemon_inject.connect(target(), opener=opener_for(sock))
    assert "bypass permissions on" in conn.screen()


def test_frame_reader_splits_a_coalesced_stream():
    stream = ctl({"t": "hello", "replPid": 4242}) + raw("xy") + ctl({"t": "live"})
    reader = daemon_inject._FrameReader()
    # Deliver it one byte at a time — a reader that assumes whole frames per
    # recv() would fall apart here, and real sockets do coalesce and split.
    got = []
    for i in range(len(stream)):
        got.extend(reader.feed(stream[i:i + 1]))
    assert [k for k, _ in got] == [0x01, 0x00, 0x01]
    assert got[1][1] == b"xy"


# ---------------------------------------------------------------------------
# Injection
# ---------------------------------------------------------------------------

def test_authenticates_before_sending_any_input():
    sock = FakeSock(HELLO)
    paste_and_submit("/compact", target(), enter=True, opener=opener_for(sock))
    frames = sock.frames()
    kinds = [k for k, _ in frames]
    assert kinds[0] == 0x01, "first frame must be the auth control frame"
    assert json.loads(frames[0][1])["t"] == "auth"
    assert 0x00 not in kinds[:1]


def test_text_is_delivered_as_a_bracketed_paste():
    # Without bracketing, a leading `/` opens the TUI slash menu instead of
    # landing as text — the same reason the tmux path pastes with `-p`.
    sock = FakeSock(HELLO)
    paste_and_submit("/compact", target(), enter=True, opener=opener_for(sock))
    payloads = [p for k, p in sock.frames() if k == 0x00]
    assert payloads[0] == b"\x1b[200~/compact\x1b[201~"


def test_enter_is_a_separate_write_after_the_paste():
    sock = FakeSock(HELLO)
    paste_and_submit("/compact", target(), enter=True, opener=opener_for(sock))
    payloads = [p for k, p in sock.frames() if k == 0x00]
    assert payloads == [b"\x1b[200~/compact\x1b[201~", b"\r"]


def test_no_enter_when_not_submitting():
    sock = FakeSock(HELLO)
    paste_and_submit("draft", target(), enter=False, opener=opener_for(sock))
    payloads = [p for k, p in sock.frames() if k == 0x00]
    assert payloads == [b"\x1b[200~draft\x1b[201~"]


def test_unbracketed_when_the_session_does_not_report_the_mode():
    sock = FakeSock(HELLO)
    paste_and_submit("hello", target(dec_modes=(1000,)), enter=False,
                     opener=opener_for(sock))
    payloads = [p for k, p in sock.frames() if k == 0x00]
    assert payloads == [b"hello"]


def test_ping_is_answered_with_a_pong():
    sock = FakeSock(ctl({"t": "hello", "replPid": 4242}) + ctl({"t": "ping"})
                    + ctl({"t": "live"}))
    paste_and_submit("hi", target(), enter=False, opener=opener_for(sock))
    sent = [json.loads(p) for k, p in sock.frames() if k == 0x01]
    assert {"t": "pong"} in sent


def test_auth_required_is_surfaced_not_swallowed():
    sock = FakeSock(ctl({"t": "hello", "replPid": 4242}) + ctl({"t": "auth-required"}))
    with pytest.raises(daemon_inject.DaemonError, match="input token"):
        paste_and_submit("hi", target(), enter=False, opener=opener_for(sock))


def test_unrecognised_greeting_refuses():
    # A CLI update that moves this private protocol should degrade to a clear
    # refusal, not to writing bytes at a socket we no longer understand.
    sock = FakeSock(ctl({"t": "hello", "somethingElse": True, "version": "9.9.9"}))
    with pytest.raises(daemon_inject.DaemonError, match="unrecognised"):
        paste_and_submit("hi", target(), enter=False, opener=opener_for(sock))
# ---------------------------------------------------------------------------
# Identity: is this socket the caller's own session?
# ---------------------------------------------------------------------------

def test_a_socket_hosting_another_repl_refuses():
    # The roster hands out recycled `spare/*.pty.sock` paths, so the socket it
    # named can belong to a different job by the time we dial it. The greeting
    # says whose it is; not checking meant typing a stranger's compact into a
    # stranger's prompt and reporting success.
    sock = FakeSock(ctl({"t": "hello", "replPid": 999, "version": "2.1.223"})
                    + ctl({"t": "live"}))
    with pytest.raises(daemon_inject.DaemonError, match="hosting REPL 999"):
        paste_and_submit("hi", target(), enter=False, opener=opener_for(sock))


def test_a_host_that_never_greets_refuses():
    # This used to pass straight through: the unrecognised-message check was
    # guarded on having received *something*, so silence sailed past it and
    # authentication proceeded into a host that never identified itself.
    with pytest.raises(daemon_inject.DaemonError, match="never greeted"):
        paste_and_submit("hi", target(), enter=False,
                                        opener=opener_for(FakeSock(b"")))


def test_enter_refuses_before_authentication():
    conn = daemon_inject.Connection(target(), opener=opener_for(FakeSock(HELLO)))
    with pytest.raises(daemon_inject.DaemonError, match="before authenticating"):
        conn.press_enter()
# ---------------------------------------------------------------------------
# Rejection observed after the write, not after the socket is closed
# ---------------------------------------------------------------------------

class RejectingSock(FakeSock):
    """A host that greets normally, then discards the first raw frame it gets.

    Modelled on the real failure: an unauthenticated raw frame is dropped
    silently apart from the `auth-required` that follows it, and the write path
    used to close the socket before that frame could arrive.
    """

    def sendall(self, data):
        super().sendall(data)
        if any(k == 0x00 for k, _ in daemon_inject._FrameReader().feed(data)):
            self._inbound += ctl({"t": "auth-required"})


def test_input_the_host_discarded_is_not_reported_as_sent():
    sock = RejectingSock(HELLO)
    with pytest.raises(daemon_inject.DaemonError, match="input token"):
        paste_and_submit("hi", target(), enter=False, opener=opener_for(sock))


