"""Skills scanning, frontmatter parsing, search, and index regeneration."""

from __future__ import annotations

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


# Acronyms that should stay uppercase when rendering a type as a display label.
_ACRONYMS = {"awm", "mcp", "cli", "hpc", "api", "sop", "pr"}


def _format_type_label(t: str) -> str:
    """Render a type string as a human-readable section label (acronym-aware titlecase)."""
    words = t.replace("_", "-").split("-")
    return " ".join(w.upper() if w.lower() in _ACRONYMS else w.title() for w in words)


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
            if hit["source_id"] not in keyword_paths and hit["score"] > 0.3:
                try:
                    skill = _skill_from_path(SKILLS_DIR / hit["source_id"])
                    semantic_results.append(skill)
                except Exception:
                    pass
    except Exception:
        pass  # semantic search unavailable — return keyword results only

    combined = keyword_results + semantic_results
    return SkillListResponse(skills=combined, total=len(combined))


def regenerate_index() -> str:
    """Rebuild skills/_index.md from a live scan of the skills directory.

    Sections are inferred from the skill type (derived from top-level subdirectory
    unless overridden in frontmatter). Templates are discovered by `.template`
    suffix wherever they live. No hardcoded category names.
    """
    skills = _scan_skills()

    # Group by type
    groups: dict[str, list[SkillInfo]] = {}
    for s in skills:
        groups.setdefault(s.type, []).append(s)

    lines = ["# Skills Catalog\n"]
    for skill_type, items in sorted(groups.items()):
        label = _format_type_label(skill_type)
        lines.append(f"\n## {label}\n")
        lines.append("| Skill | File | Description |")
        lines.append("|-------|------|-------------|")
        for s in items:
            lines.append(f"| {s.name} | `{s.file_path}` | {s.description} |")

    # Templates — discovered by suffix, regardless of directory
    if SKILLS_DIR.exists():
        template_files = sorted(SKILLS_DIR.rglob("*.template"))
        if template_files:
            lines.append("\n## Templates\n")
            lines.append("| Template | File | Description |")
            lines.append("|----------|------|-------------|")
            for t in template_files:
                if t.name.startswith("."):
                    continue
                rel = t.relative_to(SKILLS_DIR)
                name = t.name.removesuffix(".template").removesuffix(".md")
                name = name.replace("-", " ").replace("_", " ").title()
                lines.append(f"| {name} | `{rel}` | |")

    content = "\n".join(lines) + "\n"
    index_path = SKILLS_DIR / "_index.md"
    index_path.write_text(content, encoding="utf-8")
    return content
