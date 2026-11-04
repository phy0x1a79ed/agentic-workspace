"""An outside agent that knows only a room's link, and reads the rest from it."""

from __future__ import annotations

import base64

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from spake2 import SPAKE2_Symmetric


class Guest:
    def __init__(self, link: str, name: str = "visitor") -> None:
        self.link = link
        self.name = name
        self.after = 0
        self.http = httpx.AsyncClient(timeout=30)

    async def how_to(self) -> str:
        resp = await self.http.get(self.link)
        resp.raise_for_status()
        return resp.text

    async def post(self, **fields) -> httpx.Response:
        return await self.http.post(f"{self.link}/send", json={"from": self.name, **fields})

    async def say(self, text: str) -> int:
        resp = await self.post(text=text)
        resp.raise_for_status()
        return resp.json()["seq"]

    async def hear(self, wait: int = 5) -> dict:
        """The host's next messages, or the end of the room. Skips my own."""
        while True:
            resp = await self.http.get(f"{self.link}/recv",
                                       params={"after": self.after, "wait": wait})
            resp.raise_for_status()
            body = resp.json()
            self.after = body["next"]
            body["messages"] = [m for m in body["messages"] if m["by"] == "host"]
            if body["messages"] or body["ended"] or not self.after:
                return body
            if not body["messages"] and resp.elapsed.total_seconds() >= wait:
                return body

    # -- encryption, written from the how-to's ENCRYPTION section alone --------

    @property
    def room(self) -> str:
        return self.link.rstrip("/").rsplit("/", 1)[1]

    async def answer_offer(self, offer_b64: str, password: str) -> None:
        """Steps 1 to 4, then step 6's "ready"."""
        s = SPAKE2_Symmetric(password.encode(), idSymmetric=f"tether-room:{self.room}".encode())
        mine = s.start()
        await self.post(spake2=base64.b64encode(mine).decode())
        k = s.finish(base64.b64decode(offer_b64))

        def key(info: str) -> bytes:
            return HKDF(hashes.SHA256(), 32, self.room.encode(), info.encode()).derive(k)

        self.host_key = key("tether-room host to guest")
        self.my_key = key("tether-room guest to host")
        self.n = 0
        await self.say_sealed("ready")

    async def say_sealed(self, text: str) -> int:
        nonce = b"\0\0\0\0" + self.n.to_bytes(8, "big")
        sealed = ChaCha20Poly1305(self.my_key).encrypt(nonce, text.encode(), self.room.encode())
        resp = await self.post(n=self.n, sealed=base64.b64encode(sealed).decode())
        self.n += 1
        resp.raise_for_status()
        return resp.json()["seq"]

    def unseal(self, message: dict) -> str:
        nonce = b"\0\0\0\0" + message["n"].to_bytes(8, "big")
        return ChaCha20Poly1305(self.host_key).decrypt(
            nonce, base64.b64decode(message["sealed"]), self.room.encode()).decode()

    async def leave(self) -> None:
        (await self.http.post(f"{self.link}/leave")).raise_for_status()

    async def aclose(self) -> None:
        await self.http.aclose()
