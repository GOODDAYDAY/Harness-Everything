"""MemoryStore — structured cross-round learning persistence for pipeline runs.

Persists learnings (score history, key insights, failure patterns) across outer
rounds in a JSONL file inside the run directory.  Each outer round appends one
entry per phase; the formatted context is injected into subsequent rounds so
the LLM can build on what it has already learned rather than rediscovering the
same patterns.

File layout::

    harness_output/
    └── run_20260414T120000/
        ├── memory.jsonl          ← append-only; one JSON object per line
        ├── round_1/
        └── ...

Entry schema (one JSON object per line in memory.jsonl)::

    {
      "ts": "2026-04-14T12:03:47",
      "round": 1,
      "phase": "1_requirements_analysis",
      "score": 14.0,
      "score_delta": 2.5,
      "insight": "...",
      "evaluator_top_defect": "...",
      "evaluator_key_risk": "..."
    }

Usage in PipelineLoop::

    self.memory = MemoryStore(self.artifacts)

    # After each phase completes:
    self.memory.record(outer, phase_result)

    # When building context for the next round:
    memory_ctx = self.memory.format_context(phase_label, max_entries=6)
    # Prepend memory_ctx to prior_best before passing to runner.run_phase()
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harness.core.artifacts import ArtifactStore
    from harness.pipeline.phase import PhaseResult

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex helpers for extracting structured fields from evaluator text
# ---------------------------------------------------------------------------

_TOP_DEFECT_RE = re.compile(
    r"TOP\s+DEFECT\s*:\s*(.+?)(?:\n|$)", re.IGNORECASE
)
_KEY_RISK_RE = re.compile(
    r"KEY\s+RISK\s*:\s*(.+?)(?:\n|$)", re.IGNORECASE
)

# Fallback patterns: evaluator LLMs frequently render their worst findings as a
# markdown section (``## CRITICAL DEFECTS`` / ``## SECOND-ORDER EFFECTS``) with
# a numbered bullet list underneath instead of emitting the single-line
# ``TOP DEFECT: ...`` anchor the prompt asks for. Without these fallbacks the
# memory store silently loses its most valuable cross-round learning signal.
_DEFECT_SECTION_RE = re.compile(
    r"^#{1,3}\s+CRITICAL\s+DEFECT[^\n]*\n"     # heading line
    r"(.+?)"                                     # body (non-greedy)
    r"(?=\n#{1,3}\s|\Z)",                       # stop at next heading or EOF
    re.IGNORECASE | re.DOTALL | re.MULTILINE,
)
_RISK_SECTION_RE = re.compile(
    r"^#{1,3}\s+(?:KEY\s+RISKS?|SECOND[- ]ORDER\s+EFFECTS?[^\n]*)\n"
    r"(.+?)"
    r"(?=\n#{1,3}\s|\Z)",
    re.IGNORECASE | re.DOTALL | re.MULTILINE,
)
# Pull the first non-empty content line from a section body, stripping common
# markdown prefixes (numbered bullets, dashes, asterisks, bold markers).
_BULLET_PREFIX_RE = re.compile(r"^\s*(?:\d+\.\s*|[-*+]\s+)?\**")


def _first_bullet(section_body: str) -> str:
    for raw in section_body.splitlines():
        line = raw.strip()
        if not line:
            continue
        # Drop leading "1. ", "- ", "* " and then flatten any inline bold
        # markers — evaluator output routinely writes `1. **Label**: body`
        # which would otherwise leave dangling `**` in the stored memo.
        line = _BULLET_PREFIX_RE.sub("", line).replace("**", "").strip()
        if line:
            return line
    return ""


def _extract_top_defect(text: str) -> str:
    """Pull the most critical defect description from a basic-evaluator critique.

    Accepts both the canonical ``TOP DEFECT: ...`` single-line form and the
    markdown section form (``## CRITICAL DEFECTS`` + bullet list).
    """
    m = _TOP_DEFECT_RE.search(text)
    if m:
        return m.group(1).strip()[:200]
    section = _DEFECT_SECTION_RE.search(text)
    if section:
        return _first_bullet(section.group(1))[:200]
    return ""


def _extract_key_risk(text: str) -> str:
    """Pull the most significant risk description from a diffusion critique.

    Accepts both the canonical ``KEY RISK: ...`` single-line form and the
    markdown section form (``## KEY RISKS`` or ``## SECOND-ORDER EFFECTS``).
    """
    m = _KEY_RISK_RE.search(text)
    if m:
        return m.group(1).strip()[:200]
    section = _RISK_SECTION_RE.search(text)
    if section:
        return _first_bullet(section.group(1))[:200]
    return ""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class MemoryEntry:
    """One persisted learning record (one per phase per outer round)."""

    ts: str                    # ISO-8601 timestamp
    round: int                 # 1-based outer round number
    phase: str                 # phase label, e.g. "1_requirements_analysis"
    score: float               # combined DualScore (basic + diffusion, 0-20)
    score_delta: float         # improvement vs. best previous score for this phase
    insight: str               # first ~400 chars of the phase synthesis
    evaluator_top_defect: str  # TOP DEFECT from best inner round's basic eval
    evaluator_key_risk: str    # KEY RISK from best inner round's diffusion eval

    def to_json_line(self) -> str:
        """Serialize to a compact single-line JSON string (no trailing newline)."""
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json_line(cls, line: str) -> "MemoryEntry | None":
        """Deserialize from a JSON line; returns None on parse error."""
        try:
            d = json.loads(line)
            return cls(
                ts=d.get("ts", ""),
                round=int(d.get("round", 0)),
                phase=str(d.get("phase", "")),
                score=float(d.get("score", 0.0)),
                score_delta=float(d.get("score_delta", 0.0)),
                insight=str(d.get("insight", "")),
                evaluator_top_defect=str(d.get("evaluator_top_defect", "")),
                evaluator_key_risk=str(d.get("evaluator_key_risk", "")),
            )
        except Exception:
            return None


# ---------------------------------------------------------------------------
# MemoryStore
# ---------------------------------------------------------------------------


class MemoryStore:
    """Append-only JSONL memory log with formatted context generation.

    Lifecycle
    ---------
    * ``MemoryStore(artifacts)`` — opens/creates ``memory.jsonl`` in run_dir.
      On resume, prior entries are loaded so that score history is intact.
    * ``record(outer, phase_result)`` — called once per completed phase;
      appends a new entry.
    * ``format_context(phase_label, max_entries)`` — returns a human-readable
      block summarising the most recent entries (all phases or filtered to a
      specific one) for injection into the LLM prompt.

    Thread safety
    -------------
    Not thread-safe.  ``PipelineLoop`` is single-threaded (one phase at a
    time) so this is fine.
    """

    _MEMORY_FILE = "memory.jsonl"

    def __init__(self, artifacts: "ArtifactStore") -> None:
        self.artifacts = artifacts
        self._path: Path = artifacts.run_dir / self._MEMORY_FILE
        self._entries: list[MemoryEntry] = []
        # Track best score seen per phase so we can compute score_delta
        self._best_score_by_phase: dict[str, float] = {}
        self._load()

    # ---- public API ----

    def record(self, outer: int, phase_result: "PhaseResult") -> None:
        """Append a memory entry for a completed phase.

        Args:
            outer: 0-based outer round index.
            phase_result: The PhaseResult returned by PhaseRunner.run_phase().
        """
        label = phase_result.phase.label
        score = phase_result.best_score
        prev_best = self._best_score_by_phase.get(label, 0.0)
        delta = score - prev_best

        # Find the best inner result (highest combined score) to extract
        # evaluator feedback from.
        best_inner = None
        if phase_result.inner_results:
            best_inner = max(
                phase_result.inner_results,
                key=lambda r: r.combined_score,
            )

        top_defect = ""
        key_risk = ""
        if best_inner and best_inner.dual_score:
            top_defect = _extract_top_defect(best_inner.dual_score.basic.critique)
            key_risk = _extract_key_risk(best_inner.dual_score.diffusion.critique)

        insight = phase_result.synthesis[:400].replace("\n", " ").strip()

        entry = MemoryEntry(
            ts=datetime.now().isoformat(timespec="seconds"),
            round=outer + 1,
            phase=label,
            score=round(score, 2),
            score_delta=round(delta, 2),
            insight=insight,
            evaluator_top_defect=top_defect,
            evaluator_key_risk=key_risk,
        )
        self._entries.append(entry)
        self._best_score_by_phase[label] = max(prev_best, score)

        # Append to disk immediately (crash-safe: prior entries survive)
        try:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(entry.to_json_line() + "\n")
            log.debug(
                "Memory: recorded R%d phase=%s score=%.1f delta=%+.1f",
                outer + 1, label, score, delta,
            )
        except OSError as exc:
            log.warning("Memory: failed to write entry: %s", exc)

    def format_context(
        self,
        phase_label: str | None = None,
        *,
        max_entries: int = 8,
    ) -> str:
        """Return a compact, LLM-readable memory context block.

        Args:
            phase_label: If given, include only entries for this phase first
                         (most relevant for the upcoming inner rounds).  A few
                         recent entries from *all* phases are always appended
                         so the LLM has cross-phase awareness.
            max_entries: Total cap on entries shown.

        Returns:
            A markdown-formatted string, or ``""`` when there are no entries.
        """
        if not self._entries:
            return ""

        # Entries for this specific phase (most relevant — shown first)
        phase_entries: list[MemoryEntry] = []
        other_entries: list[MemoryEntry] = []
        for e in self._entries:
            if phase_label and e.phase == phase_label:
                phase_entries.append(e)
            else:
                other_entries.append(e)

        # Most recent first within each bucket
        phase_entries = list(reversed(phase_entries))
        other_entries = list(reversed(other_entries))

        # Budget: up to (max_entries - 2) phase-specific, at most 2 cross-phase
        phase_cap = max(1, max_entries - 2)
        other_cap = min(2, max_entries - len(phase_entries[:phase_cap]))

        selected = phase_entries[:phase_cap] + other_entries[:max(0, other_cap)]
        selected = selected[:max_entries]

        if not selected:
            return ""

        lines: list[str] = [
            "## Memory: Prior Round Learnings",
            "",
            "The following entries summarise what was learned in previous rounds.",
            "Build on the insights and avoid repeating the identified defects/risks.",
            "",
        ]

        for e in selected:
            delta_str = f"+{e.score_delta:.1f}" if e.score_delta >= 0 else f"{e.score_delta:.1f}"
            lines.append(
                f"### Round {e.round} \u00b7 {e.phase}  "
                f"[score={e.score:.1f}, \u0394={delta_str}]"
            )
            if e.insight:
                lines.append(f"**Synthesis excerpt:** {e.insight}")
            if e.evaluator_top_defect and e.evaluator_top_defect.lower() != "none":
                lines.append(f"**Top defect to fix:** {e.evaluator_top_defect}")
            if e.evaluator_key_risk and e.evaluator_key_risk.lower() != "none":
                lines.append(f"**Key risk to address:** {e.evaluator_key_risk}")
            lines.append("")

        return "\n".join(lines).rstrip()

    @property
    def entry_count(self) -> int:
        """Total number of recorded entries."""
        return len(self._entries)

    def best_score(self, phase_label: str) -> float:
        """Return the best score seen so far for a given phase label."""
        return self._best_score_by_phase.get(phase_label, 0.0)

    # ---- private ----

    def _load(self) -> None:
        """Load existing entries from disk (for resume support)."""
        if not self._path.exists():
            return
        loaded = 0
        bad = 0
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    entry = MemoryEntry.from_json_line(line)
                    if entry is None:
                        bad += 1
                        continue
                    self._entries.append(entry)
                    # Rebuild best-score index
                    prev = self._best_score_by_phase.get(entry.phase, 0.0)
                    self._best_score_by_phase[entry.phase] = max(prev, entry.score)
                    loaded += 1
        except OSError as exc:
            log.warning("Memory: could not load prior entries: %s", exc)
            return

        if loaded:
            log.info(
                "Memory: loaded %d prior entr%s from %s%s",
                loaded,
                "y" if loaded == 1 else "ies",
                self._path,
                f" ({bad} malformed line(s) skipped)" if bad else "",
            )
