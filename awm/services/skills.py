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


def _skill_from_path(path: Path) -> SkillInfo:
    """Build a SkillInfo from a skill file path."""
    fm = _parse_frontmatter(path)
    rel = path.relative_to(SKILLS_DIR)
    # Infer type from parent directory if not in frontmatter
    parent_name = rel.parts[0] if len(rel.parts) > 1 else "unknown"
    type_map = {"sops": "sop", "tools": "tool", "templates": "template"}
    inferred_type = type_map.get(parent_name, parent_name)

    return SkillInfo(
        name=fm.get("name", path.stem),
        type=fm.get("type", inferred_type),
        tags=fm.get("tags", []),
        description=fm.get("description", ""),
        file_path=str(rel),
    )


def _scan_skills() -> list[SkillInfo]:
    """Scan SKILLS_DIR for all .md skill files (excluding _index.md and templates/)."""
    skills = []
    if not SKILLS_DIR.exists():
        return skills
    for path in sorted(SKILLS_DIR.rglob("*.md")):
        if path.name.startswith("_"):
            continue
        # Skip files inside templates/ (they're templates, not skills)
        rel = path.relative_to(SKILLS_DIR)
        if rel.parts[0] == "templates":
            continue
        skills.append(_skill_from_path(path))
    return skills


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
    """Read a skill file by relative path (e.g. 'sops/git-workflow.md')."""
    full_path = SKILLS_DIR / path
    if not full_path.exists() or not full_path.is_file():
        raise FileNotFoundError(f"Skill not found: {path}")
    skill = _skill_from_path(full_path)
    content = full_path.read_text(encoding="utf-8")
    return SkillContentResponse(skill=skill, content=content)


def search_skills(query: str) -> SkillListResponse:
    """Case-insensitive search across skill name, tags, description, and content."""
    q = query.lower()
    results = []
    if not SKILLS_DIR.exists():
        return SkillListResponse(skills=[], total=0)
    for path in sorted(SKILLS_DIR.rglob("*.md")):
        if path.name.startswith("_"):
            continue
        rel = path.relative_to(SKILLS_DIR)
        if rel.parts[0] == "templates":
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
            results.append(skill)
            continue
        # Search in file content
        try:
            content = path.read_text(encoding="utf-8").lower()
            if q in content:
                results.append(skill)
        except (OSError, UnicodeDecodeError):
            pass
    return SkillListResponse(skills=results, total=len(results))


def regenerate_index() -> str:
    """Rebuild skills/_index.md from a live scan of the skills directory."""
    skills = _scan_skills()

    # Group by type
    groups: dict[str, list[SkillInfo]] = {}
    for s in skills:
        groups.setdefault(s.type, []).append(s)

    # Type display names
    type_labels = {"sop": "SOPs", "tool": "Tool Guides"}

    lines = ["# Skills Catalog\n"]
    for skill_type, items in sorted(groups.items()):
        label = type_labels.get(skill_type, skill_type.title())
        lines.append(f"\n## {label}\n")
        lines.append("| Skill | File | Description |")
        lines.append("|-------|------|-------------|")
        for s in items:
            lines.append(f"| {s.name} | `{s.file_path}` | {s.description} |")

    # Also list templates
    templates_dir = SKILLS_DIR / "templates"
    if templates_dir.exists():
        template_files = sorted(templates_dir.iterdir())
        if template_files:
            lines.append("\n## Templates\n")
            lines.append("| Template | File | Description |")
            lines.append("|----------|------|-------------|")
            for t in template_files:
                if t.name.startswith("."):
                    continue
                rel = t.relative_to(SKILLS_DIR)
                name = t.stem.replace("-", " ").replace("_", " ").title()
                suffix = "/" if t.is_dir() else ""
                lines.append(f"| {name} | `{rel}{suffix}` | |")

    content = "\n".join(lines) + "\n"
    index_path = SKILLS_DIR / "_index.md"
    index_path.write_text(content, encoding="utf-8")
    return content
