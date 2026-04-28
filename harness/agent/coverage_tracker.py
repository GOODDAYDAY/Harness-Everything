"""Cross-cycle file coverage tracking for MetaReview (US-11).

Accumulates the set of files read and written by the agent across all cycles.
At checkpoint time, computes a coverage report comparing touched files against
the project's file inventory, giving MetaReview concrete data to guide pivot
decisions.

Design:
  * Stateful — lives on AgentLoop for the duration of the run.
  * Lightweight — only set unions per cycle, glob at checkpoint time.
  * No imports from agent_loop (avoids circular deps).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


# Directories to skip when collecting project files (mirrors project_context.py).
_SKIP_DIRS: frozenset[str] = frozenset({
    ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "node_modules", ".venv", "venv", "env", ".env",
    "dist", "build", ".eggs",
    ".tox", ".nox", "htmlcov", ".coverage",
})

# Extensions considered "interesting" for coverage analysis.
_PROJECT_EXTENSIONS: frozenset[str] = frozenset({
    ".py", ".gd", ".json", ".yaml", ".yml", ".toml", ".md",
    ".js", ".ts", ".tsx", ".jsx", ".sh", ".bat",
})

# Hard cap on project files to avoid memory blowup on huge repos.
_MAX_PROJECT_FILES = 2000

# Max untouched files to include in the report (prompt size bound).
_MAX_UNTOUCHED_LIST = 50


@dataclass
class CoverageReport:
    """Coverage gap analysis for meta-review consumption."""

    total_project_files: int
    files_read: int
    files_written: int
    files_touched: int       # union of read + written
    coverage_ratio: float
    untouched_files: list[str] = field(default_factory=list)


class CoverageTracker:
    """Accumulates file coverage across agent cycles."""

    def __init__(self) -> None:
        self._read_paths: set[str] = set()
        self._written_paths: set[str] = set()

    def update(
        self,
        read_paths: Iterable[str],
        written_paths: Iterable[str],
    ) -> None:
        """Called once per cycle with that cycle's touched paths."""
        self._read_paths.update(read_paths)
        self._written_paths.update(written_paths)

    def report(self, project_files: list[str]) -> CoverageReport:
        """Compute coverage against the known project file inventory."""
        project_set = set(project_files)
        touched = (self._read_paths | self._written_paths) & project_set
        total = len(project_set)

        untouched = sorted(project_set - touched)
        # Prioritise .py files, then shorter paths (closer to root).
        untouched.sort(key=lambda p: (not p.endswith(".py"), p.count(os.sep), p))
        untouched = untouched[:_MAX_UNTOUCHED_LIST]

        return CoverageReport(
            total_project_files=total,
            files_read=len(self._read_paths & project_set),
            files_written=len(self._written_paths & project_set),
            files_touched=len(touched),
            coverage_ratio=len(touched) / total if total else 0.0,
            untouched_files=untouched,
        )

    @staticmethod
    def format_report(report: CoverageReport) -> str:
        """Format a CoverageReport as text for the MetaReview prompt."""
        lines = [
            f"Total project files: {report.total_project_files}",
            f"Files read (across all cycles): {report.files_read}",
            f"Files written (across all cycles): {report.files_written}",
            f"Files touched (read or written): {report.files_touched}",
            f"Coverage ratio: {report.coverage_ratio:.1%}",
        ]
        if report.untouched_files:
            lines.append("")
            lines.append(
                f"Top {len(report.untouched_files)} untouched files "
                "(potential pivot targets):"
            )
            for f in report.untouched_files:
                lines.append(f"  - {f}")
        return "\n".join(lines)


def collect_project_files(workspace: str | Path) -> list[str]:
    """Collect 'interesting' project files under *workspace*, returning relative paths.

    Skips noise directories and filters by extension.  Capped at
    ``_MAX_PROJECT_FILES`` to bound memory on very large repos.
    """
    root = Path(workspace)
    results: list[str] = []

    for dirpath, dirnames, filenames in os.walk(root):
        # Prune skip dirs in-place so os.walk doesn't recurse into them.
        dirnames[:] = [
            d for d in dirnames
            if d not in _SKIP_DIRS and not d.endswith(".egg-info")
        ]

        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in _PROJECT_EXTENSIONS:
                continue

            full = os.path.join(dirpath, fname)
            rel = os.path.relpath(full, root)
            results.append(rel)

            if len(results) >= _MAX_PROJECT_FILES:
                return results

    return results
