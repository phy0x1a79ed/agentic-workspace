"""The device chain of R65 item 4, as pure functions.

A message is canonical JSON ``{device, seq, prev, action, args, ts}``, signed by the device
key. The new head is ``sha256(prev || sha256(message) || signature)``. A passkey request
binds its challenge to the head it extends: ``sha256(prev || sha256(canonical request))``.
"""

from __future__ import annotations

import hashlib
import secrets

from verify_grant import canon

MESSAGE_FIELDS = {"device", "seq", "prev", "action", "args", "ts"}


def next_head(prev_hex: str, message: dict, sig: bytes) -> str:
    h = hashlib.sha256()
    h.update(bytes.fromhex(prev_hex))
    h.update(hashlib.sha256(canon(message)).digest())
    h.update(sig)
    return h.hexdigest()


def passkey_challenge(prev_hex: str, request: dict) -> bytes:
    return hashlib.sha256(bytes.fromhex(prev_hex) + hashlib.sha256(canon(request)).digest()).digest()


def unlock_challenge(device: str, nonce_hex: str) -> bytes:
    return hashlib.sha256(b"unlock\0" + device.encode() + b"\0" + bytes.fromhex(nonce_hex)).digest()


def fresh_head() -> str:
    return secrets.token_hex(32)
