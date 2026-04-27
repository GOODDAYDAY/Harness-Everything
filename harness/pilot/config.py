"""Pilot configuration — defines the complete config structure for the daily improvement loop.

Separates Feishu credentials, schedule, diagnosis/execution agent configs, and discussion
LLM settings into distinct sections.  Parsed from a single JSON file at startup.
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
class DiscussionConfig:
    """LLM settings for the operator discussion phase."""

    model: str = ""
    base_url: str = ""
    api_key: str = ""
    max_tokens: int = _DEFAULT_DISCUSSION_MAX_TOKENS

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DiscussionConfig:
        """Parse discussion LLM config from a dict."""
        return cls(
            model=data.get("model", ""),
            base_url=data.get("base_url", ""),
            api_key=data.get("api_key", ""),
            max_tokens=data.get("max_tokens", _DEFAULT_DISCUSSION_MAX_TOKENS),
        )


@dataclass
class PilotConfig:
    """Complete configuration for the pilot daemon.

    Combines Feishu credentials, schedule, diagnosis agent config,
    execution overrides, discussion LLM settings, and safety parameters.
    """

    feishu: FeishuConfig
    schedule: ScheduleConfig
    diagnosis: dict[str, Any]
    execution: dict[str, Any] = field(default_factory=dict)
    discussion: DiscussionConfig = field(default_factory=DiscussionConfig)
    proposal_expiry_hours: int = _DEFAULT_PROPOSAL_EXPIRY_HOURS

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PilotConfig:
        """Parse pilot config from a JSON-compatible dict.

        Validates required sections and applies defaults for optional ones.
        """
        # 1. Validate required sections
        config = cls._validate_required_sections(data)

        # 2. Parse each section
        feishu = FeishuConfig.from_dict(config["feishu"])
        schedule = cls._parse_schedule(config.get("schedule", {}))
        diagnosis = cls._parse_diagnosis(config["diagnosis"])
        execution = config.get("execution", {})
        discussion = DiscussionConfig.from_dict(config.get("discussion", {}))

        # 3. Build config
        pilot = cls(
            feishu=feishu,
            schedule=schedule,
            diagnosis=diagnosis,
            execution=execution,
            discussion=discussion,
            proposal_expiry_hours=config.get(
                "proposal_expiry_hours", _DEFAULT_PROPOSAL_EXPIRY_HOURS
            ),
        )
        log.info(
            "PilotConfig loaded: schedule=%02d:%02d, diagnosis_cycles=%d, execution_cycles=%d, expiry=%dh",
            schedule.hour,
            schedule.minute,
            diagnosis.get("max_cycles", _DEFAULT_DIAGNOSIS_MAX_CYCLES),
            execution.get("max_cycles", _DEFAULT_EXECUTION_MAX_CYCLES),
            pilot.proposal_expiry_hours,
        )
        return pilot

    def build_diagnosis_agent_config(self) -> dict[str, Any]:
        """Build a complete AgentConfig-compatible dict for the diagnosis run.

        Ensures diagnosis-specific defaults: auto_commit=false, auto_evaluate=false.
        """
        cfg = dict(self.diagnosis)
        cfg.setdefault("auto_commit", False)
        cfg.setdefault("auto_evaluate", False)
        cfg.setdefault("max_cycles", _DEFAULT_DIAGNOSIS_MAX_CYCLES)
        cfg.setdefault("continuous", False)
        log.debug("Built diagnosis agent config, max_cycles=%d", cfg["max_cycles"])
        return cfg

    def build_execution_agent_config(self, mission: str) -> dict[str, Any]:
        """Build a complete AgentConfig-compatible dict for the execution run.

        Merges execution overrides onto the diagnosis base, then sets the
        approved mission text.
        """
        cfg = dict(self.diagnosis)
        cfg.update(self.execution)
        cfg["mission"] = mission
        cfg.setdefault("auto_commit", True)
        cfg.setdefault("max_cycles", _DEFAULT_EXECUTION_MAX_CYCLES)
        cfg.setdefault("continuous", False)
        log.debug("Built execution agent config, max_cycles=%d", cfg["max_cycles"])
        return cfg

    def build_discussion_harness_config(self) -> dict[str, Any]:
        """Build a HarnessConfig-compatible dict for the discussion LLM.

        Falls back to the diagnosis harness config for model/base_url/api_key
        if the discussion section doesn't specify them.
        """
        diag_harness = self.diagnosis.get("harness", {})
        return {
            "model": self.discussion.model or diag_harness.get("model", ""),
            "base_url": self.discussion.base_url or diag_harness.get("base_url", ""),
            "api_key": self.discussion.api_key or diag_harness.get("api_key", ""),
            "max_tokens": self.discussion.max_tokens,
        }

    # ══════════════════════════════════════════════════════════════════════
    #  Private helpers
    # ══════════════════════════════════════════════════════════════════════

    @staticmethod
    def _validate_required_sections(data: dict[str, Any]) -> dict[str, Any]:
        """Ensure required top-level sections exist."""
        cleaned = {k: v for k, v in data.items() if not str(k).startswith("//")}
        missing = []
        if "feishu" not in cleaned or not isinstance(cleaned["feishu"], dict):
            missing.append("feishu")
        if "diagnosis" not in cleaned or not isinstance(cleaned["diagnosis"], dict):
            missing.append("diagnosis")
        if missing:
            raise ValueError(f"Missing required pilot config sections: {', '.join(missing)}")
        return cleaned

    @staticmethod
    def _parse_schedule(data: dict[str, Any]) -> ScheduleConfig:
        """Parse schedule config with defaults."""
        return ScheduleConfig(
            hour=data.get("hour", _DEFAULT_SCHEDULE_HOUR),
            minute=data.get("minute", _DEFAULT_SCHEDULE_MINUTE),
        )

    @staticmethod
    def _parse_diagnosis(data: dict[str, Any]) -> dict[str, Any]:
        """Validate diagnosis config has a harness section."""
        if "harness" not in data or not isinstance(data["harness"], dict):
            raise ValueError("diagnosis section must contain a 'harness' dict")
        return data
