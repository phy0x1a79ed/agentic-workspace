"""IFF deletion-regression: the retired conversational + skills machinery must
stay gone. These assert on the source tree itself so a future re-introduction
(a resurrected scope-delivery loop, a conversational create_session default, an
agent_spawn op, the skills service) trips a test rather than silently returning.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

pytestmark = [pytest.mark.agent, pytest.mark.smoke]

import awm.agents.agent_instances as ai_mod
from awm.agents import hub_adapter


def _agents_pkg_root() -> Path:
    return Path(ai_mod.__file__).resolve().parent


class TestConversationalRetired:
    def test_scope_delivery_machinery_gone(self):
        for name in ("_scope_delivery_loop", "_resync_scope_posts",
                     "_maybe_deliver_post", "_ensure_scope_exists",
                     "_post_to_scope", "respawn_after_restart",
                     "start_resume_driver", "scrub_resume_queue_for_scope"):
            assert not hasattr(ai_mod, name), f"{name} should be deleted"

    def test_orchestration_module_gone(self):
        assert not (_agents_pkg_root() / "orchestration.py").exists()

    def test_create_session_requires_a_workdir(self):
        # No conversational fallback to a git-scope home: workdir is mandatory and
        # the default mode is a placement mode (not 'conversational').
        sig = inspect.signature(ai_mod.create_session)
        assert sig.parameters["mode"].default == "worker"
        src = inspect.getsource(ai_mod.create_session)
        assert "a placement requires a workdir" in src
        assert "PROJECTS_DIR / project / scope" not in src

    def test_no_conversational_in_runtime_source(self):
        # The DB column default ('conversational') may persist for legacy rows,
        # but no RUNTIME branch should switch on it anymore.
        src = inspect.getsource(ai_mod)
        assert 'mode != "conversational"' not in src
        assert '== "conversational"' not in src

    def test_agent_spawn_op_gone(self):
        assert "create_session" not in hub_adapter.HANDLERS
        tools = {f.get("tool") or f["name"]
                 for f in hub_adapter.API_MANIFEST["functions"]}
        assert "agent_spawn" not in tools


class TestSkillsServiceRetired:
    def test_skills_service_dir_deleted(self):
        # repo root is .../awm/services/agents/awm/agents → up to <repo>/awm/services
        services = _agents_pkg_root().parents[2]
        assert services.name == "services"
        assert not (services / "skills").exists(), \
            "the skills service dist must be deleted"

    def test_config_skills_dir_is_plain_workspace_dir(self):
        import awm.config as cfg
        assert cfg.SKILLS_DIR == cfg.WORKSPACE_ROOT / "skills"
