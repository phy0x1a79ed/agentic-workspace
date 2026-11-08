"""Write rsa_leaf.json: a synthetic RSA root and RSA leaf, shaped like the ones openssl cut.

The fleet's existing CA and leaves are RSA-2048, minted by the Python front with openssl.
rcgen on ring cannot generate RSA, so this script writes a committed pair for the Rust
tests: a node must still load an RSA leaf, sign with an RSA CA key, and not re-mint a
leaf whose SAN set already matches. The validity is a century so that the tests never
expire. The keys are test-only and protect nothing.

Run from the repo root, before the Python is deleted:
    mamba run -n awm python -I core/plugins/auth/tests/vectors/make_rsa_leaf.py
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import json
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

OUT = Path(__file__).resolve().parent / "rsa_leaf.json"
SANS = ["IP:127.0.0.1", "DNS:localhost", "IP:10.74.81.213"]
NOT_BEFORE = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
NOT_AFTER = dt.datetime(2126, 1, 1, tzinfo=dt.timezone.utc)


def pem_pkcs8(key) -> str:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


def pem_pkcs1(key) -> str:
    return key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.TraditionalOpenSSL,
                             serialization.NoEncryption()).decode()


def general_name(tok: str) -> x509.GeneralName:
    kind, val = tok.split(":", 1)
    if kind == "IP":
        return x509.IPAddress(ipaddress.ip_address(val))
    return x509.DNSName(val)


def main() -> None:
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "remote-audio local CA")])
    ca_ski = x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key())
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name).issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOT_BEFORE).not_valid_after(NOT_AFTER)
        .add_extension(ca_ski, critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ca_ski),
                       critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False,
                                     key_encipherment=False, data_encipherment=False,
                                     key_agreement=False, key_cert_sign=True, crl_sign=True,
                                     encipher_only=False, decipher_only=False), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "awm-https-front")]))
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOT_BEFORE).not_valid_after(NOT_AFTER)
        .add_extension(x509.SubjectAlternativeName([general_name(s) for s in SANS]),
                       critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                     key_encipherment=True, data_encipherment=False,
                                     key_agreement=False, key_cert_sign=False, crl_sign=False,
                                     encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()),
                       critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ca_ski),
                       critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    out = {
        "sans": SANS,
        "ca_pem": ca.public_bytes(serialization.Encoding.PEM).decode(),
        "ca_key_pkcs8": pem_pkcs8(ca_key),
        "ca_key_pkcs1": pem_pkcs1(ca_key),
        "leaf_pem": leaf.public_bytes(serialization.Encoding.PEM).decode(),
        "leaf_key_pkcs8": pem_pkcs8(leaf_key),
        "leaf_key_pkcs1": pem_pkcs1(leaf_key),
    }
    OUT.write_text(json.dumps(out, indent=1) + "\n")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
