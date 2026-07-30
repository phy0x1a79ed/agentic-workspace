"""Cert provisioning on a node that holds the root but not the root's key.

The bug: ``ensure_certs`` minted a fresh root whenever *either* half was missing.
``ca.pem`` present without ``ca-key.pem`` is not a broken CA — it is exactly the
state a fleet node is put in on purpose, so it can verify its peers without being
able to mint for them. Re-minting there replaced the fleet's trust root with an
unrelated one, and that surfaced as a certificate error on every peer rather than
as the configuration mistake it was.

The property that matters most: ``ca.pem`` comes back byte-identical.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from awm.httpsfront import certs

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

needs_openssl = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="openssl not on PATH")

FAKE_ROOT = b"-----BEGIN CERTIFICATE-----\nnot-a-real-root\n-----END CERTIFICATE-----\n"


@pytest.fixture()
def dirs(tmp_path):
    ca = tmp_path / "ca"
    cert = tmp_path / ".certs"
    ca.mkdir()
    return ca, cert


# ---------------------------------------------------------------------------
# The refusal — no openssl needed, which is the point: it never gets that far.
# ---------------------------------------------------------------------------

def test_a_copied_root_without_a_leaf_refuses(dirs):
    ca_dir, cert_dir = dirs
    (ca_dir / "ca.pem").write_bytes(FAKE_ROOT)
    with pytest.raises(certs.TrustConsumerError) as e:
        certs.ensure_certs(cert_dir, ca_dir=ca_dir, sans=["IP:10.74.81.213"])
    # The message has to name the fix; nobody debugging this has the context.
    assert "cannot sign" in str(e.value)
    assert "IP:10.74.81.213" in str(e.value)


def test_the_copied_root_is_left_byte_identical(dirs):
    """The whole point. A re-mint here silently breaks every peer."""
    ca_dir, cert_dir = dirs
    (ca_dir / "ca.pem").write_bytes(FAKE_ROOT)
    with pytest.raises(certs.TrustConsumerError):
        certs.ensure_certs(cert_dir, ca_dir=ca_dir, sans=["IP:10.74.81.213"])
    assert (ca_dir / "ca.pem").read_bytes() == FAKE_ROOT
    assert not (ca_dir / "ca-key.pem").exists()


def test_a_half_provisioned_leaf_refuses(dirs):
    """cert.pem without key.pem is unusable and cannot be completed here."""
    ca_dir, cert_dir = dirs
    (ca_dir / "ca.pem").write_bytes(FAKE_ROOT)
    cert_dir.mkdir()
    (cert_dir / "cert.pem").write_bytes(FAKE_ROOT)
    with pytest.raises(certs.TrustConsumerError):
        certs.ensure_certs(cert_dir, ca_dir=ca_dir, sans=["IP:10.74.81.213"])
    assert (ca_dir / "ca.pem").read_bytes() == FAKE_ROOT


def test_a_key_without_its_public_root_refuses(dirs):
    """The mirror case: a key alone cannot regenerate the cert it signed."""
    ca_dir, cert_dir = dirs
    (ca_dir / "ca-key.pem").write_bytes(b"-----BEGIN PRIVATE KEY-----\nx\n"
                                       b"-----END PRIVATE KEY-----\n")
    with pytest.raises(certs.TrustConsumerError) as e:
        certs.ensure_certs(cert_dir, ca_dir=ca_dir, sans=["IP:10.74.81.213"])
    assert "ca.pem" in str(e.value)


# ---------------------------------------------------------------------------
# The happy paths — these do mint, so they need openssl.
# ---------------------------------------------------------------------------

@needs_openssl
def test_an_empty_ca_dir_still_mints_a_root(dirs):
    """Unchanged behaviour for the node that owns the CA."""
    ca_dir, cert_dir = dirs
    info = certs.ensure_certs(cert_dir, ca_dir=ca_dir, sans=["IP:127.0.0.1"])
    assert (ca_dir / "ca.pem").exists()
    assert (ca_dir / "ca-key.pem").exists()
    assert (cert_dir / "cert.pem").exists()
    assert info["san"] == "IP:127.0.0.1"


@needs_openssl
def test_an_existing_root_is_reused_not_replaced(dirs):
    ca_dir, cert_dir = dirs
    certs.ensure_certs(cert_dir, ca_dir=ca_dir, sans=["IP:127.0.0.1"])
    root = (ca_dir / "ca.pem").read_bytes()
    certs.ensure_certs(cert_dir, ca_dir=ca_dir, sans=["IP:127.0.0.1", "DNS:x"])
    assert (ca_dir / "ca.pem").read_bytes() == root


@needs_openssl
def test_a_pre_provisioned_leaf_is_accepted(dirs, tmp_path):
    """The altair case end to end: mint on the CA holder, verify on the consumer.

    The provisioned leaf carries no ``.san`` sidecar — it was cut on another
    host — so acceptance has to come from reading the certificate itself.
    """
    ca_dir, cert_dir = dirs
    # 1. The CA holder mints a root and a leaf for the consumer's addresses.
    certs.ensure_certs(cert_dir, ca_dir=ca_dir, sans=["IP:10.74.81.213"])
    root = (ca_dir / "ca.pem").read_bytes()

    # 2. The consumer gets ca.pem + the leaf pair, and no CA key or sidecar.
    consumer_ca = tmp_path / "consumer-ca"
    consumer_certs = tmp_path / "consumer-certs"
    consumer_ca.mkdir()
    consumer_certs.mkdir()
    (consumer_ca / "ca.pem").write_bytes(root)
    for name in ("cert.pem", "key.pem"):
        shutil.copyfile(cert_dir / name, consumer_certs / name)
    assert not (consumer_certs / ".san").exists()

    info = certs.ensure_certs(consumer_certs, ca_dir=consumer_ca,
                              sans=["IP:10.74.81.213"])
    assert info["cert"] == str(consumer_certs / "cert.pem")
    assert (consumer_ca / "ca.pem").read_bytes() == root
    # ca.pem is mirrored into the cert dir so the front can serve /ca.crt.
    assert (consumer_certs / "ca.pem").read_bytes() == root
    assert not (consumer_ca / "ca-key.pem").exists()


@needs_openssl
def test_partial_san_coverage_warns_but_serves(dirs, tmp_path, caplog):
    """A new docker bridge in `hostname -I` must not take the whole edge down.

    Absence of a leaf is fatal; partial coverage is not — usually it is an
    address no client will ever dial. The log line has to name the fix.
    """
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

    with caplog.at_level("WARNING"):
        info = certs.ensure_certs(
            consumer_certs, ca_dir=consumer_ca,
            sans=["IP:10.74.81.213", "IP:172.18.0.1"])
    assert info["cert"] == str(consumer_certs / "cert.pem")
    assert "172.18.0.1" in caplog.text
    assert "Re-mint" in caplog.text


# ---------------------------------------------------------------------------
# cert_sans
# ---------------------------------------------------------------------------

@needs_openssl
def test_cert_sans_reads_the_set_back_out(dirs):
    ca_dir, cert_dir = dirs
    certs.ensure_certs(cert_dir, ca_dir=ca_dir,
                       sans=["IP:127.0.0.1", "DNS:localhost", "IP:10.74.81.213"])
    got = certs.cert_sans(cert_dir / "cert.pem")
    assert got == {"IP:127.0.0.1", "DNS:localhost", "IP:10.74.81.213"}


def test_cert_sans_of_a_missing_file_is_empty(tmp_path):
    assert certs.cert_sans(tmp_path / "nope.pem") == set()


def test_cert_sans_survives_openssl_being_absent(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise FileNotFoundError("openssl")
    monkeypatch.setattr(subprocess, "run", _boom)
    assert certs.cert_sans(tmp_path / "any.pem") == set()


# ---------------------------------------------------------------------------
# The wrong pair, and the clock — the two things that actually go wrong on a
# node that can never re-mint for itself.
# ---------------------------------------------------------------------------

def _provision(tmp_path, src_cert_dir, root, *, cert_from=None, key_from=None):
    """Lay out a trust-consumer node: ca.pem only, plus a leaf pair."""
    ca = tmp_path / "consumer-ca"
    cd = tmp_path / "consumer-certs"
    ca.mkdir()
    cd.mkdir()
    (ca / "ca.pem").write_bytes(root)
    shutil.copyfile((cert_from or src_cert_dir) / "cert.pem", cd / "cert.pem")
    shutil.copyfile((key_from or src_cert_dir) / "key.pem", cd / "key.pem")
    return ca, cd


@needs_openssl
def test_a_leaf_from_a_different_ca_refuses(dirs, tmp_path):
    """The easy provisioning mistake: right filenames, wrong root."""
    ca_dir, cert_dir = dirs
    certs.ensure_certs(cert_dir, ca_dir=ca_dir, sans=["IP:10.74.81.213"])
    root = (ca_dir / "ca.pem").read_bytes()

    # A second, unrelated CA + leaf.
    other_ca = tmp_path / "other-ca"
    other_certs = tmp_path / "other-certs"
    other_ca.mkdir()
    certs.ensure_certs(other_certs, ca_dir=other_ca, sans=["IP:10.74.81.213"])

    ca, cd = _provision(tmp_path, other_certs, root)
    with pytest.raises(certs.TrustConsumerError) as e:
        certs.ensure_certs(cd, ca_dir=ca, sans=["IP:10.74.81.213"])
    assert "chain" in str(e.value)


@needs_openssl
def test_a_mismatched_key_refuses(dirs, tmp_path):
    """cert.pem from one mint, key.pem from another — both chain, neither works."""
    ca_dir, cert_dir = dirs
    certs.ensure_certs(cert_dir, ca_dir=ca_dir, sans=["IP:10.74.81.213"])
    root = (ca_dir / "ca.pem").read_bytes()
    second = tmp_path / "second"
    certs.ensure_certs(second, ca_dir=ca_dir, sans=["IP:10.74.81.99"])

    ca, cd = _provision(tmp_path, cert_dir, root, key_from=second)
    with pytest.raises(certs.TrustConsumerError) as e:
        certs.ensure_certs(cd, ca_dir=ca, sans=["IP:10.74.81.213"])
    assert "private key" in str(e.value)


@needs_openssl
def test_an_expired_leaf_refuses_rather_than_serving(dirs, tmp_path, monkeypatch):
    """Nothing on a trust consumer ever re-cuts the leaf, so expiry is silent
    unless somebody looks. Refuse loudly instead of serving a dead cert."""
    ca_dir, cert_dir = dirs
    certs.ensure_certs(cert_dir, ca_dir=ca_dir, sans=["IP:10.74.81.213"])
    root = (ca_dir / "ca.pem").read_bytes()
    ca, cd = _provision(tmp_path, cert_dir, root)

    monkeypatch.setattr(certs, "cert_days_left", lambda p: -3)
    with pytest.raises(certs.TrustConsumerError) as e:
        certs.ensure_certs(cd, ca_dir=ca, sans=["IP:10.74.81.213"])
    assert "expired 3 day(s) ago" in str(e.value)


@needs_openssl
def test_a_soon_to_expire_leaf_warns_with_lead_time(dirs, tmp_path, monkeypatch,
                                                    caplog):
    ca_dir, cert_dir = dirs
    certs.ensure_certs(cert_dir, ca_dir=ca_dir, sans=["IP:10.74.81.213"])
    root = (ca_dir / "ca.pem").read_bytes()
    ca, cd = _provision(tmp_path, cert_dir, root)

    monkeypatch.setattr(certs, "cert_days_left", lambda p: 12)
    with caplog.at_level("WARNING"):
        certs.ensure_certs(cd, ca_dir=ca, sans=["IP:10.74.81.213"])
    assert "expires in 12 day(s)" in caplog.text


@needs_openssl
def test_the_reported_san_is_what_the_leaf_carries(dirs, tmp_path):
    """`httpsfront status` reads this. Reporting the request instead of the cert
    would confirm coverage the served leaf does not have — right after the drift
    warning fires, which is exactly when an operator looks."""
    ca_dir, cert_dir = dirs
    certs.ensure_certs(cert_dir, ca_dir=ca_dir, sans=["IP:10.74.81.213"])
    root = (ca_dir / "ca.pem").read_bytes()
    ca, cd = _provision(tmp_path, cert_dir, root)

    info = certs.ensure_certs(cd, ca_dir=ca,
                             sans=["IP:10.74.81.213", "IP:172.18.0.1"])
    assert info["san"] == "IP:10.74.81.213"
    assert "172.18.0.1" not in info["san"]


@needs_openssl
def test_cert_days_left_on_a_fresh_leaf(dirs):
    ca_dir, cert_dir = dirs
    certs.ensure_certs(cert_dir, ca_dir=ca_dir, sans=["IP:127.0.0.1"])
    days = certs.cert_days_left(cert_dir / "cert.pem")
    assert days is not None and 390 <= days <= 397


def test_cert_days_left_of_a_missing_file_is_none(tmp_path):
    assert certs.cert_days_left(tmp_path / "nope.pem") is None


@needs_openssl
def test_leaf_matches_ca_accepts_a_real_pair(dirs):
    ca_dir, cert_dir = dirs
    certs.ensure_certs(cert_dir, ca_dir=ca_dir, sans=["IP:127.0.0.1"])
    assert certs.leaf_matches_ca(cert_dir / "cert.pem", cert_dir / "key.pem",
                                 ca_dir / "ca.pem") is None
