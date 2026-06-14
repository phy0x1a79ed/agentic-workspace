"""Regression: the skills catalog must resolve to a real on-disk directory.

Guards against the split-editable-install bug where ``SKILLS_DIR`` pointed at a
non-existent ``…/awm/config/skills`` while the real catalog lived beside the
``awm.skills`` package. Both the skills service (owner) and ``awm.config`` (the
scopes-service consumer) must resolve to the same existing catalog containing the
known ``awm/debrief.md`` skill.

The skills-suite conftest monkeypatches ``SKILLS_DIR`` to a temp dir inside every
test (via an autouse fixture), so we capture the REAL resolved values here at
module import time — before any fixture runs — and assert against those.
"""

from __future__ import annotations

import awm.config as _config
import awm.skills.skills as _skills

# Captured at import, before the autouse temp-dir monkeypatching takes effect.
_REAL_CONFIG_SKILLS_DIR = _config.SKILLS_DIR
_REAL_SKILLS_SKILLS_DIR = _skills.SKILLS_DIR


def test_skills_package_catalog_exists_with_debrief():
    """The skills service owns its catalog and ships the debrief skill."""
    assert _REAL_SKILLS_SKILLS_DIR.exists(), _REAL_SKILLS_SKILLS_DIR
    assert (_REAL_SKILLS_SKILLS_DIR / "awm" / "debrief.md").is_file()


def test_config_skills_dir_resolves_to_real_catalog():
    """awm.config defers to the installed awm.skills catalog for consumers."""
    assert _REAL_CONFIG_SKILLS_DIR.exists(), _REAL_CONFIG_SKILLS_DIR
    assert (_REAL_CONFIG_SKILLS_DIR / "awm" / "debrief.md").is_file()


def test_config_and_skills_agree_on_catalog():
    """Both resolutions point at the same catalog — single owner, no drift."""
    assert _REAL_CONFIG_SKILLS_DIR == _REAL_SKILLS_SKILLS_DIR
