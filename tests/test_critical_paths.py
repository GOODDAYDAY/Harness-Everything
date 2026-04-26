"""Critical-path tests for tool dispatch, tag filtering, path security,
and config comment stripping.

These cover previously untested code paths that are high-risk:
- ToolRegistry.execute() routing, allowed_tools enforcement, alias normalisation
- ToolRegistry.filter_by_tags() semantics
- Tool._resolve_and_check() path-security guards
- HarnessConfig.from_dict() comment-key stripping
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
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
        reg, cfg = _make_registry(str(tmp_path), allowed_tools=["git_status"])
        result = _run(
            reg.execute("bash", cfg, {"command": "echo hi"})
        )
        assert result.is_error
        assert "PERMISSION ERROR" in result.error
        assert "allowed_tools" in result.error

    def test_allowed_tools_permits_listed_tool(self, tmp_path):
        """A tool explicitly in allowed_tools must be permitted to execute."""
        from harness.tools import build_registry
        reg = build_registry(extra_tools=["read_file"])
        cfg = _make_config(str(tmp_path), allowed_tools=["read_file"])
        # write a file so read_file has something to read
        test_file = tmp_path / "hello.txt"
        test_file.write_text("hello")
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
        from harness.tools import build_registry
        test_file = tmp_path / "alias_test.txt"
        test_file.write_text("alias content")
        reg = build_registry(extra_tools=["read_file"])
        cfg = _make_config(str(tmp_path))
        # Pass the aliased name; tool expects 'path'
        result = _run(
            reg.execute("read_file", cfg, {"file_path": str(test_file)})
        )
        assert not result.is_error
        assert "alias content" in result.output

    def test_alias_normalisation_text_to_content(self, tmp_path):
        """'text' must be silently rewritten to 'content' for write_file."""
        from harness.tools import build_registry
        reg = build_registry(extra_tools=["write_file"])
        cfg = _make_config(str(tmp_path))
        out_path = str(tmp_path / "out.txt")
        result = _run(
            reg.execute("write_file", cfg, {"path": out_path, "text": "written via alias"})
        )
        assert not result.is_error
        assert Path(out_path).read_text() == "written via alias"

    def test_schema_error_on_missing_required_param(self, tmp_path):
        """Omitting a required parameter must produce a SCHEMA ERROR, not a crash."""
        from harness.tools import build_registry
        reg = build_registry(extra_tools=["write_file"])
        cfg = _make_config(str(tmp_path))
        # write_file requires 'path' and 'content'
        result = _run(
            reg.execute("write_file", cfg, {"path": str(tmp_path / "x.txt")})
            # 'content' is missing
        )
        assert result.is_error
        assert "SCHEMA ERROR" in result.error

    def test_unknown_param_returns_schema_error(self, tmp_path):
        """Passing a hallucinated parameter name must return a SCHEMA ERROR."""
        from harness.tools import build_registry
        test_file = tmp_path / "hello.txt"
        test_file.write_text("hi")
        reg = build_registry(extra_tools=["read_file"])
        cfg = _make_config(str(tmp_path))
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
# 4. Config comment stripping: HarnessConfig.from_dict()
# ---------------------------------------------------------------------------

class TestConfigCommentStripping:
    """from_dict() silently drops // and _ prefixed keys (JSON comment convention)."""

    def test_harness_config_strips_comment_keys(self, tmp_path):
        """HarnessConfig.from_dict() must strip // and _ prefixed keys."""
        from harness.core.config import HarnessConfig
        data = {
            "// model comment": "ignored",
            "_note": "also ignored",
            "workspace": str(tmp_path),
        }
        cfg = HarnessConfig.from_dict(data)
        assert cfg.workspace == str(Path(str(tmp_path)).resolve())

    def test_harness_config_raises_on_truly_unknown_keys(self, tmp_path):
        """Keys that are neither comments nor valid fields must raise ValueError."""
        from harness.core.config import HarnessConfig
        data = {
            "totally_unknown_field": "bad",
            "workspace": str(tmp_path),
        }
        with pytest.raises(ValueError, match="unknown config key"):
            HarnessConfig.from_dict(data)


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


class TestASTUtils:
    """Tests for AST utility functions consolidated in _ast_utils.py."""
    
    def test_parent_class_nested(self):
        """Test parent_class function with nested class inheritance."""
        import ast
        from harness.tools._ast_utils import parent_class
        
        # Create AST for: class Outer: class Inner: pass
        source = """
class Outer:
    class Inner:
        pass
"""
        tree = ast.parse(source)
        
        # Find the Inner class node
        inner_class = None
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "Inner":
                inner_class = node
                break
        
        assert inner_class is not None, "Inner class not found in AST"
        
        # Test parent_class function
        result = parent_class(tree, inner_class)
        assert result == "Outer", f"Expected 'Outer', got {result}"
        
        # Also test that parent_class returns None for top-level class
        outer_class = None
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "Outer":
                outer_class = node
                break
        
        assert outer_class is not None, "Outer class not found in AST"
        result = parent_class(tree, outer_class)
        assert result is None, f"Expected None for top-level class, got {result}"
