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
    extra_tools: list[str] = field(default_factory=list)
    # Names from OPTIONAL_TOOLS (e.g. ["web_search"]) to add on top of the
    # default registry.  These are NOT included by default to keep schema size
    # small; opt in explicitly when the task needs them.

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
        # Claude's documented hard ceiling is 64 000 output tokens (claude-3-*) or
        # 8 192 for older models.  Values above 64 000 will be rejected by the API
        # with a 400 error and a confusing message; catch them here instead.
        if self.max_tokens > 64_000:
            raise ValueError(
                f"HarnessConfig.max_tokens={self.max_tokens} exceeds the Claude API maximum "
                "of 64 000 output tokens.  Typical values: 8 096 (default), 16 384, 32 768."
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

        # --- validate extra_tools entries are non-empty strings ---
        bad_extra = [t for t in self.extra_tools if not isinstance(t, str) or not t.strip()]
        if bad_extra:
            raise ValueError(
                f"HarnessConfig.extra_tools contains invalid entries: {bad_extra!r}. "
                "All entries must be non-empty strings (tool names)."
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

        Raises ValueError on unknown top-level keys AND on unknown keys in the
        nested ``planner`` / ``evaluator`` sub-dicts so that typos like
        ``"conservtive_system"`` are caught immediately rather than silently
        dropped (the dataclass ``__init__`` would raise a cryptic TypeError
        without this check).
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

        # Validate nested sub-dicts so typos in prompt overrides are caught early.
        known_planner = {f.name for f in dataclasses.fields(PlannerConfig)}
        unknown_planner = set(planner_data) - known_planner
        if unknown_planner:
            raise ValueError(
                f"HarnessConfig.planner: unknown key(s): {sorted(unknown_planner)}.  "
                f"Known keys: {sorted(known_planner)}"
            )

        known_evaluator = {f.name for f in dataclasses.fields(EvaluatorConfig)}
        unknown_evaluator = set(evaluator_data) - known_evaluator
        if unknown_evaluator:
            raise ValueError(
                f"HarnessConfig.evaluator: unknown key(s): {sorted(unknown_evaluator)}.  "
                f"Known keys: {sorted(known_evaluator)}"
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

        # Scale sanity check: each inner round makes 3 LLM calls (proposal +
        # basic eval + diffusion eval) plus 1 synthesis call per phase.  Warn
        # loudly when the configured budget exceeds 500 API calls so operators
        # don't accidentally submit a job that runs for hours/costs a fortune.
        n_phases = len(self.phases) if self.phases else 0
        if n_phases > 0:
            # per-phase inner_rounds may override the default; use the default
            # here as a conservative estimate (actual may be higher).
            estimated_calls = self.outer_rounds * n_phases * (self.inner_rounds * 3 + 1)
            if estimated_calls > 500:
                log.warning(
                    "PipelineConfig: estimated LLM call budget is ~%d "
                    "(%d outer × %d phases × (%d inner×3 + 1 synthesis)) — "
                    "this may be expensive and time-consuming; "
                    "reduce outer_rounds/inner_rounds or add patience early-stopping",
                    estimated_calls,
                    self.outer_rounds,
                    n_phases,
                    self.inner_rounds,
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
