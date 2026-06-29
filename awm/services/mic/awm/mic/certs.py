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
so whatever ZeroTier IP the host has is covered without hand-editing.
"""

from __future__ import annotations

import ipaddress
import logging
import os
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
