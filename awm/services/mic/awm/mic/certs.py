"""TLS cert provisioning — a local root CA + a short-lived leaf for HTTPS.

``getUserMedia`` requires a secure context, so a phone reaching the bridge over
ZeroTier needs real HTTPS. This mints a local root CA (long-lived) and a leaf
server cert signed by it (<=397 days, the mobile cap), with the host's IPs in
the SAN set.

**Trust reuse:** the CA lives at ``~/.config/remote-audio/ca`` (override with
``REMOTE_AUDIO_CA_DIR``) — the SAME root remote-audio already uses — so a phone
that already trusts that root needs no new setup. The root is never re-minted
once it exists; only the leaf rotates. A reimplementation of remote-audio's
``ensure-certs.sh`` in pure Python so the service is self-contained (it doesn't
need the remote-audio worktree on disk), while sharing the trust root.

SANs are auto-enumerated from the host's non-loopback IPv4 addresses (plus
``127.0.0.1`` / ``localhost``); the leaf is re-minted whenever that set changes,
so whatever ZeroTier IP the host has is covered without hand-editing. Addresses
the host can't see for itself — e.g. the Windows ZeroTier IP a phone reaches a
WSL bridge through — are declared explicitly via ``MIC_EXTRA_SANS`` / a ``.sans``
file and merged in by :func:`resolve_sans`.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("awm.mic.certs")


def _enumerate_ipv4() -> list[str]:
    """Best-effort list of the host's non-loopback IPv4 addresses."""
    ips: list[str] = []
    try:
        out = subprocess.run(
            ["hostname", "-I"], capture_output=True, text=True, timeout=5
        )
        for tok in out.stdout.split():
            try:
                ip = ipaddress.ip_address(tok)
            except ValueError:
                continue
            if ip.version == 4 and not ip.is_loopback and str(ip) not in ips:
                ips.append(str(ip))
    except Exception as exc:  # noqa: BLE001
        log.debug("ipv4 enumeration failed: %s", exc)
    return ips


def default_sans() -> list[str]:
    sans = ["IP:127.0.0.1", "DNS:localhost"]
    sans += [f"IP:{ip}" for ip in _enumerate_ipv4()]
    return sans


def _normalize_san(tok: str) -> str | None:
    """Coerce an operator-supplied SAN token into openssl form.

    Accepts already-prefixed ``IP:x`` / ``DNS:x`` as well as a bare token,
    which becomes ``IP:`` if it parses as an address and ``DNS:`` otherwise.
    Returns ``None`` for empties.
    """
    tok = tok.strip()
    if not tok:
        return None
    if tok.upper().startswith(("IP:", "DNS:")):
        pre, val = tok.split(":", 1)
        val = val.strip()
        return f"{pre.upper()}:{val}" if val else None
    try:
        ipaddress.ip_address(tok)
        return f"IP:{tok}"
    except ValueError:
        return f"DNS:{tok}"


def extra_sans(*, env: str | None = None, san_file: str | Path | None = None) -> list[str]:
    """Operator-declared SANs the host can't auto-enumerate — e.g. the Windows
    ZeroTier IP a phone connects to, which is invisible from inside WSL.

    Sourced from the ``MIC_EXTRA_SANS`` env var (comma/space separated) and an
    optional host-specific ``san_file`` (one token per line, ``#`` comments),
    both optional. Tokens may be bare (``10.147.0.5``) or prefixed
    (``IP:10.147.0.5`` / ``DNS:mic.zt``).
    """
    raw: list[str] = []
    val = env if env is not None else os.environ.get("MIC_EXTRA_SANS", "")
    raw += re.split(r"[,\s]+", val or "")
    if san_file:
        p = Path(san_file)
        if p.exists():
            for line in p.read_text().splitlines():
                raw += re.split(r"[,\s]+", line.split("#", 1)[0])
    out: list[str] = []
    for tok in raw:
        norm = _normalize_san(tok)
        if norm and norm not in out:
            out.append(norm)
    return out


def resolve_sans(*, env: str | None = None, san_file: str | Path | None = None) -> list[str]:
    """Auto-enumerated host SANs plus operator-declared extras, deduped with a
    stable order (defaults first) so the leaf only re-mints on a real change."""
    sans = default_sans()
    for s in extra_sans(env=env, san_file=san_file):
        if s not in sans:
            sans.append(s)
    return sans


def _ca_dir() -> Path:
    env = os.environ.get("REMOTE_AUDIO_CA_DIR")
    if env:
        return Path(env)
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "remote-audio" / "ca"


def ensure_certs(cert_dir, *, ca_dir=None, sans: list[str] | None = None) -> dict:
    """Ensure a CA-signed leaf exists under ``cert_dir``; mint what's missing.

    Returns ``{"cert", "key", "ca", "san"}`` (absolute paths + the SAN string).
    Reuses the shared remote-audio root CA so device trust carries over.
    """
    cert_dir = Path(cert_dir)
    ca_dir = Path(ca_dir) if ca_dir else _ca_dir()
    cert_dir.mkdir(parents=True, exist_ok=True)
    ca_dir.mkdir(parents=True, exist_ok=True)

    ca_key = ca_dir / "ca-key.pem"
    ca_cert = ca_dir / "ca.pem"
    key = cert_dir / "key.pem"
    cert = cert_dir / "cert.pem"
    ca_pub = cert_dir / "ca.pem"          # mirrored so the bridge serves /ca.crt
    san_file = cert_dir / ".san"          # remembers the SAN the leaf was cut for

    san = ",".join(sans if sans is not None else default_sans())

    # 1. Local root CA — long-lived, shared, install-once on devices. Reused if
    #    it already exists (so trust never breaks); minted only the first time.
    if not ca_cert.exists() or not ca_key.exists():
        log.info("minting local root CA at %s (install ca.pem on devices once)", ca_dir)
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", str(ca_key), "-out", str(ca_cert), "-days", "3650",
             "-subj", "/CN=remote-audio local CA",
             "-addext", "basicConstraints=critical,CA:TRUE",
             "-addext", "keyUsage=critical,keyCertSign,cRLSign"],
            check=True,
        )
        os.chmod(ca_key, 0o600)
    shutil.copyfile(ca_cert, ca_pub)

    # 2. Leaf server cert signed by the CA — (re)mint if missing or SAN changed.
    prev_san = san_file.read_text().strip() if san_file.exists() else None
    if not cert.exists() or not key.exists() or prev_san != san:
        log.info("minting leaf cert signed by local CA (SAN=%s)", san)
        csr = cert_dir / "csr.pem"
        ext_file = cert_dir / "leaf.ext"
        subprocess.run(
            ["openssl", "req", "-newkey", "rsa:2048", "-nodes",
             "-keyout", str(key), "-out", str(csr),
             "-subj", "/CN=remote-audio-mic"],
            check=True,
        )
        ext_file.write_text(
            f"subjectAltName={san}\n"
            "basicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature,keyEncipherment\n"
            "extendedKeyUsage=serverAuth\n"
        )
        subprocess.run(
            ["openssl", "x509", "-req", "-in", str(csr), "-CA", str(ca_cert),
             "-CAkey", str(ca_key), "-CAcreateserial", "-days", "397",
             "-out", str(cert), "-extfile", str(ext_file)],
            check=True,
        )
        csr.unlink(missing_ok=True)
        ext_file.unlink(missing_ok=True)
        san_file.write_text(san)

    return {"cert": str(cert), "key": str(key), "ca": str(ca_pub), "san": san}


if __name__ == "__main__":  # quick manual mint: python -m awm.mic.certs <dir>
    logging.basicConfig(level=logging.INFO)
    d = sys.argv[1] if len(sys.argv) > 1 else ".certs"
    print(ensure_certs(d))
