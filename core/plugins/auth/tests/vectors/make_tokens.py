"""Write tokens.json: the device-token codec, the session-cookie codec, the scope matrix and
the edge gate's slide rule, as the Python wrote them. Synthetic inputs only.

Run from the repository root while the Python still exists:
    mamba run -n awm python -I core/plugins/auth/tests/vectors/make_tokens.py
"""

from __future__ import annotations

import asyncio
import json
import random
import sys
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[4]
sys.path[:0] = [str(ROOT / "awm/service_components" / d) for d in ("config", "tlsfront", "persistence", "gatewayclient")]

from awm.config import tokens, usertoken  # noqa: E402
import awm.tlsfront.auth as edge  # noqa: E402

assert Path(tokens.__file__).is_relative_to(ROOT), tokens.__file__

SECRETS = ["awm-test-secret-0001", "awm-test-secret-0002", "sëcret-ünicode-0003"]
SUBS = ["operator", "tony", "device:0123456789abcdef", "tönÿ", "emoji-\U0001f600", 'quo"te\\back', "", "line\nbreak"]


def device_vectors(rng: random.Random) -> dict:
    mints = []
    for _ in range(8):
        id_bytes, secret_bytes = rng.randbytes(8), rng.randbytes(32)
        fake = types.SimpleNamespace(token_hex=lambda n, b=id_bytes: b[:n].hex(),
                                     token_bytes=lambda n, b=secret_bytes: b[:n])
        real, usertoken.secrets = usertoken.secrets, fake
        try:
            token_id, secret, token = usertoken.mint()
        finally:
            usertoken.secrets = real
        mints.append({"id_bytes": id_bytes.hex(), "secret_bytes": secret_bytes.hex(), "id": token_id,
                      "secret": secret, "token": token, "digest": usertoken.digest(secret)})
    good = mints[0]
    candidates = [good["token"], good["token"] + "\n", good["token"] + " ",
                  f"awmut1.{good['id']}\n.{good['secret']}", "", "awmut1.", "awmut1.0123456789abcdef",
                  "awmut1.0123456789abcdef.", "awmut1.0123456789ABCDEF." + "a" * 43, "awmut1.0123." + "a" * 43,
                  "awmut1.0123456789abcdef." + "a" * 42, "awmut1.0123456789abcdef." + "a" * 44,
                  "awmut1.0123456789abcdef." + "a" * 42 + "=", "awmut1.0123456789abcdef.x.y",
                  "awmpt1.0123456789abcdef." + "a" * 43, "AWMUT1.0123456789abcdef." + "a" * 43,
                  "awmut1.0123456789abcdef." + "a" * 41 + "-_", "awmut1.0123456789abcdef." + "a" * 42 + "+",
                  "awmut1.0123456789abcdef." + "a" * 42 + "é", "awmut1.0123456789abcdeg." + "a" * 43,
                  "awmut1.0123456789abcdef.." + "a" * 42, "awmut1." + "a" * 43]
    parses = []
    for t in candidates:
        try:
            got = list(usertoken.parse(t))
        except usertoken.TokenError:
            got = None
        parses.append({"token": t, "looks": usertoken.looks_like_token(t), "parsed": got})
    matches = []
    for m in mints[:3]:
        for stored in (m["digest"], mints[3]["digest"], "", "ünicode", m["digest"].upper(), m["digest"][:-1]):
            matches.append({"secret": m["secret"], "stored": stored, "ok": usertoken.matches(m["secret"], stored)})
    return {"scopes": sorted(usertoken.SCOPES), "mint": mints, "parse": parses, "matches": matches}


def scope_vectors() -> list:
    session = "/hub/session/avatar/stage"
    paths = [session, session + "/", "/hub/session/avatar/other", "/hub/emit/avatar/live", "/hub/session/journal/stage",
             "/invoke", "/s/avatar/stage", "/svc/avatar/session/stage", "/", "", "/hub/session/avatar/stagé",
             "/HUB/session/avatar/stage", "//hub/session/avatar/stage", "/hub/session/avatar/stage%20"]
    raws = {session: [None, b"/hub/session/avatar/%73tage", session.encode() + b"?", b"/hub/session/avatar/stage/"],
            "/hub/session/avatar/stagé": [None, "/hub/session/avatar/stagé".encode("utf-8"),
                                          b"/hub/session/avatar/stag?", b"/hub/session/avatar/stag%C3%A9"]}
    out = []
    for scope in ("avatar-stage", "everything", ""):
        for method in ("GET", "POST", "get", "HEAD"):
            for path in paths:
                for raw in raws.get(path, [None]):
                    raw_b = path.encode("ascii", "replace") if raw is None else raw
                    for query in (b"", b"peer=deneb", b"?"):
                        for ws in (True, False):
                            ok = usertoken.scope_admits(scope, method=method, path=path, raw=raw_b, query=query, websocket=ws)
                            out.append({"scope": scope, "method": method, "path": path, "raw": raw_b.hex(),
                                        "query": query.hex(), "websocket": ws, "ok": ok})
    return out


def _b64(raw: bytes) -> str:
    return tokens._b64u_encode(raw)


def session_vectors() -> dict:
    mints = []
    clocks = [1_700_000_000.0, 1_700_000_000.999, 1_799_999_999.5, 0.0, 12.25]
    for secret in SECRETS:
        for sub in SUBS:
            for now in clocks[:3]:
                for ttl, sat in ((3600.0, None), (86400.0, 1_600_000_000), (0.0, None), (43200.7, 1_699_999_999), (-5.5, None)):
                    mints.append({"secret": secret, "sub": sub, "ttl": ttl, "sat": sat, "now": now,
                                  "token": tokens.mint(secret, sub=sub, ttl=ttl, sat=sat, now=now)})
    verifies = []
    secret = SECRETS[0]
    base = tokens.mint(secret, sub="tony", ttl=3600, sat=1_700_000_000, now=1_700_000_000.0)
    exp = 1_700_003_600
    for now in (exp - 3600.0, exp - 0.001, float(exp), exp + 0.001, exp + 10_000.0):
        verifies.append({"secret": secret, "token": base, "now": now, "claims": tokens.verify(secret, base, now=now)})
    payload, sig = base.split(".")

    def signed(raw: bytes) -> str:
        p = _b64(raw)
        return f"{p}.{tokens._sign(secret, p)}"

    odd = [
        base.replace(sig, sig[:-1]), base + "x", base + ".x", payload, "", ".", f"{payload}.", f".{sig}",
        tokens.mint(SECRETS[1], sub="tony", ttl=3600, now=1_700_000_000.0),
        signed(b"[1,2]"), signed(b"\"x\""), signed(b"not json"), signed(b"{}"), signed(b'{"exp":"1800000000"}'),
        signed(b'{"exp":true}'), signed(b'{"exp":null,"sub":"x"}'), signed(b'{"exp":1800000000.5,"sub":"x","sat":1}'),
        signed(b'{"exp":1800000000,"sub":"a","sub":"b"}'), signed(b'{"exp":1800000000,"extra":[1,{"k":null}]}'),
        signed(b'{"exp":1800000000,"sub":"t\\u00f6ny"}'), signed(b'{"exp": 1800000000 , "sub" : 7}'),
        signed(b'{"exp":1e10}'), signed('{"exp":1800000000,"sub":"tönÿ"}'.encode("utf-8")),
    ]
    for tok in odd:
        verifies.append({"secret": secret, "token": tok, "now": 1_700_000_000.0, "claims": tokens.verify(secret, tok, now=1_700_000_000.0)})
    return {"mint": mints, "verify": verifies}


def _gate(material: dict) -> edge.AuthGate:
    gate = edge.AuthGate()
    gate._material = material
    gate._fetched_at = time.monotonic()
    return gate


def _at(now: float):
    clock = types.SimpleNamespace(time=lambda: now, monotonic=time.monotonic)
    edge.time, tokens.time = clock, clock


def slide_vectors() -> dict:
    secret = SECRETS[0]
    sat = 1_700_000_000
    slides = []
    materials = [
        {"secret": secret, "session_ttl_seconds": 86400.0, "max_session_seconds": 2_592_000.0},
        {"secret": secret, "session_ttl_seconds": 43200.0, "max_session_seconds": 0},
        {"secret": secret, "session_ttl_seconds": 0, "max_session_seconds": 3600.0},
        {"secret": secret, "session_ttl_seconds": 3600.5, "max_session_seconds": 7200.25},
    ]
    real = time
    try:
        for mat in materials:
            ceiling = float(mat["max_session_seconds"] or 0)
            for sub in ("tony", "operator", "", "tönÿ"):
                for age in (0.0, 1.0, ceiling - 1.0, ceiling - 0.001, ceiling, ceiling + 0.001, ceiling + 1.0):
                    now = sat + age
                    cookie = tokens.mint(secret, sub=sub, ttl=10 * 86400 * 30, sat=sat, now=sat + max(age - 1.0, 0.0))
                    _at(now)
                    ok, refreshed, who = asyncio.run(_gate(dict(mat)).authenticate(cookie=cookie, bearer=None))
                    slides.append({"material": mat, "cookie": cookie, "now": now, "ok": ok, "refreshed": refreshed, "sub": who})
            # A cookie past its own exp is refused, even under the ceiling.
            cookie = tokens.mint(secret, sub="tony", ttl=60, sat=sat, now=sat)
            for now in (sat + 59.999, sat + 60.0):
                _at(now)
                ok, refreshed, who = asyncio.run(_gate(dict(mat)).authenticate(cookie=cookie, bearer=None))
                slides.append({"material": mat, "cookie": cookie, "now": now, "ok": ok, "refreshed": refreshed, "sub": who})
        sessions = []
        for mat in materials:
            for sub in ("tony", "tönÿ"):
                now = 1_700_000_000.75
                _at(now)
                token, ttl = asyncio.run(_gate(dict(mat)).mint_session(sub))
                _at(now)
                ttl_report = asyncio.run(_gate(dict(mat)).session_ttl_seconds())
                sessions.append({"material": mat, "sub": sub, "now": now, "token": token, "ttl": ttl, "session_ttl_seconds": ttl_report})
    finally:
        edge.time, tokens.time = real, real
    return {"slide": slides, "mint_session": sessions}


def header_vectors() -> dict:
    headers = [None, "", "Bearer abc", "bearer abc", "BEARER  abc  ", "Bearer", "Bearer ", "Basic abc", "  Bearer abc def ",
               "Bearer\tabc", "Bearer abc", "Bearerabc", "Token abc", "Bearer awmut1.x.y"]
    bearers = [{"header": h, "bearer": edge.bearer_of(h)} for h in headers]
    subs = [{"sub": s, "machine": edge.is_machine_sub(s)} for s in
            (None, "", "operator", "peer", "peer:", "peer:mira", "peers", "Operator", "tony", "device:0123456789abcdef", "user:peer")]
    return {"bearer_of": bearers, "is_machine_sub": subs}


def main() -> None:
    rng = random.Random(20261107)
    out = {"device": device_vectors(rng), "scope_admits": scope_vectors(), "session": session_vectors(),
           "gate": slide_vectors(), "headers": header_vectors()}
    (HERE / "tokens.json").write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
