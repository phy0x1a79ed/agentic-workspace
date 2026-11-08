"""Write test_vectors.json: fixed grants and orders that every verify_grant port must agree on.

Run ``python make_vectors.py`` from this directory. Ed25519 is deterministic, so the file
is stable. Each case gives the envelope, the check to run, ``now``, and ``ok`` or the
error it must raise.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from verify_grant import b64u, canon, key_id

SEED = hashlib.sha256(b"awm-grantlib test vector key").digest()
KEY = Ed25519PrivateKey.from_private_bytes(SEED)
OTHER = Ed25519PrivateKey.from_private_bytes(hashlib.sha256(b"another key").digest())
T0 = 1_800_000_000


def env(field: str, body: dict, key: Ed25519PrivateKey = KEY) -> dict:
    return {field: body, "sig": b64u(key.sign(canon(body))), "key_id": key_id(KEY.public_key())}


def main() -> None:
    ssh = {"grant_id": "gTESTSSH01", "request_id": "qTESTREQ01", "kind": "admin_ssh",
           "node": None, "nodes": ["altair", "capella"], "subject": "cc-0001",
           "payload_sha256": hashlib.sha256(canon({"pubkey": "ssh-ed25519 AAAA test",
                                                   "nodes": ["capella", "altair"]})).hexdigest(),
           "not_before": T0, "not_after": T0 + 1800, "approved_by_device": "dTESTDEV1"}
    raise_ = {"grant_id": "gTESTTRU01", "request_id": "qTESTREQ02", "kind": "trust_raise",
              "node": "mizar", "subject": "cc-0002", "payload_sha256": "00" * 32,
              "not_before": T0, "not_after": T0 + 900, "approved_by_device": "dTESTDEV1"}
    order = {"order_id": "oTESTORD01", "kind": "zotero_sync", "node": "deneb", "payload": {},
             "not_after": T0 + 600}
    # Non-ASCII and escaped text in the signed body and the payload: every canon writer
    # must emit the same bytes for them.
    raise_payload = {"session_id": "sé1", "from": "cooperative", "to": "trusted",
                     "note": "tab\tquote\" ünï"}
    raise_text = dict(raise_, grant_id="gTESTTRU02", subject="sé1",
                      payload_sha256=hashlib.sha256(canon(raise_payload)).hexdigest())
    tampered = copy.deepcopy(env("grant", raise_))
    tampered["grant"]["subject"] = "cc-9999"
    floaty = dict(raise_, not_after=T0 + 900.5)
    floaty_unsigned = {**env("grant", floaty), "sig": "AAAA"}
    no_start = {k: v for k, v in raise_.items() if k != "not_before"}
    no_start["grant_id"] = "gTESTTRU03"
    ctrl_payload = {"session_id": "cc-0003", "from": "cooperative", "to": "trusted",
                    "note": "\x01 del\x7f sep\u2028 \\ /"}
    ctrl = dict(raise_, grant_id="gTESTTRU04", subject="cc-0003",
                payload_sha256=hashlib.sha256(canon(ctrl_payload)).hexdigest())
    cases = [
        {"name": "admin_ssh grant, node in nodes", "check": "grant", "kind": "admin_ssh",
         "node": "altair", "now": T0 + 60, "envelope": env("grant", ssh), "expect": "ok"},
        {"name": "admin_ssh grant, node not covered", "check": "grant", "node": "deneb",
         "now": T0 + 60, "envelope": env("grant", ssh), "expect": "does not cover"},
        {"name": "trust_raise grant", "check": "grant", "kind": "trust_raise", "node": "mizar",
         "now": T0 + 60, "envelope": env("grant", raise_), "expect": "ok"},
        {"name": "trust_raise grant, non-ASCII subject and payload", "check": "grant",
         "kind": "trust_raise", "node": "mizar", "subject": "sé1", "payload": raise_payload,
         "now": T0 + 60, "envelope": env("grant", raise_text), "expect": "ok"},
        {"name": "wrong kind", "check": "grant", "kind": "admin_ssh", "now": T0 + 60,
         "envelope": env("grant", raise_), "expect": "not 'admin_ssh'"},
        {"name": "one byte changed", "check": "grant", "now": T0 + 60, "envelope": tampered,
         "expect": "does not verify"},
        {"name": "signed by another key", "check": "grant", "now": T0 + 60,
         "envelope": env("grant", raise_, OTHER), "expect": "does not verify"},
        {"name": "key_id names another key", "check": "grant", "now": T0 + 60,
         "envelope": {**env("grant", raise_), "key_id": "0" * 16}, "expect": "another key"},
        {"name": "a float time", "check": "grant", "now": T0 + 60,
         "envelope": env("grant", floaty), "expect": "float"},
        {"name": "not valid yet", "check": "grant", "now": T0 - 1,
         "envelope": env("grant", raise_), "expect": "not valid yet"},
        {"name": "expired at not_after", "check": "grant", "now": T0 + 900,
         "envelope": env("grant", raise_), "expect": "expired"},
        {"name": "order for this node", "check": "order", "node": "deneb", "now": T0 + 10,
         "envelope": env("order", order), "expect": "ok"},
        {"name": "order for another node", "check": "order", "node": "altair", "now": T0 + 10,
         "envelope": env("order", order), "expect": "not 'altair'"},
        {"name": "no not_before means valid from 0", "check": "grant", "now": T0 + 60,
         "envelope": env("grant", no_start), "expect": "ok"},
        {"name": "an order checked as a grant", "check": "grant", "now": T0 + 10,
         "envelope": env("order", order), "expect": "no grant in the envelope"},
        {"name": "a float is named before the signature", "check": "grant", "now": T0 + 60,
         "envelope": floaty_unsigned, "expect": "not_after is a float, and signed times are integers"},
        {"name": "wrong subject", "check": "grant", "subject": "cc-9999", "now": T0 + 60,
         "envelope": env("grant", raise_), "expect": "the grant is for 'cc-0002', not 'cc-9999'"},
        {"name": "payload not covered", "check": "grant", "payload": {"session_id": "cc-0002"},
         "now": T0 + 60, "envelope": env("grant", raise_),
         "expect": "the grant does not cover this request"},
        {"name": "control characters in the payload", "check": "grant", "subject": "cc-0003",
         "payload": ctrl_payload, "now": T0 + 60, "envelope": env("grant", ctrl), "expect": "ok"},
        {"name": "order expired", "check": "order", "node": "deneb", "now": T0 + 600,
         "envelope": env("order", order), "expect": "expired"},
    ]
    out = {"public_key_b64u": b64u(KEY.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)),
           "key_id": key_id(KEY.public_key()), "cases": cases}
    Path(__file__).with_name("test_vectors.json").write_text(json.dumps(out, indent=1) + "\n")


if __name__ == "__main__":
    main()
