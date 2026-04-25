"""Unit tests for harness.core.config.

Covers: HarnessConfig, PipelineConfig, DualEvaluatorConfig validation,
is_path_allowed(), from_dict(), startup_banner(), and apply_log_level().
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from harness.core.config import (
    DualEvaluatorConfig,
    EvaluatorConfig,
    HarnessConfig,
    PipelineConfig,
    PlannerConfig,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def make_config(tmp_path: Path, **overrides) -> HarnessConfig:
    """Build a minimal valid HarnessConfig using *tmp_path* as the workspace."""
    defaults = dict(workspace=str(tmp_path), model="test-model")
    defaults.update(overrides)
    return HarnessConfig(**defaults)


# ===========================================================================
# HarnessConfig.__post_init__ — numeric validation
# ===========================================================================

class TestHarnessConfigValidation:
    def test_valid_defaults_accepted(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path)
        assert cfg.model == "test-model"
        assert cfg.max_tokens > 0

    def test_max_tokens_below_one_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="max_tokens"):
            make_config(tmp_path, max_tokens=0)

    def test_max_tokens_above_64000_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="max_tokens"):
            make_config(tmp_path, max_tokens=64_001)

    def test_max_tokens_at_64000_accepted(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path, max_tokens=64_000)
        assert cfg.max_tokens == 64_000

    def test_max_tokens_at_one_accepted(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path, max_tokens=1)
        assert cfg.max_tokens == 1

    def test_max_iterations_below_one_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="max_iterations"):
            make_config(tmp_path, max_iterations=0)

    def test_max_iterations_one_accepted(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path, max_iterations=1)
        assert cfg.max_iterations == 1

    def test_max_tool_turns_below_one_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="max_tool_turns"):
            make_config(tmp_path, max_tool_turns=0)

    def test_max_tool_turns_at_200_accepted(self, tmp_path: Path) -> None:
        # 200 is accepted (though it emits a warning)
        cfg = make_config(tmp_path, max_tool_turns=200)
        assert cfg.max_tool_turns == 200

    def test_extra_tools_empty_string_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="extra_tools"):
            make_config(tmp_path, extra_tools=[""])

    def test_extra_tools_whitespace_string_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="extra_tools"):
            make_config(tmp_path, extra_tools=["  "])

    def test_extra_tools_valid_strings_accepted(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path, extra_tools=["web_search", "image_gen"])
        assert cfg.extra_tools == ["web_search", "image_gen"]

    def test_extra_tools_empty_list_accepted(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path, extra_tools=[])
        assert cfg.extra_tools == []

    def test_allowed_tools_empty_string_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="allowed_tools"):
            make_config(tmp_path, allowed_tools=[""])

    def test_allowed_tools_none_is_allowed(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path, allowed_tools=None)
        assert cfg.allowed_tools is None

    def test_allowed_tools_valid_list_accepted(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path, allowed_tools=["bash", "read_file"])
        assert cfg.allowed_tools == ["bash", "read_file"]

    def test_bash_command_denylist_empty_string_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="bash_command_denylist"):
            make_config(tmp_path, bash_command_denylist=[""])

    def test_bash_command_denylist_valid_accepted(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path, bash_command_denylist=["rm", "curl"])
        assert cfg.bash_command_denylist == ["rm", "curl"]


# ===========================================================================
# HarnessConfig.__post_init__ — log_level
# ===========================================================================

class TestHarnessLogLevel:
    @pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    def test_valid_log_levels_accepted(self, tmp_path: Path, level: str) -> None:
        cfg = make_config(tmp_path, log_level=level)
        assert cfg.log_level == level

    def test_lowercase_log_level_normalised(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path, log_level="debug")
        assert cfg.log_level == "DEBUG"

    def test_invalid_log_level_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="log_level"):
            make_config(tmp_path, log_level="VERBOSE")

    def test_empty_log_level_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="log_level"):
            make_config(tmp_path, log_level="")


# ===========================================================================
# HarnessConfig.__post_init__ — workspace validation
# ===========================================================================

class TestHarnessWorkspace:
    def test_workspace_resolved_to_absolute(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path)
        assert os.path.isabs(cfg.workspace)

    def test_nonexistent_workspace_raises(self, tmp_path: Path) -> None:
        with pytest.raises((ValueError, FileNotFoundError)):
            make_config(tmp_path, workspace=str(tmp_path / "nonexistent"))

    def test_workspace_in_default_allowed_paths(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path)
        assert cfg.workspace in cfg.allowed_paths

    def test_custom_allowed_paths_resolved(self, tmp_path: Path) -> None:
        allowed = str(tmp_path)
        cfg = make_config(tmp_path, allowed_paths=[allowed])
        assert any(p == os.path.realpath(allowed) for p in cfg.allowed_paths)


# ===========================================================================
# HarnessConfig.__post_init__ — homoglyph_blocklist
# ===========================================================================

class TestHomoglyphBlocklist:
    def test_default_blocklist_populated(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path)
        assert len(cfg.homoglyph_blocklist) > 0

    def test_fraction_slash_in_default_blocklist(self, tmp_path: Path) -> None:
        # U+2044 FRACTION SLASH looks like forward slash
        cfg = make_config(tmp_path)
        assert '\u2044' in cfg.homoglyph_blocklist

    def test_custom_blocklist_not_overridden(self, tmp_path: Path) -> None:
        custom: dict[str, str] = {'a': 'fake a'}
        cfg = make_config(tmp_path, homoglyph_blocklist=custom)
        assert cfg.homoglyph_blocklist == custom


# ===========================================================================
# HarnessConfig.is_path_allowed()
# ===========================================================================

class TestIsPathAllowed:
    def test_path_inside_workspace_allowed(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path)
        inside = tmp_path / "sub" / "file.py"
        assert cfg.is_path_allowed(inside) is True

    def test_path_equal_to_workspace_allowed(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path)
        assert cfg.is_path_allowed(tmp_path) is True

    def test_path_outside_workspace_denied(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path)
        outside = tmp_path.parent / "other" / "file.py"
        assert cfg.is_path_allowed(outside) is False

    def test_null_byte_in_path_denied(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path)
        malicious = str(tmp_path) + "/file\x00.txt"
        assert cfg.is_path_allowed(malicious) is False

    def test_path_traversal_denied(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path)
        traversal = str(tmp_path) + "/../sensitive.txt"
        assert cfg.is_path_allowed(traversal) is False

    def test_string_path_inside_workspace_allowed(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path)
        inside = str(tmp_path / "file.txt")
        assert cfg.is_path_allowed(inside) is True


# ===========================================================================
# HarnessConfig.startup_banner()
# ===========================================================================

class TestStartupBanner:
    def test_banner_is_single_line(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path)
        banner = cfg.startup_banner()
        assert "\n" not in banner

    def test_banner_contains_model(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path, model="my-model")
        assert "model=my-model" in cfg.startup_banner()

    def test_banner_contains_workspace(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path)
        # workspace is resolved to real path (may differ on macOS /private/var)
        assert cfg.workspace in cfg.startup_banner()

    def test_banner_allowed_tools_all_when_none(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path, allowed_tools=None)
        assert "allowed_tools=all" in cfg.startup_banner()

    def test_banner_allowed_tools_all_when_empty_list(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path, allowed_tools=[])
        assert "allowed_tools=all" in cfg.startup_banner()

    def test_banner_allowed_tools_listed_when_set(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path, allowed_tools=["bash", "read_file"])
        assert "bash,read_file" in cfg.startup_banner()

    def test_banner_contains_log_level(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path, log_level="DEBUG")
        assert "log_level=DEBUG" in cfg.startup_banner()


# ===========================================================================
# HarnessConfig.apply_log_level()
# ===========================================================================

class TestApplyLogLevel:
    def test_sets_harness_logger_to_debug(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path, log_level="DEBUG")
        cfg.apply_log_level()
        harness_log = logging.getLogger("harness")
        assert harness_log.level == logging.DEBUG

    def test_sets_harness_logger_to_warning(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path, log_level="WARNING")
        cfg.apply_log_level()
        harness_log = logging.getLogger("harness")
        assert harness_log.level == logging.WARNING


# ===========================================================================
# HarnessConfig.from_dict()
# ===========================================================================

class TestHarnessFromDict:
    def test_minimal_dict_creates_config(self, tmp_path: Path) -> None:
        cfg = HarnessConfig.from_dict({"workspace": str(tmp_path), "model": "test"})
        assert cfg.model == "test"

    def test_unknown_key_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="unknown config key"):
            HarnessConfig.from_dict(
                {"workspace": str(tmp_path), "model": "test", "bogus_key": 42}
            )

    def test_unknown_planner_key_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="planner"):
            HarnessConfig.from_dict(
                {"workspace": str(tmp_path), "model": "test",
                 "planner": {"no_such_field": "oops"}}
            )

    def test_unknown_evaluator_key_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="evaluator"):
            HarnessConfig.from_dict(
                {"workspace": str(tmp_path), "model": "test",
                 "evaluator": {"typo_field": "oops"}}
            )

    def test_comment_keys_stripped(self, tmp_path: Path) -> None:
        # Keys starting with // or _ should not raise unknown key errors
        cfg = HarnessConfig.from_dict(
            {"workspace": str(tmp_path), "model": "test",
             "// comment": "this is a comment",
             "_note": "ignored"}
        )
        assert cfg.model == "test"

    def test_planner_subdict_applied(self, tmp_path: Path) -> None:
        cfg = HarnessConfig.from_dict(
            {"workspace": str(tmp_path), "model": "test",
             "planner": {}}
        )
        assert isinstance(cfg.planner, PlannerConfig)

    def test_evaluator_subdict_applied(self, tmp_path: Path) -> None:
        cfg = HarnessConfig.from_dict(
            {"workspace": str(tmp_path), "model": "test",
             "evaluator": {}}
        )
        assert isinstance(cfg.evaluator, EvaluatorConfig)

    def test_empty_dict_uses_defaults(self) -> None:
        # Default workspace is '.' which exists
        cfg = HarnessConfig.from_dict({})
        assert cfg.max_tokens > 0


# ===========================================================================
# DualEvaluatorConfig
# ===========================================================================

class TestDualEvaluatorConfig:
    def test_default_constructs(self) -> None:
        cfg = DualEvaluatorConfig()
        assert cfg.score_pattern is not None

    def test_valid_score_pattern_accepted(self) -> None:
        cfg = DualEvaluatorConfig(score_pattern=r"Score:\s*(\d+)")
        assert cfg.score_pattern == r"Score:\s*(\d+)"

    def test_invalid_score_pattern_raises(self) -> None:
        with pytest.raises(ValueError, match="score_pattern"):
            DualEvaluatorConfig(score_pattern="[unclosed")

    def test_valid_regex_without_capture_group_accepted(self) -> None:
        # score_pattern only requires valid regex, not a capture group
        cfg = DualEvaluatorConfig(score_pattern=r"Score:\s*\d+")
        assert cfg.score_pattern == r"Score:\s*\d+"


# ===========================================================================
# PipelineConfig.__post_init__ — validation
# ===========================================================================

class TestPipelineConfigValidation:
    def _minimal(self, **overrides) -> PipelineConfig:
        defaults: dict = {}
        defaults.update(overrides)
        return PipelineConfig(**defaults)

    def test_default_constructs(self) -> None:
        cfg = self._minimal()
        assert cfg.outer_rounds >= 1

    def test_invalid_evaluation_mode_raises(self) -> None:
        with pytest.raises(ValueError, match="evaluation_mode"):
            self._minimal(evaluation_mode="quadruple")

    @pytest.mark.parametrize("mode", ["three_way", "dual_isolated"])
    def test_valid_evaluation_modes_accepted(self, mode: str) -> None:
        cfg = self._minimal(evaluation_mode=mode)
        assert cfg.evaluation_mode == mode

    def test_outer_rounds_below_one_raises(self) -> None:
        with pytest.raises(ValueError, match="outer_rounds"):
            self._minimal(outer_rounds=0)

    def test_inner_rounds_below_one_raises(self) -> None:
        with pytest.raises(ValueError, match="inner_rounds"):
            self._minimal(inner_rounds=0)

    def test_patience_zero_accepted(self) -> None:
        # patience=0 means disabled, not invalid
        cfg = self._minimal(patience=0)
        assert cfg.patience == 0

    def test_patience_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="patience"):
            self._minimal(patience=-1)

    def test_inner_early_exit_threshold_below_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="inner_early_exit_threshold"):
            self._minimal(inner_early_exit_threshold=-0.1)

    def test_inner_early_exit_threshold_above_10_raises(self) -> None:
        with pytest.raises(ValueError, match="inner_early_exit_threshold"):
            self._minimal(inner_early_exit_threshold=10.1)

    def test_inner_early_exit_threshold_zero_accepted(self) -> None:
        # 0.0 means disabled
        cfg = self._minimal(inner_early_exit_threshold=0.0)
        assert cfg.inner_early_exit_threshold == 0.0

    def test_inner_early_exit_threshold_10_accepted(self) -> None:
        cfg = self._minimal(inner_early_exit_threshold=10.0)
        assert cfg.inner_early_exit_threshold == 10.0

    def test_inner_early_exit_threshold_midrange_accepted(self) -> None:
        cfg = self._minimal(inner_early_exit_threshold=7.0)
        assert cfg.inner_early_exit_threshold == 7.0

    def test_min_synthesis_chars_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="min_synthesis_chars"):
            self._minimal(min_synthesis_chars=-1)

    def test_min_synthesis_chars_zero_accepted(self) -> None:
        cfg = self._minimal(min_synthesis_chars=0)
        assert cfg.min_synthesis_chars == 0

    def test_max_file_context_chars_below_one_raises(self) -> None:
        with pytest.raises(ValueError, match="max_file_context_chars"):
            self._minimal(max_file_context_chars=0)

    def test_max_file_context_chars_minimum_accepted(self) -> None:
        # Minimum is 1000, not just > 0
        cfg = self._minimal(max_file_context_chars=1000)
        assert cfg.max_file_context_chars == 1000

    def test_max_file_context_chars_below_minimum_raises(self) -> None:
        with pytest.raises(ValueError, match="max_file_context_chars"):
            self._minimal(max_file_context_chars=100)

    def test_synthesis_system_must_be_string(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            self._minimal(synthesis_system=12345)  # type: ignore[arg-type]

    def test_synthesis_system_string_accepted(self) -> None:
        cfg = self._minimal(synthesis_system="You are a synthesiser.")
        assert cfg.synthesis_system == "You are a synthesiser."

    def test_run_mode_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="run_mode"):
            self._minimal(run_mode="turbo")

    @pytest.mark.parametrize("mode", ["automatic", "manual"])
    def test_run_mode_valid_accepted(self, mode: str) -> None:
        cfg = self._minimal(run_mode=mode)
        assert cfg.run_mode == mode

    def test_meta_review_interval_zero_accepted(self) -> None:
        # 0 = disabled, not invalid
        cfg = self._minimal(meta_review_interval=0)
        assert cfg.meta_review_interval == 0

    def test_meta_review_interval_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="meta_review_interval"):
            self._minimal(meta_review_interval=-1)

    def test_meta_review_interval_positive_accepted(self) -> None:
        cfg = self._minimal(meta_review_interval=3)
        assert cfg.meta_review_interval == 3


# ===========================================================================
# PipelineConfig.from_dict()
# ===========================================================================

class TestPipelineFromDict:
    def test_empty_dict_uses_defaults(self) -> None:
        cfg = PipelineConfig.from_dict({})
        assert cfg.outer_rounds >= 1

    def test_unknown_key_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown config key"):
            PipelineConfig.from_dict({"no_such_field": 42})

    def test_unknown_dual_evaluator_key_raises(self) -> None:
        with pytest.raises(ValueError, match="DualEvaluatorConfig"):
            PipelineConfig.from_dict({"dual_evaluator": {"bad_field": 1}})

    def test_comment_keys_stripped(self) -> None:
        cfg = PipelineConfig.from_dict({
            "// comment": "ignored",
            "_note": "also ignored",
        })
        assert cfg.outer_rounds >= 1

    def test_outer_rounds_applied(self) -> None:
        cfg = PipelineConfig.from_dict({"outer_rounds": 5})
        assert cfg.outer_rounds == 5

    def test_phases_non_dict_raises(self) -> None:
        with pytest.raises(ValueError, match="must be a dict"):
            PipelineConfig.from_dict({"phases": ["not_a_dict"]})

    def test_phases_unknown_key_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown key"):
            PipelineConfig.from_dict(
                {"phases": [{"name": "dev", "typo_key": "oops"}]}
            )

    def test_harness_subdict_applied(self, tmp_path: Path) -> None:
        cfg = PipelineConfig.from_dict(
            {"harness": {"model": "mymodel", "workspace": str(tmp_path)}}
        )
        assert cfg.harness.model == "mymodel"

    def test_dual_evaluator_subdict_applied(self) -> None:
        cfg = PipelineConfig.from_dict(
            {"dual_evaluator": {}}
        )
        assert isinstance(cfg.dual_evaluator, DualEvaluatorConfig)

    def test_phase_with_required_keys_and_comment_accepted(self) -> None:
        # Comment keys in phase dicts should not cause unknown key errors
        cfg = PipelineConfig.from_dict({
            "phases": [
                {"name": "dev", "index": 0, "system_prompt": "You are...",
                 "// note": "ignored"}
            ]
        })
        assert cfg.phases[0]["name"] == "dev"
