"""Harness Skills — multi-level context loading for autonomous agents.

Skills are SKILL.md files with YAML frontmatter stored in
``<workspace>/.harness/skills/<name>/SKILL.md``.  Each skill provides a
discrete unit of project knowledge that can be auto-loaded into the
system prompt or loaded on-demand via the ``skill_lookup`` tool.
"""

from __future__ import annotations

import logging
from pathlib import Path

from harness.skills.loader import Skill, load_skill
from harness.skills.registry import SkillRegistry

__all__ = [
    "Skill",
    "SkillRegistry",
    "discover_skills",
    "build_skill_registry",
]

log = logging.getLogger(__name__)


def discover_skills(workspace: str) -> list[Skill]:
    """Scan ``<workspace>/.harness/skills/`` for SKILL.md files.

    Each immediate subdirectory containing a ``SKILL.md`` file is treated
    as a skill.  Malformed files are logged as warnings and skipped.

    Returns a list sorted by skill name for deterministic ordering.
    """
    skills_dir = Path(workspace) / ".harness" / "skills"
    if not skills_dir.is_dir():
        return []

    skills: list[Skill] = []
    for entry in sorted(skills_dir.iterdir()):
        if not entry.is_dir():
            continue
        skill_file = entry / "SKILL.md"
        if not skill_file.is_file():
            continue
        try:
            skill = load_skill(skill_file)
            skills.append(skill)
        except Exception as exc:
            log.warning("skills: failed to load %s — %s", skill_file, exc)
    return skills


def build_skill_registry(
    workspace: str,
    mission: str = "",
) -> SkillRegistry:
    """Build a :class:`SkillRegistry` from disk skills and an optional mission.

    1. Discover skills in ``<workspace>/.harness/skills/``
    2. Register each discovered skill
    3. If *mission* is non-empty, create a virtual ``_mission`` skill
       with ``auto_load=True`` for backward compatibility

    Returns the populated registry (may be empty).
    """
    registry = SkillRegistry()

    for skill in discover_skills(workspace):
        registry.register(skill)

    if mission:
        registry.register(Skill(
            name="_mission",
            description="Project mission statement",
            auto_load=True,
            body=mission,
            path=None,
            is_virtual=True,
        ))

    return registry
