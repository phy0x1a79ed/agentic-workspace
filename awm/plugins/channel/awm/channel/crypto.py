"""A room's escalation to encryption, exactly as the relay's how-to tells a guest.

Symmetric SPAKE2 over Ed25519, the suite of Python ``spake2`` and Rust
``spake2`` 0.4, which interoperate. The identity binds the exchange to one
room. HKDF-SHA256 then gives one key per direction, and ChaCha20-Poly1305
seals each message with its count as the nonce. The how-to is the guest's
whole protocol, so every constant here must match its text in
``tether-relay/src/room.rs``.
"""

from __future__ import annotations

import base64
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from spake2 import SPAKE2_Symmetric

HOST_TO_GUEST = b"tether-room host to guest"
GUEST_TO_HOST = b"tether-room guest to host"

#: No 0, o, 1, l or i, so a password survives being read aloud.
_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


class CryptoError(Exception):
    """A sealed message did not open, or a SPAKE2 message was malformed."""


def new_password() -> str:
    """Twelve characters in three groups, about 59 bits."""
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(12))
    return "-".join(raw[i:i + 4] for i in range(0, 12, 4))


def _identity(room: str) -> bytes:
    return f"tether-room:{room}".encode()


def offer(room: str, password: str) -> tuple[str, str]:
    """Start SPAKE2: our message to post, and the state to finish it with later."""
    s = SPAKE2_Symmetric(password.encode(), idSymmetric=_identity(room))
    ours = s.start()
    return _b64(ours), _b64(s.serialize())


def finish(room: str, state: str, theirs: str) -> dict[str, str]:
    """Finish SPAKE2 with the guest's message: the host's two keys, as hex."""
    s = SPAKE2_Symmetric.from_serialized(_unb64(state))
    try:
        k = s.finish(_unb64(theirs))
    except Exception as exc:  # noqa: BLE001 — spake2 raises several kinds on bad input
        raise CryptoError(f"not a SPAKE2 message: {exc}") from exc
    return {"out": _derive(k, room, HOST_TO_GUEST).hex(),
            "in": _derive(k, room, GUEST_TO_HOST).hex()}


def _derive(k: bytes, room: str, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=room.encode(),
                info=info).derive(k)


def _nonce(n: int) -> bytes:
    return b"\0\0\0\0" + n.to_bytes(8, "big")


def seal(key: str, n: int, room: str, text: str) -> str:
    return _b64(ChaCha20Poly1305(bytes.fromhex(key)).encrypt(
        _nonce(n), text.encode(), room.encode()))


def open_(key: str, n: int, room: str, sealed: str) -> str:
    try:
        plain = ChaCha20Poly1305(bytes.fromhex(key)).decrypt(
            _nonce(n), _unb64(sealed), room.encode())
    except (InvalidTag, ValueError) as exc:
        raise CryptoError("a sealed message did not open") from exc
    return plain.decode("utf-8", errors="replace")


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _unb64(s: str) -> bytes:
    try:
        return base64.b64decode(s, validate=True)
    except (ValueError, TypeError) as exc:
        raise CryptoError("not base64") from exc
