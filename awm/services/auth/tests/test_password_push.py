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
        mint_cadence_hours = 12.0
        discord_account = "discord-bot"
        discord_channel = "1522674357762261112"
        # 1 attempt / 0 backoff: existing tests exercise a single send, same
        # as before retries existed. Tests of the retry behavior itself
        # override these via monkeypatch.setattr(service, "_settings", ...).
        push_retry_attempts = 1
        push_retry_backoff_seconds = 0.0

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


@pytest.fixture(autouse=True)
def reset_push_status():
    """_push_status is module-level state; start each test with it unknown."""
    from awm.auth import service
    service._push_status.update(ok=None, at=None, error=None)


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


# ---------------------------------------------------------------------------
# Retry behavior
# ---------------------------------------------------------------------------


class _RetrySettings:
    push_enabled = True
    validity_hours = 24.0
    discord_account = "discord-bot"
    discord_channel = "1522674357762261112"
    push_retry_attempts = 3
    push_retry_backoff_seconds = 0.0  # keep tests fast


async def test_a_transient_failure_is_retried_until_it_succeeds(monkeypatch):
    from awm.auth import service

    monkeypatch.setattr(service, "_settings", lambda: _RetrySettings())

    calls = 0

    async def _flaky(*a, **k):
        nonlocal calls
        calls += 1
        if calls < 2:
            raise RuntimeError("peer unreachable")
        return {"ok": True}

    import awm.gatewayclient as gc
    monkeypatch.setattr(gc, "call_maybe_peer", _flaky)
    monkeypatch.setattr(gc, "peer_env", lambda var: None)

    await service._push_password_to_discord(PASSWORD, 0.0)

    assert calls == 2
    assert service._push_status["ok"] is True
    assert service._push_status["error"] is None
    assert service._push_status["at"] is not None


async def test_a_persistent_failure_gives_up_after_max_attempts(monkeypatch):
    from awm.auth import service

    monkeypatch.setattr(service, "_settings", lambda: _RetrySettings())

    calls = 0

    async def _boom(*a, **k):
        nonlocal calls
        calls += 1
        raise RuntimeError("social is down")

    import awm.gatewayclient as gc
    monkeypatch.setattr(gc, "call_maybe_peer", _boom)
    monkeypatch.setattr(gc, "peer_env", lambda var: None)

    await service._push_password_to_discord(PASSWORD, 0.0)   # must not raise

    assert calls == _RetrySettings.push_retry_attempts
    assert service._push_status["ok"] is False
    assert "social is down" in service._push_status["error"]
    assert service._push_status["at"] is not None


async def test_h_status_reports_the_last_push_outcome(pushed, monkeypatch):
    from awm.auth import service, store

    # h_status also reads the credential store; stub it so this stays a unit
    # test of the push-status wiring, not a DB integration test.
    monkeypatch.setattr(store, "latest", lambda: None)
    monkeypatch.setattr(store, "valid_generations", lambda: [])

    await service._push_password_to_discord(PASSWORD, 0.0)

    status = service.h_status({})
    assert status["push_last_ok"] is True
    assert status["push_last_error"] is None
    assert status["push_last_attempt_at"] is not None
