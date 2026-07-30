"""`mic` shares the trust root, so it must share the rule that protects it.

`httpsfront` was hardened first, but this module is a near-copy of the same cert
code and `mic` is *enabled by default on every node* (no `service.toml` profile
marker, absent from `enabled.json` ⇒ enabled). So if this copy kept the old
"either half missing ⇒ mint" predicate, `mic` would win the boot race on a trust
consumer, put a `ca-key.pem` back, and swap the fleet's root — and httpsfront's
own guard would never fire, because by then both halves exist.

That makes these tests the ones that actually close the hole.
"""

from __future__ import annotations

import shutil

import pytest

from awm.mic import certs

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

needs_openssl = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="openssl not on PATH")

FAKE_ROOT = b"-----BEGIN CERTIFICATE-----\nnot-a-real-root\n-----END CERTIFICATE-----\n"


@pytest.fixture()
def dirs(tmp_path):
    ca = tmp_path / "ca"
    ca.mkdir()
    return ca, tmp_path / ".certs"


def test_a_copied_root_is_never_re_minted(dirs):
    """The whole point: `ca.pem` present, `ca-key.pem` absent, and it stays that
    way — byte-identical, with no key conjured beside it."""
    ca_dir, cert_dir = dirs
    (ca_dir / "ca.pem").write_bytes(FAKE_ROOT)
    with pytest.raises(certs.TrustConsumerError) as e:
        certs.ensure_certs(cert_dir, ca_dir=ca_dir, sans=["IP:10.74.81.213"])
    assert (ca_dir / "ca.pem").read_bytes() == FAKE_ROOT
    assert not (ca_dir / "ca-key.pem").exists()
    # An operator reading this in a service log needs the way out named.
    assert "disable the mic service" in str(e.value)


def test_a_key_without_its_public_root_refuses(dirs):
    ca_dir, cert_dir = dirs
    (ca_dir / "ca-key.pem").write_bytes(b"-----BEGIN PRIVATE KEY-----\nx\n"
                                        b"-----END PRIVATE KEY-----\n")
    with pytest.raises(certs.TrustConsumerError):
        certs.ensure_certs(cert_dir, ca_dir=ca_dir, sans=["IP:127.0.0.1"])


@needs_openssl
def test_a_pre_provisioned_leaf_is_used_as_is(dirs, tmp_path):
    ca_dir, cert_dir = dirs
    certs.ensure_certs(cert_dir, ca_dir=ca_dir, sans=["IP:10.74.81.213"])
    root = (ca_dir / "ca.pem").read_bytes()

    consumer_ca = tmp_path / "consumer-ca"
    consumer_certs = tmp_path / "consumer-certs"
    consumer_ca.mkdir()
    consumer_certs.mkdir()
    (consumer_ca / "ca.pem").write_bytes(root)
    for name in ("cert.pem", "key.pem"):
        shutil.copyfile(cert_dir / name, consumer_certs / name)

    info = certs.ensure_certs(consumer_certs, ca_dir=consumer_ca,
                              sans=["IP:10.74.81.213"])
    assert info["cert"] == str(consumer_certs / "cert.pem")
    assert (consumer_ca / "ca.pem").read_bytes() == root
    assert not (consumer_ca / "ca-key.pem").exists()


@needs_openssl
def test_a_node_that_owns_the_ca_is_unchanged(dirs):
    """The predicate change must not alter the fresh-node path."""
    ca_dir, cert_dir = dirs
    info = certs.ensure_certs(cert_dir, ca_dir=ca_dir, sans=["IP:127.0.0.1"])
    assert (ca_dir / "ca.pem").exists() and (ca_dir / "ca-key.pem").exists()
    assert info["san"] == "IP:127.0.0.1"
    root = (ca_dir / "ca.pem").read_bytes()
    certs.ensure_certs(cert_dir, ca_dir=ca_dir, sans=["IP:127.0.0.1", "DNS:x"])
    assert (ca_dir / "ca.pem").read_bytes() == root


def test_the_two_copies_agree_on_the_predicate():
    """These two files are duplicates by design and diverged once already.

    Read as TEXT, not imported: each dist resolves the `awm` namespace to its own
    source root, so importing `awm.httpsfront` from here would silently pick up
    the deployed copy instead of this tree's — see gateway/scripts/run-tests.sh.
    """
    from pathlib import Path

    here = Path(certs.__file__).resolve()          # .../services/mic/awm/mic/certs.py
    services = here.parents[3]                     # .../awm/services
    twin = services / "httpsfront" / "awm" / "httpsfront" / "certs.py"
    assert twin.exists(), twin

    for path in (here, twin):
        src = path.read_text()
        assert "if not ca_cert.exists() and not ca_key.exists():" in src, path
        assert "class TrustConsumerError" in src, path
