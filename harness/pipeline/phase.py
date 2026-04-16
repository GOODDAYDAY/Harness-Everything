"""Phase data classes — pure data, no behavior."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class PhaseConfig:
    """Configuration for a single phase in the pipeline.

    Fully serializable — all project-specific content lives in config files,
    not here.
    """

    name: str
    index: int
    system_prompt: str  # executor prompt template; use $file_context, $prior_best, etc.
    falsifiable_criterion: str = ""

    # File injection
    glob_patterns: list[str] = field(default_factory=list)

    # Mode: "debate" = text-only proposals, "implement" = tool_use edits files
    mode: Literal["debate", "implement"] = "debate"

    # Skip logic: skip in outer rounds > this value (None = never skip)
    skip_after_round: int | None = None

    # Periodic skip: run only every N rounds (None = no cycle, run every round).
    # E.g., skip_cycle=3 means run on outer rounds 0, 3, 6, 9, ...
    # When both skip_after_round and skip_cycle are set, the phase is skipped
    # if EITHER condition triggers.
    skip_cycle: int | None = None

    # Inner rounds override (None = use pipeline default)
    inner_rounds: int | None = None

    # Tool filtering: if non-empty, only tools with at least one matching tag
    # are included.  Valid tags: "file_read", "file_write", "search", "git",
    # "analysis", "execution", "network", "testing".  Empty = all tools.
    tool_tags: list[str] = field(default_factory=list)

    # Skip evaluation for proposals shorter than this (saves 2 API calls).
    # 0 = always evaluate (default).
    min_proposal_chars: int = 0

    # Verification hooks (implement mode only)
    syntax_check_patterns: list[str] = field(default_factory=list)
    run_tests: bool = False
    test_path: str = "tests/"
    commit_on_success: bool = False
    commit_repos: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        """Short label used in directory names: ``'1_requirements_analysis'``."""
        return f"{self.index + 1}_{self.name}"

    def __post_init__(self) -> None:
        if self.skip_cycle is not None and self.skip_cycle < 1:
            raise ValueError(
                f"PhaseConfig.skip_cycle must be >= 1 (or None to disable), "
                f"got {self.skip_cycle}"
            )

    def should_skip(self, outer: int) -> bool:
        """Whether this phase should be skipped in the given outer round.

        Skip conditions (any True => skip):
        - skip_after_round is set and outer > skip_after_round
        - skip_cycle is set and outer % skip_cycle != 0
        """
        if self.skip_after_round is not None and outer > self.skip_after_round:
            return True
        if self.skip_cycle is not None and self.skip_cycle > 0:
            return (outer % self.skip_cycle) != 0
        return False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PhaseConfig:
        return cls(**data)


@dataclass
class ScoreItem:
    """A single evaluator's score and critique."""

    score: float
    critique: str


@dataclass
class DualScore:
    """Result from dual-isolated evaluation."""

    basic: ScoreItem
    diffusion: ScoreItem

    @property
    def combined(self) -> float:
        return self.basic.score + self.diffusion.score


@dataclass
class InnerResult:
    """Per-inner-round result."""

    proposal: str
    dual_score: DualScore | None = None  # dual-isolated mode
    verdict: Any = None  # three-way mode (Verdict from evaluator.py)

    implement_log: str = ""
    post_impl_snapshot: str = ""
    syntax_errors: str = ""
    pytest_result: str = ""
    # Structured tool call records from call_with_tools execution log.
    # Each entry: {"tool": <name>, "success": <bool>, "duration_ms": <int>}
    # Empty list for debate-mode rounds (no tool calls).
    tool_call_log: list[dict] = field(default_factory=list)

    @property
    def combined_score(self) -> float:
        if self.dual_score:
            return self.dual_score.combined
        if self.verdict and hasattr(self.verdict, "passed"):
            return 10.0 if self.verdict.passed else 0.0
        return 0.0


@dataclass
class PhaseResult:
    """Output of one phase execution."""

    phase: PhaseConfig
    synthesis: str
    best_score: float
    inner_results: list[InnerResult] = field(default_factory=list)
