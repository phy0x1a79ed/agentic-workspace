"""Write policy.json: the Python edge's verdict on every synthetic path, raw target and body.

Run from anywhere with `mamba run -n awm python -I make_policy.py`; it reads the Python from
this worktree, not from an installed copy.
"""

import base64
import json
import pathlib
import sys

WS = pathlib.Path(__file__).resolve().parents[5]
sys.path[:0] = [str(WS / "awm/services/httpsfront"), str(WS / "awm/service_components/tlsfront"),
                str(WS / "awm/service_components/config"), str(WS / "awm/service_components/grantlib"),
                str(WS / "awm/service_components/persistence")]

from awm.httpsfront import board, policy, slices, tether, vault  # noqa: E402

assert policy.__file__.startswith(str(WS)), policy.__file__

SUBS = [None, "", "tony", "steven", "peer", "peer:mira", "peer:", "operator", "Tony", "tony\n"]
TOKEN = "Ab3-_9xYzQw012345678"
TICKET = "0123456789abcdef0123456789abcdef"
CARD = TICKET
ROOM = "acre-anchor-kettle"
ROOM6 = "acre-anchor-kettle-abbey-otter-lamp"

paths = [
    # the exact and prefix lists
    "/", "", "/p/trilium/trilium", "/p/trilium/trilium/", "/p/trilium/trilium/assets/index.js",
    "/p/trilium/trilium//x", "/p/trilium/trilium/../../hub/plugins", "/p/trilium/triliumXYZ",
    "/p/trilium/trilium-admin/x", "/p/trilium/", "/p/trilium", "/P/trilium/trilium/", "/p/trilium/trilium\n",
    "//p/trilium/trilium/", "/p/trilium/trilium?x=1", "/p/trilium/trilium/?x=1", "/p/trilium/trilium%2f",
    "/__auth/portal", "/__auth/logout", "/__auth/whoami", "/__auth/portal/", "/__auth/portal\n",
    "/__auth/login", "/__auth/link", "/__auth/", "/__auth", "/__auth/whoami?x", "/__AUTH/whoami",
    "/invoke", "/invoke/", "/invoke\n", "/INVOKE", "//invoke", "/invoke?x=1", "/invoke/x",
    # the gateway and other services
    "/hub/services", "/hub/plugins", "/tools", "/svc/scopes/fn/scope_search", "/svc/auth/fn/password",
    "/svc/auth/fn/verify", "/ca.crt", "/ca.pem", "/ui/agents/", "/p/dashboard/dashboard/",
    "/p/httpsfront/login/", "/files/", "/s/fileviewer/", "/s/fileviewer/projects/awm/dev/x",
    "/s/claude-science/mcp", "/__landing/tags", "/__landing/filter", "/__landing/name",
    "/ui/notes/", "/svc/notes/fn/list", "/svc/notes/emit/note:tony:1", "/ui/drawio/",
    "/drawio-app/index.html", "/svc/drawio/fn/save", "/svc/drawio/emit/drawio:tony:x.drawio",
    "/etapi/app-info", "/custom/x", "/mcp", "/share/abc", "/login", "/setup", "/set-password",
    "/robots.txt", "/ui/trilium", "/ui/trilium/", "/svc/trilium/fn/status", "/svc/trilium/fn/restore",
    "/api/tree", "/src/index.js", "/assets/x.css", "/favicon.ico", "/bootstrap", "/icon.png",
    # the fileviewer user subtree
    "/s/fileviewer/projects/userdata/tony", "/s/fileviewer/projects/userdata/tony/",
    "/s/fileviewer/projects/userdata/tony/notes/a.md", "/s/fileviewer/projects/userdata/steven/notes/a.md",
    "/s/fileviewer/projects/userdata/", "/s/fileviewer/projects/userdata", "/s/fileviewer/projects/userdata//x",
    "/s/fileviewer/projects/userdata/tony\n", "/s/fileviewer/projects/userdata/tony/x\n",
    "/s/fileviewer/projects/userdata/tony/x\ny", "/s/fileviewer/projects/userdata/tony\n/x",
    "/s/fileviewer/projects/userdata/peer:mira/x", "/s/fileviewer/projects/userdata/operator",
    "/s/fileviewer/projects/userdata/..", "/s/fileviewer/projects/userdata/../awm/x",
    "/s/fileviewer/projects/userdata/Tony/x", "/s/fileviewer/projects/userdata/töny/x",
    "/files/projects/userdata/tony/notes/a.md", "/s/fileviewer/projects/userdata/tony/../steven",
    "/s/fileviewer//projects/userdata/tony", "/x/s/fileviewer/projects/userdata/tony",
    # the vault
    "/trilium", "/trilium/", "/trilium/bootstrap", "/trilium/favicon.ico", "/trilium/icon.png",
    "/trilium/api/tree", "/trilium/api/notes/abc123", "/trilium/assets/v0.105.0/src/index.js",
    "/trilium/src/index-Dso57yoQ.js", "/trilium/node_modules/@excalidraw/excalidraw/dist/prod/x.js",
    "/trilium/pdfjs/viewer.html", "/trilium/manifest.webmanifest", "/trilium/logout", "/trilium/logout/x",
    "/trilium/logoutx", "/trilium/login", "/trilium/login/", "/trilium/loginx", "/trilium/mcp",
    "/trilium/mcp/x", "/trilium/mcpx", "/trilium/etapi/", "/trilium/etapi/app-info", "/trilium/etapi",
    "/trilium/etapix", "/trilium/custom/x", "/trilium/custom", "/trilium/share/abc", "/trilium/share",
    "/trilium/setup", "/trilium/set-password", "/trilium/set-password/x", "/trilium//etapi/x",
    "/trilium/./etapi/x", "/trilium/../hub/plugins", "/trilium\n", "/trilium/\n", "/Trilium/",
    "/triliumx", "/trilium-admin", "//trilium/", "/trilium/robots.txt", "/trilium/%2e%2e/x",
    # slices
    "/slice/" + TOKEN, "/slice/" + TOKEN + "/", "/slice/" + TOKEN + "/api/tree", "/slice/" + TOKEN + "/icon.png",
    "/slice/" + TOKEN + "/etapi/app-info", "/slice/" + TOKEN + "/etapi", "/slice/" + TOKEN + "/manifest.webmanifest",
    "/slice/" + TOKEN + "/robots.txt", "/slice/" + TOKEN + "/robots.txt/x", "/slice/" + TOKEN + "/login",
    "/slice/" + TOKEN + "/logout", "/slice/" + TOKEN + "/custom/x", "/slice/" + TOKEN + "/share/x",
    "/slice/" + TOKEN + "\n", "/slice/" + TOKEN + "\n/api", "/slice/" + TOKEN + "//api",
    "/slice/", "/slice", "/slice/short", "/slice/has.a.dot0123456789", "/slice/" + "x" * 200,
    "/slice/" + "x" * 16, "/slice/" + "x" * 15, "/slice/" + "x" * 128, "/slice/" + "x" * 129,
    "/slices/" + TOKEN, "/Slice/" + TOKEN, "/slice/" + TOKEN.replace("A", "Ä"),
    # tether
    "/tether", "/tether/win", "/tether/bin/tether", "/tether/bin/tether-windows-x86_64.exe",
    "/tether/bin/tether-macos-arm64", "/tether/bin/" + "a" * 64, "/tether/bin/" + "a" * 65, "/tether/bin/.x",
    "/tether/claim/1", "/tether/claim/999", f"/tether/join/7/{TICKET}", f"/tether/release/7/{TICKET}",
    f"/tether/hold/7/{TICKET}", f"/tether/poll/7/{TICKET}", f"/tether/seat/{TICKET}/recv",
    f"/tether/seat/{TICKET}/send", f"/tether/seat/{TICKET}/close", "/room/" + ROOM, "/room/" + ROOM + "/send",
    "/room/" + ROOM + "/recv", "/room/" + ROOM + "/ws", "/room/" + ROOM + "/leave", "/room/" + ROOM6,
    "/room/" + ROOM6 + "/recv", "/tether\n", "/tether/win\n", "/tether/claim/7\n", "/room/" + ROOM + "\n",
    "/tether/health", "/tether/", "/tether/claim/0", "/tether/claim/07", "/tether/claim/1000",
    "/tether/claim/7/extra", "/tether/claim", f"/tether/join/7/{TICKET[:31]}", f"/tether/join/7/{TICKET.upper()}",
    "/tether/join/7", f"/tether/release/7/{TICKET[:31]}", "/tether/release/7", f"/tether/release/0/{TICKET}",
    "/tether/hold/7", f"/tether/hold/7/{TICKET}/extra", f"/tether/poll/0/{TICKET}", f"/tether/poll/7/{TICKET[:31]}",
    f"/tether/seat/{TICKET}", f"/tether/seat/{TICKET}/join", f"/tether/seat/{TICKET.upper()}/recv",
    "/tether/seat/7/recv", "/tether/bin/../../etc/passwd", "/tether/bin/", "/tether/win/", "/tether/windows",
    "/tether/invoke", "/tether/issue", "/tether/status", "/room", "/room/", "/room/acre-anchor",
    "/room/acre-anchor-kettle-acre", "/room/acre-anchor-kettle-acre-anchor",
    "/room/acre-anchor-kettle-acre-anchor-kettle-acre", "/room/Acre-anchor-kettle", "/room/acre_anchor_kettle",
    "/room/ab-anchor-kettle", "/room/acre-anchor-kettle/", "/room/acre-anchor-kettle/close",
    "/tether-admin", "/tetherfoo", "/p/tether/tether", "/rooms", "/roomy", "/p/room/room", "//tether",
    # board
    "/board/cards", f"/board/cards/{CARD}", f"/board/cards/{CARD}/claim", f"/board/cards/{CARD}/complete",
    f"/board/cards/{CARD}/fail", f"/board/cards/{CARD}/status", f"/board/cards/{CARD}/reject",
    f"/board/cards/{CARD}/messages", f"/board/cards/{CARD}/events", f"/board/cards/{CARD}/files",
    f"/board/cards/{CARD}/files/{CARD}", "/board/stream", "/board", "/board/", "/board/cards/",
    "/board/stream/", "/board/health", "/board/parties", "/board/cards/short", f"/board/cards/{CARD[:-1]}",
    f"/board/cards/{CARD}0", f"/board/cards/{CARD.upper()}", "/board/cards/01234567-89ab-cdef-0123-456789abcdef",
    "/board/cards/" + "g" * 32, f"/board/cards/{CARD}/release", f"/board/cards/{CARD}/claim/extra",
    f"/board/cards/{CARD}/messages/1", f"/board/cards/{CARD}/", "/board/cards/../stream", "/board//cards",
    f"/board/cards/{CARD}\n", "/board/cards\n", "/board/stream\n", "/boards/cards", "/boarding/x", "/boards",
    f"/board/cards/{CARD}/files/../../stream", f"/board/cards/{CARD}/files/{CARD}/x",
    # odd bytes and characters
    "/é", "/trilium/é", "/p/trilium/trilium/é", "/\x00", "/trilium/\x00", "/ invoke", "/invoke ",
]
assert len(set(paths)) == len(paths), [p for p in paths if paths.count(p) > 1]


def verdict_rows():
    for path in paths:
        yield {
            "path": path,
            "verdict": policy.classify(path).value,
            "allows": [policy.allows(path, sub) for sub in SUBS],
            "vault_owns": vault.owns(path),
            "vault_upstream": vault.upstream_path(path),
            "slices_token": slices.token_of(path),
            "slices_owns": slices.owns(path),
            "slices_refused": slices.refused(path),
            "slices_upstream": slices.upstream_path(path),
            "tether_owns": tether.owns(path),
            "tether_refused": tether.refused(path),
            "tether_upstream": tether.upstream_path(path),
            "board_owns": board.owns(path),
            "board_in_mount": board.in_mount(path),
        }


RAWS = [b"/trilium", b"/trilium/", b"/trilium/api/search/%23x", b"/%74rilium/api/tree", b"/triliumx",
        b"/trilium%2f", b"/", b"", b"/slice/" + TOKEN.encode(), b"/slice/" + TOKEN.encode() + b"/",
        b"/slice/" + TOKEN.encode() + b"/api/search/%23foo", b"/%73lice/" + TOKEN.encode(), b"/slice/short/x",
        b"/slice/" + TOKEN.encode() + b"\n/x", b"/slice/" + TOKEN.encode().replace(b"A", b"\xc4") + b"/x",
        b"/slice/" + b"\xff" * 16, b"/tether", b"/tether/", b"/tether/claim/7", b"/%74ether/claim/7", b"/tetherx",
        b"/room", b"/room/" + ROOM.encode() + b"/recv", b"/roomx", b"/room/", b"/board/cards", b"/board/c%61rds",
        b"/board/cards/" + CARD.encode(), b"/board/cards/" + CARD.encode() + b"\n", b"/board/\xff",
        b"/board/stream", b"/board", b"/board/cards/" + CARD.encode() + b"/files/" + CARD.encode()]


def raw_rows():
    for raw in RAWS:
        out = {"raw": raw.hex()}
        for name, mod in (("vault", vault), ("slices", slices), ("tether", tether), ("board", board)):
            got = mod.upstream_raw_path(raw)
            out[name] = None if got is None else got.hex()
        yield out


METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "get", "post", "Post"]


def board_rows():
    for path in paths:
        if board.in_mount(path) or path.startswith("/boards"):
            for m in METHODS:
                yield {"method": m, "path": path, "allows": board.allows(m, path), "body_cap": board.body_cap(m, path)}


def call(verb, **extra):
    return {"name": "trilium", "args": {"verb": verb, "args": {}, **extra}}


BODIES = [json.dumps(b).encode() for b in [
    call("status"), call("snapshots"), call("url"), call("restore"), call("stop"), call("logs"),
    call("export"), call("slice_resolve"), call("status", peer="mira"),
    {"name": "trilium_status", "args": {}}, {"name": "journal", "args": {"verb": "status", "args": {}}},
    {"name": "trilium", "args": {"verb": "status", "args": []}},
    {"name": "trilium", "args": {"verb": "status"}, "peer": "mira"},
    [], "status", {"name": "trilium", "args": {"verb": "status"}}, {"name": "trilium"},
    {"name": "trilium", "args": None}, {"name": "trilium", "args": {"verb": "status", "args": None}},
    {"name": "trilium", "args": {"args": {}}}, {"name": "trilium", "args": {"verb": None}},
    {"name": "trilium", "args": {"verb": "Status"}}, {"name": "trilium", "args": {"verb": 1}},
    {"args": {"verb": "status"}}, {"name": ["trilium"], "args": {"verb": "status"}},
    {"name": "trilium", "args": {"verb": "status", "args": {"pad": "x" * 100, "n": [1, 2.5, None, True]}}},
    {"name": "trilium", "args": {"verb": "url", "args": {"é": "☃", "deep": {"a": [[[{}]]]}}}},
    None, 1, True, {},
]] + [
    b"", b"{not json", b"   ", b"null", b'{"name":"trilium","args":{"verb":"status"}}  \n',
    b'{"name":"trilium","args":{"verb":"status"}} x', b'\xef\xbb\xbf{"name":"trilium","args":{"verb":"status"}}',
    b'\xef\xbb\xbf\xef\xbb\xbf{"name":"trilium","args":{"verb":"status"}}',
    '{"name":"trilium","args":{"verb":"status"}}'.encode("utf-16"),
    '{"name":"trilium","args":{"verb":"status"}}'.encode("utf-16-le"),
    '{"name":"trilium","args":{"verb":"status"}}'.encode("utf-16-be"),
    '{"name":"trilium","args":{"verb":"status"}}'.encode("utf-32"),
    '{"name":"trilium","args":{"verb":"status"}}'.encode("utf-32-le"),
    '{"name":"trilium","args":{"verb":"status"}}'.encode("utf-32-be"),
    '{"name":"trilium","args":{"verb":"status"}}'.encode("utf-16")[:-1],
    b'{"name":"trilium","args":{"verb":"status","args":{"x":NaN,"y":Infinity,"z":-Infinity}}}',
    b'{"name":"trilium","args":{"verb":"status","args":{"x":1e400,"y":123456789012345678901234567890}}}',
    b'{"name":"trilium","args":{"verb":"status","args":{"x":"\\ud800"}}}',
    b'{"name":"trilium","args":{"verb":"status","args":{"x":"\\ud800\\udc00"}}}',
    b'{"name":"trilium","args":{"verb":"status","args":{"x":"\\ud800\\u0041"}}}',
    b'{"name":"trilium","args":{"verb":"status","args":{"x":"\\ud800\\uzzzz"}}}',
    b'{"name":"trilium","args":{"verb":"status","args":{"x":"\xed\xa0\x80"}}}',
    b'{"name":"trilium","args":{"verb":"status","args":{"x":"\xff"}}}',
    b'{"name":"trilium","args":{"verb":"status","args":{"x":"\\x"}}}',
    b'{"name":"trilium","args":{"verb":"status","args":{"x":"a\tb"}}}',
    b'{"name":"tri\\u006cium","args":{"verb":"st\\u0061tus"}}',
    b'{"name":"trilium","args":{"verb":"status"},}', b'{"name":"trilium","args":{"verb":"status"}',
    b'{"name":"trilium","name":"journal","args":{"verb":"status"}}',
    b'{"name":"journal","name":"trilium","args":{"verb":"status"}}',
    b'{"name":"trilium","args":{"verb":"restore","verb":"status"}}',
    b'{"name":"trilium","args":{"verb":"status","args":{"x":01}}}',
    b'{"name":"trilium","args":{"verb":"status","args":{"x":1.}}}',
    b'{"name":"trilium","args":{"verb":"status","args":{"x":-}}}',
    b'{"name":"trilium","args":{"verb":"status","args":{"x":1e}}}',
    b'{"name":"trilium","args":{"verb":"status","args":{"x":-0.5E+3}}}',
    b'{"name":"trilium","args":{"verb":"status","args":{"x":[1,]}}}',
    b'{"name":"trilium","args":{"verb":"status","args":{"x":tru}}}',
    b'{"name" : "trilium" , "args" : { "verb" : "url" , "args" : { } } }',
    b'{\n\t"name":"trilium",\r\n"args":{"verb":"url"}}', b'\x0c{"name":"trilium","args":{"verb":"url"}}',
    b'{"name":"trilium","args":{"verb":"status","args":{"x":' + b"[" * 200 + b"]" * 200 + b"}}}",
]


def invoke_rows():
    for body in BODIES:
        yield {"body": base64.b64encode(body).decode(), "allows": policy.invoke_allows(body)}


out = {
    "subs": SUBS,
    "constants": {
        "open_exact": sorted(policy.OPEN_EXACT), "open_prefixes": list(policy.OPEN_PREFIXES),
        "trilium_open_fns": sorted(policy.TRILIUM_OPEN_FNS), "invoke_body_cap": policy.INVOKE_BODY_CAP,
        "vault_not_forwarded": vault.NOT_FORWARDED, "slices_not_forwarded": slices.NOT_FORWARDED,
        "slices_all_not_forwarded": list(slices.ALL_NOT_FORWARDED), "tether_not_forwarded": tether.NOT_FORWARDED,
        "board_max_body": board.MAX_BODY, "board_max_file_body": board.MAX_FILE_BODY,
        "vault_manifest": vault.manifest(),
    },
    "paths": list(verdict_rows()),
    "raw": list(raw_rows()),
    "board": list(board_rows()),
    "invoke": list(invoke_rows()),
}
dest = pathlib.Path(__file__).with_name("policy.json")
dest.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
print(f"{dest}: {len(out['paths'])} paths, {len(out['raw'])} raw, {len(out['board'])} board, {len(out['invoke'])} bodies")
