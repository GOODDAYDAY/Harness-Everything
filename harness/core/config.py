"""Harness configuration — all knobs in one place."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Valid Python logging level names accepted by HarnessConfig.log_level.
_VALID_LOG_LEVELS: frozenset[str] = frozenset(
    {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
)


@dataclass
class HarnessConfig:
    # --- LLM ---
    model: str = "bedrock/claude-sonnet-4-6"
    max_tokens: int = 8096
    base_url: str = ""
    # API base URL. If empty, uses ANTHROPIC_BASE_URL env var (if set) or
    # Anthropic default.  Set this explicitly to avoid inheriting a local proxy
    # (e.g., a local proxy at <PROXY_HOST>:<PROXY_PORT>).
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
    homoglyph_blocklist: dict[str, str] = field(default_factory=dict)
    # Unicode homoglyph characters that are disallowed in paths for security.
    # Maps character to description. If empty, uses a minimal high-risk set.
    # Example: {'\u2044': 'Fraction slash (looks like ASCII /)'}

    # --- tools ---
    allowed_tools: list[str] | None = field(default_factory=list)
    # If empty or None, all registered tools are available.
    # None is an explicit "allow all" sentinel; [] (empty list) also allows all.
    extra_tools: list[str] = field(default_factory=list)
    # Names from OPTIONAL_TOOLS (e.g. ["web_search"]) to add on top of the
    # default registry.  These are NOT included by default to keep schema size
    # small; opt in explicitly when the task needs them.
    tool_config: dict[str, Any] = field(default_factory=dict)
    # Per-tool configuration for optional tools.  Each key is a tool name
    # (e.g. "db_query") mapping to a dict of tool-specific settings.
    # The harness does not validate these — each tool validates its own
    # section at execution time.  See architecture.md "Adding tool-specific
    # config for an optional tool" for the convention.
    custom_tools_path: str = ""
    # External directory of custom Tool subclasses to auto-load.
    # Each .py file in this directory is scanned for concrete Tool subclasses
    # which are instantiated and registered in the tool registry automatically.
    # The directory is relative to workspace (or absolute).  Default "" = off.
    bash_command_denylist: list[str] = field(default_factory=list)
    # Shell commands (or leading tokens) that BashTool will refuse to execute.
    # Each entry is matched against the first whitespace-separated token of the
    # command string (case-sensitive, path-basename stripped).  Example:
    #   bash_command_denylist = ["rm", "curl", "wget", "nc", "ssh"]

    # --- tool-use budget ---
    max_tool_turns: int = 60
    # Cap on the number of tool-use turns in a single executor call_with_tools
    # loop.  Lower values reduce runaway token spend on simple tasks; higher
    # values allow more complex multi-step executions.  Valid range: 1–200.
    # Raised from 30 on 2026-04-21: empirical observation showed executors
    # hitting the 20-turn server cap in 7/7 rounds, with 50–60% of turns
    # going to read_file/grep_search for state inspection, leaving 0–1 turns
    # for actual edits. 60 gives breathing room for read → edit → verify.

    # --- API concurrency ---
    max_concurrent_llm_calls: int = 20
    # Upper bound on in-flight LLM API requests across the whole process.
    # Pipeline debate parallel inner rounds + dual evaluators + planner three-way
    # can easily stack 5+ concurrent calls against the provider; without a cap,
    # bedrock / DeepSeek proxies will start returning 429s. Clamped to [1, 20]
    # in LLM.__init__.

    # --- observability ---
    log_level: str = "INFO"
    # Python logging level name for the harness logger hierarchy.
    # Valid values: "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL".
    # Applied at startup via apply_log_level() so every run picks up
    # the configured verbosity.

    def __post_init__(self) -> None:
        # --- resolve paths ---
        self.workspace = str(Path(self.workspace).resolve())
        if not self.allowed_paths:
            self.allowed_paths = [self.workspace]
        self.allowed_paths = [str(Path(p).resolve()) for p in self.allowed_paths]
        if self.custom_tools_path:
            self.custom_tools_path = str(
                (Path(self.workspace) / self.custom_tools_path).resolve()
            )
        
        # --- initialize homoglyph blocklist if empty ---
        if not self.homoglyph_blocklist:
            # Default minimal high-risk set targeting path delimiter spoofs
            self.homoglyph_blocklist = {
                '\u0430': 'Cyrillic small a (looks like ASCII a)',
                '\u04CF': 'Cyrillic small palochka (looks like ASCII l)',
                '\u0500': 'Cyrillic capital komi s (looks like ASCII O)',
                '\u01C3': 'Latin letter retroflex click (looks like ASCII !)',
                '\u0391': 'Greek capital alpha (looks like ASCII A)',
                '\u03B1': 'Greek small alpha (looks like ASCII a)',
                '\u041E': 'Cyrillic capital O (looks like ASCII O)',
                '\u043E': 'Cyrillic small o (looks like ASCII o)',
                '\u0555': 'Armenian comma (looks like ASCII comma)',
                '\u058A': 'Armenian hyphen (looks like ASCII hyphen)',
                '\u2044': 'Fraction slash (looks like ASCII /)',
                '\uFF0F': 'Full-width solidus (looks like ASCII /)',
            }

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
        # None is a sentinel meaning "allow all tools" — skip validation.
        if self.allowed_tools is not None:
            bad_allowed = [t for t in self.allowed_tools if not isinstance(t, str) or not t.strip()]
            if bad_allowed:
                raise ValueError(
                    f"HarnessConfig.allowed_tools contains invalid entries: {bad_allowed!r}. "
                    "All entries must be non-empty strings (tool names)."
                )

        # --- validate bash_command_denylist entries are non-empty strings ---
        if self.bash_command_denylist is not None:
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

        Call this once at startup so all child loggers inherit the level.
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
workspace=/home/user/project max_tool_turns=30 \
allowed_tools=all log_level=INFO

        Returns:
            A single-line string (no trailing newline).
        """
        tools_str = ",".join(self.allowed_tools) if self.allowed_tools else "all"
        return (
            f"harness startup: model={self.model} max_tokens={self.max_tokens} "
            f"workspace={self.workspace} "
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
        from harness.core.security import validate_path_security
        
        path_str = str(path)
        # Use comprehensive security validation
        if validate_path_security(path_str, self):
            return False
        resolved = os.path.realpath(path_str)
        return any(
            resolved == ap or resolved.startswith(ap + os.sep)
            for ap in self.allowed_paths
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HarnessConfig:
        """Build config from a plain dict (e.g. loaded from YAML/JSON).

        Raises ValueError on unknown top-level keys so that typos are caught
        immediately rather than silently dropped.
        """
        import dataclasses

        data = dict(data)  # don't mutate caller's dict
        # Strip JSON "comment" keys (// or _ prefix) before validation.
        data = {k: v for k, v in data.items() if not k.startswith("//") and not k.startswith("_")}

        known_fields = {f.name for f in dataclasses.fields(cls)}
        unknown = set(data) - known_fields
        if unknown:
            raise ValueError(
                f"HarnessConfig: unknown config key(s): {sorted(unknown)}.  "
                f"Known keys: {sorted(known_fields)}"
            )

        return cls(**data)
