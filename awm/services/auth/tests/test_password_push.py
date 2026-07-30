"""What the daily password push says, and that it never breaks the mint.

Three nodes now push into one Discord ``#notifications`` channel every ~12h, so a
message that does not name its node is unusable. The link is the second half: the
password is a tap, not a transcription job.

The invariant these guard hardest is the older one — the push is best-effort and
must never raise into rotation. That is why the node-name and edge-URL lookups
live *inside* the try.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

PASSWORD = "correct horse/battery+staple"


@pytest.fixture()
def pushed(monkeypatch):
    """Capture the Discord message the push hands to social, and stub settings."""
    from awm.auth import service

    sent: list[dict] = []

    class _S:
        push_enabled = True
        validity_hours = 24.0
        discord_account = "discord-bot"
        discord_channel = "1522674357762261112"

    monkeypatch.setattr(service, "_settings", lambda: _S())

    import awm.gatewayclient as gc

    async def _call(peer, svc, fn, args=None, **kw):
        sent.append({"peer": peer, "service": svc, "fn": fn, "args": args})
        return {"ok": True}

    monkeypatch.setattr(gc, "call_maybe_peer", _call)
    monkeypatch.setattr(gc, "peer_env", lambda var: None)
    return sent


@pytest.fixture(autouse=True)
def node_env(monkeypatch):
    monkeypatch.setenv("AWM_NODE_NAME", "altair")
    monkeypatch.setenv("AWM_EDGE_URL", "https://10.74.81.213:12100")


async def test_the_message_names_the_node(pushed):
    from awm.auth import service
    await service._push_password_to_discord(PASSWORD, 0.0)
    text = pushed[0]["args"]["text"]
    assert "altair" in text
    assert PASSWORD in text


async def test_the_message_carries_an_autologin_link(pushed):
    from awm.auth import service
    await service._push_password_to_discord(PASSWORD, 0.0)
    text = pushed[0]["args"]["text"]
    assert "https://10.74.81.213:12100/__auth/link?p=" in text


async def test_the_link_percent_encodes_the_password(pushed):
    """Defensive, not currently load-bearing: `mint_generation` uses
    `secrets.token_urlsafe`, whose alphabet is entirely unreserved, so today the
    encoding is a no-op and the real password appears verbatim in both the code
    block and the URL. This pins the encoding anyway, against a future mint that
    widens the alphabet — a `/` or `&` in a password would otherwise truncate the
    query and the autologin would fail in a way nobody could read from Discord."""
    from awm.auth import service
    await service._push_password_to_discord(PASSWORD, 0.0)
    text = pushed[0]["args"]["text"]
    assert "correct%20horse%2Fbattery%2Bstaple" in text


async def test_a_urlsafe_password_needs_no_encoding(pushed):
    """The shape real passwords actually have — token_urlsafe output."""
    from awm.auth import service
    await service._push_password_to_discord("Ab3-_xYz90qW", 0.0)
    text = pushed[0]["args"]["text"]
    assert "/__auth/link?p=Ab3-_xYz90qW>" in text


async def test_the_link_is_bracketed_so_discord_will_not_unfurl_it(pushed):
    from awm.auth import service
    await service._push_password_to_discord(PASSWORD, 0.0)
    text = pushed[0]["args"]["text"]
    assert "<https://10.74.81.213:12100/__auth/link?p=" in text
    assert ">" in text.split("<https://", 1)[1]


async def test_the_message_still_works_with_no_edge_url(pushed, monkeypatch):
    """A node whose edge address cannot be determined omits the link, not the
    password."""
    from awm.auth import service
    monkeypatch.delenv("AWM_EDGE_URL")
    monkeypatch.setattr("awm.config.mesh_address", lambda: None)
    await service._push_password_to_discord(PASSWORD, 0.0)
    text = pushed[0]["args"]["text"]
    assert PASSWORD in text
    assert "altair" in text
    assert "__auth/link" not in text


async def test_push_disabled_sends_nothing(pushed, monkeypatch):
    from awm.auth import service

    class _Off:
        push_enabled = False

    monkeypatch.setattr(service, "_settings", lambda: _Off())
    await service._push_password_to_discord(PASSWORD, 0.0)
    assert pushed == []


async def test_a_social_outage_does_not_raise_into_the_mint(pushed, monkeypatch):
    from awm.auth import service
    import awm.gatewayclient as gc

    async def _boom(*a, **k):
        raise RuntimeError("social is down")

    monkeypatch.setattr(gc, "call_maybe_peer", _boom)
    await service._push_password_to_discord(PASSWORD, 0.0)   # must not raise


async def test_a_broken_node_lookup_does_not_raise_into_the_mint(
        pushed, monkeypatch):
    """The reason the lookups are inside the try and not above it."""
    from awm.auth import service

    def _boom():
        raise OSError("hostname unavailable")

    monkeypatch.setattr("awm.config.node_name", _boom)
    await service._push_password_to_discord(PASSWORD, 0.0)   # must not raise
    assert pushed == []


# ---------------------------------------------------------------------------
# _autologin_link in isolation
# ---------------------------------------------------------------------------

def test_autologin_link_is_none_without_an_edge_url(monkeypatch):
    from awm.auth import service
    monkeypatch.delenv("AWM_EDGE_URL")
    monkeypatch.setattr("awm.config.mesh_address", lambda: None)
    assert service._autologin_link("x") is None


def test_autologin_link_shape(monkeypatch):
    from awm.auth import service
    monkeypatch.setenv("AWM_EDGE_URL", "https://mira:12100")
    assert service._autologin_link("abc") == \
        "https://mira:12100/__auth/link?p=abc"
