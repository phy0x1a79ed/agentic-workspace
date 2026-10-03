"""Tests for ``peer_files.materialize`` — the proxy's path rewrite.

The defect this exists to prevent is a *successful-looking* reply whose ``path``
names a file on another machine. So the assertions are mostly about honesty:

* a fetched file's ``path`` is local and its remote one is preserved elsewhere;
* a file that could NOT be fetched has ``path: None`` and a named ``error`` —
  never a foreign path left in place;
* one file's failure does not cost the others;
* a reply with nothing to rewrite comes back byte-for-byte unchanged, since this
  runs over every peer-routed reply regardless of verb.
"""

from __future__ import annotations

import json

import pytest

from awm.gateway import peer_files


@pytest.fixture
def fetches(monkeypatch, tmp_path):
    """Fake ``fetch_peer_file_sync``: writes a stub file, or raises on demand."""
    calls: list = []
    fail: dict = {}

    def _fetch(peer, url, *, dest_dir=None, filename=None, entry=None, as_=None):
        calls.append((peer, url, dest_dir, filename))
        if url in fail:
            raise fail[url]
        name = (filename or url.rsplit("/", 1)[-1])
        dest = f"{dest_dir}/{name}"
        with open(dest, "wb") as fh:
            fh.write(b"bytes")
        return dest

    from awm import gatewayclient
    monkeypatch.setattr(gatewayclient, "fetch_peer_file_sync", _fetch)
    return calls, fail


def _reply(*files, **extra):
    body = {"files": list(files), "count": len(files),
            "dir": "/tmp/awm-social-remote", "node": "mira"}
    body.update(extra)
    return json.dumps(body)


# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "not json at all",
    json.dumps([1, 2, 3]),                       # not an object
    json.dumps({"accounts": []}),                # no files key
    json.dumps({"files": []}),                   # empty
    json.dumps({"files": [{"path": "/tmp/x"}]}),  # files, but no url to fetch
])
def test_replies_with_nothing_to_rewrite_pass_through_unchanged(fetches, text):
    assert peer_files.materialize(text, "mira") == text


def test_non_string_result_passes_through(fetches):
    sentinel = {"already": "decoded"}
    assert peer_files.materialize(sentinel, "mira") is sentinel


def test_path_is_rewritten_and_the_remote_one_kept(fetches):
    out = json.loads(peer_files.materialize(
        _reply({"filename": "image.png", "size": 5, "path": "/tmp/r/image.png",
                "url": "/files/tmp/r/image.png"}), "mira"))

    (f,) = out["files"]
    assert f["path"] != "/tmp/r/image.png"
    assert open(f["path"], "rb").read() == b"bytes"
    assert f["remote_path"] == "/tmp/r/image.png"
    assert "error" not in f
    # `dir` follows `path`: it named the remote temp dir too.
    assert out["remote_dir"] == "/tmp/awm-social-remote"
    assert out["dir"] != "/tmp/awm-social-remote"
    assert out["fetched_from"] == "mira"


def test_a_refused_file_becomes_a_named_error_not_a_phantom_path(fetches):
    _, fail = fetches
    fail["/files/tmp/r/id.pem"] = RuntimeError("denylist hides it")

    out = json.loads(peer_files.materialize(
        _reply({"filename": "id.pem", "path": "/tmp/r/id.pem",
                "url": "/files/tmp/r/id.pem"}), "mira"))

    (f,) = out["files"]
    assert f["path"] is None                      # <- the whole point
    assert "denylist hides it" in f["error"]
    assert f["remote_path"] == "/tmp/r/id.pem"


def test_one_failure_does_not_cost_the_others(fetches):
    _, fail = fetches
    fail["/files/tmp/r/b.pem"] = RuntimeError("nope")

    out = json.loads(peer_files.materialize(
        _reply({"filename": "a.png", "path": "/tmp/r/a.png",
                "url": "/files/tmp/r/a.png"},
               {"filename": "b.pem", "path": "/tmp/r/b.pem",
                "url": "/files/tmp/r/b.pem"},
               {"filename": "c.ics", "path": "/tmp/r/c.ics",
                "url": "/files/tmp/r/c.ics"}), "mira"))

    a, b, c = out["files"]
    assert open(a["path"], "rb").read() == b"bytes"
    assert b["path"] is None and b["error"]
    assert open(c["path"], "rb").read() == b"bytes"


def test_a_file_with_no_url_is_an_error_too(fetches):
    out = json.loads(peer_files.materialize(
        _reply({"filename": "a.png", "path": "/tmp/r/a.png", "url": None},
               {"filename": "b.png", "path": "/tmp/r/b.png",
                "url": "/files/tmp/r/b.png"}), "mira"))

    a, b = out["files"]
    assert a["path"] is None and "no url" in a["error"]
    assert b["path"] is not None


def test_all_files_share_one_local_dir(fetches):
    calls, _ = fetches
    peer_files.materialize(
        _reply({"filename": "a.png", "url": "/files/tmp/r/a.png"},
               {"filename": "b.png", "url": "/files/tmp/r/b.png"}), "mira")
    assert len({c[2] for c in calls}) == 1
