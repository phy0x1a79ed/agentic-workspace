"""Skills scanning, frontmatter parsing, search, and embedding sync."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

from awm.config import SKILLS_DIR
from awm.models import SkillInfo, SkillListResponse, SkillContentResponse


def _parse_frontmatter(path: Path) -> dict:
    """Parse YAML frontmatter from a markdown file. Returns {} if none found."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("---", 3)
    if end == -1:
        return {}
    try:
        fm = yaml.safe_load(text[3:end])
        return fm if isinstance(fm, dict) else {}
    except yaml.YAMLError:
        return {}


def _is_template(path: Path) -> bool:
    """Templates are identified by the `.template` filename suffix, regardless of location."""
    return path.name.endswith(".template")


def _skill_from_path(path: Path) -> SkillInfo:
    """Build a SkillInfo from a skill file path.

    The skill's `type` is inferred from its top-level subdirectory under SKILLS_DIR
    (e.g. `awm/debrief.md` → type `awm`, `tools/git.md` → type `tools`). Frontmatter
    may override via an explicit `type:` field.
    """
    fm = _parse_frontmatter(path)
    rel = path.relative_to(SKILLS_DIR)
    inferred_type = rel.parts[0] if len(rel.parts) > 1 else "unknown"

    return SkillInfo(
        name=fm.get("name", path.stem),
        type=fm.get("type", inferred_type),
        tags=fm.get("tags", []),
        description=fm.get("description", ""),
        file_path=str(rel),
    )


def _scan_skills() -> list[SkillInfo]:
    """Scan SKILLS_DIR for all .md skill files (excluding _index.md and *.template files)."""
    skills = []
    if not SKILLS_DIR.exists():
        return skills
    for path in sorted(SKILLS_DIR.rglob("*.md")):
        if path.name.startswith("_"):
            continue
        if _is_template(path):
            continue
        skills.append(_skill_from_path(path))
    return skills


def find_by_name(name: str) -> SkillInfo | None:
    """Look up a skill by its frontmatter `name` field.

    Use this instead of hardcoding file paths when another service needs to reference
    a skill (e.g. to generate context.md pointers). Layout-independent.
    """
    for skill in _scan_skills():
        if skill.name == name:
            return skill
    return None


def list_skills(
    type_filter: str | None = None,
    tags: list[str] | None = None,
) -> SkillListResponse:
    """List skills with optional type/tag filters."""
    skills = _scan_skills()
    if type_filter:
        skills = [s for s in skills if s.type == type_filter]
    if tags:
        tag_set = set(t.lower() for t in tags)
        skills = [s for s in skills if tag_set & set(t.lower() for t in s.tags)]
    return SkillListResponse(skills=skills, total=len(skills))


def get_skill(path: str) -> SkillContentResponse:
    """Read a skill file by relative path (e.g. 'tools/git.md')."""
    full_path = SKILLS_DIR / path
    if not full_path.exists() or not full_path.is_file():
        raise FileNotFoundError(f"Skill not found: {path}")
    skill = _skill_from_path(full_path)
    content = full_path.read_text(encoding="utf-8")
    return SkillContentResponse(skill=skill, content=content)


def search_skills(query: str) -> SkillListResponse:
    """Hybrid search: keyword matching + semantic similarity.

    Returns keyword matches first, then semantic matches not already found.
    """
    q = query.lower()
    keyword_results = []
    keyword_paths: set[str] = set()

    if SKILLS_DIR.exists():
        for path in sorted(SKILLS_DIR.rglob("*.md")):
            if path.name.startswith("_"):
                continue
            if _is_template(path):
                continue
            skill = _skill_from_path(path)
            # Search across metadata fields
            searchable = " ".join([
                skill.name,
                skill.type,
                " ".join(skill.tags),
                skill.description,
            ]).lower()
            if q in searchable:
                keyword_results.append(skill)
                keyword_paths.add(skill.file_path)
                continue
            # Search in file content
            try:
                content = path.read_text(encoding="utf-8").lower()
                if q in content:
                    keyword_results.append(skill)
                    keyword_paths.add(skill.file_path)
            except (OSError, UnicodeDecodeError):
                pass

    # Augment with semantic search (best-effort — skip if deps unavailable)
    semantic_results = []
    try:
        from awm.services.embeddings import semantic_search
        hits = semantic_search(query, source_type="skill", limit=10)
        for hit in hits:
            if hit["source_id"] in keyword_paths or hit["score"] <= 0.3:
                continue
            # Guard against stale embeddings rows whose skill file has been
            # deleted or moved. Without this check, _skill_from_path would
            # silently return a ghost SkillInfo (empty description, inferred
            # type) because _parse_frontmatter tolerates missing files.
            full = SKILLS_DIR / hit["source_id"]
            if not full.is_file():
                continue
            try:
                skill = _skill_from_path(full)
                semantic_results.append(skill)
            except Exception:
                pass
    except Exception:
        pass  # semantic search unavailable — return keyword results only

    combined = keyword_results + semantic_results
    return SkillListResponse(skills=combined, total=len(combined))


# ---------------------------------------------------------------------------
# Sync — keep the embeddings index aligned with the live skills directory.
# ---------------------------------------------------------------------------

_SYNC_FP_KEY = "skills_sync_fp"


def _fingerprint_skills_dir() -> str:
    """Cheap fingerprint of the live skills tree.

    Captures every file that `_scan_skills` would include, plus its mtime_ns,
    so any add/delete/edit invalidates the hash. O(N) stat calls only — no file
    reads — so safe to call on every invocation of `sync_skills`.
    """
    if not SKILLS_DIR.exists():
        return "empty"
    entries: list[tuple[str, int]] = []
    for path in sorted(SKILLS_DIR.rglob("*.md")):
        if path.name.startswith("_") or _is_template(path):
            continue
        try:
            entries.append((str(path.relative_to(SKILLS_DIR)), path.stat().st_mtime_ns))
        except OSError:
            continue
    payload = json.dumps(entries, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def sync_skills(force: bool = False) -> dict:
    """Sync the embeddings index with the live skills directory.

    Lazy: returns immediately with `skipped=True` when the directory fingerprint
    matches the last successful sync. Designed to be called from workflow skills
    (e.g. skill-update) without concern for redundant work.

    When drift is detected (or `force=True`): re-embeds every live skill and
    deletes embeddings rows whose source file no longer exists. Returns stats.
    """
    from awm.services.config_service import get_config, set_config
    from awm.services.embeddings import index_skill
    from awm.db import get_connection

    fp = _fingerprint_skills_dir()
    if not force and get_config(_SYNC_FP_KEY) == fp:
        return {"skipped": True, "reason": "fingerprint_unchanged"}

    skills = _scan_skills()
    live_paths = {s.file_path for s in skills}

    indexed = 0
    for s in skills:
        try:
            index_skill(s.file_path)
            indexed += 1
        except Exception:
            pass

    pruned = 0
    conn = get_connection()
    try:
        existing = [
            r[0] for r in conn.execute(
                "SELECT source_id FROM embeddings WHERE source_type='skill'"
            ).fetchall()
        ]
        stale = [sid for sid in existing if sid not in live_paths]
        if stale:
            conn.executemany(
                "DELETE FROM embeddings WHERE source_type='skill' AND source_id=?",
                [(sid,) for sid in stale],
            )
            conn.commit()
            pruned = len(stale)
    finally:
        conn.close()

    set_config(_SYNC_FP_KEY, fp)
    return {"skipped": False, "indexed": indexed, "pruned": pruned}

