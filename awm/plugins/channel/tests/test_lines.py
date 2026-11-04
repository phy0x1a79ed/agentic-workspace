from pathlib import Path

import pytest

from awm.channel import lines

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

T = Path("/state/transcripts/acre-anchor-kettle.txt")


def test_a_short_message_is_pushed_whole_with_its_trust_level():
    line = lines.push_line(kind="room", name="acre-anchor-kettle", trust="untrusted",
                           who="visitor", text="hello", seq=1, transcript=T)
    assert line == "room acre-anchor-kettle [untrusted] from visitor: hello"


def test_a_long_message_is_cut_and_the_line_names_where_it_is_whole():
    text = "x" * (lines.TEXT_MAX + 500)
    line = lines.push_line(kind="room", name="n", trust="untrusted", who="v",
                           text=text, seq=7, transcript=T)
    assert len(line) < lines.TEXT_MAX + 200
    assert f"message 7 in full: {T}" in line


def test_a_message_of_many_lines_is_one_pushed_line():
    line = lines.push_line(kind="room", name="n", trust="untrusted", who="v",
                           text="one\ntwo", seq=1, transcript=T)
    assert "\n" not in line
    assert "one ⏎ two" in line


def test_the_transcript_holds_each_message_whole_after_a_header(tmp_path):
    t = lines.Transcripts(tmp_path)
    t.append("n", seq=1, by="guest", who="v", text="first\nsecond")
    t.append("n", seq=2, by="host", who="me", text="third")
    body = t.path("n").read_text()
    assert body.startswith("--- 1 guest v ")
    assert "first\nsecond\n--- 2 host me " in body
