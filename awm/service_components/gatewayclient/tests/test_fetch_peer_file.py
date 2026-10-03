"""Tests for ``fetch_peer_file_sync`` — cross-peer bytes.

This is the transport that makes a peer-produced file openable on the calling
node. Four properties matter, and each has a failure mode worth a test:

1. **The peer names the path; this side names the host.** A reply is data from
   another machine, so an absolute or protocol-relative URL must be refused
   rather than dialled — otherwise a compromised or buggy peer could aim a
   credentialed GET at a third party.
2. **The bytes land locally, streamed.** The point of the whole exercise.
3. **A 404 is explained.** The mount's denylist 404s exactly like a missing
   file, so a bare "404" would send the reader looking in the wrong place.
4. **A rotated bearer retries once** — same contract as ``call_peer``.

Driven against fakes: no gateway, no TLS, no ssh.
"""

from __future__ import annotations

import os

import pytest

from awm import gatewayclient as gc


PEER = {"edge_url": "https://10.0.0.9:12100", "ssh_alias": "peerz"}


class _FakeStream:
    """What ``httpx.Client.stream`` yields — a context manager over a response."""

    def __init__(self, status, chunks=(), text=""):
        self.status_code = status
        self._chunks = list(chunks)
        self.text = text

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_bytes(self, _size=None):
        yield from self._chunks

    def read(self):
        return b""


class _FakeClient:
    """Stand-in for ``httpx.Client``; records what it was asked to fetch."""

    def __init__(self, responses, calls):
        self._responses = responses
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def stream(self, method, url, headers=None):
        self._calls.append((method, url, dict(headers or {})))
        return self._responses.pop(0)


@pytest.fixture
def wired(monkeypatch):
    """``fetch_peer_file_sync`` with resolve/cred/CA/httpx all faked out."""
    calls: list = []
    creds: list = []
    state = {"responses": []}

    monkeypatch.setattr(gc, "resolve_peer", lambda name, **kw: dict(PEER))
    monkeypatch.setattr(gc, "_peer_ca", lambda: "/nonexistent/ca.pem")

    def _cred(alias, *, force=False, timeout=15.0):
        creds.append((alias, force))
        return "rotated" if force else "original"

    monkeypatch.setattr(gc, "fetch_peer_cred", _cred)
    monkeypatch.setattr(
        gc.httpx, "Client",
        lambda **kw: _FakeClient(state["responses"], calls))
    return state, calls, creds


# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [
    "https://evil.example/x",          # absolute — names its own host
    "//evil.example/x",                # protocol-relative — still names a host
    "files/tmp/a.png",                 # not origin-relative
    "",
])
def test_refuses_a_url_that_names_a_host(wired, bad):
    """The bearer must never be sent anywhere the local side did not choose."""
    with pytest.raises(gc.PeerError):
        gc.fetch_peer_file_sync("mira", bad)


def test_downloads_to_a_local_path(wired, tmp_path):
    state, calls, _ = wired
    state["responses"] = [_FakeStream(200, [b"abc", b"def"])]

    got = gc.fetch_peer_file_sync(
        "mira", "/files/tmp/awm-social-x/image.png", dest_dir=str(tmp_path))

    assert got == str(tmp_path / "image.png")
    assert open(got, "rb").read() == b"abcdef"
    method, url, headers = calls[0]
    assert (method, url) == (
        "GET", "https://10.0.0.9:12100/files/tmp/awm-social-x/image.png")
    assert headers["Authorization"] == "Bearer original"


def test_peer_chosen_filename_cannot_escape_the_dest_dir(wired, tmp_path):
    """The remote side names the file, this side names the directory."""
    state, _, _ = wired
    state["responses"] = [_FakeStream(200, [b"x"])]

    got = gc.fetch_peer_file_sync(
        "mira", "/files/tmp/a/b.png", dest_dir=str(tmp_path),
        filename="../../etc/passwd")

    assert os.path.dirname(got) == str(tmp_path)
    assert os.path.basename(got) == "passwd"


def test_404_names_both_causes(wired, tmp_path):
    """A masked file and an unregistered mount are indistinguishable by status."""
    state, _, _ = wired
    state["responses"] = [_FakeStream(404)]

    with pytest.raises(gc.PeerError) as exc:
        gc.fetch_peer_file_sync(
            "mira", "/files/tmp/a/id_rsa.pem", dest_dir=str(tmp_path))

    msg = str(exc.value)
    assert "denylist" in msg and "mount" in msg


def test_401_refetches_the_credential_once(wired, tmp_path):
    state, calls, creds = wired
    state["responses"] = [_FakeStream(401), _FakeStream(200, [b"ok"])]

    got = gc.fetch_peer_file_sync(
        "mira", "/files/tmp/a/b.txt", dest_dir=str(tmp_path))

    assert open(got, "rb").read() == b"ok"
    assert creds == [("peerz", False), ("peerz", True)]
    assert calls[1][2]["Authorization"] == "Bearer rotated"


def test_non_404_error_status_raises(wired, tmp_path):
    state, _, _ = wired
    state["responses"] = [_FakeStream(500, text="boom")]

    with pytest.raises(gc.PeerError) as exc:
        gc.fetch_peer_file_sync("mira", "/files/a", dest_dir=str(tmp_path))
    assert "500" in str(exc.value)
