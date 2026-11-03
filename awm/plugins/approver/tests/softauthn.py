"""A software passkey and a WebCrypto-like device key, for tests only."""

from __future__ import annotations

import hashlib
import json
import os

import cbor2
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from awm.approver.keys import der_to_p1363
from verify_grant import b64u, canon, unb64u


class SoftPasskey:
    def __init__(self, rp_id: str, origin: str) -> None:
        self.rp_id, self.origin = rp_id, origin
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = os.urandom(16)
        self.count = 0

    def _cdj(self, kind: str, challenge: bytes) -> bytes:
        return json.dumps({"type": kind, "challenge": b64u(challenge), "origin": self.origin,
                           "crossOrigin": False}).encode()

    def create(self, options: dict) -> dict:
        nums = self.key.public_key().public_numbers()
        cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"),
                            -3: nums.y.to_bytes(32, "big")})
        auth = (hashlib.sha256(self.rp_id.encode()).digest() + bytes([0x45]) +
                self.count.to_bytes(4, "big") + bytes(16) + len(self.cred_id).to_bytes(2, "big") +
                self.cred_id + cose)
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth})
        cdj = self._cdj("webauthn.create", unb64u(options["challenge"]))
        return {"id": b64u(self.cred_id), "rawId": b64u(self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64u(cdj), "attestationObject": b64u(att)}}

    def get(self, challenge: bytes) -> dict:
        self.count += 1
        auth = (hashlib.sha256(self.rp_id.encode()).digest() + bytes([0x05]) +
                self.count.to_bytes(4, "big"))
        cdj = self._cdj("webauthn.get", challenge)
        sig = self.key.sign(auth + hashlib.sha256(cdj).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": b64u(self.cred_id), "rawId": b64u(self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": b64u(cdj), "authenticatorData": b64u(auth),
                             "signature": b64u(sig)}}


class SoftDevice:
    """What the panel keeps: a P-256 device key, the acked head and its sequence."""

    def __init__(self) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.pub = b64u(self.key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo))
        self.id = ""
        self.head = ""
        self.seq = 0

    def sign(self, obj) -> str:
        return b64u(der_to_p1363(self.key.sign(canon(obj), ec.ECDSA(hashes.SHA256()))))

    def message(self, action: str, args: dict, ts: int, *, prev: str | None = None,
                seq: int | None = None) -> tuple[dict, str]:
        m = {"device": self.id, "seq": self.seq + 1 if seq is None else seq,
             "prev": self.head if prev is None else prev, "action": action, "args": args, "ts": ts}
        return m, self.sign(m)

    def take(self, ack: dict) -> None:
        self.head, self.seq = ack["head"], ack["seq"]
