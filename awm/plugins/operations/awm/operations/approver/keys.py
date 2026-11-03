"""alcor's own keys: the Ed25519 grant key, the P-256 ack key and the ssh user CA."""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.utils import (decode_dss_signature,
                                                             encode_dss_signature)
from verify_grant import b64u, canon, key_id, unb64u


def p1363_to_der(sig: bytes) -> bytes:
    """WebCrypto signs ECDSA as r||s. ``cryptography`` wants DER."""
    if len(sig) != 64:
        raise ValueError("a P-256 signature is 64 bytes")
    return encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))


def der_to_p1363(sig: bytes) -> bytes:
    r, s = decode_dss_signature(sig)
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def load_device_key(spki_b64u: str) -> ec.EllipticCurvePublicKey:
    key = serialization.load_der_public_key(unb64u(spki_b64u))
    if not isinstance(key, ec.EllipticCurvePublicKey) or key.curve.name != "secp256r1":
        raise ValueError("a device key is ECDSA P-256")
    return key


def device_sig_ok(key: ec.EllipticCurvePublicKey, data: bytes, sig_p1363: bytes) -> bool:
    try:
        key.verify(p1363_to_der(sig_p1363), data, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError):
        return False


def _utc(t: int) -> str:
    return time.strftime("%Y%m%d%H%M%SZ", time.gmtime(t))


def _private_file(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


@dataclass
class Keys:
    grant: Ed25519PrivateKey
    ack: ec.EllipticCurvePrivateKey
    ssh_ca: Path

    @classmethod
    def load_or_create(cls, d: Path) -> "Keys":
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        g, a, ca = d / "grant_ed25519.pem", d / "ack_p256.pem", d / "ssh_user_ca"
        pem = dict(encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.PKCS8,
                   encryption_algorithm=serialization.NoEncryption())
        if not g.exists():
            _private_file(g, Ed25519PrivateKey.generate().private_bytes(**pem))
        if not a.exists():
            _private_file(a, ec.generate_private_key(ec.SECP256R1()).private_bytes(**pem))
        if not ca.exists():
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "awm-admin-ca",
                            "-f", str(ca)], check=True)
        grant = serialization.load_pem_private_key(g.read_bytes(), None)
        ack = serialization.load_pem_private_key(a.read_bytes(), None)
        assert isinstance(grant, Ed25519PrivateKey) and isinstance(ack, ec.EllipticCurvePrivateKey)
        return cls(grant, ack, ca)

    # -- grants and orders (agreed with redux-trust) -------------------------------

    def grant_pub_b64u(self) -> str:
        return b64u(self.grant.public_key().public_bytes(serialization.Encoding.Raw,
                                                         serialization.PublicFormat.Raw))

    def envelope(self, field: str, body: dict[str, Any]) -> dict[str, Any]:
        return {field: body, "sig": b64u(self.grant.sign(canon(body))),
                "key_id": key_id(self.grant.public_key())}

    # -- acks (R65) ----------------------------------------------------------------

    def ack_pub_spki_b64u(self) -> str:
        return b64u(self.ack.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo))

    def sign_ack(self, body: dict[str, Any]) -> dict[str, Any]:
        """The ack the browser checks with WebCrypto: body plus a P1363 signature over its canonical JSON."""
        sig = self.ack.sign(canon(body), ec.ECDSA(hashes.SHA256()))
        return {**body, "sig": b64u(der_to_p1363(sig))}

    # -- ssh user certificates for admin_ssh grants ----------------------------------

    def ssh_ca_pub(self) -> str:
        return Path(f"{self.ssh_ca}.pub").read_text().strip()

    def ssh_cert(self, pubkey: str, principals: list[str], not_before: int, not_after: int,
                 key_id_: str) -> str:
        """Sign a user certificate with ``ssh-keygen -s``. Returns the ``*-cert.pub`` line."""
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "key.pub"
            p.write_text(pubkey.strip() + "\n")
            serial = int.from_bytes(hashlib.sha256(key_id_.encode()).digest()[:8], "big") >> 1
            subprocess.run(["ssh-keygen", "-q", "-s", str(self.ssh_ca), "-I", key_id_,
                            "-n", ",".join(principals), "-V", f"{_utc(not_before)}:{_utc(not_after)}",
                            "-z", str(serial), str(p)], check=True, capture_output=True)
            return (Path(tmp) / "key-cert.pub").read_text().strip()
