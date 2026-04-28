"""Skill registry — stores discovered skills and provides lookup/listing."""

from __future__ import annotations

from harness.skills.loader import Skill


class SkillRegistry:
    """Manages a collection of :class:`Skill` instances.

    Provides lookup by name, auto-load/on-demand partitioning, and a
    compact index suitable for system-prompt injection.
    """

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    # -- mutators ----------------------------------------------------------

    def register(self, skill: Skill) -> None:
        """Register a skill.  Later registration overwrites earlier."""
        self._skills[skill.name] = skill

    # -- accessors ---------------------------------------------------------

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    @property
    def names(self) -> list[str]:
        return list(self._skills)

    def auto_load_skills(self) -> list[Skill]:
        """Return skills with ``auto_load=True``, sorted by name."""
        return sorted(
            (s for s in self._skills.values() if s.auto_load),
            key=lambda s: s.name,
        )

    def on_demand_skills(self) -> list[Skill]:
        """Return skills with ``auto_load=False``, sorted by name."""
        return sorted(
            (s for s in self._skills.values() if not s.auto_load),
            key=lambda s: s.name,
        )

    def compact_index(self) -> str:
        """Return a compact listing of on-demand skills for the system prompt.

        Example output::

            ## Available Skills
            Use `skill_lookup(name="...")` to load full content.
            - **sql-diagnostics**: SQL diagnostic patterns (~2400 chars)
            - **workflow-guide**: Standard debugging workflows (~1800 chars)
        """
        on_demand = self.on_demand_skills()
        if not on_demand:
            return ""
        lines = [
            "## Available Skills",
            'Use `skill_lookup(name="...")` to load full content.',
        ]
        for s in on_demand:
            lines.append(f"- **{s.name}**: {s.description} (~{s.char_count} chars)")
        return "\n".join(lines) + "\n"

    # -- dunder ------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._skills)

    def __bool__(self) -> bool:
        return bool(self._skills)
