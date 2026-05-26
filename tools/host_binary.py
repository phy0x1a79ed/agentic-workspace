#!/usr/bin/env python3
"""Static binary + install.sh server for probe.

Serves the install.sh launcher (with EMQX creds + host rewritten in) and the
probe release binary on 0.0.0.0:12110. The port is inside the pre-forwarded
ZT range 12100-12150 on capella, so peers can reach it for free without
extra netsh portproxy config.

Signaling is NOT served here — that lives on the EMQX broker. This server
is purely the binary-distribution endpoint.

Routes:
    GET /                      friendly index
    GET /probe                 installer/install.sh, served verbatim. The
                               install.sh now reads EMQX_PASS from the
                               friend's env (or PROBE_BINARY_URL override
                               to fetch the binary from this host instead
                               of github releases). URL+USER are baked
                               into the binary; no placeholders rewritten.
    GET /probe/linux-x86_64    binary/target/release/probe if built,
                               otherwise binary/stub.sh as a fallback so the
                               harness is exercisable pre-rustup
    GET /healthz               liveness

Configuration:
    Reads tools/.env.emqx (for the PASS shown in the index hint) first,
    then process env. EMQX_PASS optional — only used to print a hint.

Usage:
    python3 tools/host_binary.py [--port 12110] [--host 0.0.0.0]
"""
from __future__ import annotations

import argparse
import http.server
import os
import socket
import sys
from pathlib import Path

SCOPE_ROOT = Path(__file__).resolve().parent.parent
INSTALLER = SCOPE_ROOT / "installer" / "install.sh"
BINARY_RELEASE = SCOPE_ROOT / "binary" / "target" / "release" / "probe"
BINARY_STUB = SCOPE_ROOT / "binary" / "stub.sh"
ENV_FILE = SCOPE_ROOT / "tools" / ".env.probe"
DEFAULT_PORT = 12110
DEFAULT_HOST = "0.0.0.0"


def load_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    env = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def resolve_emqx_pass() -> str:
    file_env = load_env_file(ENV_FILE)
    return os.environ.get("EMQX_PASS") or file_env.get("EMQX_PASS", "")


def resolve_binary() -> tuple[Path, str]:
    if BINARY_RELEASE.is_file():
        return BINARY_RELEASE, "application/octet-stream"
    if BINARY_STUB.is_file():
        return BINARY_STUB, "application/x-sh"
    raise FileNotFoundError(
        f"neither {BINARY_RELEASE} nor {BINARY_STUB} exists — "
        "cannot serve /probe/linux-x86_64"
    )


def index_body(host: str, pass_hint: str) -> bytes:
    pw = pass_hint or "<EMQX_PASS>"
    return (
        f"probe binary host\n"
        f"=================\n\n"
        f"try it:    curl -fsSL http://{host}/probe \\\n"
        f"               | PROBE_BINARY_URL=http://{host}/probe/linux-x86_64 \\\n"
        f"                 EMQX_PASS={pw} sh -s <probe-name>\n\n"
        f"routes:\n"
        f"  GET /probe                 install.sh (verbatim)\n"
        f"  GET /probe/linux-x86_64    release binary or stub\n"
        f"  GET /healthz               liveness\n"
    ).encode()


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "probe-host/0.2"
    pass_hint: str = ""

    def _send(self, status: int, body: bytes, content_type: str, extra_headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _host_header(self) -> str:
        return self.headers.get("Host") or f"localhost:{self.server.server_port}"

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/")
        if path in ("", "/"):
            self._send(200, index_body(self._host_header(), self.pass_hint), "text/plain; charset=utf-8")
            return
        if path == "/healthz":
            self._send(200, b"ok\n", "text/plain; charset=utf-8")
            return
        if path == "/probe":
            self._serve_install()
            return
        if path == "/probe/linux-x86_64" or path == "/probe-linux-x86_64":
            self._serve_binary()
            return
        self._send(404, b"not found\n", "text/plain; charset=utf-8")

    def _serve_install(self) -> None:
        try:
            body = INSTALLER.read_bytes()
        except FileNotFoundError:
            self._send(500, b"install.sh missing\n", "text/plain; charset=utf-8")
            return
        self._send(200, body, "text/x-shellscript; charset=utf-8")

    def _serve_binary(self) -> None:
        try:
            binary_path, ctype = resolve_binary()
        except FileNotFoundError as e:
            self._send(503, f"{e}\n".encode(), "text/plain; charset=utf-8")
            return
        data = binary_path.read_bytes()
        source = "release" if binary_path == BINARY_RELEASE else "stub"
        self._send(200, data, ctype, extra_headers={"X-Probe-Source": source})

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(
            "[%s] %s - %s\n"
            % (self.log_date_time_string(), self.address_string(), fmt % args)
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default=DEFAULT_HOST)
    args = parser.parse_args()

    if not INSTALLER.is_file():
        print(f"FATAL: {INSTALLER} not found", file=sys.stderr)
        return 2

    try:
        served_binary, _ = resolve_binary()
    except FileNotFoundError as e:
        print(f"FATAL: {e}", file=sys.stderr)
        return 2

    Handler.pass_hint = resolve_emqx_pass()
    server = http.server.ThreadingHTTPServer((args.host, args.port), Handler)
    src_tag = "release binary" if served_binary == BINARY_RELEASE else "stub.sh"
    print(
        f"probe-host listening on http://{args.host}:{args.port}/\n"
        f"  serving:    {served_binary}  ({src_tag})\n"
        f"  hostname:   {socket.gethostname()}\n"
        f"  url/user:   baked into binary; only PASS is runtime\n"
        f"  try:        curl http://localhost:{args.port}/  (for the one-liner)",
        file=sys.stderr,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", file=sys.stderr)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
