"""Tests for PilotConfig — parsing, validation, and builder methods."""

import pytest

from harness.pilot.config import (
    FeishuConfig,
    LLMConfig,
    PilotConfig,
    ProjectConfig,
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
        "projects": [
            {"name": "TestProject", "workspace": "/tmp/test-workspace"},
        ],
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
        assert len(cfg.projects) == 1
        assert cfg.projects[0].name == "TestProject"

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

    def test_missing_projects_raises(self):
        """Missing projects section raises ValueError."""
        raw = _minimal_config()
        del raw["projects"]
        with pytest.raises(ValueError, match="projects"):
            PilotConfig.from_dict(raw)

    def test_empty_projects_raises(self):
        """Empty projects list raises ValueError."""
        raw = _minimal_config()
        raw["projects"] = []
        with pytest.raises(ValueError, match="At least one project"):
            PilotConfig.from_dict(raw)

    def test_multi_project(self):
        """Multiple projects are parsed correctly."""
        raw = _minimal_config()
        raw["projects"].append({"name": "Second", "workspace": "/tmp/second"})
        cfg = PilotConfig.from_dict(raw)
        assert len(cfg.projects) == 2
        assert cfg.projects[1].name == "Second"


class TestProjectConfig:
    """Project config validation."""

    def test_missing_name_raises(self):
        with pytest.raises(ValueError, match="name"):
            ProjectConfig.from_dict({"workspace": "/tmp/ws"})

    def test_missing_workspace_raises(self):
        with pytest.raises(ValueError, match="workspace"):
            ProjectConfig.from_dict({"name": "X"})

    def test_tools_optional(self):
        p = ProjectConfig.from_dict({"name": "X", "workspace": "/tmp"})
        assert p.tools == {}

    def test_tools_parsed(self):
        p = ProjectConfig.from_dict({
            "name": "X", "workspace": "/tmp",
            "tools": {"db_query": {"dsn": "pg://host/db"}},
        })
        assert p.tools["db_query"]["dsn"] == "pg://host/db"


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
    """Config builder methods for diagnosis/execution/discussion."""

    def test_diagnosis_config_with_tools(self):
        """Diagnosis config merges tools from projects."""
        raw = _minimal_config()
        raw["projects"][0]["tools"] = {"db_query": {"dsn": "pg://host/db"}}
        raw["diagnosis"] = {"max_cycles": 3}
        cfg = PilotConfig.from_dict(raw)
        diag = cfg.build_diagnosis_agent_config("test mission")
        assert diag["mission"] == "test mission"
        assert diag["max_cycles"] == 3
        assert diag["auto_commit"] is False
        assert "db_query" in diag["harness"]["extra_tools"]
        assert diag["harness"]["tool_config"]["db_query"]["dsn"] == "pg://host/db"

    def test_diagnosis_config_multi_project_paths(self):
        """Diagnosis config sets workspace + allowed_paths for multi-project."""
        raw = _minimal_config()
        raw["projects"].append({"name": "Second", "workspace": "/tmp/second"})
        cfg = PilotConfig.from_dict(raw)
        diag = cfg.build_diagnosis_agent_config("mission")
        assert diag["harness"]["workspace"] == "/tmp/test-workspace"
        assert "/tmp/test-workspace" in diag["harness"]["allowed_paths"]
        assert "/tmp/second" in diag["harness"]["allowed_paths"]

    def test_execution_config(self):
        """Execution config merges execution overrides."""
        raw = _minimal_config()
        raw["execution"] = {"max_cycles": 50, "auto_commit": True}
        cfg = PilotConfig.from_dict(raw)
        exe = cfg.build_execution_agent_config("approved mission")
        assert exe["mission"] == "approved mission"
        assert exe["max_cycles"] == 50
        assert exe["auto_commit"] is True
        assert "harness" in exe

    def test_execution_config_commit_repos(self):
        """Execution config sets commit_repos for all projects."""
        raw = _minimal_config()
        raw["projects"].append({"name": "Second", "workspace": "/tmp/second"})
        cfg = PilotConfig.from_dict(raw)
        exe = cfg.build_execution_agent_config("mission")
        assert "/tmp/test-workspace" in exe["commit_repos"]
        assert "/tmp/second" in exe["commit_repos"]

    def test_discussion_config_from_llm(self):
        """Discussion LLM config uses shared llm settings."""
        raw = _minimal_config()
        raw["llm"] = {"model": "test-model", "base_url": "http://localhost"}
        cfg = PilotConfig.from_dict(raw)
        disc = cfg.build_discussion_harness_config()
        assert disc["model"] == "test-model"
        assert disc["base_url"] == "http://localhost"

    def test_comment_keys_stripped(self):
        """Keys starting with // are ignored."""
        raw = _minimal_config()
        raw["// comment"] = "this should be ignored"
        cfg = PilotConfig.from_dict(raw)
        assert cfg.feishu.app_id == "cli_test"
