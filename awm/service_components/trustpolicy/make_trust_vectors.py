"""Write core/crates/awm-trust/tests/trust_vectors.json: trustpolicy's answers that awm-trust must match.

Run it with grantlib, config, trustpolicy and cch on the path (scripts/run-tests.sh sets the
same path). The grant key is grantlib's vector key, so the file is stable.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from verify_grant import b64u, canon, key_id

import awm.trustpolicy as tp
from awm.cch.lifecycle import Refused, check_effort

KEY = Ed25519PrivateKey.from_private_bytes(hashlib.sha256(b"awm-grantlib test vector key").digest())
PUB = KEY.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
T0 = 1_800_000_000
OUT = Path(__file__).resolve().parents[3] / "core/crates/awm-trust/tests/trust_vectors.json"
EDGES = {
    "nodes": {"home": {"role": "homebase"}, "laptop": {"role": "laptop"}, "work": {"role": "work"}},
    "edges": {"home": {"laptop": "tailscale", "work": "tailscale"}, "laptop": {"home": "tailscale"}},
}


def reachable(node: str) -> list[str]:
    return sorted(EDGES["edges"].get(node, {}))


def raise_envelope(session_id: str, current: str, to: str, **over) -> dict:
    payload = {"session_id": session_id, "from": current, "to": to}
    body = {"grant_id": "gTESTRAISE", "request_id": "qTESTRAISE", "kind": "trust_raise",
            "node": "home", "subject": session_id, "payload_sha256": hashlib.sha256(canon(payload)).hexdigest(),
            "not_before": T0, "not_after": T0 + 900, "approved_by_device": "dTESTDEV1", **over}
    return {"grant": body, "sig": b64u(KEY.sign(canon(body))), "key_id": key_id(KEY.public_key())}


def start(starter, requested=None, session_id=None, grant=None, node=None, now=T0 + 60) -> dict:
    case = {"starter": starter, "requested": requested, "session_id": session_id, "grant": grant,
            "node": node, "now": now}
    try:
        case["level"] = tp.level_for_start(starter, requested, session_id=session_id, grant=grant,
                                           grant_key=PUB if grant else None, node=node, now=now)
    except tp.TrustError as exc:
        case["error"] = str(exc)
    return case


def steer(actor, verb, target=None) -> dict:
    ok, why = tp.may_steer(actor, verb, target_level=target)
    return {"actor": actor, "verb": verb, "target": target, "ok": ok, "why": why}


def bound(level, node, project, scope) -> dict:
    b = tp.journal_bound(level, node=node, project=project, scope=scope, reachable=reachable)
    probes = [(n, p, s) for n in ("home", "work", "elsewhere") for p, s in
              ((project, scope), ("awm", "other"), (None, None), ("", ""))]
    return {"level": level, "node": node, "project": project, "scope": scope,
            "read_nodes": sorted(b.read_nodes) if b.read_nodes is not None else None,
            "read_scope": b.read_scope, "write_scope": b.write_scope,
            "reads": [[n, p, s, b.may_read(n, p, s)] for n, p, s in probes],
            "writes": [[p, s, b.may_write(p, s)] for _, p, s in probes[:4]]}


def effort(e: str) -> dict:
    try:
        check_effort(e)
        return {"effort": e, "error": None}
    except Refused as exc:
        return {"effort": e, "error": str(exc)}


def main() -> None:
    good = raise_envelope("cc-0001", "cooperative", "trusted")
    cases = {
        "public_key_b64u": b64u(PUB),
        "levels": list(tp.LEVELS),
        "steer_verbs": sorted(tp.STEER_VERBS),
        "first_local_message_hint": tp.FIRST_LOCAL_MESSAGE_HINT,
        "edges": EDGES,
        "starts": [
            start("trusted"),
            start("trusted", "untrusted"),
            start("cooperative", "cooperative"),
            start("untrusted", "cooperative"),
            start("cooperative", "trusted", session_id="cc-0001"),
            start("cooperative", "trusted", session_id="cc-0001", grant=good, node="home"),
            start("cooperative", "trusted", session_id="cc-0001", grant=good),
            start("cooperative", "trusted", session_id="cc-0001", grant=good, node="work"),
            start("cooperative", "trusted", session_id="cc-0002", grant=good, node="home"),
            start("untrusted", "trusted", session_id="cc-0001", grant=good, node="home"),
            start("cooperative", "trusted", session_id="cc-0001", grant=good, node="home", now=T0 + 900),
            start("cooperative", "trusted", session_id="cc-0001", grant=good, node="home", now=T0 - 1),
        ],
        "steers": [steer(a, v, t) for a in tp.LEVELS
                   for v in ("message", "stop", "mcp", "start_other", "journal_post")
                   for t in (None, *tp.LEVELS)],
        "bounds": [bound(level, node, p, s) for level in tp.LEVELS
                   for node in ("home", "work") for p, s in (("awm", "x"), (None, None))],
        "efforts": [effort(e) for e in ("low", "medium", "high", "xhigh", "max", "bogus", "")],
    }
    OUT.write_text(json.dumps(cases, indent=1, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
