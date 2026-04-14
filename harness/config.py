"""Harness configuration — all knobs in one place."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


@dataclass
class PlannerConfig:
    """Prompts for the three-way planner."""

    conservative_system: str = ""
    aggressive_system: str = ""
    merge_system: str = ""


@dataclass
class EvaluatorConfig:
    """Prompts for the three-way evaluator."""

    conservative_system: str = ""
    aggressive_system: str = ""
    merge_system: str = ""


@dataclass
class HarnessConfig:
    # --- LLM ---
    model: str = "bedrock/claude-sonnet-4-6"
    max_tokens: int = 8096

    # --- workspace & security ---
    workspace: str = "."
    allowed_paths: list[str] = field(default_factory=list)
    # If empty, defaults to [workspace]. Paths outside these are rejected.

    # --- tools ---
    allowed_tools: list[str] = field(default_factory=list)
    # If empty, all registered tools are available.

    # --- loop ---
    max_iterations: int = 5

    # --- sub-configs ---
    planner: PlannerConfig = field(default_factory=PlannerConfig)
    evaluator: EvaluatorConfig = field(default_factory=EvaluatorConfig)

    def __post_init__(self) -> None:
        self.workspace = str(Path(self.workspace).resolve())
        if not self.allowed_paths:
            self.allowed_paths = [self.workspace]
        self.allowed_paths = [str(Path(p).resolve()) for p in self.allowed_paths]

    def is_path_allowed(self, path: str | Path) -> bool:
        """Check whether *path* falls under one of the allowed directories."""
        resolved = str(Path(path).resolve())
        return any(
            resolved == ap or resolved.startswith(ap + "/")
            for ap in self.allowed_paths
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HarnessConfig:
        """Build config from a plain dict (e.g. loaded from YAML/JSON)."""
        data = dict(data)  # don't mutate caller's dict
        planner_data = data.pop("planner", {})
        evaluator_data = data.pop("evaluator", {})
        return cls(
            **data,
            planner=PlannerConfig(**planner_data),
            evaluator=EvaluatorConfig(**evaluator_data),
        )


# ===========================================================================
# Pipeline mode configs
# ===========================================================================


@dataclass
class DualEvaluatorConfig:
    """Prompts for dual-isolated evaluation (Basic + Diffusion)."""

    basic_system: str = ""
    diffusion_system: str = ""
    score_pattern: str = r"SCORE[:\s]+(\d+(?:\.\d+)?)"


@dataclass
class PipelineConfig:
    """Configuration for the phase pipeline orchestration mode."""

    harness: HarnessConfig = field(default_factory=HarnessConfig)

    # Phase list — loaded from config file
    phases: list[dict[str, Any]] = field(default_factory=list)

    # Pipeline parameters
    outer_rounds: int = 5
    inner_rounds: int = 3  # default per-phase, overridable in PhaseConfig

    # Evaluation mode
    evaluation_mode: Literal["three_way", "dual_isolated"] = "dual_isolated"
    dual_evaluator: DualEvaluatorConfig = field(default_factory=DualEvaluatorConfig)

    # Artifact & checkpoint
    output_dir: str = "harness_output"
    run_id: str | None = None  # auto-generated if None

    # Early stopping
    patience: int = 3  # 0 = disable

    # Synthesis
    synthesis_system: str = ""
    min_synthesis_chars: int = 150

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PipelineConfig:
        """Build from a plain dict (e.g. loaded from YAML/JSON)."""
        data = dict(data)
        harness_data = data.pop("harness", {})
        dual_data = data.pop("dual_evaluator", {})
        return cls(
            harness=HarnessConfig.from_dict(harness_data),
            dual_evaluator=DualEvaluatorConfig(**dual_data),
            **data,
        )
