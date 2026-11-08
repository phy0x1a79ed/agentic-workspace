"""Write portal.json: the Python edge's portal helpers and grant check on synthetic inputs.

The signing key is derived from a fixed test secret and every time is fixed, so the file is
the same on every run. Run from anywhere with `mamba run -n awm python -I make_portal.py`; it
reads the Python from this worktree, not from an installed copy.
"""

import base64
import hashlib
import json
import os
import pathlib
import sys

WS = pathlib.Path(__file__).resolve().parents[5]
sys.path[:0] = [str(WS / "awm/services/httpsfront"), str(WS / "awm/service_components/tlsfront"),
                str(WS / "awm/service_components/config"), str(WS / "awm/service_components/grantlib"),
                str(WS / "awm/service_components/persistence")]

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat  # noqa: E402
from verify_grant import GrantInvalid, canon, key_id, payload_digest  # noqa: E402

from awm.tlsfront import portal  # noqa: E402

assert portal.__file__.startswith(str(WS)), portal.__file__

NOW = 1_800_000_000
NODE = "altair"
HOST = "node.example.test"
SEED = hashlib.sha256(b"awm-test-secret-0001").digest()
OTHER_SEED = hashlib.sha256(b"awm-test-secret-0002").digest()
KEY = Ed25519PrivateKey.from_private_bytes(SEED)
OTHER = Ed25519PrivateKey.from_private_bytes(OTHER_SEED)


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


PUB = b64u(KEY.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
os.environ["AWM_NODE_NAME"] = NODE
os.environ[portal.GRANT_KEY_ENV] = PUB


def body(*, node=NODE, host=HOST, path="/trilium/", kind="portal_login", nb=NOW, na=NOW + 60,
         grant_id="g-0001", subject="operator", **extra):
    out = {"grant_id": grant_id, "request_id": "", "kind": kind, "node": node, "subject": subject,
           "payload_sha256": payload_digest({"site": host, "return": path}),
           "not_before": nb, "not_after": na, "approved_by_device": "d1"}
    out.update(extra)
    return {k: v for k, v in out.items() if v is not ...}


def envelope(b, key=KEY, kid=None, sig=None):
    return {"grant": b, "sig": sig if sig is not None else b64u(key.sign(canon(b))),
            "key_id": kid or key_id(key.public_key())}


def grant(**kw):
    return portal.encode_grant(envelope(body(**kw)))


def accept(grants, raw, host=HOST, path="/trilium/"):
    try:
        return {"subject": grants.accept(raw, host, path)}
    except GrantInvalid as exc:
        return {"error": str(exc)}


def fresh(now=NOW):
    return portal.Grants(clock=lambda: now)


good = grant()
CASES = [
    ("good", good, HOST, "/trilium/", NOW),
    ("good at not_before", grant(nb=NOW), HOST, "/trilium/", NOW),
    ("good mid-second", good, HOST, "/trilium/", NOW + 0.5),
    ("another node", grant(node="capella"), HOST, "/trilium/", NOW),
    ("a node list", grant(node=None, nodes=["capella", NODE]), HOST, "/trilium/", NOW),
    ("a node list without this node", grant(node=None, nodes=["capella"]), HOST, "/trilium/", NOW),
    ("another host", grant(host="other.example.test"), HOST, "/trilium/", NOW),
    ("asked for another host", good, "other.example.test", "/trilium/", NOW),
    ("another path", grant(path="/elsewhere"), HOST, "/trilium/", NOW),
    ("asked for another path", good, HOST, "/elsewhere", NOW),
    ("another kind", grant(kind="admin_ssh"), HOST, "/trilium/", NOW),
    ("no kind", grant(kind=...), HOST, "/trilium/", NOW),
    ("expired", grant(na=NOW - 1), HOST, "/trilium/", NOW),
    ("expires now", grant(na=NOW), HOST, "/trilium/", NOW),
    ("expires in half a second", grant(na=NOW + 1), HOST, "/trilium/", NOW + 0.5),
    ("not valid yet", grant(nb=NOW + 10), HOST, "/trilium/", NOW),
    ("valid in half a second", grant(nb=NOW + 1), HOST, "/trilium/", NOW + 0.5),
    ("no not_before", grant(nb=...), HOST, "/trilium/", NOW),
    ("a float time", grant(na=NOW + 60.0), HOST, "/trilium/", NOW),
    ("no subject", grant(subject=...), HOST, "/trilium/", NOW),
    ("an empty subject", grant(subject=""), HOST, "/trilium/", NOW),
    ("a unicode path", grant(path="/café/☃"), HOST, "/café/☃", NOW),
    ("signed by another key", portal.encode_grant(envelope(body(), key=OTHER)), HOST, "/trilium/", NOW),
    ("naming this key, signed by another", portal.encode_grant(envelope(body(), key=OTHER, kid=key_id(KEY.public_key()))), HOST, "/trilium/", NOW),
    ("a tampered body", portal.encode_grant({**envelope(body()), "grant": body(subject="root")}), HOST, "/trilium/", NOW),
    ("no sig", portal.encode_grant({k: v for k, v in envelope(body()).items() if k != "sig"}), HOST, "/trilium/", NOW),
    ("no grant", portal.encode_grant({"order": body(), "sig": "", "key_id": key_id(KEY.public_key())}), HOST, "/trilium/", NOW),
    ("padded", good + "=" * (-len(good) % 4), HOST, "/trilium/", NOW),
    ("the standard alphabet", good.replace("-", "+").replace("_", "/"), HOST, "/trilium/", NOW),
    ("empty", "", HOST, "/trilium/", NOW),
    ("not base64", "!!!!", HOST, "/trilium/", NOW),
    ("not json", b64u(b"nope"), HOST, "/trilium/", NOW),
    ("a list", b64u(b"[]"), HOST, "/trilium/", NOW),
    ("a string", b64u(b'"grant"'), HOST, "/trilium/", NOW),
]


def accept_rows():
    for name, raw, host, path, now in CASES:
        yield {"name": name, "raw": raw, "host": host, "path": path, "now": now,
               "result": accept(fresh(now), raw, host, path)}


def sequences():
    """Calls on one Grants, each at its own time."""
    once = grant(grant_id="g-once", na=NOW + 30)
    other = grant(grant_id="g-other", na=NOW + 60)
    clock = [NOW]
    grants = portal.Grants(clock=lambda: clock[0])
    steps = [(NOW, once), (NOW + 1, once), (NOW + 2, other), (NOW + 3, other), (NOW + 29, once), (NOW + 30, once)]
    out = []
    for t, raw in steps:
        clock[0] = t
        out.append({"now": t, "raw": raw, "host": HOST, "path": "/trilium/", "result": accept(grants, raw)})
        out[-1]["spent"] = sorted(grants._spent)
    return out


def no_key():
    os.environ[portal.GRANT_KEY_ENV] = "  "
    try:
        return accept(fresh(), good)
    finally:
        os.environ[portal.GRANT_KEY_ENV] = PUB


ORIGINS = [None, "", "https://auth.example", "https://auth.example/", "https://auth.example///"]
VISITS = ["https://node.example.test/trilium/", "https://node.example.test:8443/a b?x=1&y=é#f", "https://h/~_.-!*'()"]


def login_rows():
    for origin in ORIGINS:
        if origin is None:
            os.environ.pop(portal.ORIGIN_ENV, None)
        else:
            os.environ[portal.ORIGIN_ENV] = origin
        for visit in VISITS:
            yield {"origin": origin, "visit": visit, "url": portal.login_url(visit)}
    os.environ.pop(portal.ORIGIN_ENV, None)


ENVELOPES = [envelope(body()), {"grant": {"s": "café ☃ \U0001F600 \x7f \x01 \n \"q\" \\ /"}, "n": [1, -2, None, True, False]}, {}]


def decode_rows():
    for raw in [good, "", "!!!!", b64u(b"nope"), b64u(b"[]"), b64u(b"{}"), b64u(b'{"a":1}'), good[:-1], "a", "ab=="]:
        try:
            got = {"envelope": portal._decode(raw)}
        except GrantInvalid as exc:
            got = {"error": str(exc)}
        yield {"raw": raw, **got}


LOCAL = [None, "", "/", "/trilium/", "//evil.example/", "https://evil.example/", "/a?b=c", "/a#b", "/a\\b",
         "/a\nb", "/a\rb", "/a\tb", "/a\x00b", "relative", "/café", "/%2F%2Fevil", "/./x", "/ ", "///x"]
HOSTS = [None, "", "node.example.test", "Node.Example.TEST:8443", "node.example.test.:443", "[::1]:8443",
         "[FE80::1%ETH0]:1", "[fe80::1%]", "[fe80::1%a%b]", "user@host:1", "a@b@c", "host/path", "host?x", "host#f",
         "[::1", "::1]", "[1.2.3.4]", "[v1.x]", "[V1.x]", "[vZ.x]", "[v1.]", "[::1]x", "[::1]:x", " host", "ho\tst",
         "h\nost", "HÖST.example", "xn--bcher-kva.example", "host:", ":80", "[::1]:", "[]", "[::ffff:1.2.3.4]",
         "[1:2:3:4:5:6:7:8]", "[1:2:3:4:5:6:7:8:9]", "[::01234]", "İstanbul", "a:b:c", "@", "u:p@[::2]:3"]


def host_rows():
    for h in HOSTS:
        try:
            got = portal.host_of(h)
        except ValueError:
            got = ""
            yield {"header": h, "host": got, "python_raised": True}
            continue
        yield {"header": h, "host": got}


out = {
    "node": NODE,
    "key": PUB,
    "seed": SEED.hex(),
    "callback": portal.CALLBACK,
    "default_origin": portal.DEFAULT_ORIGIN,
    "login_url": list(login_rows()),
    "encode_grant": [{"envelope": e, "raw": portal.encode_grant(e)} for e in ENVELOPES],
    "decode": list(decode_rows()),
    "local_path": [{"raw": r, "path": portal.local_path(r)} for r in LOCAL],
    "host_of": list(host_rows()),
    "accept": list(accept_rows()),
    "sequence": sequences(),
    "no_key": no_key(),
}
dest = pathlib.Path(__file__).with_name("portal.json")
dest.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
print(f"{dest}: {len(out['accept'])} grants, {len(out['sequence'])} steps, {len(out['host_of'])} hosts")
