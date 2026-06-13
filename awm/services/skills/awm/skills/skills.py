"""Skills scanning, frontmatter parsing, search, and embedding sync."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

from awm.skills.models import SkillInfo, SkillListResponse, SkillContentResponse
from awm.skills import dao as _dao_mod

# The catalog is bundled package-data right beside this module. Resolving it
# package-relative (rather than via awm.config) makes the skills service the
# single owner of the catalog location and keeps it correct under the env's
# split editable installs — awm.skills always carries its own catalog.
SKILLS_DIR = Path(__file__).resolve().parent / "catalog"


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

    Use this instead of hardcoding file paths when another service needs to
    reference a skill (e.g. to generate context.md pointers). Layout-independent.
    """
    for skill in _scan_skills():
        if skill.name == name:
            return skill
    return None


def get_skill(path: str) -> SkillContentResponse:
    """Read a skill file by relative path (e.g. 'tools/git.md')."""
    full_path = SKILLS_DIR / path
    if not full_path.exists() or not full_path.is_file():
        raise FileNotFoundError(f"Skill not found: {path}")
    skill = _skill_from_path(full_path)
    content = full_path.read_text(encoding="utf-8")
    return SkillContentResponse(skill=skill, content=content)


def search_skills(
    query: str | None = None,
    type_filter: str | None = None,
    tags: list[str] | None = None,
) -> SkillListResponse:
    """Search skills with optional structural filters (type, tags) and a
    free-text `query`. When `query` is set, returns keyword (LIKE on metadata
    and content) hits first, then semantic top-up via the shared embeddings
    helper. With no `query`, behaves like the prior `list_skills` did:
    every skill, filtered by type/tags."""
    skills = _scan_skills()
    if type_filter:
        skills = [s for s in skills if s.type == type_filter]
    if tags:
        tag_set = set(t.lower() for t in tags)
        skills = [s for s in skills if tag_set & set(t.lower() for t in s.tags)]

    if not query:
        return SkillListResponse(skills=skills, total=len(skills))

    q = query.lower()
    keyword_results: list = []
    keyword_paths: set[str] = set()

    for skill in skills:
        searchable = " ".join([
            skill.name, skill.type,
            " ".join(skill.tags), skill.description,
        ]).lower()
        if q in searchable:
            keyword_results.append(skill)
            keyword_paths.add(skill.file_path)
            continue
        full = SKILLS_DIR / skill.file_path
        try:
            content = full.read_text(encoding="utf-8").lower()
            if q in content:
                keyword_results.append(skill)
                keyword_paths.add(skill.file_path)
        except (OSError, UnicodeDecodeError):
            pass

    def _materialize(source_id: str):
        full = SKILLS_DIR / source_id
        if not full.is_file():
            return None
        try:
            skill = _skill_from_path(full)
        except Exception:
            return None
        if type_filter and skill.type != type_filter:
            return None
        if tags:
            tag_set = set(t.lower() for t in tags)
            if not (tag_set & set(t.lower() for t in skill.tags)):
                return None
        return skill

    try:
        from awm.persistence.embeddings import hybrid_augment
        _dao = _dao_mod.SkillsDAO()
        conn = _dao.open_connection()
        try:
            combined = hybrid_augment(
                conn, query, source_type="skill",
                keyword_hits=keyword_results, keyword_keys=keyword_paths,
                materialize=_materialize,
            )
        finally:
            conn.close()
    except Exception:
        combined = keyword_results
    return SkillListResponse(skills=combined, total=len(combined))


# ---------------------------------------------------------------------------
# Index — embed a single skill into the service's own embeddings table.
# ---------------------------------------------------------------------------

def index_skill(file_path: str) -> None:
    """Embed a single skill and upsert it into the skills service's embeddings table.

    ``file_path`` is relative to ``SKILLS_DIR`` (e.g. ``'tools/git.md'``).
    The text sent to the embedder is the file's full content.

    Raises ``FileNotFoundError`` if the skill file doesn't exist.
    Optional deps (sentence-transformers / sqlite-vec) are imported lazily;
    if unavailable, this raises ``ImportError`` — callers should catch it.
    """
    from awm.persistence.embeddings import upsert_embedding
    full_path = SKILLS_DIR / file_path
    if not full_path.exists():
        raise FileNotFoundError(f"Skill not found: {file_path}")
    text = full_path.read_text(encoding="utf-8")
    _dao = _dao_mod.SkillsDAO()
    conn = _dao.open_connection()
    try:
        upsert_embedding(conn, "skill", file_path, text)
    finally:
        conn.close()


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

    Degrades gracefully when sentence-transformers / sqlite-vec are absent —
    indexing errors per skill are swallowed; only the pruning (plain SQL) is
    guaranteed to run.
    """
    from awm.persistence.config_service import get_config, set_config

    _dao_mod.init()  # idempotent — ensures the skills DB + embeddings table exist

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

    _dao = _dao_mod.SkillsDAO()
    existing = _dao.list_skill_ids()
    stale = [sid for sid in existing if sid not in live_paths]
    pruned = _dao.delete_stale(stale)

    set_config(_SYNC_FP_KEY, fp)
    return {"skipped": False, "indexed": indexed, "pruned": pruned}
