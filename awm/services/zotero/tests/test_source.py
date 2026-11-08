"""Reaching the library, and what the library is called while we do.

Almost all of this module is one HTTPS request, so what is worth testing is the
naming: the mirror writes a library's name onto 823 notes, and a name that
changes is 823 notes that stop being found.
"""

from __future__ import annotations

import json

import pytest

from awm.zotero import source

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def test_the_personal_library_keeps_the_name_the_vault_already_carries(monkeypatch):
    """The migration this exists for.

    The desktop's copy of the interface numbers the signed-in user 0, and every
    note the mirror has written carries `users/0/<key>`. The published interface
    numbers the account for real. Letting that number reach the bundle would
    change the identity of every paper at once: the pass would find no note for
    any reference, build a second copy of the whole library, and then delete the
    first for being absent.
    """
    monkeypatch.setattr(source, "USER", "5331043")
    assert source.personal() == "users/0"
    assert [lib["id"] for lib in [{"id": source.personal()}]] == ["users/0"]


# -- reading a window, and refusing a read that shifted under itself ---------


@pytest.fixture(autouse=False)
def _known_account(monkeypatch):
    """The account number, so a read does not spend a request discovering it."""
    monkeypatch.setattr(source, "USER", "5331043")


class _Fake:
    """Enough of `_get` to drive the pager. Pages are (records, version)."""

    def __init__(self, pages):
        self.pages = pages
        self.asked: list[dict] = []

    def __call__(self, path, params=None):
        self.asked.append(dict(params or {}))
        records, version = self.pages[min(len(self.asked) - 1,
                                          len(self.pages) - 1)]
        total = sum(len(r) for r, _ in self.pages)
        return source.Response(
            status=200,
            headers={"last-modified-version": str(version),
                     "total-results": str(total)},
            body=json.dumps(records).encode("utf-8"))


def test_no_cursor_is_a_decision_rather_than_a_falsy_value(monkeypatch, _known_account):
    """A version of zero is falsy, so a truthiness test drops it and reads the
    library whole. That is the right answer for the wrong reason."""
    fake = _Fake([([], 1)])
    monkeypatch.setattr(source, "_get", fake)

    source.items("users/0", since=None)
    assert "since" not in fake.asked[0], "a whole read must send no cursor"

    fake.asked.clear()
    source.items("users/0", since=0)
    assert fake.asked[0]["since"] == 0, "a cursor of zero must still be sent"


class _TwoPages:
    """A two-page library that reports a version per call.

    `versions` is read in call order, so a page whose version differs from the
    first page of its own walk is a library that moved under the walk.
    """

    def __init__(self, versions):
        self.versions = list(versions)
        self.calls = 0

    def __call__(self, path, params=None):
        version = self.versions[min(self.calls, len(self.versions) - 1)]
        self.calls += 1
        records = ([{"key": f"K{i:03d}"} for i in range(source.PAGE)]
                   if params["start"] == 0 else [{"key": "TAIL"}])
        return source.Response(
            status=200,
            headers={"last-modified-version": str(version),
                     "total-results": str(source.PAGE + 1)},
            body=json.dumps(records).encode("utf-8"))


def test_a_library_edited_mid_walk_is_read_again(monkeypatch, _known_account):
    """The walk pages by offset over a list ordered by modification date, so an
    item edited part-way through jumps to the front and pushes the item on the
    page boundary out of the window. Absence from a whole read retires a paper,
    so that would delete somebody's note."""
    # Walk one shears at its second page; walk two is steady.
    fake = _TwoPages([5, 9, 9, 9])
    monkeypatch.setattr(source, "_get", fake)

    got = source.items("users/0")

    assert fake.calls == 4, "the sheared walk was not restarted"
    assert got.version == 9
    assert [r["key"] for r in got.records][-1] == "TAIL"
