"""The node's ed25519 identity: one key, minted once, and what the edge learns of it.

What these pin: the key file is 0600 and survives a re-run; ``node_key`` returns
the public half only; ``sign_peer_token`` produces a token the matching public
key verifies for the named audience; ``edge_material`` carries each book peer's
public key and relation, keeps the legacy bearers until
``AWM_PEER_LEGACY_BEARER=0``, and never carries the private key; and every verb
declares an effect.
"""

from __future__ import annotations

import json
import stat

import pytest

from awm.config import peertoken

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


@pytest.fixture()
def auth(awm_workspace, monkeypatch):
    from awm.auth import service, store
    monkeypatch.delenv("AWM_AUTH_PROFILE", raising=False)
    monkeypatch.delenv("AWM_PEER_LEGACY_BEARER", raising=False)
    monkeypatch.setenv("AWM_NODE_NAME", "altair")

    class _S:
        session_ttl_hours = 1.0
        max_session_days = 30.0
        validity_hours = 24.0
        mint_cadence_hours = 12.0
        push_enabled = False
        penpot_rotation_hour = 4
        penpot_rotation_enabled = True

    monkeypatch.setattr(service, "_settings", lambda: _S())
    store.init()
    store.mint_generation(validity_seconds=3600)
    return service


def _book(awm_workspace, book):
    path = awm_workspace / ".awm" / "state" / "peers.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(book))


def test_key_file_is_private_and_minted_once(auth):
    first = auth.ensure_node_key()
    path = auth.node_key_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert auth.ensure_node_key() == first
    assert path.read_text().strip() == first


def test_node_key_returns_only_the_public_half(auth):
    private = auth.ensure_node_key()
    out = auth.h_node_key({})
    assert set(out) == {"public_key", "fingerprint"}
    assert out["public_key"] == peertoken.public_key_of(private)
    assert out["fingerprint"] == peertoken.fingerprint(out["public_key"])
    assert private not in json.dumps(out)


def test_signed_token_verifies_for_its_audience_only(auth):
    public = auth.h_node_key({})["public_key"]
    token = auth.h_sign_peer_token({"aud": "mira"})["token"]
    claims = peertoken.verify(token, public, aud="mira")
    assert claims["iss"] == "altair" and claims["aud"] == "mira"
    with pytest.raises(peertoken.TokenError):
        peertoken.verify(token, public, aud="shaula")


def test_sign_requires_an_audience(auth):
    with pytest.raises(ValueError):
        auth.h_sign_peer_token({})


def test_edge_material_lists_book_peers_without_secrets(auth, awm_workspace):
    peer_public = peertoken.public_key_of(peertoken.generate_private_key())
    _book(awm_workspace, {
        "mira": {"relation": "domestic", "public_key": peer_public},
        "shaula": {"relation": "foreign"},
    })
    mat = auth.h_edge_material({})
    peers = {p["name"]: p for p in mat["peers"]}
    assert peers["mira"] == {"name": "mira", "public_key": peer_public,
                             "relation": "domestic"}
    assert peers["shaula"]["public_key"] is None
    assert peers["shaula"]["relation"] == "foreign"
    assert mat["peer_credentials"]
    assert auth.ensure_node_key() not in json.dumps(mat)


def test_legacy_bearers_are_withheld_when_retired(auth, monkeypatch):
    assert auth.h_edge_material({})["peer_credentials"]
    monkeypatch.setenv("AWM_PEER_LEGACY_BEARER", "1")
    assert auth.h_edge_material({})["peer_credentials"]
    monkeypatch.setenv("AWM_PEER_LEGACY_BEARER", "0")
    mat = auth.h_edge_material({})
    assert mat["peer_credentials"] == []
    assert mat["legacy_bearer"] is False


def test_public_profile_verifies_no_peers(auth, awm_workspace, monkeypatch):
    _book(awm_workspace, {"mira": {"public_key": "x"}})
    monkeypatch.setenv("AWM_AUTH_PROFILE", "public")
    mat = auth.h_edge_material({})
    assert mat["peers"] == [] and mat["peer_credentials"] == []


def test_status_never_carries_the_private_key(auth):
    private = auth.ensure_node_key()
    assert private not in json.dumps(auth.h_status({}))


EDGE_CALLERS = ["peer", "peer:mira", "peer:shaula", "user:tony", "operator"]


@pytest.mark.parametrize("as_", EDGE_CALLERS)
def test_sign_peer_token_is_operator_only(auth, as_):
    with pytest.raises(PermissionError):
        auth.h_sign_peer_token({"aud": "mira"}, as_)
    assert auth.h_sign_peer_token({"aud": "mira"}, None)["token"]
    assert auth.h_sign_peer_token({"aud": "mira"})["token"]


@pytest.mark.parametrize("as_", EDGE_CALLERS)
def test_edge_material_is_operator_only(auth, as_):
    with pytest.raises(PermissionError):
        auth.h_edge_material({}, as_)
    assert auth.h_edge_material({}, None)["secret"]


def test_the_handlers_take_the_caller_identity_from_the_adapter():
    """The adapter passes `as_` only to a handler that declares a second
    positional parameter; without it the guard would never see an identity."""
    import inspect
    from awm.auth import service
    for fn in (service.h_edge_material, service.h_sign_peer_token):
        assert len(inspect.signature(fn).parameters) >= 2, fn.__name__


def test_the_edge_fetches_material_with_no_identity():
    """`AuthGate` calls `gatewayclient.call("auth", "edge_material", {})` with no
    `as_`, which the gateway delivers as `as_=None`: the one caller admitted."""
    from pathlib import Path
    src = (Path(__file__).parents[2] / "httpsfront/awm/httpsfront/auth.py").read_text()
    assert 'call("auth", "edge_material", {})' in src


def test_an_fqdn_node_name_signs_as_its_first_label(auth, monkeypatch):
    monkeypatch.setenv("AWM_NODE_NAME", "Altair.Lab.Example.com")
    public = auth.h_node_key({})["public_key"]
    out = auth.h_sign_peer_token({"aud": "Mira.Lab.Example.com"})
    assert (out["iss"], out["aud"]) == ("altair", "mira")
    assert peertoken.verify(out["token"], public, aud="mira")["iss"] == "altair"


def test_the_key_file_is_linked_into_place_and_leaves_no_temp(auth):
    auth.ensure_node_key()
    names = [p.name for p in auth.node_key_file().parent.iterdir()]
    assert not [n for n in names if n.endswith(".tmp")]


def test_a_losing_racer_keeps_the_winners_key(auth):
    path = auth.node_key_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    winner = peertoken.generate_private_key()
    auth._write_new_key_file(path, winner)
    auth._write_new_key_file(path, peertoken.generate_private_key())
    assert path.read_text().strip() == winner
    assert not [p for p in path.parent.iterdir() if p.name.endswith(".tmp")]


def test_a_key_file_with_loose_permissions_is_refused(auth):
    auth.ensure_node_key()
    auth.node_key_file().chmod(0o640)
    with pytest.raises(auth.NodeKeyError, match="0600"):
        auth.ensure_node_key()
    with pytest.raises(auth.NodeKeyError):
        auth.h_node_key({})


def test_a_key_file_of_the_wrong_length_is_refused(auth):
    import base64
    path = auth.node_key_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(base64.b64encode(b"too short").decode() + "\n")
    path.chmod(0o600)
    with pytest.raises(auth.NodeKeyError, match="32-byte"):
        auth.ensure_node_key()


def test_a_key_file_that_is_not_base64_is_refused(auth):
    path = auth.node_key_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not a key !!\n")
    path.chmod(0o600)
    with pytest.raises(auth.NodeKeyError):
        auth.ensure_node_key()


def test_a_symlinked_key_file_is_refused(auth, tmp_path):
    target = tmp_path / "elsewhere.key"
    target.write_text(peertoken.generate_private_key() + "\n")
    target.chmod(0o600)
    path = auth.node_key_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target)
    with pytest.raises(auth.NodeKeyError, match="regular file"):
        auth.ensure_node_key()


def test_every_verb_declares_an_effect():
    from awm.auth import hub_adapter
    from awm.config import EFFECTS
    names = {f["name"]: f for f in hub_adapter.API_MANIFEST["functions"]}
    assert set(names) == set(hub_adapter.HANDLERS) - {"config_get", "config_set"}
    for name, spec in names.items():
        assert spec.get("effect") in EFFECTS, name
    assert names["node_key"]["effect"] == "read"
    assert names["sign_peer_token"]["effect"] == "secret"
    assert names["status"]["effect"] == "read"
    assert names["edge_material"]["effect"] == "secret"
