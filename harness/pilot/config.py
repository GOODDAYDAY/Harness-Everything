"""Pilot configuration — defines the complete config structure for the daily improvement loop.

Projects, LLM settings, Feishu credentials, schedule, diagnosis/execution parameters,
and discussion overrides are grouped into cohesive sections.  Parsed from a single JSON
file at startup.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

_DEFAULT_SCHEDULE_HOUR: int = 9
_DEFAULT_SCHEDULE_MINUTE: int = 0
_DEFAULT_DIAGNOSIS_MAX_CYCLES: int = 5
_DEFAULT_EXECUTION_MAX_CYCLES: int = 50
_DEFAULT_PROPOSAL_EXPIRY_HOURS: int = 24
_DEFAULT_DISCUSSION_MAX_TOKENS: int = 8096
_DEFAULT_MAX_TOKENS: int = 16384


@dataclass
class FeishuConfig:
    """Feishu application credentials and target chat."""

    app_id: str
    app_secret: str
    chat_id: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FeishuConfig:
        """Parse Feishu config from a dict, validating required fields."""
        missing = [k for k in ("app_id", "app_secret", "chat_id") if not data.get(k)]
        if missing:
            raise ValueError(f"Missing required feishu config fields: {', '.join(missing)}")
        return cls(
            app_id=data["app_id"],
            app_secret=data["app_secret"],
            chat_id=data["chat_id"],
        )


@dataclass
class ScheduleConfig:
    """Daily trigger schedule (hour + minute in local time)."""

    hour: int = _DEFAULT_SCHEDULE_HOUR
    minute: int = _DEFAULT_SCHEDULE_MINUTE

    def __post_init__(self) -> None:
        if not (0 <= self.hour <= 23):
            raise ValueError(f"schedule.hour must be 0-23, got {self.hour}")
        if not (0 <= self.minute <= 59):
            raise ValueError(f"schedule.minute must be 0-59, got {self.minute}")


@dataclass
class ProjectConfig:
    """A single project that the pilot can analyze and modify."""

    name: str
    workspace: str
    tools: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProjectConfig:
        """Parse a project config from a dict."""
        if not data.get("name") or not data.get("workspace"):
            raise ValueError("Each project must have 'name' and 'workspace'")
        return cls(
            name=data["name"],
            workspace=data["workspace"],
            tools=data.get("tools", {}),
        )


@dataclass
class LLMConfig:
    """Shared LLM settings used by diagnosis, execution, and discussion."""

    model: str = ""
    base_url: str = ""
    api_key: str = ""
    max_tokens: int = _DEFAULT_MAX_TOKENS

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LLMConfig:
        """Parse LLM config from a dict."""
        return cls(
            model=data.get("model", ""),
            base_url=data.get("base_url", ""),
            api_key=data.get("api_key", ""),
            max_tokens=data.get("max_tokens", _DEFAULT_MAX_TOKENS),
        )


@dataclass
class PilotConfig:
    """Complete configuration for the pilot daemon.

    Core structure:
    - projects: list of repos to analyze/modify (workspace, tools per project)
    - llm: shared LLM settings (model, base_url, api_key)
    - feishu: notification channel
    - schedule: when to trigger
    - diagnosis/execution: agent run parameters (max_cycles, etc.)
    """

    feishu: FeishuConfig
    schedule: ScheduleConfig
    projects: list[ProjectConfig]
    llm: LLMConfig
    diagnosis: dict[str, Any] = field(default_factory=dict)
    execution: dict[str, Any] = field(default_factory=dict)
    proposal_expiry_hours: int = _DEFAULT_PROPOSAL_EXPIRY_HOURS

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PilotConfig:
        """Parse pilot config from a JSON-compatible dict.

        Validates required sections and applies defaults for optional ones.
        """
        # 1. Strip comment keys
        config = {k: v for k, v in data.items() if not str(k).startswith("//")}

        # 2. Validate required sections
        missing = []
        if "feishu" not in config or not isinstance(config["feishu"], dict):
            missing.append("feishu")
        if "projects" not in config or not isinstance(config["projects"], list):
            missing.append("projects")
        if missing:
            raise ValueError(f"Missing required pilot config sections: {', '.join(missing)}")

        # 3. Parse each section
        feishu = FeishuConfig.from_dict(config["feishu"])
        schedule = ScheduleConfig(
            hour=config.get("schedule", {}).get("hour", _DEFAULT_SCHEDULE_HOUR),
            minute=config.get("schedule", {}).get("minute", _DEFAULT_SCHEDULE_MINUTE),
        )
        projects = [ProjectConfig.from_dict(p) for p in config["projects"]]
        if not projects:
            raise ValueError("At least one project is required")
        llm = LLMConfig.from_dict(config.get("llm", {}))

        # 4. Build config
        pilot = cls(
            feishu=feishu,
            schedule=schedule,
            projects=projects,
            llm=llm,
            diagnosis=config.get("diagnosis", {}),
            execution=config.get("execution", {}),
            proposal_expiry_hours=config.get(
                "proposal_expiry_hours", _DEFAULT_PROPOSAL_EXPIRY_HOURS,
            ),
        )
        log.info(
            "PilotConfig loaded: %d projects, schedule=%02d:%02d, expiry=%dh",
            len(projects), schedule.hour, schedule.minute, pilot.proposal_expiry_hours,
        )
        return pilot

    # ══════════════════════════════════════════════════════════════════════
    #  Agent config builders
    # ══════════════════════════════════════════════════════════════════════

    def build_diagnosis_agent_config(self, mission: str) -> dict[str, Any]:
        """Build an AgentConfig-compatible dict for the diagnosis agent run.

        The diagnosis agent has read-only tool access (file read, search, db_query)
        so it can freely investigate production data and correlate with source code.
        """
        harness_cfg = self._build_harness_config()

        # Restrict to read-only tools — diagnosis must not modify code
        harness_cfg["allowed_tools"] = [
            "batch_read", "grep_search", "glob_search", "tree",
            "list_directory", "symbol_extractor", "code_analysis",
            "cross_reference", "feature_search", "project_map",
            "file_info", "data_flow", "call_graph",
            "dependency_analyzer", "git_status", "git_diff", "git_log",
            "context_budget", "scratchpad", "db_query",
        ]

        max_cycles = self.diagnosis.get("max_cycles", _DEFAULT_DIAGNOSIS_MAX_CYCLES)
        return {
            "harness": harness_cfg,
            "mission": mission,
            "max_cycles": max_cycles,
            "auto_commit": False,
            "auto_push": False,
            "auto_evaluate": False,
            "cycle_hooks": [],
            "continuous": False,
        }

    def build_execution_agent_config(self, mission: str) -> dict[str, Any]:
        """Build a complete AgentConfig-compatible dict for the execution run.

        Sets commit_repos so the agent commits in all project repos.
        """
        harness_cfg = self._build_harness_config()
        execution = self.execution

        # commit_repos: all project workspace paths (absolute)
        commit_repos = [p.workspace for p in self.projects]

        max_cycles = execution.get("max_cycles", _DEFAULT_EXECUTION_MAX_CYCLES)
        return {
            "harness": harness_cfg,
            "mission": mission,
            "max_cycles": max_cycles,
            "commit_repos": commit_repos,
            "auto_commit": execution.get("auto_commit", True),
            "auto_push": execution.get("auto_push", False),
            "auto_push_remote": execution.get("push_remote", "origin"),
            "auto_push_branch": execution.get("push_branch", "main"),
            "continuous": False,
        }

    def build_discussion_harness_config(self) -> dict[str, Any]:
        """Build a HarnessConfig-compatible dict for the discussion LLM."""
        return {
            "model": self.llm.model,
            "base_url": self.llm.base_url,
            "api_key": self.llm.api_key,
            "max_tokens": self.llm.max_tokens,
        }

    # ══════════════════════════════════════════════════════════════════════
    #  Private helpers
    # ══════════════════════════════════════════════════════════════════════

    def _build_harness_config(self) -> dict[str, Any]:
        """Build the common harness config section from projects + llm.

        First project is the workspace; others are allowed_paths.
        Tool configs from all projects are merged.
        """
        primary = self.projects[0]
        harness: dict[str, Any] = {
            "workspace": primary.workspace,
            "model": self.llm.model,
            "base_url": self.llm.base_url,
            "api_key": self.llm.api_key,
            "max_tokens": self.llm.max_tokens,
        }

        # allowed_paths = all project workspaces
        if len(self.projects) > 1:
            harness["allowed_paths"] = [p.workspace for p in self.projects]

        # Merge extra_tools and tool_config from all projects
        extra_tools: list[str] = []
        tool_config: dict[str, Any] = {}
        for project in self.projects:
            for tool_name, config in project.tools.items():
                if tool_name not in extra_tools:
                    extra_tools.append(tool_name)
                tool_config[tool_name] = config

        if extra_tools:
            harness["extra_tools"] = extra_tools
            harness["tool_config"] = tool_config

        return harness
