"""The mesh front's contract with the dashboard.

The front is the whole reason this service can put the GUI in front of a browser
at all, and it rests on three arguments to one call. Two of them fail loudly if
wrong. The third, ``rewrite_origin``, does not: without it the dashboard's
Host/Origin guard refuses every WebSocket upgrade while HTTP keeps working
perfectly, so the GUI loads, looks right, and silently never streams. That is
the failure this file exists to make impossible to ship.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


@pytest.fixture()
def front():
    """Imported lazily: ``awm.hermes.front`` pulls in ``awm.httpsfront``, and a
    cross-dist import at module scope re-triggers the namespace shadowing the
    per-dist runner exists to avoid."""
    from awm.hermes import front as mod
    return mod


class _Stop(BaseException):
    """Breaks the deliberately-infinite supervision loop.

    A ``BaseException`` on purpose: ``_serve_forever`` swallows every
    ``Exception`` by design, so a normal error cannot end the loop and a test
    that raised one would hang instead of failing.
    """


@pytest.fixture()
def one_pass(front, monkeypatch):
    """Run exactly one iteration of the supervision loop.

    Returns the kwargs ``proxy.serve`` was called with. ``serve`` blocks
    forever in production, so the stub records and stops instead.
    """
    def _run(serve_impl):
        captured: dict = {}

        def _serve(**kwargs):
            captured.update(kwargs)
            return serve_impl()

        monkeypatch.setattr(front, "_borrow_leaf", lambda: None)
        monkeypatch.setattr(front.certs, "resolve_sans", lambda **k: ["IP:10.0.0.1"])
        monkeypatch.setattr(front.certs, "ensure_certs", lambda d, **k: {
            "cert": "/x/cert.pem", "key": "/x/key.pem", "ca": "/x/ca.pem",
            "san": "IP:10.0.0.1",
        })
        monkeypatch.setattr(front.proxy, "serve", _serve)
        # The loop only sleeps on its way round again, so this is the exit.
        monkeypatch.setattr(front.time, "sleep", lambda s: (_ for _ in ()).throw(_Stop()))
        with pytest.raises(_Stop):
            front._serve_forever()
        return captured

    return _run


# -- what the front is pointed at -------------------------------------------


def test_serve_is_given_the_loopback_dashboard_at_its_root(front, one_pass):
    """Serving the dashboard at `/` is the fix, not a detail: its bundle
    resolves lazy chunks against the server root, so any other mount point
    404s them."""
    kwargs = one_pass(lambda: None)
    assert kwargs["upstream"] == f"http://127.0.0.1:{front.daemon.PORT}/"
    assert kwargs["port"] == front.PORT


def test_serve_rewrites_origin(front, one_pass):
    """Without this the dashboard's WS guard sees a mesh origin against a
    loopback bind and refuses every upgrade — HTTP-only, so it presents as a
    GUI that loads and never streams."""
    assert one_pass(lambda: None)["rewrite_origin"] is True


def test_serve_does_not_take_the_landing_page(front, one_pass):
    """`/` belongs to the dashboard's SPA, not to awm's page index."""
    assert one_pass(lambda: None)["landing"] is False


# -- what it reports --------------------------------------------------------


def test_origin_uses_the_mesh_address_not_the_first_local_one(front, monkeypatch):
    """This host also carries a LAN address and a docker bridge; a link to
    either goes nowhere from the device the page is read on."""
    monkeypatch.setattr(front.config, "mesh_address", lambda: "10.74.81.84")
    assert front.origin() == "https://10.74.81.84:12401"


def test_origin_degrades_to_loopback_off_the_mesh(front, monkeypatch):
    monkeypatch.setattr(front.config, "mesh_address", lambda: None)
    assert front.origin() == f"https://127.0.0.1:{front.PORT}"


def test_landing_url_is_the_declared_edge_plus_the_prefix(front, monkeypatch):
    """`AWM_EDGE_URL` in the environment is the preferred form and wins over
    everything below it — that is what a production service inherits."""
    monkeypatch.setenv("AWM_EDGE_URL", "https://10.0.0.1:12100/")
    assert front.landing_url() == f"https://10.0.0.1:12100{front.LANDING_PREFIX}/"


def test_landing_url_degrades_to_the_bare_prefix_without_an_edge(front, monkeypatch):
    monkeypatch.delenv("AWM_EDGE_URL", raising=False)
    monkeypatch.setattr(front, "_declared_edge", lambda: "")
    monkeypatch.setattr(front.config, "edge_url", lambda: None)
    assert front.landing_url() == f"{front.LANDING_PREFIX}/"


def test_landing_url_reads_the_workspace_env_when_it_is_not_inherited(
    front, monkeypatch, tmp_path,
):
    """A shadow overlay runs against an isolated `.awm-shadow` root and does not
    inherit the daemon's env, so `config.edge_url` falls back to guessing
    AWM_HTTPS_PORT's default — a link that goes nowhere on a node whose edge is
    on another port. The canonical workspace's env file is the node's own
    answer, so read it rather than guess."""
    monkeypatch.delenv("AWM_EDGE_URL", raising=False)
    (tmp_path / ".awm").mkdir()
    (tmp_path / ".awm" / "env").write_text(
        "# a comment\nexport AWM_EDGE_URL=https://10.74.81.84:12100\nOTHER=1\n")
    monkeypatch.setattr(front.config, "canonical_workspace", lambda: tmp_path)
    monkeypatch.setattr(front.config, "edge_url", lambda: "https://10.74.81.84:8443")
    assert front.landing_url() == "https://10.74.81.84:12100/ui/hermes/"


def test_a_failed_listener_is_reported_rather_than_fatal(front, one_pass):
    """The registration, the verbs and the dashboard all stay useful when the
    front is the broken part — `status` is how anyone finds out which."""
    one_pass(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    st = front.status()
    assert st["serving"] is False
    assert "RuntimeError: boom" in st["error"]


def test_a_returning_listener_is_reported_as_not_serving(front, one_pass):
    """`proxy.serve` blocks forever; a return means the listener fell over."""
    one_pass(lambda: None)
    assert front.status()["serving"] is False


# -- where the leaf comes from ----------------------------------------------


def test_leaf_is_borrowed_from_the_canonical_workspace_as_well_as_the_local_tree(
    front, monkeypatch,
):
    """This node is a trust consumer — it holds `ca.pem` without the signing
    key, so `ensure_certs` provisions nothing and validates a pre-placed leaf
    instead. A worktree carries no `.certs` of its own (gitignored state), so
    shadowing this service out of one has to reach the real workspace's copy or
    the front cannot come up at all.

    Pinned against `canonical_workspace`, not `WORKSPACE_ROOT`: under a shadow
    the local root is an isolated `.awm-shadow` that holds no certs, and reading
    it is a silent no-op that presents as a TrustConsumerError loop."""
    monkeypatch.setattr(front.config, "canonical_workspace",
                        lambda: front.Path("/ws"))
    sources = front._leaf_sources()
    assert front.SERVICE_DIR.parent / "httpsfront" / ".certs" in sources
    assert front.Path("/ws/awm/services/httpsfront/.certs") in sources


def test_service_dir_is_the_dist_root_not_its_namespace_dir(front):
    """`.certs` and `.sans` live beside `run.sh`. The dist nests its PEP 420
    namespace as `<service>/awm/hermes/`, so an off-by-one here silently
    scatters cert state into the package directory."""
    assert front.SERVICE_DIR.name == "hermes"
    assert (front.SERVICE_DIR / "run.sh").is_file()
