"""What a foreign peer learns from `kb status`."""

from awm.kb import hub_adapter


async def test_foreign_status_says_only_that_the_server_is_up(monkeypatch):
    async def full():
        return {"encoder": {"state": "ready"}, "serve": {"entries": {"path": "/secret/path"}}}

    def boom():
        raise AssertionError("child snapshot must not be read for a foreign caller")

    monkeypatch.setattr(hub_adapter.client, "status", full)
    monkeypatch.setattr(hub_adapter.CHILD, "snapshot", boom)
    monkeypatch.setattr(hub_adapter.config, "peer_relation", lambda n: "foreign")
    assert await hub_adapter.HANDLERS["status"]({}, as_="peer:x") == {"kb": {"up": True}}


async def test_foreign_status_hides_a_failure(monkeypatch):
    async def down():
        raise RuntimeError("/secret/path refused")

    monkeypatch.setattr(hub_adapter.client, "status", down)
    monkeypatch.setattr(hub_adapter.config, "peer_relation", lambda n: "foreign")
    assert await hub_adapter.HANDLERS["status"]({}, as_="peer:x") == {"kb": {"error": "unavailable"}}
