"""Harness configuration — all knobs in one place."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger(__name__)


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
        # --- resolve paths ---
        self.workspace = str(Path(self.workspace).resolve())
        if not self.allowed_paths:
            self.allowed_paths = [self.workspace]
        self.allowed_paths = [str(Path(p).resolve()) for p in self.allowed_paths]

        # --- validate numeric fields ---
        if self.max_tokens < 1:
            raise ValueError(f"HarnessConfig.max_tokens must be ≥ 1, got {self.max_tokens}")
        if self.max_tokens > 200_000:
            raise ValueError(
                f"HarnessConfig.max_tokens={self.max_tokens} seems unreasonably large; "
                "check your config (max supported by Claude is ~8096-64000 depending on model)"
            )
        if self.max_iterations < 1:
            raise ValueError(f"HarnessConfig.max_iterations must be ≥ 1, got {self.max_iterations}")
        if self.max_iterations > 100:
            log.warning(
                "HarnessConfig.max_iterations=%d is very large — "
                "this may run for a very long time",
                self.max_iterations,
            )

        # --- validate model string ---
        if not self.model or not self.model.strip():
            raise ValueError("HarnessConfig.model must not be empty")

        # --- validate workspace exists ---
        ws_path = Path(self.workspace)
        if not ws_path.exists():
            raise ValueError(
                f"HarnessConfig.workspace does not exist: {self.workspace!r}"
            )
        if not ws_path.is_dir():
            raise ValueError(
                f"HarnessConfig.workspace is not a directory: {self.workspace!r}"
            )

        # --- warn about allowed_paths outside workspace ---
        for ap in self.allowed_paths:
            if ap != self.workspace and not ap.startswith(self.workspace + "/"):
                log.warning(
                    "allowed_path %r is outside workspace %r — "
                    "ensure this is intentional",
                    ap,
                    self.workspace,
                )

    def is_path_allowed(self, path: str | Path) -> bool:
        """Check whether *path* falls under one of the allowed directories."""
        resolved = str(Path(path).resolve())
        return any(
            resolved == ap or resolved.startswith(ap + "/")
            for ap in self.allowed_paths
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HarnessConfig:
        """Build config from a plain dict (e.g. loaded from YAML/JSON).

        Raises ValueError on unknown top-level keys so that typos in config
        files are caught immediately rather than silently ignored.
        """
        import dataclasses

        data = dict(data)  # don't mutate caller's dict
        planner_data = data.pop("planner", {})
        evaluator_data = data.pop("evaluator", {})

        known_fields = {f.name for f in dataclasses.fields(cls)}
        unknown = set(data) - known_fields
        if unknown:
            raise ValueError(
                f"HarnessConfig: unknown config key(s): {sorted(unknown)}.  "
                f"Known keys: {sorted(known_fields)}"
            )

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

    def __post_init__(self) -> None:
        if self.outer_rounds < 1:
            raise ValueError(
                f"PipelineConfig.outer_rounds must be ≥ 1, got {self.outer_rounds}"
            )
        if self.inner_rounds < 1:
            raise ValueError(
                f"PipelineConfig.inner_rounds must be ≥ 1, got {self.inner_rounds}"
            )
        if self.patience < 0:
            raise ValueError(
                f"PipelineConfig.patience must be ≥ 0 (0 = disabled), got {self.patience}"
            )
        if self.min_synthesis_chars < 0:
            raise ValueError(
                f"PipelineConfig.min_synthesis_chars must be ≥ 0, got {self.min_synthesis_chars}"
            )
        if not self.phases:
            log.warning(
                "PipelineConfig.phases is empty — the pipeline will exit immediately"
            )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PipelineConfig:
        """Build from a plain dict (e.g. loaded from YAML/JSON).

        Raises ValueError on unknown top-level keys and on unknown/missing keys
        in each phase entry so that typos in config files are caught immediately
        rather than surfacing as a cryptic TypeError deep inside a run.
        """
        import dataclasses

        data = dict(data)
        harness_data = data.pop("harness", {})
        dual_data = data.pop("dual_evaluator", {})

        known_fields = {f.name for f in dataclasses.fields(cls)}
        unknown = set(data) - known_fields
        if unknown:
            raise ValueError(
                f"PipelineConfig: unknown config key(s): {sorted(unknown)}.  "
                f"Known keys: {sorted(known_fields)}"
            )

        known_dual = {f.name for f in dataclasses.fields(DualEvaluatorConfig)}
        unknown_dual = set(dual_data) - known_dual
        if unknown_dual:
            raise ValueError(
                f"DualEvaluatorConfig: unknown config key(s): {sorted(unknown_dual)}.  "
                f"Known keys: {sorted(known_dual)}"
            )

        # Eagerly validate each phase dict so that config typos (e.g.
        # "sytem_prompt" instead of "system_prompt") are caught at startup
        # rather than causing a cryptic TypeError during the first inner round.
        phases_raw: list[Any] = data.get("phases", [])
        if phases_raw:
            # Import here to avoid a circular import at module load time
            from harness.phase import PhaseConfig as _PhaseConfig
            known_phase = {f.name for f in dataclasses.fields(_PhaseConfig)}
            # These fields have no defaults and MUST be supplied
            required_phase = {
                f.name for f in dataclasses.fields(_PhaseConfig)
                if f.default is dataclasses.MISSING
                and f.default_factory is dataclasses.MISSING  # type: ignore[misc]
            }
            for i, phase_raw in enumerate(phases_raw):
                if not isinstance(phase_raw, dict):
                    raise ValueError(
                        f"PipelineConfig.phases[{i}] must be a dict, got {type(phase_raw).__name__}"
                    )
                unknown_phase = set(phase_raw) - known_phase
                if unknown_phase:
                    raise ValueError(
                        f"PipelineConfig.phases[{i}] (name={phase_raw.get('name', '?')!r}): "
                        f"unknown key(s): {sorted(unknown_phase)}.  "
                        f"Known keys: {sorted(known_phase)}"
                    )
                missing_phase = required_phase - set(phase_raw)
                if missing_phase:
                    raise ValueError(
                        f"PipelineConfig.phases[{i}] (name={phase_raw.get('name', '?')!r}): "
                        f"missing required key(s): {sorted(missing_phase)}"
                    )

        return cls(
            harness=HarnessConfig.from_dict(harness_data),
            dual_evaluator=DualEvaluatorConfig(**dual_data),
            **data,
        )
