"""Tests for the Gmail connector's IMAP argument handling.

``imaplib`` does no quoting at all — it concatenates command arguments verbatim
— so selecting ``[Gmail]/All Mail`` unquoted put a malformed ``EXAMINE`` on the
wire and every ``search`` / ``download_attachments`` on the account failed with
``EXAMINE command error: BAD``. The fallback beside it could not help, because
a ``BAD`` reply **raises** rather than returning a non-OK status.

Both halves are tested here: the value reaches the server quoted, and the
fallback is reachable at all.
"""

from __future__ import annotations

import imaplib

import pytest

from awm.social.connectors import gmail_conn


class _FakeIMAP:
    """Records selects; optionally raises for a mailbox, as a real BAD would."""

    def __init__(self, bad: set[str] | None = None):
        self.selected: list[tuple[str, bool]] = []
        self._bad = bad or set()

    def select(self, mailbox="INBOX", readonly=False):
        self.selected.append((mailbox, readonly))
        if mailbox in self._bad:
            raise imaplib.IMAP4.error(
                f"EXAMINE command error: BAD [b'Could not parse command']")
        return "OK", [b"1"]


@pytest.mark.parametrize("value,want", [
    ("[Gmail]/All Mail", '"[Gmail]/All Mail"'),
    ("INBOX", '"INBOX"'),
    ('has:attachment from:"a b"', '"has:attachment from:\\"a b\\""'),
    ("back\\slash", '"back\\\\slash"'),
])
def test_imap_quote(value, want):
    assert gmail_conn._imap_quote(value) == want


def test_all_mail_is_selected_quoted_and_readonly():
    m = _FakeIMAP()
    assert gmail_conn._select_mailbox(m, gmail_conn.ALL_MAIL) == gmail_conn.ALL_MAIL
    assert m.selected == [('"[Gmail]/All Mail"', True)]


def test_a_bad_reply_falls_back_to_inbox(caplog):
    """The fallback must be an ``except``: a BAD reply raises, it does not
    return a status, so the old ``if typ != "OK"`` guard was dead code."""
    m = _FakeIMAP(bad={'"[Gmail]/All Mail"'})
    with caplog.at_level("WARNING"):
        assert gmail_conn._select_mailbox(m, gmail_conn.ALL_MAIL) == "INBOX"
    assert [s[0] for s in m.selected] == ['"[Gmail]/All Mail"', "INBOX"]
    # Narrowing the scope loses archived mail — it must not be silent.
    assert "INBOX" in caplog.text
