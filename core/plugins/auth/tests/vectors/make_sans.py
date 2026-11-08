"""Write sans.json: the Python SAN normaliser over synthetic inputs, for the Rust port.

Run from the repo root, before the Python is deleted:
    mamba run -n awm python -I core/plugins/auth/tests/vectors/make_sans.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(REPO / "awm" / "service_components" / "tlsfront"))

from awm.tlsfront import certs  # noqa: E402

OUT = Path(__file__).resolve().parent / "sans.json"

TOKENS = [
    "", " ", "\t", "  \n ",
    "10.74.81.213", " 10.74.81.213 ", "192.168.1.20", "0.0.0.0", "255.255.255.255",
    "127.0.0.1", "01.2.3.4", "1.2.3", "1.2.3.4.5", "256.1.1.1", "1.2.3.04",
    "::1", "fe80::1", "FE80::1", "2001:db8::42", "2001:DB8:0:0:0:0:0:42",
    "::ffff:10.0.0.1", "fe80::1%eth0", "fe80::1%", "[::1]", "::",
    "node.example.test", "NODE.Example.TEST", "localhost", "notes.zt", "a_b.example.test",
    "IP:10.74.81.5", "ip:10.74.81.5", "Ip:10.74.81.5", "IP: 10.74.81.5", "IP:", "IP: ",
    "IP:node.example.test", "IP:fe80::1", "IP::1",
    "DNS:node.example.test", "dns:node.example.test", "Dns:Node.Example.Test", "DNS:", "DNS:  ",
    "DNS:10.0.0.1", "DNS:a:b", "IP:a:b",
    "URI:https://node.example.test", "email:operator@example.test", "IPX:1.2.3.4",
    "D NS:x", ":10.0.0.1", "10.0.0.1:443", "node.example.test:443",
    " 10.0.0.9　", "ıp:10.0.0.7", "ıP:10.0.0.7",
    "ßnode.example.test", "nöde.example.test",
]

ENVS = [
    "", " ", ",", ",,, ,", "10.74.81.5", "10.74.81.5,node.example.test",
    "10.74.81.5 node.example.test", "10.74.81.5,\tnode.example.test\n DNS:other.example.test",
    "IP:10.74.81.5,10.74.81.5,ip:10.74.81.5", "a,A,a", "DNS:x,dns:x,x",
    "127.0.0.1,localhost,DNS:localhost", " 10.0.0.9　10.0.0.10",
    "10.0.0.11\x1c10.0.0.12\x1f10.0.0.13\x8510.0.0.14",
    "#notacomment,10.0.0.1",
]

FILES = [
    None,
    "",
    "10.74.81.5\n",
    "# the windows-side address\n10.74.81.5\n",
    "10.74.81.5 # windows zerotier\nnode.example.test\n\n  \n",
    "10.74.81.5, 10.74.81.6,\tnode.example.test\n",
    "IP:10.74.81.5\nDNS:node.example.test\nip:10.74.81.5\n",
    "10.74.81.5\r\nnode.example.test\r\n",
    "10.74.81.5\rnode.example.test\r",
    "# only comments\n#10.0.0.1\n   # 10.0.0.2\n",
    "10.0.0.1#inline\n10.0.0.2 #x 10.0.0.3\n",
    "10.0.0.1\x0b# after vt\n10.0.0.2\x0c#after ff\n10.0.0.3\x1c#after fs\n",
    "10.0.0.4\x85# after nel\n10.0.0.5 # after ls\n10.0.0.6 #after ps\n",
    "10.0.0.7\x1d#gs\n10.0.0.8\x1e#rs\n10.0.0.9\x1f#us is not a line break 10.0.0.10\n",
    "IP:\nDNS:\nDNS: x\n",
    "127.0.0.1\nlocalhost\n",
    "no-trailing-newline.example.test",
]

DEFAULTS = [
    ["IP:127.0.0.1", "DNS:localhost"],
    ["IP:127.0.0.1", "DNS:localhost", "IP:10.74.81.213", "IP:172.18.0.1"],
]

RESOLVE = [
    ("", None),
    ("10.74.81.5", None),
    ("", "10.74.81.213\n10.74.81.5\n"),
    ("127.0.0.1,DNS:localhost,localhost", "IP:172.18.0.1\nnode.example.test\n"),
    ("node.example.test", "node.example.test\n10.74.81.5\n"),
]


def extra(env: str, content: str | None, tmp: Path) -> list[str]:
    if content is None:
        return certs.extra_sans(env=env, san_file=tmp / "absent.sans")
    p = tmp / ".sans"
    p.write_text(content, encoding="utf-8", newline="")
    return certs.extra_sans(env=env, san_file=p)


def main() -> None:
    out: dict = {"normalize": [], "extra": [], "resolve": []}
    for tok in TOKENS:
        out["normalize"].append({"input": tok, "output": certs._normalize_san(tok)})
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        for env in ENVS:
            for content in FILES:
                out["extra"].append({"env": env, "file": content,
                                     "output": extra(env, content, tmp)})
        for defaults in DEFAULTS:
            certs._enumerate_ipv4 = lambda defaults=defaults: [
                s.split(":", 1)[1] for s in defaults[2:]]
            for env, content in RESOLVE:
                if content is None:
                    got = certs.resolve_sans(env=env, san_file=tmp / "absent.sans")
                else:
                    p = tmp / ".sans"
                    p.write_text(content, encoding="utf-8", newline="")
                    got = certs.resolve_sans(env=env, san_file=p)
                out["resolve"].append({"defaults": defaults, "env": env, "file": content,
                                       "output": got})
    OUT.write_text(json.dumps(out, indent=1, ensure_ascii=True) + "\n")
    print(f"wrote {OUT}: {len(out['normalize'])} tokens, {len(out['extra'])} extra, "
          f"{len(out['resolve'])} resolve")


if __name__ == "__main__":
    main()
