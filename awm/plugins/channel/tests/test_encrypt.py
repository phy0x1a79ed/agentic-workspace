"""A live room moves to encryption and stays open (R73, R110 item 4). The guest
side is written from the how-to's ENCRYPTION section alone (tests/guest.py)."""

from __future__ import annotations

import re

import pytest

from awm.channel import crypto
from awm.channel.service import ChannelError

from .conftest import HOLDER, eventually, needs_relay
from .guest import Guest

pytestmark = [pytest.mark.integration, needs_relay]


async def _offer(guest: Guest) -> str:
    while True:
        for m in (await guest.hear())["messages"]:
            if "spake2" in m:
                return m["spake2"]


async def _encrypted(channels, pushed, guest: Guest, opened) -> None:
    offer = await channels.encrypt(HOLDER)
    await guest.answer_offer(await _offer(guest), offer["password"])
    await eventually(lambda: any("is encrypted now" in t for t in pushed.texts()))


async def test_a_live_room_moves_to_encryption_and_carries_on(channels, pushed):
    opened = await channels.open(HOLDER, "room")
    guest = Guest(opened["link"])
    try:
        await guest.say("before")
        await eventually(lambda: pushed.texts())

        offer = await channels.encrypt(HOLDER)
        assert re.fullmatch(r"[a-z2-9]{4}-[a-z2-9]{4}-[a-z2-9]{4}", offer["password"])
        await guest.answer_offer(await _offer(guest), offer["password"])
        await eventually(lambda: any("is encrypted now" in t for t in pushed.texts()))
        assert any("answered the encryption offer" in t for t in pushed.texts())
        # "ready" is the handshake, not something the other side said.
        assert not any(t.endswith(": ready") for t in pushed.texts())

        assert (await channels.write(HOLDER, "the secret plan"))["sealed"]
        heard = (await guest.hear())["messages"]
        assert "text" not in heard[-1], "the relay must only ever see the sealed form"
        assert guest.unseal(heard[-1]) == "the secret plan"

        await guest.say_sealed("sealed hello")
        await eventually(lambda: any(t.endswith("from visitor: sealed hello")
                                     for t in pushed.texts()))

        [row] = (await channels.status(HOLDER))["channels"]
        assert row["open"] and row["encrypted"]
        transcript = channels.transcripts.path(opened["name"]).read_text()
        assert "the secret plan" in transcript and "sealed hello" in transcript
    finally:
        await guest.aclose()


async def test_plaintext_after_the_escalation_is_refused(channels, pushed):
    opened = await channels.open(HOLDER, "room")
    guest = Guest(opened["link"])
    try:
        await _encrypted(channels, pushed, guest, opened)
        await guest.say("a plaintext slip")
        await eventually(lambda: any("refused a plaintext message" in t for t in pushed.texts()))
        assert not any(t.endswith(": a plaintext slip") for t in pushed.texts())
    finally:
        await guest.aclose()


async def test_a_replayed_sealed_message_is_not_taken_twice(channels, pushed):
    opened = await channels.open(HOLDER, "room")
    guest = Guest(opened["link"])
    try:
        await _encrypted(channels, pushed, guest, opened)
        await guest.say_sealed("once")
        guest.n -= 1
        await guest.say_sealed("once")
        await guest.say_sealed("after")
        await eventually(lambda: any(t.endswith(": after") for t in pushed.texts()))
        assert sum(t.endswith(": once") for t in pushed.texts()) == 1
    finally:
        await guest.aclose()


async def test_a_wrong_password_is_reported_and_a_new_offer_works(channels, pushed):
    opened = await channels.open(HOLDER, "room")
    guest = Guest(opened["link"])
    try:
        await channels.encrypt(HOLDER)
        await guest.answer_offer(await _offer(guest), "not-the-password")
        await eventually(lambda: any("did not open" in t for t in pushed.texts()))

        await _encrypted(channels, pushed, guest, opened)
        await guest.say_sealed("second try")
        await eventually(lambda: any(t.endswith(": second try") for t in pushed.texts()))
    finally:
        await guest.aclose()


async def test_an_encrypted_room_does_not_offer_again(channels, pushed):
    opened = await channels.open(HOLDER, "room")
    guest = Guest(opened["link"])
    try:
        await _encrypted(channels, pushed, guest, opened)
        with pytest.raises(ChannelError, match="encrypted already"):
            await channels.encrypt(HOLDER)
    finally:
        await guest.aclose()


def test_a_sealed_message_opens_only_with_its_key_count_and_room():
    k = "11" * 32
    sealed = crypto.seal(k, 3, "acre-anchor-kettle", "hi")
    assert crypto.open_(k, 3, "acre-anchor-kettle", sealed) == "hi"
    for args in ((k, 4, "acre-anchor-kettle"), (k, 3, "acre-anchor-other"),
                 ("22" * 32, 3, "acre-anchor-kettle")):
        with pytest.raises(crypto.CryptoError):
            crypto.open_(*args, sealed)


async def test_the_how_to_states_every_constant_the_guest_and_our_side_use(channels):
    """The guest in tests/guest.py is written from the how-to, and crypto.py must
    agree with it. A change on either side shows here."""
    opened = await channels.open(HOLDER, "room")
    guest = Guest(opened["link"])
    try:
        text = await guest.how_to()
    finally:
        await guest.aclose()
    name = opened["name"]
    for needle in (
        f'"tether-room:{name}"',
        crypto.HOST_TO_GUEST.decode(), crypto.GUEST_TO_HOST.decode(),
        f'salt "{name}"', f'associated data is "{name}"',
        "4 zero bytes, then", "n as 8 bytes big-endian", "Count n from 0",
        'text "ready"', "spake2 0.4", "SPAKE2_Symmetric", "HKDF-SHA256",
        "ChaCha20-Poly1305", "standard alphabet with padding",
        "KEYS FOR NEXT TIME",
    ):
        assert needle in text, needle
