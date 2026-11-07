"""Readable text from Slack ``blocks`` and ``attachments``.

Slack sets ``text`` to "This message contains interactive elements." when a
message carries a block it cannot flatten, so the content lives in ``blocks``.
"""

from __future__ import annotations

import pytest

from awm.social.connectors.slack_blocks import slack_text

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

PLACEHOLDER = "This message contains interactive elements."


def rich(*elements):
    return {"type": "rich_text", "elements": list(elements)}


def section(*runs):
    return {"type": "rich_text_section", "elements": list(runs)}


def txt(s):
    return {"type": "text", "text": s}


def test_real_text_wins_over_blocks():
    m = {"text": "hello *there*", "blocks": [rich(section(txt("hello there")))]}
    assert slack_text(m) == "hello *there*"


def test_no_blocks_no_attachments_is_the_text():
    assert slack_text({"text": "plain"}) == "plain"
    assert slack_text({}) == ""


def test_placeholder_is_replaced_by_rich_text():
    m = {"text": PLACEHOLDER, "blocks": [rich(section(
        txt("see "), {"type": "link", "url": "https://x.org/a", "text": "the doc"},
        txt(" cc "), {"type": "user", "user_id": "U1"},
        txt(" in "), {"type": "channel", "channel_id": "C1"},
        txt(" "), {"type": "emoji", "name": "tada"},
        txt(" "), {"type": "broadcast", "range": "here"}))]}
    assert slack_text(m) == "see the doc (https://x.org/a) cc <@U1> in <#C1> :tada: @here"


def test_a_bare_link_is_its_url():
    m = {"text": "", "blocks": [rich(section({"type": "link", "url": "https://x.org"}))]}
    assert slack_text(m) == "https://x.org"


def test_lists_preformatted_and_quotes():
    m = {"text": PLACEHOLDER, "blocks": [rich(
        section(txt("steps:")),
        {"type": "rich_text_list", "style": "ordered", "elements": [
            section(txt("one")), section(txt("two"))]},
        {"type": "rich_text_list", "style": "bullet", "indent": 1, "elements": [
            section(txt("nested"))]},
        {"type": "rich_text_preformatted", "elements": [txt("a,b\n1,2")]},
        {"type": "rich_text_quote", "elements": [txt("q1\nq2")]},
    )]}
    assert slack_text(m) == "\n".join([
        "steps:", "1. one", "2. two", "  • nested", "```\na,b\n1,2\n```", "> q1", "> q2"])


def test_table_block_cells_raw_and_rich():
    table = {"type": "table", "rows": [
        [{"type": "raw_text", "text": "name"}, {"type": "raw_text", "text": "n"}],
        [rich(section(txt("ab"))), {"type": "raw_text", "text": "3"}],
    ]}
    assert slack_text({"text": PLACEHOLDER, "blocks": [table]}) == "name | n\nab | 3"


def test_table_is_appended_to_real_text():
    table = {"type": "table", "rows": [[{"type": "raw_text", "text": "k"}]]}
    assert slack_text({"text": "results:", "blocks": [table]}) == "results:\nk"


def test_section_header_context_and_markdown_blocks():
    m = {"text": "", "blocks": [
        {"type": "header", "text": {"type": "plain_text", "text": "Report"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*body*"},
         "fields": [{"type": "mrkdwn", "text": "a"}, {"type": "mrkdwn", "text": "b"}]},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": "note"}, {"type": "image", "alt_text": "icon"}]},
        {"type": "divider"},
        {"type": "actions", "elements": [{"type": "button"}]},
        {"type": "markdown", "text": "# md"},
    ]}
    assert slack_text(m) == "Report\n*body*\na\nb\nnote icon\n# md"


def test_attachments_text_then_fallback():
    m = {"text": "", "attachments": [
        {"pretext": "pre", "title": "T", "text": "body",
         "fields": [{"title": "k", "value": "v"}], "fallback": "ignored"},
        {"fallback": "only the fallback"}]}
    assert slack_text(m) == "pre\nT\nbody\nk: v\nonly the fallback"


def test_a_placeholder_with_nothing_to_show_stays_the_placeholder():
    assert slack_text({"text": PLACEHOLDER, "blocks": [{"type": "divider"}]}) == PLACEHOLDER


async def test_the_bot_connector_ingests_block_text(awm_workspace):
    from awm.social.connectors.base import Account
    from awm.social.connectors.slack_bot_conn import SlackBotConnector

    async def on_message(m):
        pass

    conn = SlackBotConnector(
        Account(name="bot", platform="slack", token="xoxb-1"), on_message)

    async def user_name(_):
        return "Kalen"
    conn._user_name = user_name
    m = {"ts": "1.1", "user": "U2", "text": PLACEHOLDER,
         "blocks": [rich({"type": "rich_text_preformatted", "elements": [txt("a,b")]})]}
    got = await conn._inbound("D1", m)
    assert got.text == "```\na,b\n```"
