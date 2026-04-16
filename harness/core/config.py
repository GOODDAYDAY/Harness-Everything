"""Harness configuration — all knobs in one place."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger(__name__)

# Valid Python logging level names accepted by HarnessConfig.log_level.
_VALID_LOG_LEVELS: frozenset[str] = frozenset(
    {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
)


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
    base_url: str = ""
    # API base URL. If empty, uses ANTHROPIC_BASE_URL env var (if set) or
    # Anthropic default.  Set this explicitly to avoid inheriting a local proxy
    # (e.g., Claude Code's 127.0.0.1:9099).
    # Examples:
    #   "https://api.anthropic.com"            — Anthropic direct
    #   "https://api.deepseek.com/anthropic"   — DeepSeek (Anthropic-compat; /v1 is OpenAI-format)
    #   "https://your-gateway.example.com/v1"  — custom gateway
    api_key: str = ""
    # API key / auth token.  If empty, uses ANTHROPIC_AUTH_TOKEN or
    # ANTHROPIC_API_KEY env var.  Set this to use a different provider's key.

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
    bash_command_denylist: list[str] = field(default_factory=list)
    # Shell commands (or leading tokens) that BashTool will refuse to execute.
    # Each entry is matched against the first whitespace-separated token of the
    # command string (case-sensitive, path-basename stripped).  Example:
    #   bash_command_denylist = ["rm", "curl", "wget", "nc", "ssh"]

    # --- loop ---
    max_iterations: int = 5

    # --- tool-use budget ---
    max_tool_turns: int = 30
    # Cap on the number of tool-use turns in a single executor call_with_tools
    # loop.  Lower values reduce runaway token spend on simple tasks; higher
    # values allow more complex multi-step executions.  Valid range: 1–200.

    # --- observability ---
    log_level: str = "INFO"
    # Python logging level name for the harness logger hierarchy.
    # Valid values: "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL".
    # Applied by HarnessLoop.__init__ and PipelineLoop.__init__ via
    # apply_log_level() so every run picks up the configured verbosity.

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

        # Warn when the model string looks like a raw Anthropic model ID rather
        # than a LiteLLM-prefixed name.  The harness uses LiteLLM routing, so
        # bare IDs like "claude-3-opus-20240229" will fail at runtime with a
        # confusing auth error.  Valid forms: "bedrock/...", "anthropic/...",
        # "vertex_ai/...", or any other LiteLLM provider prefix.
        _model_stripped = self.model.strip()
        if "/" not in _model_stripped and _model_stripped.startswith("claude"):
            log.warning(
                "HarnessConfig.model=%r looks like a bare Anthropic model ID "
                "without a LiteLLM provider prefix.  Did you mean "
                "'anthropic/%s' or 'bedrock/%s'?  "
                "Bare IDs may cause auth errors at runtime.",
                _model_stripped, _model_stripped, _model_stripped,
            )

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

        # --- validate max_tool_turns ---
        if self.max_tool_turns < 1:
            raise ValueError(
                f"HarnessConfig.max_tool_turns must be ≥ 1, got {self.max_tool_turns}"
            )
        if self.max_tool_turns > 200:
            log.warning(
                "HarnessConfig.max_tool_turns=%d is very large — "
                "this may allow runaway tool loops and high token spend; "
                "typical values are 10–50",
                self.max_tool_turns,
            )

        # --- validate extra_tools entries are non-empty strings ---
        bad_extra = [t for t in self.extra_tools if not isinstance(t, str) or not t.strip()]
        if bad_extra:
            raise ValueError(
                f"HarnessConfig.extra_tools contains invalid entries: {bad_extra!r}. "
                "All entries must be non-empty strings (tool names)."
            )

        # --- validate allowed_tools entries are non-empty strings ---
        bad_allowed = [t for t in self.allowed_tools if not isinstance(t, str) or not t.strip()]
        if bad_allowed:
            raise ValueError(
                f"HarnessConfig.allowed_tools contains invalid entries: {bad_allowed!r}. "
                "All entries must be non-empty strings (tool names)."
            )

        # --- validate bash_command_denylist entries are non-empty strings ---
        bad_deny = [t for t in self.bash_command_denylist if not isinstance(t, str) or not t.strip()]
        if bad_deny:
            raise ValueError(
                f"HarnessConfig.bash_command_denylist contains invalid entries: {bad_deny!r}. "
                "All entries must be non-empty strings (command names/prefixes)."
            )

        # --- validate log_level ---
        _level_upper = self.log_level.upper().strip()
        if _level_upper not in _VALID_LOG_LEVELS:
            raise ValueError(
                f"HarnessConfig.log_level={self.log_level!r} is not valid.  "
                f"Must be one of: {sorted(_VALID_LOG_LEVELS)}.  "
                "Example: 'DEBUG' for verbose output, 'WARNING' for quiet runs."
            )
        self.log_level = _level_upper

    def apply_log_level(self) -> None:
        """Apply ``self.log_level`` to the root ``harness`` logger hierarchy.

        Call this once at startup (``HarnessLoop.__init__``,
        ``PipelineLoop.__init__``) so all child loggers inherit the level.
        Does not touch the root logging configuration — only the ``harness``
        package logger — so the caller's own logging setup is preserved.
        """
        harness_log = logging.getLogger("harness")
        harness_log.setLevel(self.log_level)
        log.debug("apply_log_level: harness logger set to %s", self.log_level)

    def startup_banner(self) -> str:
        """Return a one-line structured startup banner for the run log.

        Emitting this at INFO level at the start of every run gives operators
        a single line they can grep for in long log files to find run boundaries
        and quickly audit configuration without reading config files.

        Example output::

            harness startup: model=bedrock/claude-sonnet-4-6 max_tokens=8096 \
workspace=/home/user/project max_iterations=5 max_tool_turns=30 \
allowed_tools=all log_level=INFO

        Returns:
            A single-line string (no trailing newline).
        """
        tools_str = ",".join(self.allowed_tools) if self.allowed_tools else "all"
        return (
            f"harness startup: model={self.model} max_tokens={self.max_tokens} "
            f"workspace={self.workspace} max_iterations={self.max_iterations} "
            f"max_tool_turns={self.max_tool_turns} allowed_tools={tools_str} "
            f"log_level={self.log_level}"
        )

    def is_path_allowed(self, path: str | Path) -> bool:
        """Check whether *path* falls under one of the allowed directories.

        Rejects null bytes before calling into the OS — a null byte can be
        used to truncate the path string at the OS syscall boundary, causing
        the prefix check to pass on the Python string while the OS operates
        on a different (shorter) path.

        Uses ``os.path.realpath`` (which calls ``realpath(3)``) rather than
        ``Path.resolve()`` to resolve symlinks before the comparison, closing
        the symlink-escape attack where a symlink inside an allowed path points
        to a target outside it.
        """
        import os
        path_str = str(path)
        if "\x00" in path_str:
            return False
        resolved = os.path.realpath(path_str)
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
        # Strip JSON "comment" keys (// or _ prefix) before validation.
        data = {k: v for k, v in data.items() if not k.startswith("//") and not k.startswith("_")}
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

    def __post_init__(self) -> None:
        import re as _re
        try:
            _re.compile(self.score_pattern)
        except _re.error as exc:
            raise ValueError(
                f"DualEvaluatorConfig.score_pattern is not a valid regex: {exc}"
            ) from exc


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

    # File context budget (chars) for source file injection into executor prompts.
    # Controls the total size of $file_context in executor prompts.  Lower values
    # prevent context-window bloat in long runs; higher values give the executor
    # more code visibility.  Default matches the original hard-coded limit.
    max_file_context_chars: int = 60_000

    # Meta-review: periodic pause to review progress across rounds
    meta_review_interval: int = 0    # 0 = disabled; N = run meta-review every N rounds
    meta_review_system: str = ""     # custom system prompt (empty = use default)
    meta_review_inject: bool = False  # prepend meta-review findings to next round's prompts

    # Auto-push
    auto_push_interval: int = 0    # 0 = disabled; N = push every N outer rounds
    auto_push_remote: str = "origin"
    auto_push_branch: str = ""     # empty = push current branch

    # Auto-tag (configurable; legacy end-of-run tag still fires when interval=0)
    auto_tag_interval: int = 0          # 0 = legacy end-of-run only; N = tag every N rounds
    auto_tag_prefix: str = "v-auto"     # tag name prefix (legacy: "v-auto")
    auto_tag_push: bool = False         # whether to git push the tag after creating it
    auto_tag_min_score: float = 7.0     # minimum best_score to create a tag (legacy: 7.0)
    auto_tag_at_end: bool = False       # force a tag at end of pipeline run regardless of
                                        # interval / score / patience early stop. Use this
                                        # for self-improvement loops where the next chunk
                                        # is triggered by tag push: every chunk MUST emit
                                        # a tag or the loop dies.

    # Rich commit metadata
    rich_commit_metadata: bool = False  # include score/round/phase in commit messages

    # Prompt auto-update
    auto_update_prompts: bool = False  # allow meta-review to modify phase prompts

    # Manual/Automatic dual mode
    run_mode: str = "automatic"  # "automatic" or "manual" (pause at meta-review)

    def __post_init__(self) -> None:
        # --- validate evaluation_mode (Literal enforcement at runtime) ---
        _VALID_EVAL_MODES = ("three_way", "dual_isolated")
        if self.evaluation_mode not in _VALID_EVAL_MODES:
            raise ValueError(
                f"PipelineConfig.evaluation_mode={self.evaluation_mode!r} is not valid.  "
                f"Must be one of: {_VALID_EVAL_MODES}.  "
                "Check for typos — common mistakes: 'dual_isolate', 'threeway', 'three-way'."
            )

        # --- validate output_dir is a non-empty string ---
        if not isinstance(self.output_dir, str) or not self.output_dir.strip():
            raise ValueError(
                f"PipelineConfig.output_dir must be a non-empty string, "
                f"got {self.output_dir!r}.  "
                "Set it to a relative or absolute directory path, e.g. 'harness_output'."
            )

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
        if self.min_synthesis_chars > 10_000:
            log.warning(
                "PipelineConfig.min_synthesis_chars=%d is very large — "
                "synthesis may always retry and fall back to the best inner result; "
                "typical values are 100–1 000",
                self.min_synthesis_chars,
            )

        # --- validate max_file_context_chars ---
        if self.max_file_context_chars < 1_000:
            raise ValueError(
                f"PipelineConfig.max_file_context_chars must be >= 1000, "
                f"got {self.max_file_context_chars}"
            )
        if self.max_file_context_chars > 500_000:
            log.warning(
                "PipelineConfig.max_file_context_chars=%d is very large — "
                "this may cause prompt overflow and high token costs; "
                "typical values are 30000–80000",
                self.max_file_context_chars,
            )

        # --- validate synthesis_system is a plain string (not an accidental list) ---
        if not isinstance(self.synthesis_system, str):
            raise ValueError(
                f"PipelineConfig.synthesis_system must be a string, "
                f"got {type(self.synthesis_system).__name__!r}"
            )

        # --- validate meta-review fields ---
        if self.meta_review_interval < 0:
            raise ValueError(
                f"PipelineConfig.meta_review_interval must be >= 0, "
                f"got {self.meta_review_interval}"
            )
        if self.auto_push_interval < 0:
            raise ValueError(
                f"PipelineConfig.auto_push_interval must be >= 0, "
                f"got {self.auto_push_interval}"
            )
        if self.auto_tag_interval < 0:
            raise ValueError(
                f"PipelineConfig.auto_tag_interval must be >= 0, "
                f"got {self.auto_tag_interval}"
            )
        if not isinstance(self.auto_tag_prefix, str) or not self.auto_tag_prefix.strip():
            raise ValueError(
                f"PipelineConfig.auto_tag_prefix must be a non-empty string, "
                f"got {self.auto_tag_prefix!r}"
            )
        if self.run_mode not in ("automatic", "manual"):
            raise ValueError(
                f"PipelineConfig.run_mode must be 'automatic' or 'manual', "
                f"got {self.run_mode!r}"
            )

        # --- scale warnings for inner_rounds / outer_rounds ---
        if self.outer_rounds > 20:
            log.warning(
                "PipelineConfig.outer_rounds=%d is very large — "
                "consider using patience early-stopping instead",
                self.outer_rounds,
            )
        if self.inner_rounds > 10:
            log.warning(
                "PipelineConfig.inner_rounds=%d is very large — "
                "more than 5–6 inner rounds rarely improves synthesis quality",
                self.inner_rounds,
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
        # Strip JSON "comment" keys (// or _ prefix) before validation.
        data = {k: v for k, v in data.items() if not k.startswith("//") and not k.startswith("_")}
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
            from harness.pipeline.phase import PhaseConfig as _PhaseConfig
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
                # Strip comment keys from phase dicts too
                phase_raw = {k: v for k, v in phase_raw.items()
                             if not k.startswith("//") and not k.startswith("_")}
                phases_raw[i] = phase_raw
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
