"""A task's output chunks, joined and decoded before anyone reads them."""

from __future__ import annotations

import base64

import pytest

from awm.channel import machines

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def event(seq: int, type_: str = "owner.said", **fields) -> dict:
    return {"type": type_, "seq": seq, "at": 1757620391812, "slot": 7, **fields}


def test_output_split_across_chunks_is_whole_again_before_anyone_reads_it():
    """The reason output travels as bytes rather than text.

    A three-byte character cut in half by a chunk boundary is two invalid
    fragments. Joining before decoding is the only place that can be repaired,
    and it is why decoding happens here rather than in the daemon.
    """
    star = "★".encode()
    first, second = star[:2], star[2:]
    events = [
        event(0, "task.output", task=1, stream="stdout",
              data=base64.b64encode(first).decode(), bytes=len(first)),
        event(1, "task.output", task=1, stream="stdout",
              data=base64.b64encode(second).decode(), bytes=len(second)),
    ]

    merged = machines.decoded(events, raw=False)

    assert len(merged) == 1, "consecutive chunks of one stream are one thing"
    assert merged[0]["text"] == "★"
    assert merged[0]["bytes"] == 3


def test_two_streams_are_not_run_together():
    events = [
        event(0, "task.output", task=1, stream="stdout",
              data=base64.b64encode(b"out").decode(), bytes=3),
        event(1, "task.output", task=1, stream="stderr",
              data=base64.b64encode(b"err").decode(), bytes=3),
    ]

    merged = machines.decoded(events, raw=False)

    assert [e["stream"] for e in merged] == ["stdout", "stderr"]
    assert [e["text"] for e in merged] == ["out", "err"]


def test_raw_keeps_the_bytes_for_things_that_are_not_text():
    events = [
        event(0, "task.output", task=1, stream="screen",
              data=base64.b64encode(b"\x1b[2J").decode(), bytes=4),
    ]

    merged = machines.decoded(events, raw=True)

    assert base64.b64decode(merged[0]["data"]) == b"\x1b[2J"
    assert "text" not in merged[0]
