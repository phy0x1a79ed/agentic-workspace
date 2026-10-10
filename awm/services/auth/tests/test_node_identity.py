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
