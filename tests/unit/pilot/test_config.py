"""Tests for PilotConfig — parsing, validation, and builder methods."""

import pytest

from harness.pilot.config import (
    DiscussionConfig,
    FeishuConfig,
    PilotConfig,
    ScheduleConfig,
)


def _minimal_config() -> dict:
    """Return a minimal valid pilot config dict."""
    return {
        "feishu": {
            "app_id": "cli_test",
            "app_secret": "secret",
            "chat_id": "oc_test",
        },
        "diagnosis": {
            "harness": {"model": "test-model", "base_url": "http://localhost"},
            "mission": "diagnose",
        },
    }


class TestPilotConfigParsing:
    """Config loading and validation."""

    def test_from_dict_minimal(self):
        """Minimal config parses with correct defaults."""
        cfg = PilotConfig.from_dict(_minimal_config())
        assert cfg.feishu.app_id == "cli_test"
        assert cfg.schedule.hour == 9
        assert cfg.schedule.minute == 0
        assert cfg.proposal_expiry_hours == 24

    def test_from_dict_custom_schedule(self):
        """Custom schedule overrides defaults."""
        raw = _minimal_config()
        raw["schedule"] = {"hour": 14, "minute": 30}
        cfg = PilotConfig.from_dict(raw)
        assert cfg.schedule.hour == 14
        assert cfg.schedule.minute == 30

    def test_missing_feishu_raises(self):
        """Missing feishu section raises ValueError."""
        raw = _minimal_config()
        del raw["feishu"]
        with pytest.raises(ValueError, match="feishu"):
            PilotConfig.from_dict(raw)

    def test_missing_diagnosis_raises(self):
        """Missing diagnosis section raises ValueError."""
        raw = _minimal_config()
        del raw["diagnosis"]
        with pytest.raises(ValueError, match="diagnosis"):
            PilotConfig.from_dict(raw)

    def test_missing_harness_in_diagnosis_raises(self):
        """Diagnosis without harness subsection raises ValueError."""
        raw = _minimal_config()
        del raw["diagnosis"]["harness"]
        with pytest.raises(ValueError, match="harness"):
            PilotConfig.from_dict(raw)


class TestFeishuConfig:
    """Feishu credential validation."""

    def test_missing_app_id_raises(self):
        raw = {"app_id": "", "app_secret": "s", "chat_id": "c"}
        with pytest.raises(ValueError, match="app_id"):
            FeishuConfig.from_dict(raw)

    def test_valid_parses(self):
        raw = {"app_id": "a", "app_secret": "s", "chat_id": "c"}
        cfg = FeishuConfig.from_dict(raw)
        assert cfg.app_id == "a"


class TestScheduleConfig:
    """Schedule hour/minute validation."""

    def test_invalid_hour_raises(self):
        with pytest.raises(ValueError, match="hour"):
            ScheduleConfig(hour=25, minute=0)

    def test_invalid_minute_raises(self):
        with pytest.raises(ValueError, match="minute"):
            ScheduleConfig(hour=9, minute=60)


class TestBuilderMethods:
    """Config builder methods for execution/discussion."""

    def test_US09_execution_config_merges_overrides(self):
        """US-09: Execution config merges execution overrides onto diagnosis base."""
        raw = _minimal_config()
        raw["execution"] = {"max_cycles": 50, "auto_commit": True}
        cfg = PilotConfig.from_dict(raw)
        exe = cfg.build_execution_agent_config("approved mission text")
        assert exe["mission"] == "approved mission text"
        assert exe["max_cycles"] == 50
        assert exe["auto_commit"] is True
        # Inherits harness from diagnosis
        assert "harness" in exe

    def test_discussion_config_falls_back_to_diagnosis(self):
        """Discussion LLM config inherits from diagnosis if not specified."""
        cfg = PilotConfig.from_dict(_minimal_config())
        disc = cfg.build_discussion_harness_config()
        assert disc["model"] == "test-model"
        assert disc["base_url"] == "http://localhost"

    def test_discussion_config_overrides(self):
        """Discussion section overrides diagnosis LLM settings."""
        raw = _minimal_config()
        raw["discussion"] = {"model": "override-model", "max_tokens": 4096}
        cfg = PilotConfig.from_dict(raw)
        disc = cfg.build_discussion_harness_config()
        assert disc["model"] == "override-model"
        assert disc["max_tokens"] == 4096

    def test_comment_keys_stripped(self):
        """Keys starting with // are ignored."""
        raw = _minimal_config()
        raw["// comment"] = "this should be ignored"
        cfg = PilotConfig.from_dict(raw)
        assert cfg.feishu.app_id == "cli_test"
