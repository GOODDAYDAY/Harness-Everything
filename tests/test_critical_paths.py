"""Critical-path tests for tool dispatch, tag filtering, path security,
config comment stripping, and meta-review prompt safety.

These cover previously untested code paths that are high-risk:
- ToolRegistry.execute() routing, allowed_tools enforcement, alias normalisation
- ToolRegistry.filter_by_tags() semantics
- Tool._resolve_and_check() path-security guards
- PipelineConfig.from_dict() and HarnessConfig.from_dict() comment-key stripping
- _auto_update_prompts() variable-preservation guard
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

# Resolve circular-import ordering issue (same guard as test_tools_registry.py)
import harness.core.config  # noqa: F401


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _run(coro):
    """Run a coroutine to completion using a fresh event loop.

    Using asyncio.run() instead of the deprecated get_event_loop().run_until_complete()
    avoids DeprecationWarning on Python 3.10+ where there is no current event loop
    outside an async context.
    """
    return asyncio.run(coro)


def _make_config(tmpdir: str, **kwargs: Any):
    """Return a HarnessConfig rooted at *tmpdir*."""
    from harness.core.config import HarnessConfig
    return HarnessConfig(workspace=tmpdir, **kwargs)


def _make_registry(tmpdir: str, **kwargs: Any):
    """Return a fully-loaded ToolRegistry and a matching HarnessConfig."""
    from harness.tools import build_registry
    cfg = _make_config(tmpdir, **kwargs)
    reg = build_registry()
    return reg, cfg


# ---------------------------------------------------------------------------
# 1. ToolRegistry.execute() — routing, allowed_tools, alias normalisation
# ---------------------------------------------------------------------------

class TestToolRegistryExecute:
    """ToolRegistry.execute() dispatches correctly and enforces security."""

    def test_unknown_tool_returns_error(self, tmp_path):
        reg, cfg = _make_registry(str(tmp_path))
        result = _run(
            reg.execute("nonexistent_tool_xyz", cfg, {})
        )
        assert result.is_error
        assert "Unknown tool" in result.error

    def test_allowed_tools_blocks_unlisted_tool(self, tmp_path):
        """When allowed_tools is set, tools not in the list must be blocked."""
        reg, cfg = _make_registry(str(tmp_path), allowed_tools=["read_file"])
        result = _run(
            reg.execute("bash", cfg, {"command": "echo hi"})
        )
        assert result.is_error
        assert "PERMISSION ERROR" in result.error
        assert "allowed_tools" in result.error

    def test_allowed_tools_permits_listed_tool(self, tmp_path):
        """A tool explicitly in allowed_tools must be permitted to execute."""
        # write a file so read_file has something to read
        test_file = tmp_path / "hello.txt"
        test_file.write_text("hello")
        reg, cfg = _make_registry(str(tmp_path), allowed_tools=["read_file"])
        result = _run(
            reg.execute("read_file", cfg, {"path": str(test_file)})
        )
        assert not result.is_error

    def test_empty_allowed_tools_permits_all(self, tmp_path):
        """An empty allowed_tools list must not restrict any tool."""
        reg, cfg = _make_registry(str(tmp_path), allowed_tools=[])
        # git_status works without a git repo (may error on git side, but not PERMISSION)
        result = _run(
            reg.execute("git_status", cfg, {})
        )
        # Whether git is available or not, the error must NOT be a PERMISSION ERROR
        if result.is_error:
            assert "PERMISSION ERROR" not in result.error

    def test_alias_normalisation_file_path_to_path(self, tmp_path):
        """'file_path' must be silently rewritten to 'path' before dispatch."""
        test_file = tmp_path / "alias_test.txt"
        test_file.write_text("alias content")
        reg, cfg = _make_registry(str(tmp_path))
        # Pass the aliased name; tool expects 'path'
        result = _run(
            reg.execute("read_file", cfg, {"file_path": str(test_file)})
        )
        assert not result.is_error
        assert "alias content" in result.output

    def test_alias_normalisation_text_to_content(self, tmp_path):
        """'text' must be silently rewritten to 'content' for write_file."""
        reg, cfg = _make_registry(str(tmp_path))
        out_path = str(tmp_path / "out.txt")
        result = _run(
            reg.execute("write_file", cfg, {"path": out_path, "text": "written via alias"})
        )
        assert not result.is_error
        assert Path(out_path).read_text() == "written via alias"

    def test_schema_error_on_missing_required_param(self, tmp_path):
        """Omitting a required parameter must produce a SCHEMA ERROR, not a crash."""
        reg, cfg = _make_registry(str(tmp_path))
        # write_file requires 'path' and 'content'
        result = _run(
            reg.execute("write_file", cfg, {"path": str(tmp_path / "x.txt")})
            # 'content' is missing
        )
        assert result.is_error
        assert "SCHEMA ERROR" in result.error

    def test_unknown_param_returns_schema_error(self, tmp_path):
        """Passing a hallucinated parameter name must return a SCHEMA ERROR."""
        test_file = tmp_path / "hello.txt"
        test_file.write_text("hi")
        reg, cfg = _make_registry(str(tmp_path))
        result = _run(
            reg.execute(
                "read_file",
                cfg,
                {"path": str(test_file), "hallucinated_param": "oops"},
            )
        )
        assert result.is_error
        assert "SCHEMA ERROR" in result.error
        assert "hallucinated_param" in result.error


# ---------------------------------------------------------------------------
# 2. ToolRegistry.filter_by_tags()
# ---------------------------------------------------------------------------

class TestFilterByTags:
    """filter_by_tags() returns the correct subset of tools."""

    def test_known_tag_returns_matching_tools(self, tmp_path):
        """filter_by_tags({'git'}) should return exactly the three git tools."""
        reg, _ = _make_registry(str(tmp_path))
        filtered = reg.filter_by_tags(frozenset({"git"}))
        assert set(filtered.names) == {"git_status", "git_diff", "git_log"}

    def test_multiple_tags_returns_union(self, tmp_path):
        """Passing two tags must return tools matching either tag."""
        reg, _ = _make_registry(str(tmp_path))
        filtered = reg.filter_by_tags(frozenset({"git", "testing"}))
        names = set(filtered.names)
        assert "git_status" in names
        assert "test_runner" in names

    def test_unknown_tag_excludes_all_tagged_tools(self, tmp_path):
        """A tag with no matches must produce a registry with only untagged tools.

        Tools with empty tag sets (the base Tool default) are always included
        for backward compatibility.
        """
        from harness.tools import ALL_TOOLS
        reg, _ = _make_registry(str(tmp_path))
        filtered = reg.filter_by_tags(frozenset({"nonexistent_tag_xyz"}))
        # Only untagged tools (tags == frozenset()) should appear
        untagged_names = {t.name for t in ALL_TOOLS if not t.tags}
        assert set(filtered.names) == untagged_names

    def test_empty_tags_arg_includes_only_untagged(self, tmp_path):
        """Passing an empty frozenset must only include untagged tools."""
        from harness.tools import ALL_TOOLS
        reg, _ = _make_registry(str(tmp_path))
        filtered = reg.filter_by_tags(frozenset())
        untagged_names = {t.name for t in ALL_TOOLS if not t.tags}
        assert set(filtered.names) == untagged_names

    def test_filter_result_is_new_registry(self, tmp_path):
        """filter_by_tags must return a NEW ToolRegistry, not mutate the original."""
        from harness.tools.registry import ToolRegistry
        reg, _ = _make_registry(str(tmp_path))
        original_count = len(reg.names)
        filtered = reg.filter_by_tags(frozenset({"git"}))
        assert isinstance(filtered, ToolRegistry)
        assert len(reg.names) == original_count  # original unchanged
        assert len(filtered.names) < original_count


# ---------------------------------------------------------------------------
# 3. Path security: _resolve_and_check() and _check_path()
# ---------------------------------------------------------------------------

class TestPathSecurity:
    """Tool path security guards reject dangerous inputs."""

    def test_null_byte_in_path_is_rejected(self, tmp_path):
        """A null byte anywhere in the path must trigger PERMISSION ERROR."""
        from harness.tools.file_read import ReadFileTool
        cfg = _make_config(str(tmp_path))
        tool = ReadFileTool()
        result = _run(
            tool.execute(cfg, path="foo\x00bar")
        )
        assert result.is_error
        assert "PERMISSION ERROR" in result.error
        assert "null byte" in result.error

    def test_dotdot_escape_is_rejected(self, tmp_path):
        """A path using '../..' to escape the workspace must be rejected."""
        from harness.tools.file_read import ReadFileTool
        cfg = _make_config(str(tmp_path))
        tool = ReadFileTool()
        result = _run(
            tool.execute(cfg, path=str(tmp_path / ".." / ".." / "etc" / "passwd"))
        )
        assert result.is_error
        # Should get "not allowed" or "not found" (the path doesn't exist under workspace)
        # The key invariant: it must not successfully read an out-of-workspace file
        assert result.is_error

    def test_symlink_pointing_outside_workspace_is_rejected(self, tmp_path):
        """A symlink inside the workspace that resolves outside must be blocked."""
        from harness.tools.file_read import ReadFileTool
        cfg = _make_config(str(tmp_path))
        tool = ReadFileTool()
        # Create a symlink inside workspace pointing to /tmp (outside)
        link = tmp_path / "escape_link"
        link.symlink_to("/tmp")
        result = _run(
            tool.execute(cfg, path=str(link))
        )
        assert result.is_error

    def test_path_inside_workspace_is_allowed(self, tmp_path):
        """A legitimate file inside the workspace must not be rejected by the guard."""
        from harness.tools.file_read import ReadFileTool
        cfg = _make_config(str(tmp_path))
        tool = ReadFileTool()
        legit = tmp_path / "legit.txt"
        legit.write_text("safe content")
        result = _run(
            tool.execute(cfg, path=str(legit))
        )
        assert not result.is_error
        assert "safe content" in result.output

    def test_check_path_null_byte_via_config(self, tmp_path):
        """HarnessConfig.is_path_allowed() must reject null bytes before any OS call."""
        cfg = _make_config(str(tmp_path))
        assert cfg.is_path_allowed("foo\x00bar") is False

    def test_check_path_rejects_path_outside_allowed(self, tmp_path):
        """HarnessConfig.is_path_allowed() must reject paths outside allowed_paths."""
        cfg = _make_config(str(tmp_path))
        assert cfg.is_path_allowed("/etc/passwd") is False

    def test_write_file_rejects_null_byte(self, tmp_path):
        """write_file must also reject null bytes (not just read_file)."""
        from harness.tools.file_write import WriteFileTool
        cfg = _make_config(str(tmp_path))
        tool = WriteFileTool()
        result = _run(
            tool.execute(cfg, path="some\x00path.txt", content="hi")
        )
        assert result.is_error
        assert "PERMISSION ERROR" in result.error

    def test_resolve_and_check_blocks_unicode_homoglyph(self, tmp_path):
        """_resolve_and_check must reject paths containing Unicode compatibility characters."""
        from harness.tools.file_read import ReadFileTool
        cfg = _make_config(str(tmp_path))
        tool = ReadFileTool()
        
        # Test with superscript 2 (U+00B2) which NFKC normalizes to ASCII '2'
        # This is a compatibility character that could be used to obscure the real filename
        superscript_path = str(tmp_path / "file¹.txt")  # Note: superscript 1 (U+00B9)
        result = _run(tool.execute(cfg, path=superscript_path))
        
        assert result.is_error
        assert "PERMISSION ERROR" in result.error
        assert "Unicode homoglyphs" in result.error
        
        # Test with decomposed e + acute accent (U+0065 U+0301) which NFKC normalizes to é (U+00E9)
        decomposed_path = str(tmp_path / "cafe\u0301.txt")  # "cafe" + combining acute accent
        result = _run(tool.execute(cfg, path=decomposed_path))
        
        assert result.is_error
        assert "PERMISSION ERROR" in result.error
        assert "Unicode homoglyphs" in result.error
        
        # Also test that legitimate Unicode characters are allowed
        # e.g., "café.txt" with the precomposed é character (U+00E9)
        legit_unicode = str(tmp_path / "café.txt")  # Precomposed é
        legit_file = tmp_path / "café.txt"
        legit_file.write_text("test content")
        result = _run(tool.execute(cfg, path=legit_unicode))
        
        # Should succeed - legitimate Unicode filename
        assert not result.is_error
        assert "test content" in result.output


# ---------------------------------------------------------------------------
# 4. Config comment stripping: PipelineConfig.from_dict() and HarnessConfig.from_dict()
# ---------------------------------------------------------------------------

class TestConfigCommentStripping:
    """from_dict() silently drops // and _ prefixed keys (JSON comment convention)."""

    def test_pipeline_config_strips_double_slash_keys(self, tmp_path):
        """PipelineConfig.from_dict() must silently drop keys starting with '//'."""
        from harness.core.config import PipelineConfig
        data = {
            "// this is a comment": "ignored",
            "//another": "also ignored",
            "outer_rounds": 2,
            "inner_rounds": 1,
            "phases": [],
            "harness": {"workspace": str(tmp_path)},
        }
        cfg = PipelineConfig.from_dict(data)
        assert cfg.outer_rounds == 2
        # No error means the // keys were stripped before validation

    def test_pipeline_config_strips_underscore_prefix_keys(self, tmp_path):
        """PipelineConfig.from_dict() must silently drop keys starting with '_'."""
        from harness.core.config import PipelineConfig
        data = {
            "_comment": "ignored",
            "_todo": "also ignored",
            "outer_rounds": 3,
            "inner_rounds": 1,
            "phases": [],
            "harness": {"workspace": str(tmp_path)},
        }
        cfg = PipelineConfig.from_dict(data)
        assert cfg.outer_rounds == 3

    def test_pipeline_config_raises_on_truly_unknown_keys(self, tmp_path):
        """Keys that are neither comments nor valid fields must raise ValueError."""
        from harness.core.config import PipelineConfig
        data = {
            "totally_unknown_field": "bad",
            "harness": {"workspace": str(tmp_path)},
        }
        with pytest.raises(ValueError, match="unknown config key"):
            PipelineConfig.from_dict(data)

    def test_harness_config_strips_comment_keys(self, tmp_path):
        """HarnessConfig.from_dict() must also strip // and _ prefixed keys."""
        from harness.core.config import HarnessConfig
        data = {
            "// model comment": "ignored",
            "_note": "also ignored",
            "workspace": str(tmp_path),
        }
        cfg = HarnessConfig.from_dict(data)
        assert cfg.workspace == str(Path(str(tmp_path)).resolve())

    def test_phase_config_strips_comment_keys(self, tmp_path):
        """PhaseConfig.from_dict() must strip // and _ prefixed keys."""
        from harness.pipeline.phase import PhaseConfig
        data = {
            "// comment": "ignored",
            "_todo": "ignored",
            "name": "test_phase",
            "index": 0,
            "system_prompt": "Do something",
        }
        phase = PhaseConfig.from_dict(data)
        assert phase.name == "test_phase"
        assert phase.index == 0


# ---------------------------------------------------------------------------
# 5. Meta-review safety: _auto_update_prompts() variable-preservation guard
# ---------------------------------------------------------------------------

class TestAutoUpdatePromptsGuard:
    """_auto_update_prompts() must reject rewrites that drop template variables."""

    def _make_pipeline_loop(self, tmp_path):
        """Build a minimal PipelineLoop for testing _auto_update_prompts."""
        from harness.core.config import PipelineConfig
        from harness.pipeline.pipeline_loop import PipelineLoop

        cfg_data = {
            "outer_rounds": 1,
            "inner_rounds": 1,
            "phases": [],
            "harness": {"workspace": str(tmp_path)},
        }
        pipeline_cfg = PipelineConfig.from_dict(cfg_data)
        # Patch LLM to avoid real API calls
        loop = object.__new__(PipelineLoop)
        loop.config = pipeline_cfg
        loop._meta_review_context = ""
        loop._shutdown_requested = False
        from harness.artifacts import ArtifactStore
        loop.artifacts = ArtifactStore(str(tmp_path / "output"))
        return loop

    @pytest.mark.asyncio
    async def test_rewrite_dropping_variables_is_rejected(self, tmp_path):
        """A rewritten prompt that drops $file_context must be rejected; original kept."""
        from harness.pipeline.phase import PhaseConfig
        from harness.pipeline.pipeline_loop import PipelineLoop
        from harness.core.config import PipelineConfig
        from harness.artifacts import ArtifactStore
        from harness.core.llm import LLM

        cfg_data = {
            "outer_rounds": 1,
            "inner_rounds": 1,
            "phases": [],
            "harness": {"workspace": str(tmp_path)},
        }
        pipeline_cfg = PipelineConfig.from_dict(cfg_data)
        loop = object.__new__(PipelineLoop)
        loop.config = pipeline_cfg
        loop._meta_review_context = ""
        loop._shutdown_requested = False
        loop.artifacts = ArtifactStore(str(tmp_path / "output"))

        # LLM mock: returns a prompt that drops $file_context
        mock_llm = AsyncMock(spec=LLM)
        mock_llm.call = AsyncMock(
            return_value="Rewritten prompt without the important variable"
        )
        loop.llm = mock_llm

        original_prompt = "Do $file_context things with $prior_best guidance"
        phase = PhaseConfig(
            name="test",
            index=0,
            system_prompt=original_prompt,
        )

        updated = await loop._auto_update_prompts(
            meta_review_text="Some review", phases=[phase], outer=0
        )
        # The guard must have caught the missing variables and kept the original
        assert updated[0].system_prompt == original_prompt

    @pytest.mark.asyncio
    async def test_rewrite_preserving_variables_is_accepted(self, tmp_path):
        """A rewritten prompt that keeps all $variables must be applied."""
        from harness.pipeline.phase import PhaseConfig
        from harness.pipeline.pipeline_loop import PipelineLoop
        from harness.core.config import PipelineConfig
        from harness.artifacts import ArtifactStore
        from harness.core.llm import LLM

        cfg_data = {
            "outer_rounds": 1,
            "inner_rounds": 1,
            "phases": [],
            "harness": {"workspace": str(tmp_path)},
        }
        pipeline_cfg = PipelineConfig.from_dict(cfg_data)
        loop = object.__new__(PipelineLoop)
        loop.config = pipeline_cfg
        loop._meta_review_context = ""
        loop._shutdown_requested = False
        loop.artifacts = ArtifactStore(str(tmp_path / "output"))

        original_prompt = "Do $file_context things with $prior_best guidance"
        new_prompt = "Improved: use $file_context and consider $prior_best carefully"

        mock_llm = AsyncMock(spec=LLM)
        mock_llm.call = AsyncMock(return_value=new_prompt)
        loop.llm = mock_llm

        phase = PhaseConfig(
            name="test",
            index=0,
            system_prompt=original_prompt,
        )

        updated = await loop._auto_update_prompts(
            meta_review_text="Some review", phases=[phase], outer=0
        )
        # All variables preserved → the new prompt should be applied
        assert updated[0].system_prompt == new_prompt

    @pytest.mark.asyncio
    async def test_llm_failure_keeps_original_prompt(self, tmp_path):
        """When the LLM call raises, the original prompt must be preserved."""
        from harness.pipeline.phase import PhaseConfig
        from harness.pipeline.pipeline_loop import PipelineLoop
        from harness.core.config import PipelineConfig
        from harness.artifacts import ArtifactStore
        from harness.core.llm import LLM

        cfg_data = {
            "outer_rounds": 1,
            "inner_rounds": 1,
            "phases": [],
            "harness": {"workspace": str(tmp_path)},
        }
        pipeline_cfg = PipelineConfig.from_dict(cfg_data)
        loop = object.__new__(PipelineLoop)
        loop.config = pipeline_cfg
        loop._meta_review_context = ""
        loop._shutdown_requested = False
        loop.artifacts = ArtifactStore(str(tmp_path / "output"))

        original_prompt = "Do $file_context things"
        mock_llm = AsyncMock(spec=LLM)
        mock_llm.call = AsyncMock(side_effect=RuntimeError("API down"))
        loop.llm = mock_llm

        phase = PhaseConfig(
            name="test",
            index=0,
            system_prompt=original_prompt,
        )

        updated = await loop._auto_update_prompts(
            meta_review_text="Some review", phases=[phase], outer=0
        )
        # Exception must be handled gracefully; original prompt must survive
        assert updated[0].system_prompt == original_prompt


# ---------------------------------------------------------------------------
# 6. parse_score — two-tier extraction (strict anchored vs loose fallback)
# ---------------------------------------------------------------------------

class TestParseScore:
    """parse_score() correctly extracts scores using the two-tier strategy."""

    def test_strict_match_takes_last_anchor(self):
        """Anchored SCORE: N on its own line must be preferred."""
        from harness.evaluation.dual_evaluator import parse_score
        text = "The score is approximately 5.5\nSCORE: 7.5\n"
        assert parse_score(text) == 7.5

    def test_strict_ignores_inline_arithmetic(self):
        """A SCORE buried in an arithmetic expression must be ignored by strict tier."""
        from harness.evaluation.dual_evaluator import parse_score
        text = "SCORE = (4×0.4 + 6×0.6) = 5.2\nSCORE: 8\n"
        assert parse_score(text) == 8.0

    def test_loose_fallback_when_no_anchored_score(self):
        """When no strict match, the loose pattern must be used."""
        from harness.evaluation.dual_evaluator import parse_score
        text = "I would give this SCORE: 6.0 overall"
        assert parse_score(text) == 6.0

    def test_no_score_returns_zero(self):
        """When no score token is found, return 0.0."""
        from harness.evaluation.dual_evaluator import parse_score
        assert parse_score("No score here at all.") == 0.0

    def test_clamps_above_ten(self):
        """Scores above 10.0 must be clamped to 10.0."""
        from harness.evaluation.dual_evaluator import parse_score
        text = "SCORE: 11\n"
        assert parse_score(text) == 10.0

    def test_clamps_below_zero(self):
        """Scores below 0 must be clamped to 0.0."""
        from harness.evaluation.dual_evaluator import parse_score
        text = "SCORE: -2\n"
        assert parse_score(text) == 0.0

    def test_last_strict_match_wins(self):
        """When multiple anchored scores appear, the LAST one must win."""
        from harness.evaluation.dual_evaluator import parse_score
        text = "SCORE: 3\nSome analysis...\nSCORE: 9\n"
        assert parse_score(text) == 9.0
