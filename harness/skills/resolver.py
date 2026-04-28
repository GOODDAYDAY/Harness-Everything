"""Cycle-level skill resolution — decide what to inject into the system prompt."""

from __future__ import annotations

import logging

from harness.skills.registry import SkillRegistry

log = logging.getLogger(__name__)

_AUTO_LOAD_BUDGET_CHARS: int = 12_000


def resolve_cycle_skills(
    registry: SkillRegistry,
    budget_chars: int = _AUTO_LOAD_BUDGET_CHARS,
) -> tuple[str, str]:
    """Resolve which skills to inject into the system prompt this cycle.

    Returns ``(auto_loaded_text, index_text)``:

    *auto_loaded_text*
        Concatenated bodies of ``auto_load=True`` skills that fit within
        *budget_chars*.  The ``_mission`` virtual skill gets priority
        (always included first); remaining auto-load skills follow in
        name-sorted order.  Skills that exceed the remaining budget are
        demoted to the index.

    *index_text*
        Compact listing of all on-demand skills (plus any demoted
        auto-load skills) for the agent to discover via
        ``skill_lookup``.
    """
    auto_skills = registry.auto_load_skills()
    if not auto_skills and not registry.on_demand_skills():
        return "", ""

    # Partition: _mission first, then the rest sorted by name.
    mission_skill = None
    other_auto: list = []
    for s in auto_skills:
        if s.name == "_mission":
            mission_skill = s
        else:
            other_auto.append(s)

    # Build auto-loaded text within budget.
    auto_parts: list[str] = []
    demoted: list = []
    remaining = budget_chars

    def _try_include(skill) -> bool:  # noqa: ANN001
        nonlocal remaining
        if skill.char_count <= remaining:
            label = "auto-loaded"
            auto_parts.append(f"## Skill: {skill.name} ({label})\n\n{skill.body}\n")
            remaining -= skill.char_count
            return True
        return False

    # _mission always gets priority.
    if mission_skill:
        if not _try_include(mission_skill):
            # Mission exceeds full budget — include it anyway with a warning.
            auto_parts.append(
                f"## Skill: _mission (auto-loaded)\n\n{mission_skill.body}\n"
            )
            remaining = 0
            log.warning(
                "skills: _mission (%d chars) exceeds budget (%d) — included anyway",
                mission_skill.char_count,
                budget_chars,
            )

    for skill in other_auto:
        if not _try_include(skill):
            demoted.append(skill)
            log.info(
                "skills: %s demoted to on-demand (budget exhausted, %d chars remaining)",
                skill.name,
                remaining,
            )

    auto_text = "\n".join(auto_parts) if auto_parts else ""

    # Build index: on-demand skills + demoted auto-load skills.
    on_demand = registry.on_demand_skills()
    index_entries = list(demoted) + list(on_demand)
    if index_entries:
        lines = [
            "## Available Skills",
            'Use `skill_lookup(name="...")` to load full content.',
        ]
        for s in index_entries:
            tag = " [auto-load, over budget]" if s in demoted else ""
            lines.append(
                f"- **{s.name}**: {s.description} (~{s.char_count} chars){tag}"
            )
        index_text = "\n".join(lines) + "\n"
    else:
        index_text = ""

    return auto_text, index_text
