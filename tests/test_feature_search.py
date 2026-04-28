"""Tests for the feature_search tool."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path


from harness.core.config import HarnessConfig
from harness.tools.feature_search import FeatureSearchTool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_config(workspace: Path) -> HarnessConfig:
    ws = str(workspace)
    return HarnessConfig(workspace=ws, allowed_paths=[ws])


def run(coro):
    return asyncio.run(coro)


def make_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    return ws


# ---------------------------------------------------------------------------
# Tool metadata
# ---------------------------------------------------------------------------


class TestToolMetadata:
    def test_name(self):
        assert FeatureSearchTool().name == "feature_search"

    def test_description_not_empty(self):
        assert FeatureSearchTool().description

    def test_input_schema_has_required_fields(self):
        schema = FeatureSearchTool().input_schema()
        assert "keyword" in schema["required"]
        assert "max_results" in schema["required"]

    def test_input_schema_categories_enum(self):
        schema = FeatureSearchTool().input_schema()
        cat_items = schema["properties"]["categories"]["items"]
        assert set(cat_items["enum"]) == {"symbols", "files", "comments", "config"}

    def test_input_schema_scoring_enum(self):
        schema = FeatureSearchTool().input_schema()
        scoring = schema["properties"]["scoring"]
        assert "substring" in scoring["enum"]
        assert "token_overlap" in scoring["enum"]

    def test_has_search_tag(self):
        assert "search" in FeatureSearchTool().tags


# ---------------------------------------------------------------------------
# Error conditions
# ---------------------------------------------------------------------------


class TestErrorConditions:
    def test_empty_keyword_returns_error(self, tmp_path):
        ws = make_workspace(tmp_path)
        config = make_config(ws)
        result = run(FeatureSearchTool().execute(config, keyword="", max_results=10))
        assert result.is_error
        assert "keyword" in result.error.lower()

    def test_whitespace_only_keyword_returns_error(self, tmp_path):
        ws = make_workspace(tmp_path)
        config = make_config(ws)
        result = run(FeatureSearchTool().execute(config, keyword="   ", max_results=10))
        assert result.is_error
        assert "keyword" in result.error.lower()

    def test_invalid_root_returns_error(self, tmp_path):
        ws = make_workspace(tmp_path)
        config = make_config(ws)
        # Root outside allowed_paths should be rejected
        outside = str(tmp_path.parent / "outside")
        result = run(
            FeatureSearchTool().execute(config, keyword="foo", max_results=10, root=outside)
        )
        assert result.is_error

    def test_nonexistent_root_returns_empty(self, tmp_path):
        """Nonexistent root within allowed_paths returns empty results, not an error."""
        ws = make_workspace(tmp_path)
        config = make_config(ws)
        result = run(
            FeatureSearchTool().execute(
                config, keyword="foo", max_results=10, root=str(ws / "nonexistent")
            )
        )
        # The tool resolves the (non-existent) root without strict=True, finds
        # no files, and returns an empty-but-valid result.
        assert not result.is_error
        data = json.loads(result.output)
        assert data["files_scanned"] == 0


# ---------------------------------------------------------------------------
# Empty workspace
# ---------------------------------------------------------------------------


class TestEmptyWorkspace:
    def test_empty_workspace_returns_no_hits(self, tmp_path):
        ws = make_workspace(tmp_path)
        config = make_config(ws)
        result = run(FeatureSearchTool().execute(config, keyword="checkpoint", max_results=10))
        assert not result.is_error
        data = json.loads(result.output)
        assert data["files_scanned"] == 0
        assert data["symbols"] == []
        assert data["files"] == []
        assert data["comments"] == []
        assert data["config"] == []

    def test_returns_keyword_in_output(self, tmp_path):
        ws = make_workspace(tmp_path)
        config = make_config(ws)
        result = run(FeatureSearchTool().execute(config, keyword="retry", max_results=10))
        assert not result.is_error
        data = json.loads(result.output)
        assert data["keyword"] == "retry"


# ---------------------------------------------------------------------------
# File name search
# ---------------------------------------------------------------------------


class TestFileCategory:
    def test_finds_file_by_name(self, tmp_path):
        ws = make_workspace(tmp_path)
        (ws / "checkpoint_manager.py").write_text("# empty\n")
        (ws / "other_module.py").write_text("# empty\n")
        config = make_config(ws)
        result = run(
            FeatureSearchTool().execute(config, keyword="checkpoint", max_results=10)
        )
        assert not result.is_error
        data = json.loads(result.output)
        file_names = [h["file"] for h in data["files"]]
        assert any("checkpoint_manager" in f for f in file_names)
        # other_module should not appear in files category
        assert not any("other_module" in f for f in file_names)

    def test_file_search_case_insensitive(self, tmp_path):
        ws = make_workspace(tmp_path)
        (ws / "Checkpoint.py").write_text("# empty\n")
        config = make_config(ws)
        result = run(
            FeatureSearchTool().execute(
                config, keyword="checkpoint", max_results=10, categories=["files"]
            )
        )
        assert not result.is_error
        data = json.loads(result.output)
        assert len(data["files"]) == 1

    def test_file_search_respects_max_results(self, tmp_path):
        ws = make_workspace(tmp_path)
        for i in range(10):
            (ws / f"retry_module_{i}.py").write_text("x = 1\n")
        config = make_config(ws)
        result = run(
            FeatureSearchTool().execute(
                config, keyword="retry", max_results=3, categories=["files"]
            )
        )
        data = json.loads(result.output)
        assert len(data["files"]) <= 3


# ---------------------------------------------------------------------------
# Symbol search
# ---------------------------------------------------------------------------


class TestSymbolCategory:
    def test_finds_function_by_name(self, tmp_path):
        ws = make_workspace(tmp_path)
        (ws / "module.py").write_text(
            "def retry_with_backoff(n):\n    pass\n"
            "def unrelated_func():\n    pass\n"
        )
        config = make_config(ws)
        result = run(
            FeatureSearchTool().execute(
                config, keyword="retry", max_results=10, categories=["symbols"]
            )
        )
        assert not result.is_error
        data = json.loads(result.output)
        names = [h["name"] for h in data["symbols"]]
        assert "retry_with_backoff" in names
        assert "unrelated_func" not in names

    def test_finds_class_by_name(self, tmp_path):
        ws = make_workspace(tmp_path)
        (ws / "module.py").write_text("class RetryPolicy:\n    pass\n")
        config = make_config(ws)
        result = run(
            FeatureSearchTool().execute(
                config, keyword="retry", max_results=10, categories=["symbols"]
            )
        )
        data = json.loads(result.output)
        hits = data["symbols"]
        assert any(h["name"] == "RetryPolicy" and h["kind"] == "class" for h in hits)

    def test_finds_async_function(self, tmp_path):
        ws = make_workspace(tmp_path)
        (ws / "module.py").write_text("async def retry_request():\n    pass\n")
        config = make_config(ws)
        result = run(
            FeatureSearchTool().execute(
                config, keyword="retry", max_results=10, categories=["symbols"]
            )
        )
        data = json.loads(result.output)
        hits = data["symbols"]
        assert any(h["kind"] == "async_function" for h in hits)

    def test_symbol_hit_includes_line_number(self, tmp_path):
        ws = make_workspace(tmp_path)
        (ws / "module.py").write_text("\n\ndef retry_fn():\n    pass\n")
        config = make_config(ws)
        result = run(
            FeatureSearchTool().execute(
                config, keyword="retry", max_results=10, categories=["symbols"]
            )
        )
        data = json.loads(result.output)
        assert data["symbols"][0]["line"] == 3

    def test_symbol_hit_includes_file(self, tmp_path):
        ws = make_workspace(tmp_path)
        (ws / "module.py").write_text("def retry_fn():\n    pass\n")
        config = make_config(ws)
        result = run(
            FeatureSearchTool().execute(
                config, keyword="retry", max_results=10, categories=["symbols"]
            )
        )
        data = json.loads(result.output)
        assert "module.py" in data["symbols"][0]["file"]

    def test_symbol_search_case_insensitive(self, tmp_path):
        ws = make_workspace(tmp_path)
        (ws / "module.py").write_text("def RETRY_HANDLER():\n    pass\n")
        config = make_config(ws)
        result = run(
            FeatureSearchTool().execute(
                config, keyword="retry", max_results=10, categories=["symbols"]
            )
        )
        data = json.loads(result.output)
        assert len(data["symbols"]) >= 1

    def test_symbols_respects_max_results(self, tmp_path):
        ws = make_workspace(tmp_path)
        funcs = "\n".join(f"def retry_fn_{i}():\n    pass" for i in range(10))
        (ws / "module.py").write_text(funcs)
        config = make_config(ws)
        result = run(
            FeatureSearchTool().execute(
                config, keyword="retry", max_results=3, categories=["symbols"]
            )
        )
        data = json.loads(result.output)
        assert len(data["symbols"]) <= 3


# ---------------------------------------------------------------------------
# Comment and docstring search
# ---------------------------------------------------------------------------


class TestCommentCategory:
    def test_finds_inline_comment(self, tmp_path):
        ws = make_workspace(tmp_path)
        (ws / "module.py").write_text(
            "x = 1\n"
            "# retry logic starts here\n"
            "y = 2\n"
        )
        config = make_config(ws)
        result = run(
            FeatureSearchTool().execute(
                config, keyword="retry", max_results=10, categories=["comments"]
            )
        )
        data = json.loads(result.output)
        assert len(data["comments"]) >= 1
        hit = data["comments"][0]
        assert hit["kind"] == "comment"
        assert "retry" in hit["text"].lower()

    def test_comment_hit_includes_line_number(self, tmp_path):
        ws = make_workspace(tmp_path)
        (ws / "module.py").write_text("x = 1\n# retry here\n")
        config = make_config(ws)
        result = run(
            FeatureSearchTool().execute(
                config, keyword="retry", max_results=10, categories=["comments"]
            )
        )
        data = json.loads(result.output)
        assert data["comments"][0]["line"] == 2

    def test_finds_docstring(self, tmp_path):
        """Docstrings are found when AST parsing is active (symbols or config in categories)."""
        ws = make_workspace(tmp_path)
        (ws / "module.py").write_text(
            'def process():\n'
            '    """Handle retry logic for requests."""\n'
            '    pass\n'
        )
        config = make_config(ws)
        # AST parsing (needed for docstrings) is only triggered when 'symbols' or 'config'
        # is in the active categories, so include 'symbols' here.
        result = run(
            FeatureSearchTool().execute(
                config, keyword="retry", max_results=10, categories=["symbols", "comments"]
            )
        )
        data = json.loads(result.output)
        docstring_hits = [h for h in data["comments"] if h["kind"] == "docstring"]
        assert len(docstring_hits) >= 1
        assert docstring_hits[0]["symbol"] == "process"

    def test_comment_search_case_insensitive(self, tmp_path):
        ws = make_workspace(tmp_path)
        (ws / "module.py").write_text("# RETRY this operation\n")
        config = make_config(ws)
        result = run(
            FeatureSearchTool().execute(
                config, keyword="retry", max_results=10, categories=["comments"]
            )
        )
        data = json.loads(result.output)
        assert len(data["comments"]) >= 1

    def test_non_comment_line_not_matched(self, tmp_path):
        ws = make_workspace(tmp_path)
        # A normal assignment line — not a comment
        (ws / "module.py").write_text('retry_count = 3\n')
        config = make_config(ws)
        result = run(
            FeatureSearchTool().execute(
                config, keyword="retry", max_results=10, categories=["comments"]
            )
        )
        data = json.loads(result.output)
        # No inline # comment here, no docstring — should not appear in comments
        assert all(h["kind"] in ("comment", "docstring") for h in data["comments"])
        # The assignment line should not produce a 'comment' hit
        comment_hits = [h for h in data["comments"] if h["kind"] == "comment"]
        assert len(comment_hits) == 0


# ---------------------------------------------------------------------------
# Config / constant search
# ---------------------------------------------------------------------------


class TestConfigCategory:
    def test_finds_module_level_assign(self, tmp_path):
        ws = make_workspace(tmp_path)
        (ws / "constants.py").write_text('RETRY_LIMIT = 3\n')
        config = make_config(ws)
        result = run(
            FeatureSearchTool().execute(
                config, keyword="retry", max_results=10, categories=["config"]
            )
        )
        data = json.loads(result.output)
        names = [h["name"] for h in data["config"]]
        assert "RETRY_LIMIT" in names

    def test_finds_annotated_assign(self, tmp_path):
        ws = make_workspace(tmp_path)
        (ws / "constants.py").write_text('retry_max: int = 5\n')
        config = make_config(ws)
        result = run(
            FeatureSearchTool().execute(
                config, keyword="retry", max_results=10, categories=["config"]
            )
        )
        data = json.loads(result.output)
        names = [h["name"] for h in data["config"]]
        assert "retry_max" in names

    def test_config_hit_includes_value_snippet(self, tmp_path):
        ws = make_workspace(tmp_path)
        (ws / "constants.py").write_text('RETRY_COUNT = 42\n')
        config = make_config(ws)
        result = run(
            FeatureSearchTool().execute(
                config, keyword="retry", max_results=10, categories=["config"]
            )
        )
        data = json.loads(result.output)
        hit = data["config"][0]
        assert "42" in hit["value_snippet"]

    def test_config_inside_function_not_matched(self, tmp_path):
        ws = make_workspace(tmp_path)
        # Assignment inside a function — not module-level
        (ws / "module.py").write_text(
            "def foo():\n"
            "    retry_count = 5\n"
        )
        config = make_config(ws)
        result = run(
            FeatureSearchTool().execute(
                config, keyword="retry", max_results=10, categories=["config"]
            )
        )
        data = json.loads(result.output)
        assert len(data["config"]) == 0

    def test_config_annotated_no_value(self, tmp_path):
        ws = make_workspace(tmp_path)
        # Annotated assignment without a value
        (ws / "module.py").write_text('retry_policy: str\n')
        config = make_config(ws)
        result = run(
            FeatureSearchTool().execute(
                config, keyword="retry", max_results=10, categories=["config"]
            )
        )
        data = json.loads(result.output)
        # Should still find the name even without a value
        names = [h["name"] for h in data["config"]]
        assert "retry_policy" in names


# ---------------------------------------------------------------------------
# Category filtering
# ---------------------------------------------------------------------------


class TestCategoryFiltering:
    def test_symbols_only_omits_other_keys(self, tmp_path):
        ws = make_workspace(tmp_path)
        (ws / "mod.py").write_text("def retry(): pass\n")
        config = make_config(ws)
        result = run(
            FeatureSearchTool().execute(
                config, keyword="retry", max_results=10, categories=["symbols"]
            )
        )
        data = json.loads(result.output)
        assert "symbols" in data
        assert "files" not in data
        assert "comments" not in data
        assert "config" not in data

    def test_files_only_omits_other_keys(self, tmp_path):
        ws = make_workspace(tmp_path)
        (ws / "retry.py").write_text("x = 1\n")
        config = make_config(ws)
        result = run(
            FeatureSearchTool().execute(
                config, keyword="retry", max_results=10, categories=["files"]
            )
        )
        data = json.loads(result.output)
        assert "files" in data
        assert "symbols" not in data
        assert "comments" not in data
        assert "config" not in data

    def test_config_only_omits_other_keys(self, tmp_path):
        ws = make_workspace(tmp_path)
        (ws / "constants.py").write_text("RETRY = 3\n")
        config = make_config(ws)
        result = run(
            FeatureSearchTool().execute(
                config, keyword="retry", max_results=10, categories=["config"]
            )
        )
        data = json.loads(result.output)
        assert "config" in data
        assert "symbols" not in data
        assert "files" not in data
        assert "comments" not in data

    def test_default_categories_includes_all(self, tmp_path):
        ws = make_workspace(tmp_path)
        (ws / "mod.py").write_text("x = 1\n")
        config = make_config(ws)
        result = run(FeatureSearchTool().execute(config, keyword="retry", max_results=10))
        data = json.loads(result.output)
        for cat in ("symbols", "files", "comments", "config"):
            assert cat in data

    def test_two_categories(self, tmp_path):
        ws = make_workspace(tmp_path)
        (ws / "mod.py").write_text("def retry(): pass\n")
        config = make_config(ws)
        result = run(
            FeatureSearchTool().execute(
                config,
                keyword="retry",
                max_results=10,
                categories=["symbols", "config"],
            )
        )
        data = json.loads(result.output)
        assert "symbols" in data
        assert "config" in data
        assert "files" not in data
        assert "comments" not in data


# ---------------------------------------------------------------------------
# Scoring mode: token_overlap
# ---------------------------------------------------------------------------


class TestTokenOverlapScoring:
    def test_token_overlap_finds_matches(self, tmp_path):
        ws = make_workspace(tmp_path)
        (ws / "module.py").write_text(
            "def check_file_permissions():\n    pass\n"
            "def file_reader():\n    pass\n"
            "def unrelated():\n    pass\n"
        )
        config = make_config(ws)
        result = run(
            FeatureSearchTool().execute(
                config,
                keyword="file permission",
                max_results=10,
                categories=["symbols"],
                scoring="token_overlap",
            )
        )
        data = json.loads(result.output)
        names = [h["name"] for h in data["symbols"]]
        assert "check_file_permissions" in names
        assert "file_reader" in names
        assert "unrelated" not in names

    def test_token_overlap_adds_score_field(self, tmp_path):
        ws = make_workspace(tmp_path)
        (ws / "module.py").write_text("def file_permission_check():\n    pass\n")
        config = make_config(ws)
        result = run(
            FeatureSearchTool().execute(
                config,
                keyword="file permission",
                max_results=10,
                categories=["symbols"],
                scoring="token_overlap",
            )
        )
        data = json.loads(result.output)
        assert "score" in data["symbols"][0]
        assert data["symbols"][0]["score"] >= 1

    def test_token_overlap_higher_score_ranked_first(self, tmp_path):
        ws = make_workspace(tmp_path)
        # check_file_permissions matches 2 tokens: 'file' and 'permission'
        # file_reader matches only 1 token: 'file'
        (ws / "module.py").write_text(
            "def file_reader():\n    pass\n"
            "def check_file_permissions():\n    pass\n"
        )
        config = make_config(ws)
        result = run(
            FeatureSearchTool().execute(
                config,
                keyword="file permission",
                max_results=10,
                categories=["symbols"],
                scoring="token_overlap",
            )
        )
        data = json.loads(result.output)
        hits = data["symbols"]
        assert hits[0]["score"] >= hits[-1]["score"], "Higher-score hits should rank first"

    def test_token_overlap_no_score_in_substring_mode(self, tmp_path):
        ws = make_workspace(tmp_path)
        (ws / "module.py").write_text("def file_reader():\n    pass\n")
        config = make_config(ws)
        result = run(
            FeatureSearchTool().execute(
                config,
                keyword="file",
                max_results=10,
                categories=["symbols"],
                scoring="substring",
            )
        )
        data = json.loads(result.output)
        # substring mode should not add score field
        assert "score" not in data["symbols"][0]


# ---------------------------------------------------------------------------
# max_results clamping
# ---------------------------------------------------------------------------


class TestMaxResultsClamping:
    def test_max_results_clamped_to_at_least_one(self, tmp_path):
        ws = make_workspace(tmp_path)
        for i in range(5):
            (ws / f"retry_{i}.py").write_text("x = 1\n")
        config = make_config(ws)
        # Passing 0 should be clamped to 1
        result = run(
            FeatureSearchTool().execute(
                config, keyword="retry", max_results=0, categories=["files"]
            )
        )
        data = json.loads(result.output)
        assert len(data["files"]) <= 1

    def test_max_results_clamped_to_200(self, tmp_path):
        ws = make_workspace(tmp_path)
        for i in range(5):
            (ws / f"retry_{i}.py").write_text("x = 1\n")
        config = make_config(ws)
        # Passing >200 should still work
        result = run(
            FeatureSearchTool().execute(
                config, keyword="retry", max_results=1000, categories=["files"]
            )
        )
        data = json.loads(result.output)
        assert len(data["files"]) <= 5  # only 5 files exist


# ---------------------------------------------------------------------------
# Graceful handling of parse errors
# ---------------------------------------------------------------------------


class TestGracefulParsing:
    def test_syntax_error_file_skipped_gracefully(self, tmp_path):
        ws = make_workspace(tmp_path)
        # A file with a syntax error
        (ws / "bad_syntax.py").write_text("def foo(:\n    pass\n")
        # A valid file with a retry function
        (ws / "good.py").write_text("def retry_fn():\n    pass\n")
        config = make_config(ws)
        result = run(
            FeatureSearchTool().execute(
                config, keyword="retry", max_results=10, categories=["symbols"]
            )
        )
        # Should not error out; should still find the good file
        assert not result.is_error
        data = json.loads(result.output)
        names = [h["name"] for h in data["symbols"]]
        assert "retry_fn" in names

    def test_unreadable_file_skipped(self, tmp_path):
        import os
        ws = make_workspace(tmp_path)
        (ws / "good.py").write_text("def retry_fn():\n    pass\n")
        # Create and remove read permissions (skip on Windows)
        bad = ws / "noread.py"
        bad.write_text("def retry_other():\n    pass\n")
        try:
            os.chmod(str(bad), 0o000)
            config = make_config(ws)
            result = run(
                FeatureSearchTool().execute(
                    config, keyword="retry", max_results=10, categories=["symbols"]
                )
            )
            # Should not error; good.py still found
            assert not result.is_error
        finally:
            os.chmod(str(bad), 0o644)  # restore so cleanup works


# ---------------------------------------------------------------------------
# files_scanned reflects actual file count
# ---------------------------------------------------------------------------


class TestFilesScanned:
    def test_files_scanned_count(self, tmp_path):
        ws = make_workspace(tmp_path)
        for i in range(3):
            (ws / f"module_{i}.py").write_text("x = 1\n")
        config = make_config(ws)
        result = run(FeatureSearchTool().execute(config, keyword="retry", max_results=10))
        data = json.loads(result.output)
        assert data["files_scanned"] == 3

    def test_non_py_files_not_counted(self, tmp_path):
        ws = make_workspace(tmp_path)
        (ws / "module.py").write_text("x = 1\n")
        (ws / "notes.txt").write_text("retry everything\n")
        config = make_config(ws)
        result = run(FeatureSearchTool().execute(config, keyword="retry", max_results=10))
        data = json.loads(result.output)
        assert data["files_scanned"] == 1


# ---------------------------------------------------------------------------
# Subdirectory scanning
# ---------------------------------------------------------------------------


class TestSubdirectoryScanning:
    def test_scans_nested_subdirectories(self, tmp_path):
        ws = make_workspace(tmp_path)
        sub = ws / "sub"
        sub.mkdir()
        (sub / "retry_helper.py").write_text("def retry_op():\n    pass\n")
        config = make_config(ws)
        result = run(
            FeatureSearchTool().execute(
                config, keyword="retry", max_results=10, categories=["symbols"]
            )
        )
        data = json.loads(result.output)
        assert len(data["symbols"]) >= 1
        assert any("retry_op" == h["name"] for h in data["symbols"])

    def test_file_path_uses_relative_path(self, tmp_path):
        ws = make_workspace(tmp_path)
        sub = ws / "sub"
        sub.mkdir()
        (sub / "retry.py").write_text("def retry_fn():\n    pass\n")
        config = make_config(ws)
        result = run(
            FeatureSearchTool().execute(
                config, keyword="retry", max_results=10, categories=["symbols"]
            )
        )
        data = json.loads(result.output)
        # Path should be relative (not absolute)
        for hit in data["symbols"]:
            assert not hit["file"].startswith("/"), f"Expected relative path, got: {hit['file']}"


# ---------------------------------------------------------------------------
# Multiple files combined
# ---------------------------------------------------------------------------


class TestMultipleFiles:
    def test_results_from_multiple_files(self, tmp_path):
        ws = make_workspace(tmp_path)
        (ws / "a.py").write_text("def retry_a():\n    pass\n")
        (ws / "b.py").write_text("def retry_b():\n    pass\n")
        config = make_config(ws)
        result = run(
            FeatureSearchTool().execute(
                config, keyword="retry", max_results=10, categories=["symbols"]
            )
        )
        data = json.loads(result.output)
        names = {h["name"] for h in data["symbols"]}
        assert "retry_a" in names
        assert "retry_b" in names

    def test_files_scanned_with_multiple_files(self, tmp_path):
        ws = make_workspace(tmp_path)
        for name in ["mod_a.py", "mod_b.py", "mod_c.py"]:
            (ws / name).write_text("x = 1\n")
        config = make_config(ws)
        result = run(FeatureSearchTool().execute(config, keyword="retry", max_results=10))
        data = json.loads(result.output)
        assert data["files_scanned"] == 3
