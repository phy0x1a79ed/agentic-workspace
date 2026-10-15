"""The saved defaults survive `/effort` and `/model`, and the screen reader sees only what is shown.

The settings file is a real file in a temporary directory; the CLI's own write is
played by the test.
"""
from __future__ import annotations

import json

import pytest

from awm.reflection import settings_guard, termscreen

BASE = {
    "theme": "dark",
    "model": "opus",
    "effortLevel": "medium",
    "modelSettings": {
        "claude-opus-5-5": {"effortLevel": "high", "autoCompactWindow": 200000},
        "claude-sonnet-5": {"effortLevel": "medium"},
    },
    "hooks": {"Stop": []},
}


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.now += s


@pytest.fixture
def file(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(BASE, indent=2) + "\n")
    return path


def cli_write(path, edit, indent=2):
    doc = json.loads(path.read_text())
    edit(doc)
    path.write_text(json.dumps(doc, indent=indent) + "\n")


def run(file, edit, **kw):
    snap = settings_guard.snapshot(file)
    cli_write(file, edit)
    clock = Clock()
    return settings_guard.restore(snap, sleep=clock.sleep, clock=clock, **kw)


def test_a_changed_model_effort_is_put_back_and_nothing_else_moves(file):
    before = file.read_text()
    out = run(file, lambda d: d["modelSettings"]["claude-opus-5-5"].update(effortLevel="low"))
    assert out["restored"] == ["modelSettings.claude-opus-5-5.effortLevel"]
    assert file.read_text() == before


def test_a_changed_top_level_effort_and_model_are_put_back(file):
    before = file.read_text()
    out = run(file, lambda d: d.update(effortLevel="low", model="sonnet"))
    assert out["restored"] == ["effortLevel", "model"]
    assert file.read_text() == before


def test_keys_the_cli_added_are_removed_again(file):
    def edit(doc):
        doc["modelSettings"]["claude-fable-5"] = {"effortLevel": "low"}
    before = file.read_text()
    run(file, edit)
    assert file.read_text() == before


def test_a_whole_modelsettings_the_cli_created_is_removed(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"theme": "dark"}, indent=2) + "\n")
    run(path, lambda d: d.update(modelSettings={"claude-opus-5-5": {"effortLevel": "low"}},
                                 effortLevel="low"))
    assert json.loads(path.read_text()) == {"theme": "dark"}


def test_a_key_the_cli_deleted_comes_back(file):
    run(file, lambda d: d.pop("effortLevel"))
    assert json.loads(file.read_text())["effortLevel"] == "medium"


def test_edits_to_other_keys_made_meanwhile_are_merged_not_overwritten(file):
    def edit(doc):
        doc["modelSettings"]["claude-opus-5-5"]["effortLevel"] = "low"
        doc["theme"] = "light"
        doc["modelSettings"]["claude-opus-5-5"]["autoCompactWindow"] = 300000
        doc["added"] = True
    run(file, edit)
    doc = json.loads(file.read_text())
    assert doc["theme"] == "light" and doc["added"] is True
    assert doc["modelSettings"]["claude-opus-5-5"] == {"effortLevel": "high",
                                                       "autoCompactWindow": 300000}


@pytest.mark.parametrize("indent", [4, "\t"])
def test_the_files_own_indentation_is_kept(file, indent):
    file.write_text(json.dumps(BASE, indent=indent) + "\n")
    before = file.read_text()
    snap = settings_guard.snapshot(file)
    cli_write(file, lambda d: d.update(effortLevel="low"), indent=indent)
    clock = Clock()
    settings_guard.restore(snap, sleep=clock.sleep, clock=clock)
    assert file.read_text() == before


def test_a_file_without_a_trailing_newline_keeps_none(file):
    file.write_text(json.dumps(BASE, indent=2))
    snap = settings_guard.snapshot(file)
    file.write_text(json.dumps({**BASE, "effortLevel": "low"}, indent=2))
    clock = Clock()
    settings_guard.restore(snap, sleep=clock.sleep, clock=clock)
    assert file.read_text() == json.dumps(BASE, indent=2)


def test_the_file_mode_is_kept(file):
    file.chmod(0o600)
    run(file, lambda d: d.update(effortLevel="low"))
    assert file.stat().st_mode & 0o777 == 0o600


def test_nothing_is_written_when_the_cli_never_wrote(file):
    before = file.stat().st_mtime_ns
    snap = settings_guard.snapshot(file)
    clock = Clock()
    out = settings_guard.restore(snap, sleep=clock.sleep, clock=clock)
    assert out == {"restored": [], "changed": False}
    assert file.stat().st_mtime_ns == before
    assert clock.now >= settings_guard.WRITE_WAIT_S


def test_a_write_that_changed_nothing_it_guards_is_left_alone(file):
    out = run(file, lambda d: d.update(theme="light"))
    assert out["changed"] is True and out["restored"] == []
    assert json.loads(file.read_text())["theme"] == "light"


def test_a_write_that_lands_after_the_first_restore_is_put_back_too(file):
    snap = settings_guard.snapshot(file)
    cli_write(file, lambda d: d.update(effortLevel="low"))
    clock = Clock()
    late = []

    def sleep(s):
        clock.sleep(s)
        if len(late) == 0 and s == settings_guard.RECHECK_S:
            late.append(1)
            cli_write(file, lambda d: d.update(model="sonnet"))

    out = settings_guard.restore(snap, sleep=sleep, clock=clock)
    assert out["restored"] == ["effortLevel", "model"]
    doc = json.loads(file.read_text())
    assert doc["effortLevel"] == "medium" and doc["model"] == "opus"


@pytest.mark.parametrize("content", [None, "{not json", "[1, 2]"])
def test_a_missing_or_unreadable_file_is_not_guarded_and_not_touched(tmp_path, content):
    path = tmp_path / "settings.json"
    if content is not None:
        path.write_text(content)
    assert settings_guard.snapshot(path) is None
    assert settings_guard.restore(None) == {"restored": [], "changed": False}
    assert (path.read_text() if content is not None else None) == content


def test_a_file_that_became_unreadable_is_left_alone(file):
    snap = settings_guard.snapshot(file)
    file.write_text("{half a write")
    clock = Clock()
    out = settings_guard.restore(snap, sleep=clock.sleep, clock=clock)
    assert out["restored"] == [] and file.read_text() == "{half a write"


def test_the_snapshot_is_a_copy_the_later_write_cannot_change(file):
    snap = settings_guard.snapshot(file)
    cli_write(file, lambda d: d["modelSettings"]["claude-opus-5-5"].update(effortLevel="low"))
    assert snap.keys["modelSettings"]["claude-opus-5-5"]["effortLevel"] == "high"


# --- termscreen --------------------------------------------------------------


def test_text_and_line_breaks_render_as_rows():
    assert termscreen.render("one\r\ntwo\r\n") == "one\ntwo"


def test_colour_and_title_sequences_are_dropped():
    assert termscreen.render("\x1b[1;31mred\x1b[0m\x1b]0;title\x07 ok") == "red ok"


def test_erasing_lines_upward_removes_what_was_painted_there():
    painted = "keep\r\nold one\r\nold two"
    erased = "\x1b[2K\x1b[1A\x1b[2K\x1b[G"
    assert termscreen.render(painted + erased + "new") == "keep\nnew"


def test_an_absolute_repaint_overwrites_and_erase_in_line_clears_the_tail():
    stream = "\x1b[1;1Hhello world\x1b[1;6H\x1b[K\x1b[2;3Hbelow"
    assert termscreen.render(stream) == "hello\n  below"


def test_erase_in_display_clears_below_the_cursor():
    stream = "a\r\nb\r\nc\x1b[2;1H\x1b[J"
    assert termscreen.render(stream) == "a"


def test_switching_screens_starts_a_blank_grid():
    assert termscreen.render("old\x1b[?1049hnew") == "new"


def test_saved_cursor_comes_back():
    assert termscreen.render("ab\x1b7\r\nxy\x1b8Z") == "abZ\nxy"


def test_a_screen_with_no_escapes_is_text():
    from awm.reflection import guards
    assert guards.current_screen("plain\ntext") == "plain\ntext"
