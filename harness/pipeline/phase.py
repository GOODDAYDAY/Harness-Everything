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

    # Per-phase early-exit threshold override for implement mode.
    # None = use PipelineConfig.inner_early_exit_threshold.
    # Set to a float (0–10) to override for this phase only; 0.0 disables.
    inner_early_exit_threshold: float | None = None

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
    # Non-empty list enables ImportSmokeHook, which launches a fresh Python
    # subprocess to `import` each module (and build the tool registry). A
    # failure gates the downstream commit. Intended for self-improvement
    # pipelines where a broken import would halt the next round entirely.
    import_smoke_modules: list[str] = field(default_factory=list)
    # Additional statements the ImportSmokeHook subprocess executes AFTER the
    # imports. Use these to exercise runtime code paths — a bare `import` only
    # catches module-top-level errors, not a NameError buried in a function
    # body. Each entry is a Python statement/expression string using fully
    # qualified names, e.g.
    #   "harness.evaluation.dual_evaluator.validate_evaluator_output("
    #   "'DELTA\\nSCORE: 5', 'basic', 'debate')".
    # The corresponding module must also appear in import_smoke_modules.
    import_smoke_calls: list[str] = field(default_factory=list)
    # File-mutating tools reject edits to paths outside these globs when the
    # list is non-empty. Empty = unrestricted (back-compat). Used to bound the
    # blast radius of a misbehaving phase — e.g. framework_improvement should
    # not be editing evaluator code. Globs are matched against paths relative
    # to the workspace root using fnmatch semantics (** for recursive).
    allowed_edit_globs: list[str] = field(default_factory=list)
    # When True AND mode == "implement", run the three-way Planner
    # (conservative + aggressive + merge) BEFORE the executor tool-use loop,
    # and prepend the merged plan to the executor prompt. The Planner's
    # output is a plaintext implementation plan, not tool calls. Intended for
    # free-form "orchestrate-then-code" phases where the LLM is expected to
    # first decide what to do, then do it — two-stage reasoning within a
    # single phase. Default False preserves legacy single-call behaviour.
    use_planner: bool = False
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
        self._validate_allowed_edit_globs()

    def _validate_allowed_edit_globs(self) -> None:
        """Validate allowed_edit_globs to prevent path traversal outside workspace.
        
        Raises:
            ValueError: If any glob pattern contains '..' or starts with an
                absolute path indicator ('/' on Unix or '[A-Z]:\\' on Windows).
        """
        import os

        for pattern in self.allowed_edit_globs:
            # Check for parent directory traversal
            if '..' in pattern:
                raise ValueError(
                    f"Glob pattern '{pattern}' contains '..' which could allow "
                    f"path traversal outside workspace"
                )
            
            # Check for absolute paths
            if os.path.isabs(pattern):
                raise ValueError(
                    f"Glob pattern '{pattern}' is an absolute path. "
                    f"Use relative patterns only."
                )
            
            # Windows-specific: check for drive letter absolute paths
            if len(pattern) >= 2 and pattern[1] == ':' and pattern[2:].startswith('\\'):
                raise ValueError(
                    f"Glob pattern '{pattern}' is an absolute Windows path. "
                    f"Use relative patterns only."
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
        # Strip JSON "comment" keys (// or _ prefix) before construction.
        cleaned = {k: v for k, v in data.items()
                   if not k.startswith("//") and not k.startswith("_")}
        return cls(**cleaned)


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
        """Combined score (weighted average of basic and diffusion, 0-10).
        
        Uses 60% weight for basic score (detailed correctness evaluation) and
        40% weight for diffusion score (system-level impact evaluation).
        The result is clamped to the [0.0, 10.0] range.
        """
        # Validate that both scores are within the expected 0-10 range
        if not (0.0 <= self.basic.score <= 10.0):
            raise ValueError(f"Basic score {self.basic.score} is outside valid range [0.0, 10.0]")
        if not (0.0 <= self.diffusion.score <= 10.0):
            raise ValueError(f"Diffusion score {self.diffusion.score} is outside valid range [0.0, 10.0]")
        
        # Calculate weighted average: 60% basic, 40% diffusion
        weighted_score = 0.6 * self.basic.score + 0.4 * self.diffusion.score
        
        # Clamp to [0.0, 10.0] range
        return max(0.0, min(10.0, weighted_score))


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
