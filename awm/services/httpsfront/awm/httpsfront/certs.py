"""TLS cert provisioning — a local root CA + a short-lived leaf for HTTPS.

``getUserMedia`` (and other powerful web APIs) require a *secure context*, so a
phone or laptop reaching the awm gateway off-localhost needs real HTTPS. This
mints a local root CA (long-lived) and a leaf server cert signed by it (<=397
days, the mobile cap), with the host's IPs in the SAN set.

**Trust reuse:** the CA lives at ``~/.config/remote-audio/ca`` (override with
``REMOTE_AUDIO_CA_DIR``) — the SAME root remote-audio already uses — so a device
that already trusts that root needs no new setup. The root is never re-minted
once it exists; only the leaf rotates.

This is the only copy of this code in awm, and ``tests/test_single_cert_module``
keeps it that way. ``mic`` carried a near-copy while it ran its own off-host
listener; the two diverged on the trust-consumer predicate below, and mic — which
is enabled by default on every node — could win the boot race and put a signing
key back before this module's guard ever ran.

**Trust consumers:** a fleet node is given ``ca.pem`` and deliberately *not*
``ca-key.pem``, so it can verify its peers but cannot mint for the fleet. There
this module provisions nothing — it validates a leaf minted on the node that
holds the key. That distinction is load-bearing: treating a missing key as a
missing CA is how a copied root gets silently replaced.

SANs are auto-enumerated from the host's non-loopback IPv4 addresses (plus
``127.0.0.1`` / ``localhost``); the leaf is re-minted whenever that set changes,
so whatever ZeroTier/LAN IP the host has is covered without hand-editing.
Addresses the host can't see for itself — e.g. the Windows ZeroTier IP a phone
reaches a WSL host through — are declared explicitly via ``AWM_TLS_EXTRA_SANS``
/ a ``.sans`` file and merged in by :func:`resolve_sans`.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("awm.httpsfront.certs")


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

    Sourced from the ``AWM_TLS_EXTRA_SANS`` env var (comma/space separated) and
    an optional host-specific ``san_file`` (one token per line, ``#`` comments),
    both optional. Tokens may be bare (``10.147.0.5``) or prefixed
    (``IP:10.147.0.5`` / ``DNS:notes.zt``).
    """
    raw: list[str] = []
    val = env if env is not None else os.environ.get("AWM_TLS_EXTRA_SANS", "")
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


def cert_sans(cert_path: str | Path) -> set[str]:
    """The SAN tokens an existing leaf actually carries, in openssl input form.

    Reads them back out of the certificate rather than trusting the ``.san``
    sidecar, because a leaf minted on another host arrives without one.
    """
    try:
        out = subprocess.run(
            ["openssl", "x509", "-in", str(cert_path), "-noout",
             "-ext", "subjectAltName"],
            capture_output=True, text=True, timeout=10, check=True,
        )
    except Exception as exc:  # noqa: BLE001 — unreadable cert / no openssl
        log.debug("could not read SANs from %s: %s", cert_path, exc)
        return set()
    found: set[str] = set()
    # openssl prints e.g. "    IP Address:10.74.81.213, DNS:localhost"
    for tok in re.split(r"[,\s]*\n\s*|,\s*", out.stdout):
        tok = tok.strip()
        if tok.startswith("IP Address:"):
            found.add("IP:" + tok.split(":", 1)[1].strip())
        elif tok.upper().startswith("DNS:"):
            found.add("DNS:" + tok.split(":", 1)[1].strip())
    return found


def cert_days_left(cert_path: str | Path) -> int | None:
    """Days until the certificate expires; negative if already expired.

    ``None`` when it cannot be read. On the node that holds the CA key, expiry
    self-heals (the leaf is re-cut whenever the SAN set moves); on a trust
    consumer nothing ever re-cuts it, so somebody has to look.
    """
    try:
        out = subprocess.run(
            ["openssl", "x509", "-in", str(cert_path), "-noout", "-enddate"],
            capture_output=True, text=True, timeout=10, check=True,
        )
        _, _, raw = out.stdout.strip().partition("=")
        expires = datetime.strptime(raw.strip(), "%b %d %H:%M:%S %Y %Z").replace(
            tzinfo=timezone.utc)
    except Exception as exc:  # noqa: BLE001 — unreadable cert / no openssl
        log.debug("could not read expiry from %s: %s", cert_path, exc)
        return None
    return (expires - datetime.now(timezone.utc)).days


def leaf_matches_ca(cert_path: str | Path, key_path: str | Path,
                    ca_path: str | Path) -> str | None:
    """``None`` if the leaf chains to ``ca_path`` and ``key_path`` is its key;
    else a short reason. ``None`` also when openssl is unavailable — this is a
    confirmation step, not a gate we can enforce without the tool.

    Copying the wrong pair onto a node produces exactly the "certificate error
    rather than config error" the trust-consumer rule exists to prevent, and it
    is the easy mistake to make when provisioning by hand.
    """
    def _run(argv: list[str]) -> subprocess.CompletedProcess | None:
        try:
            return subprocess.run(argv, capture_output=True, text=True, timeout=10)
        except Exception as exc:  # noqa: BLE001
            log.debug("openssl unavailable for %s: %s", argv[:2], exc)
            return None

    verify = _run(["openssl", "verify", "-CAfile", str(ca_path), str(cert_path)])
    if verify is None:
        return None
    if verify.returncode != 0:
        return f"does not chain to {ca_path}: {verify.stdout.strip() or verify.stderr.strip()}"
    cert_pub = _run(["openssl", "x509", "-in", str(cert_path), "-noout", "-pubkey"])
    key_pub = _run(["openssl", "pkey", "-in", str(key_path), "-pubout"])
    if cert_pub is None or key_pub is None:
        return None
    if cert_pub.returncode or key_pub.returncode:
        return f"{key_path} is not readable as the private key for {cert_path}"
    if cert_pub.stdout.strip() != key_pub.stdout.strip():
        return f"{key_path} is not the private key for {cert_path}"
    return None


class TrustConsumerError(RuntimeError):
    """The CA halves are in a state this node must not resolve by minting.

    Either the public root is present without its key and no leaf was
    provisioned, or the key is present without the root. Both are raised rather
    than minted through, because minting would replace the fleet's trust root
    with an unrelated one — and that surfaces as a certificate error on every
    peer rather than as the configuration mistake it is.
    """


def ensure_certs(cert_dir, *, ca_dir=None, sans: list[str] | None = None) -> dict:
    """Ensure a CA-signed leaf exists under ``cert_dir``; mint what's missing.

    Returns ``{"cert", "key", "ca", "san"}`` (absolute paths + the SAN string).
    Reuses the shared remote-audio root CA so device trust carries over.

    A node holding ``ca.pem`` **without** ``ca-key.pem`` is a deliberate
    configuration — a *trust consumer*, given the public root so it can verify
    its peers and denied the signing key so it cannot mint for the fleet. There
    this function provisions nothing and validates instead; see
    :class:`TrustConsumerError`.
    """
    cert_dir = Path(cert_dir)
    ca_dir = Path(ca_dir) if ca_dir else _ca_dir()
    cert_dir.mkdir(parents=True, exist_ok=True)
    ca_dir.mkdir(parents=True, exist_ok=True)

    ca_key = ca_dir / "ca-key.pem"
    ca_cert = ca_dir / "ca.pem"
    key = cert_dir / "key.pem"
    cert = cert_dir / "cert.pem"
    ca_pub = cert_dir / "ca.pem"          # mirrored so the front serves /ca.crt
    san_file = cert_dir / ".san"          # remembers the SAN the leaf was cut for

    san = ",".join(sans if sans is not None else default_sans())

    # 1. Local root CA — long-lived, shared, install-once on devices. Minted only
    #    when NEITHER half is present. The old test ("either half missing") also
    #    fired on a trust consumer and silently overwrote the fleet's root with
    #    an unrelated one, which then surfaced as a certificate error on every
    #    peer rather than as the config mistake it was.
    if not ca_cert.exists() and not ca_key.exists():
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
    elif not ca_cert.exists():
        raise TrustConsumerError(
            f"{ca_key} exists but {ca_cert} does not — the root's public half is "
            f"missing and cannot be regenerated from the key alone. Restore "
            f"{ca_cert} from the node that holds the CA, or remove {ca_key} to "
            f"mint a new (fleet-incompatible) root deliberately."
        )
    shutil.copyfile(ca_cert, ca_pub)

    # 2. Leaf server cert signed by the CA — (re)mint if missing or SAN changed.
    prev_san = san_file.read_text().strip() if san_file.exists() else None
    if not ca_key.exists():
        # Trust consumer: validate the pre-provisioned leaf, provision nothing.
        required = set(sans if sans is not None else default_sans())
        actual = cert_sans(cert) if cert.exists() else set()
        if not cert.exists() or not key.exists():
            raise TrustConsumerError(
                f"{ca_cert} is present without {ca_key}, so this node cannot "
                f"sign — but no leaf was provisioned at {cert} / {key}. Mint one "
                f"on the node holding the CA key with SAN={san} and copy both "
                f"files here."
            )
        # Wrong pair beats missing pair for confusion value: a leaf that doesn't
        # chain to the ca.pem beside it, or a key that isn't its key, presents as
        # a client-side certificate error with nothing in the logs. Refuse.
        if mismatch := leaf_matches_ca(cert, key, ca_cert):
            raise TrustConsumerError(
                f"the provisioned leaf at {cert} {mismatch}. Copy the matching "
                f"cert/key pair from the node holding {ca_key}."
            )
        # Expiry, which nothing on this node will ever fix on its own — the leaf
        # is only re-cut where the CA key lives.
        days = cert_days_left(cert)
        if days is not None and days < 0:
            raise TrustConsumerError(
                f"the provisioned leaf at {cert} expired {-days} day(s) ago and "
                f"cannot be re-minted here. Cut a fresh one on the node holding "
                f"{ca_key} with SAN={san} and copy it over."
            )
        if days is not None and days < 30:
            log.warning("the leaf at %s expires in %d day(s) and can only be "
                        "renewed on the node holding %s", cert, days, ca_key)
        # Drift warns where absence refuses, and the asymmetry is deliberate: a
        # missing leaf means the listener cannot come up at all, while partial
        # coverage usually means a new docker bridge or veth appeared in
        # `hostname -I` that no client will ever dial. Refusing on drift would
        # take the whole edge — and with it every page — down for an address
        # nobody uses. The log line names the fix; the node keeps serving the
        # addresses it does cover.
        if missing := required - actual:
            log.warning(
                "the provisioned leaf at %s does not cover %s; clients dialing "
                "those addresses will see a certificate error. Re-mint on the "
                "node holding %s with SAN=%s and copy it here.",
                cert, ",".join(sorted(missing)), ca_key, san,
            )
        log.info("using pre-provisioned leaf at %s (no CA key on this node)", cert)
        # Report what the leaf ACTUALLY carries, not what was asked for: this is
        # the value `httpsfront status` surfaces, and it is the first thing an
        # operator checks after a client-side cert error. Answering with the
        # request would confirm coverage the served cert does not have.
        return {"cert": str(cert), "key": str(key), "ca": str(ca_pub),
                "san": ",".join(sorted(actual)) or san}

    if not cert.exists() or not key.exists() or prev_san != san:
        log.info("minting leaf cert signed by local CA (SAN=%s)", san)
        csr = cert_dir / "csr.pem"
        ext_file = cert_dir / "leaf.ext"
        subprocess.run(
            ["openssl", "req", "-newkey", "rsa:2048", "-nodes",
             "-keyout", str(key), "-out", str(csr),
             "-subj", "/CN=awm-https-front"],
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


if __name__ == "__main__":  # quick manual mint: python -m awm.httpsfront.certs <dir>
    logging.basicConfig(level=logging.INFO)
    d = sys.argv[1] if len(sys.argv) > 1 else ".certs"
    print(ensure_certs(d))
