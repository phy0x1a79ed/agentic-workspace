"""Who may act on the shared vault.

The vault is shared, so every write verb is one person's button acting on
everyone's work and `restore` discards it. Those verbs belong to whoever can
reach the host, not to whoever can reach the page.

The public edge's allow-list is *not* what enforces this. A mesh node's edge
runs no profile and never consults `policy`, so on altair the allow-list
protects nothing at all. The gate has to be in the handler, and these tests are
what say so.
"""

from __future__ import annotations

import asyncio

import pytest

from awm.trilium import etapi, hub_adapter, instances

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


@pytest.fixture(autouse=True)
def _off_the_real_vault(tmp_path, monkeypatch):
    """Point the singleton at a throwaway path for every test in this file.

    These tests invoke the write verbs to prove the *gate*, not the work, and
    several of them create directories before they fail. Against the live
    singleton that scaffolds `live/` under a path that is not yet a git
    worktree — which is precisely what makes `git worktree add` refuse
    afterwards. It cost a hand cleanup once; a fixture is cheaper than
    remembering.
    """
    monkeypatch.setattr(instances, "VAULT",
                        instances.Vault(scope=tmp_path / "vault"))

    # The note verbs reach ETAPI as soon as the gate admits them, and a live
    # vault on loopback would make these tests pass or fail on whether this
    # host happens to be serving one. Refusing the client keeps the assertion
    # on the gate: everything past it fails the same way, which is what the
    # `except Exception` below already tolerates.
    def _no_vault() -> None:
        raise etapi.EtapiError("no vault in this test")
    monkeypatch.setattr(etapi, "client", _no_vault)

#: Reachable from a browser. Read-only, and the vault's own recovery state.
#: Named for where a caller may be rather than for what the verb does, because
#: the note API made "read" and "reachable" two different questions: its read
#: verbs are operator-only too.
EDGE_VERBS = ["status", "snapshots", "url"]

#: Everything that starts, stops, reads a log from, rebuilds or replaces the
#: shared vault -- and the whole note API, reads included.
#:
#: A note read looks harmless and is not. The edge does not forward `/etapi/`
#: precisely so that vault-origin JavaScript cannot walk the vault, and an open
#: `note_get` would be that same surface wearing awm's name. The person reading
#: the vault already has all of it in front of them, so nothing is lost by
#: keeping these on the host.
OPERATOR_VERBS = ["start", "stop", "restart", "provision", "logs",
                  "snapshot", "export", "restore", "note_upsert",
                  "note_get", "note_children", "note_search",
                  "note_create", "note_update", "note_delete",
                  "note_move", "note_clone", "note_place",
                  "attrs_get", "attr_set", "attr_delete", "attachment_put",
                  "board_ensure", "board_cards", "card_upsert"]

#: What the edge stamps. `_as_header` never emits an empty value, so any of
#: these means the call crossed an edge listener.
EDGE_IDENTITIES = ["user:tony", "user:steven", "user:operator", "peer"]


def test_the_two_lists_are_the_whole_surface():
    """A verb added later is in neither list, and this fails until someone has
    decided which it is. Defaulting to unreachable is the point."""
    assert set(EDGE_VERBS) | set(OPERATOR_VERBS) == set(hub_adapter.HANDLERS)


@pytest.mark.parametrize("verb", OPERATOR_VERBS)
@pytest.mark.parametrize("as_", EDGE_IDENTITIES)
def test_a_caller_from_an_edge_cannot_act_on_the_vault(verb, as_):
    with pytest.raises(PermissionError, match="operator verb"):
        asyncio.run(hub_adapter.HANDLERS[verb]({}, as_))


@pytest.mark.parametrize("verb", OPERATOR_VERBS)
def test_the_console_still_works(verb):
    """`as_ is None` is the host's own CLI: `/invoke` on loopback sends no
    identity header, and the edge always sends one. It is the only way to fix a
    broken vault, so it must not be refused."""
    try:
        asyncio.run(hub_adapter.HANDLERS[verb]({}, None))
    except PermissionError:  # pragma: no cover — the failure this test is for
        pytest.fail(f"{verb} refused the operator")
    except Exception:  # noqa: BLE001 — no vault on disk here; only the gate matters
        pass


@pytest.mark.parametrize("as_", EDGE_IDENTITIES)
def test_reading_is_open_to_anyone_signed_in(as_):
    """A collaborator needs to see whether the vault is up and whether there is
    a copy to recover from. Neither fact is anyone's private business."""
    for verb in EDGE_VERBS:
        try:
            asyncio.run(hub_adapter.HANDLERS[verb]({}, as_))
        except PermissionError:  # pragma: no cover
            pytest.fail(f"{verb} refused a signed-in caller")
        except Exception:  # noqa: BLE001 — no vault on disk; only the gate matters
            pass


def test_status_sheds_the_operators_half_for_a_signed_in_caller():
    """Pids, ports and absolute paths are an operator's business. On a public
    host they are gratuitous, and a path is a small map of the machine."""
    through_edge = asyncio.run(hub_adapter.HANDLERS["status"]({}, "user:tony"))
    from_console = asyncio.run(hub_adapter.HANDLERS["status"]({}, None))
    assert "source" not in through_edge and "source" in from_console
    for key in ("pid", "port", "scope", "data_dir", "log"):
        assert key not in through_edge["vault"], key
        assert key in from_console["vault"], key


def test_the_vaults_address_names_no_host_or_port():
    """It is the same origin as the page asking. A host and a port here would
    be a guess, and the retired one guessed wrong on any multi-homed node."""
    out = asyncio.run(hub_adapter.HANDLERS["url"]({}, "user:tony"))
    assert out == {"path": "/trilium/"}
