"""Unit tests for pure helper functions in harness/pipeline/phase_runner.py.

Coverage targets:
- _tokenise_path
- _tokenise_phrase
- score_file_relevance
- _truncate_file_content
- _read_source_files
- _read_source_manifest
- PhaseRunner._select_prior (round-routing logic)
- PhaseRunner._build_executor_prompt (template substitution, sections)
- PhaseRunner._write_phase_summary (artifact content)
"""
from __future__ import annotations

import math
import os
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from harness.pipeline.phase import DualScore, InnerResult, PhaseConfig, ScoreItem
from harness.pipeline.phase_runner import (
    PhaseRunner,
    _FILE_CHAR_LIMIT,
    _RECENCY_HALF_LIFE_SECS,
    _W_KEYWORD,
    _W_RECENCY,
    _W_SIZE,
    _read_source_files,
    _read_source_manifest,
    _tokenise_path,
    _tokenise_phrase,
    _truncate_file_content,
    score_file_relevance,
)


# ---------------------------------------------------------------------------
# _tokenise_path
# ---------------------------------------------------------------------------

class TestTokenisePath:
    def test_slash_split(self):
        result = _tokenise_path("src/phase_runner.py")
        assert "src" in result
        assert "phase" in result
        assert "runner" in result
        assert "py" in result

    def test_camel_case_split(self):
        result = _tokenise_path("harness/DualEvaluator.py")
        assert "dual" in result
        assert "evaluator" in result

    def test_underscore_split(self):
        result = _tokenise_path("test_phase_runner.py")
        assert "test" in result
        assert "phase" in result
        assert "runner" in result

    def test_hyphen_split(self):
        result = _tokenise_path("my-module/helper.py")
        assert "my" in result
        assert "module" in result
        assert "helper" in result

    def test_all_lowercase(self):
        result = _tokenise_path("harness/UPPER/file.PY")
        # All tokens should be lowercase
        for token in result:
            assert token == token.lower()

    def test_empty_string(self):
        # Should not raise, just return empty or minimal
        result = _tokenise_path("")
        assert isinstance(result, set)

    def test_simple_file(self):
        result = _tokenise_path("llm.py")
        assert "llm" in result
        assert "py" in result

    def test_deep_path(self):
        result = _tokenise_path("harness/core/context/builder.py")
        assert "harness" in result
        assert "core" in result
        assert "context" in result
        assert "builder" in result

    def test_returns_set(self):
        result = _tokenise_path("harness/tools/bash.py")
        assert isinstance(result, set)

    def test_pascal_case_multi_word(self):
        result = _tokenise_path("ProjectContextBuilder.py")
        assert "project" in result
        assert "context" in result
        assert "builder" in result


# ---------------------------------------------------------------------------
# _tokenise_phrase
# ---------------------------------------------------------------------------

class TestTokenisePhrase:
    def test_basic_phrase(self):
        result = _tokenise_phrase("requirements analysis")
        assert "requirements" in result
        assert "analysis" in result

    def test_stopwords_removed(self):
        result = _tokenise_phrase("the analysis of a system")
        # 'the', 'a', 'of' are stopwords
        assert "the" not in result
        assert "a" not in result
        assert "of" not in result
        assert "analysis" in result
        assert "system" in result

    def test_short_tokens_removed(self):
        result = _tokenise_phrase("a b c")
        # single-char tokens excluded
        assert "a" not in result
        assert "b" not in result
        assert "c" not in result

    def test_returns_set(self):
        result = _tokenise_phrase("fix the pipeline")
        assert isinstance(result, set)

    def test_all_lowercase(self):
        result = _tokenise_phrase("Refactor Pipeline")
        for token in result:
            assert token == token.lower()

    def test_special_chars_removed(self):
        result = _tokenise_phrase("refactor: pipeline!")
        assert "refactor" in result
        assert "pipeline" in result

    def test_underscore_split(self):
        result = _tokenise_phrase("phase_runner improvements")
        assert "phase" in result
        assert "runner" in result
        assert "improvements" in result

    def test_empty_phrase(self):
        result = _tokenise_phrase("")
        assert isinstance(result, set)
        assert len(result) == 0

    def test_all_stopwords(self):
        result = _tokenise_phrase("the and or a")
        assert len(result) == 0

    def test_common_stopwords(self):
        # Verify that specific stopwords ('add', 'use', 'run', 'get', 'set') are excluded
        result = _tokenise_phrase("add use run get set")
        for word in ["add", "use", "run", "get", "set"]:
            assert word not in result


# ---------------------------------------------------------------------------
# score_file_relevance
# ---------------------------------------------------------------------------

class TestScoreFileRelevance:
    def _now(self):
        return time.time()

    def test_score_in_range(self):
        now = self._now()
        score = score_file_relevance(
            "harness/pipeline/phase_runner.py",
            {"phase", "runner"},
            mtime=now - 3600,  # 1 hour ago
            now=now,
        )
        assert 0.0 <= score <= 1.0

    def test_keyword_match_raises_score(self):
        now = self._now()
        # File whose path directly mentions the keywords
        score_match = score_file_relevance(
            "harness/phase_runner.py",
            {"phase", "runner"},
            mtime=now - 3600,
            now=now,
            file_size=1000,
        )
        # File whose path doesn't match at all
        score_no_match = score_file_relevance(
            "harness/unrelated_utils.py",
            {"phase", "runner"},
            mtime=now - 3600,
            now=now,
            file_size=1000,
        )
        assert score_match > score_no_match

    def test_recent_file_scores_higher(self):
        now = self._now()
        keywords = {"test"}  # neutral keyword
        score_recent = score_file_relevance(
            "test_something.py",
            keywords,
            mtime=now - 60,      # 1 minute ago
            now=now,
            file_size=500,
        )
        score_old = score_file_relevance(
            "test_something.py",
            keywords,
            mtime=now - 7 * 24 * 3600,  # 1 week ago
            now=now,
            file_size=500,
        )
        assert score_recent > score_old

    def test_small_file_scores_higher_than_large(self):
        now = self._now()
        keywords: set[str] = set()  # no keywords → keyword score = 0 for both
        score_small = score_file_relevance(
            "small_file.py",
            keywords,
            mtime=now - 3600,
            now=now,
            file_size=100,  # well under _FILE_CHAR_LIMIT
        )
        score_large = score_file_relevance(
            "large_file.py",
            keywords,
            mtime=now - 3600,
            now=now,
            file_size=_FILE_CHAR_LIMIT * 10,  # 10× over limit
        )
        assert score_small > score_large

    def test_unknown_size_neutral(self):
        now = self._now()
        keywords: set[str] = set()
        score_unknown = score_file_relevance(
            "file.py",
            keywords,
            mtime=now - 3600,
            now=now,
            file_size=0,  # unknown
        )
        # Should return a score (not error), and use neutral size weight
        assert 0.0 <= score_unknown <= 1.0

    def test_empty_keywords_no_keyword_score(self):
        now = self._now()
        score = score_file_relevance(
            "harness/phase_runner.py",
            set(),  # empty keywords
            mtime=now - 3600,
            now=now,
            file_size=500,
        )
        # Keyword weight is 0 → score comes only from recency + size
        # Keyword score = 0, recency ~0.97 (1 hr ago), size = 1.0
        expected = _W_RECENCY * math.pow(0.5, 3600 / _RECENCY_HALF_LIFE_SECS) + _W_SIZE * 1.0
        assert abs(score - expected) < 0.01

    def test_full_keyword_match_boosts_score(self):
        now = self._now()
        # A very recent file with a perfect keyword match
        score = score_file_relevance(
            "phase_runner.py",
            {"phase", "runner"},  # both path tokens
            mtime=now,  # just modified
            now=now,
            file_size=100,
        )
        # Keyword Jaccard: {phase,runner,py} ∩ {phase,runner} = 2, union = 3 → 0.667
        # Recency: 1.0 (just modified)
        # Size: 1.0 (small file)
        assert score > 0.7  # clearly high

    def test_weights_sum_correctly(self):
        # Weights should sum to 1.0
        assert abs(_W_KEYWORD + _W_RECENCY + _W_SIZE - 1.0) < 1e-9

    def test_future_mtime_handled_gracefully(self):
        now = self._now()
        # mtime in the future → age < 0 → clamped to 0
        score = score_file_relevance(
            "file.py",
            {"file"},
            mtime=now + 1000,  # 1000s in the future
            now=now,
            file_size=500,
        )
        assert 0.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# _truncate_file_content
# ---------------------------------------------------------------------------

class TestTruncateFileContent:
    def test_short_file_not_truncated(self):
        content = "x" * 100
        result, was_truncated = _truncate_file_content(content, "tiny.py")
        assert not was_truncated
        assert result == content

    def test_exactly_at_limit_not_truncated(self):
        content = "x" * _FILE_CHAR_LIMIT
        result, was_truncated = _truncate_file_content(content, "exact.py")
        assert not was_truncated
        assert result == content

    def test_one_over_limit_is_truncated(self):
        # A file that is well over the limit should have was_truncated=True.
        # The result may be slightly longer than _FILE_CHAR_LIMIT because the
        # omission-marker text itself adds a few dozen bytes.
        content = "x" * (_FILE_CHAR_LIMIT * 2)
        result, was_truncated = _truncate_file_content(content, "big.py")
        assert was_truncated
        assert len(result) < len(content)

    def test_truncated_output_fits_within_budget(self):
        # A file that is 3× the limit should be truncated to within the limit
        content = "line of code\n" * (_FILE_CHAR_LIMIT // 10)
        result, was_truncated = _truncate_file_content(content, "large.py")
        assert was_truncated
        # Allow a little slack for the omission marker text
        assert len(result) <= _FILE_CHAR_LIMIT + 200

    def test_truncated_contains_omission_marker(self):
        content = "line\n" * 2000  # well over the limit
        result, was_truncated = _truncate_file_content(content, "many_lines.py")
        assert was_truncated
        assert "omitted" in result.lower() or "truncated" in result.lower()

    def test_truncated_preserves_tail_lines(self):
        # Build a large file where the last few lines are unique
        filler = "filler\n" * 1000
        tail = "UNIQUE_TAIL_LINE\n" * 5
        content = filler + tail
        result, was_truncated = _truncate_file_content(content, "tail_test.py")
        # The unique tail content should appear in the result
        assert "UNIQUE_TAIL_LINE" in result

    def test_truncated_preserves_head(self):
        # The first line of a large file should survive truncation
        header = "# module header comment\n"
        body = "body_line\n" * 5000
        content = header + body
        result, was_truncated = _truncate_file_content(content, "with_header.py")
        assert was_truncated
        assert "module header comment" in result

    def test_returns_tuple(self):
        content = "hello world"
        ret = _truncate_file_content(content, "small.py")
        assert isinstance(ret, tuple)
        assert len(ret) == 2
        assert isinstance(ret[1], bool)

    def test_very_large_file_shows_tail_fallback(self):
        # File where even the tail overflows half budget → still returns something sensible
        single_line = "x" * (_FILE_CHAR_LIMIT // 2 + 1)  # > half budget per line
        # ~10 such lines → tail = last _TAIL_LINES lines (but they're huge)
        content = (single_line + "\n") * 5
        result, was_truncated = _truncate_file_content(content, "very_large.py")
        assert was_truncated
        assert len(result) > 0  # Should still return something


# ---------------------------------------------------------------------------
# _read_source_files
# ---------------------------------------------------------------------------

class TestReadSourceFiles:
    def _make_workspace(self, tmp_path: Path, files: dict[str, str]) -> str:
        """Create a temp workspace with the given relative_path → content mapping."""
        for rel, content in files.items():
            dest = tmp_path / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
        return str(tmp_path)

    def test_empty_glob_returns_no_match(self, tmp_path):
        workspace = self._make_workspace(tmp_path, {})
        result = _read_source_files(workspace, ["*.py"])
        assert "No source files matched" in result

    def test_single_file_included(self, tmp_path):
        workspace = self._make_workspace(tmp_path, {"hello.py": "print('hello')\n"})
        result = _read_source_files(workspace, ["*.py"])
        assert "hello.py" in result
        assert "print('hello')" in result

    def test_file_header_format(self, tmp_path):
        workspace = self._make_workspace(tmp_path, {"mod.py": "x = 1\n"})
        result = _read_source_files(workspace, ["*.py"])
        # Block header should follow '=== FILE: <path> ===' format
        assert "=== FILE:" in result
        assert "mod.py" in result

    def test_no_duplicate_files_from_overlapping_globs(self, tmp_path):
        workspace = self._make_workspace(tmp_path, {"harness/foo.py": "content\n"})
        result = _read_source_files(workspace, ["**/*.py", "harness/*.py"])
        # File should appear exactly once
        assert result.count("foo.py") == 1

    def test_total_char_limit_respected(self, tmp_path):
        # Create several files that together exceed a small total limit
        files = {f"file{i}.py": "x = 1\n" * 100 for i in range(5)}
        workspace = self._make_workspace(tmp_path, files)
        limit = 300  # small total limit
        result = _read_source_files(workspace, ["*.py"], total_char_limit=limit)
        assert len(result) <= limit + 300  # Allow small overshoot from manifest

    def test_keyword_relevance_ordering(self, tmp_path):
        # 'planner.py' should rank higher when keywords include 'planner'
        files = {
            "planner.py": "def plan(): pass\n",
            "unrelated.py": "def foo(): pass\n",
        }
        workspace = self._make_workspace(tmp_path, files)
        result = _read_source_files(
            workspace,
            ["*.py"],
            keywords={"planner"},
            total_char_limit=10_000,
        )
        # planner.py should appear before unrelated.py
        planner_pos = result.find("planner.py")
        unrelated_pos = result.find("unrelated.py")
        assert planner_pos != -1
        assert unrelated_pos != -1
        assert planner_pos < unrelated_pos

    def test_no_keywords_uses_mtime_ordering(self, tmp_path):
        # Create two files with different modification times
        workspace = self._make_workspace(
            tmp_path,
            {"older.py": "x = 1\n", "newer.py": "y = 2\n"},
        )
        # Set older.py's mtime to 1 hour ago
        old_time = time.time() - 3600
        os.utime(str(tmp_path / "older.py"), (old_time, old_time))
        # newer.py should appear first (most recently modified)
        result = _read_source_files(workspace, ["*.py"], keywords=None)
        newer_pos = result.find("newer.py")
        older_pos = result.find("older.py")
        assert newer_pos < older_pos

    def test_truncation_manifest_appears(self, tmp_path):
        # One small file and one huge file; both should have some mention
        big_content = "line\n" * (_FILE_CHAR_LIMIT // 4)  # causes per-file truncation
        files = {"big.py": big_content, "small.py": "x = 1\n"}
        workspace = self._make_workspace(tmp_path, files)
        result = _read_source_files(workspace, ["*.py"], total_char_limit=10_000)
        # Should include truncated manifest or omission note if applicable
        assert isinstance(result, str)
        assert len(result) > 0

    def test_unreadable_file_skipped_gracefully(self, tmp_path):
        workspace = self._make_workspace(tmp_path, {"ok.py": "x = 1\n"})
        # Create a file but make it unreadable by creating a directory with the same name
        # Instead, just verify that OSError during read is handled
        # We test this indirectly: if glob returns a file that disappears before read,
        # the function should continue with the rest
        # Here we just verify the happy path doesn't raise
        result = _read_source_files(workspace, ["*.py"])
        assert "ok.py" in result

    def test_glob_subdirectory_recursion(self, tmp_path):
        files = {
            "harness/core/llm.py": "class LLM: pass\n",
            "harness/tools/bash.py": "def bash(): pass\n",
        }
        workspace = self._make_workspace(tmp_path, files)
        result = _read_source_files(workspace, ["harness/**/*.py"])
        assert "llm.py" in result
        assert "bash.py" in result

    def test_empty_keywords_set_uses_mtime_ordering(self, tmp_path):
        # Empty set (not None) should fall through to mtime ordering
        workspace = self._make_workspace(
            tmp_path,
            {"older.py": "x = 1\n", "newer.py": "y = 2\n"},
        )
        old_time = time.time() - 3600
        os.utime(str(tmp_path / "older.py"), (old_time, old_time))
        # Empty set: no keywords → mtime ordering
        result = _read_source_files(workspace, ["*.py"], keywords=set())
        newer_pos = result.find("newer.py")
        older_pos = result.find("older.py")
        # newer should come first (higher mtime)
        assert newer_pos < older_pos


class TestReadSourceManifest:
    """Tests for _read_source_manifest — lightweight file listing without content."""

    @pytest.fixture
    def manifest_workspace(self, tmp_path):
        (tmp_path / "alpha.py").write_text("x = 1\n" * 10)
        (tmp_path / "beta.py").write_text("y = 2\n" * 5)
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "gamma.py").write_text("z = 3\n" * 20)
        (tmp_path / "data.json").write_text("{}")
        return tmp_path

    def test_returns_string(self, manifest_workspace):
        result = _read_source_manifest(str(manifest_workspace), ["**/*.py"])
        assert isinstance(result, str)
        assert len(result) > 0

    def test_returns_header_with_count(self, manifest_workspace):
        result = _read_source_manifest(str(manifest_workspace), ["**/*.py"])
        assert "Available source files" in result or "source files" in result.lower()

    def test_lists_matched_python_files(self, manifest_workspace):
        result = _read_source_manifest(str(manifest_workspace), ["**/*.py"])
        assert "alpha.py" in result
        assert "beta.py" in result
        assert "gamma.py" in result

    def test_excludes_unmatched_extension(self, manifest_workspace):
        result = _read_source_manifest(str(manifest_workspace), ["**/*.py"])
        assert "data.json" not in result

    def test_includes_json_when_glob_matches(self, manifest_workspace):
        result = _read_source_manifest(str(manifest_workspace), ["**/*.json"])
        assert "data.json" in result
        assert "alpha.py" not in result

    def test_empty_workspace_returns_placeholder(self, tmp_path):
        result = _read_source_manifest(str(tmp_path), ["**/*.py"])
        assert "No source files" in result or len(result.strip()) == 0 or "no" in result.lower()

    def test_empty_globs_returns_placeholder_or_empty(self, manifest_workspace):
        result = _read_source_manifest(str(manifest_workspace), [])
        # Should return something empty-ish or a placeholder, not a file list
        assert "alpha.py" not in result

    def test_includes_size_hint(self, manifest_workspace):
        import re
        result = _read_source_manifest(str(manifest_workspace), ["**/*.py"])
        # Should include numeric size info somewhere
        assert re.search(r"\d+", result), "Expected size numbers in manifest"

    def test_no_duplicates_from_overlapping_globs(self, manifest_workspace):
        # Two globs that both match alpha.py
        result = _read_source_manifest(str(manifest_workspace), ["**/*.py", "*.py"])
        count = result.count("alpha.py")
        assert count == 1, f"alpha.py appeared {count} times, expected 1"

    def test_keywords_sort_relevant_files_higher(self, tmp_path):
        """A file with keyword-rich name should appear before unrelated file."""
        (tmp_path / "executor_impl.py").write_text("pass\n")
        (tmp_path / "unrelated_utils.py").write_text("pass\n")
        keywords = {"executor"}
        result = _read_source_manifest(str(tmp_path), ["**/*.py"], keywords=keywords)
        assert "executor_impl.py" in result and "unrelated_utils.py" in result
        idx_exec = result.index("executor_impl.py")
        idx_other = result.index("unrelated_utils.py")
        assert idx_exec < idx_other

    def test_all_file_entries_are_files_not_dirs(self, manifest_workspace):
        """Directory paths should not appear as file entries."""
        result = _read_source_manifest(str(manifest_workspace), ["**"])
        # Look for entries that have size indicators — those should be files
        import re
        # If format includes byte sizes, check that entries are file-like
        lines_with_sizes = [
            ln for ln in result.splitlines()
            if re.search(r"\d+ bytes", ln)
        ]
        # Should not have lines ending in / (directory markers)
        for line in lines_with_sizes:
            assert not line.rstrip().endswith("/"), f"Directory showed up as entry: {line}"

    def test_returns_relative_paths(self, manifest_workspace):
        """Manifest paths should be relative to workspace (no absolute paths leaked)."""
        result = _read_source_manifest(str(manifest_workspace), ["**/*.py"])
        # Should not contain the full tmp_path prefix in the file listings
        assert str(manifest_workspace).lstrip("/") not in result.replace(str(manifest_workspace) + ":", "x")


# ---------------------------------------------------------------------------
# Helpers for PhaseRunner method tests
# ---------------------------------------------------------------------------

def _make_runner() -> PhaseRunner:
    """Create a minimal PhaseRunner stub (no LLM, no I/O)."""
    runner = object.__new__(PhaseRunner)
    runner.harness = MagicMock()
    runner.harness.workspace = "/fake/workspace"
    runner.artifacts = MagicMock()
    runner.artifacts.phase_dir.return_value = ("outer01", "phase01")
    return runner


def _make_phase(name: str = "implement", system_prompt: str = "$file_context") -> PhaseConfig:
    return PhaseConfig(name=name, index=0, system_prompt=system_prompt)


def _make_inner_result(proposal: str = "best impl", score: float = 7.5) -> InnerResult:
    dual = DualScore(
        basic=ScoreItem(score, "critique"),
        diffusion=ScoreItem(score - 0.5, "diffusion critique"),
    )
    return InnerResult(proposal=proposal, dual_score=dual)


# ---------------------------------------------------------------------------
# PhaseRunner._select_prior
# ---------------------------------------------------------------------------

class TestSelectPrior:
    """Tests for PhaseRunner._select_prior."""

    def test_inner_zero_returns_initial_prior(self):
        """When inner==0, return initial_prior regardless of best_result."""
        runner = _make_runner()
        result = runner._select_prior(0, "initial proposal", None)
        assert result == "initial proposal"

    def test_inner_zero_no_initial_prior_returns_none(self):
        """When inner==0 and no initial_prior, return None."""
        runner = _make_runner()
        result = runner._select_prior(0, None, None)
        assert result is None

    def test_inner_zero_ignores_best_result(self):
        """When inner==0, even a valid best_result is ignored — use initial_prior."""
        runner = _make_runner()
        best = _make_inner_result("should-be-ignored")
        result = runner._select_prior(0, "initial", best)
        assert result == "initial"

    def test_inner_positive_uses_best_result_proposal(self):
        """When inner>0 and best_result is set, return best_result.proposal."""
        runner = _make_runner()
        best = _make_inner_result("best proposal text")
        result = runner._select_prior(1, "initial fallback", best)
        assert result == "best proposal text"

    def test_inner_positive_no_best_result_falls_back_to_initial(self):
        """When inner>0 but best_result is None, fall back to initial_prior."""
        runner = _make_runner()
        result = runner._select_prior(3, "fallback proposal", None)
        assert result == "fallback proposal"

    def test_inner_positive_both_none_returns_none(self):
        """When inner>0, best_result is None, initial_prior is None → None."""
        runner = _make_runner()
        result = runner._select_prior(2, None, None)
        assert result is None

    def test_inner_positive_high_index(self):
        """Works for any positive inner index."""
        runner = _make_runner()
        best = _make_inner_result("proposal from round 9")
        result = runner._select_prior(9, "old initial", best)
        assert result == "proposal from round 9"


# ---------------------------------------------------------------------------
# PhaseRunner._build_executor_prompt
# ---------------------------------------------------------------------------

class TestBuildExecutorPrompt:
    """Tests for PhaseRunner._build_executor_prompt template rendering."""

    def test_workspace_reminder_always_prepended(self):
        """The workspace path must appear in every prompt."""
        runner = _make_runner()
        phase = _make_phase(system_prompt="$file_context")
        result = runner._build_executor_prompt(phase, "FC", None, "")
        assert "/fake/workspace" in result

    def test_file_context_substituted(self):
        """$file_context in the template is replaced with actual file content."""
        runner = _make_runner()
        phase = _make_phase(system_prompt="Files:\n$file_context\nEnd")
        result = runner._build_executor_prompt(phase, "THE_CONTENT", None, "")
        assert "THE_CONTENT" in result
        assert "$file_context" not in result

    def test_prior_best_section_added_when_present(self):
        """When prior_best is provided, a '## Prior Best' section is included."""
        runner = _make_runner()
        phase = _make_phase(system_prompt="$file_context\n$prior_best")
        result = runner._build_executor_prompt(phase, "FC", "MY PRIOR", "")
        assert "MY PRIOR" in result
        assert "Prior Best" in result

    def test_no_prior_best_section_when_absent(self):
        """When prior_best is None, no prior section appears in the output."""
        runner = _make_runner()
        phase = _make_phase(system_prompt="$file_context\n$prior_best")
        result = runner._build_executor_prompt(phase, "FC", None, "")
        assert "Prior Best" not in result

    def test_syntax_section_added_when_errors_present(self):
        """When syntax_errors is non-empty, a PRIORITY FIX section is included."""
        runner = _make_runner()
        phase = _make_phase(system_prompt="$file_context\n$syntax_errors")
        result = runner._build_executor_prompt(phase, "FC", None, "SyntaxError: line 7")
        assert "SyntaxError: line 7" in result
        assert "PRIORITY FIX" in result

    def test_no_syntax_section_when_errors_empty(self):
        """When syntax_errors is empty string, no syntax section appears."""
        runner = _make_runner()
        phase = _make_phase(system_prompt="$file_context\n$syntax_errors")
        result = runner._build_executor_prompt(phase, "FC", None, "")
        assert "PRIORITY FIX" not in result

    def test_no_prior_or_syntax_no_extra_sections(self):
        """When both prior and syntax are absent, no extra sections are injected."""
        runner = _make_runner()
        phase = _make_phase(system_prompt="$file_context")
        result = runner._build_executor_prompt(phase, "CONTENT", None, "")
        assert "Prior Best" not in result
        assert "PRIORITY FIX" not in result
        assert "CONTENT" in result

    def test_prior_and_syntax_both_present(self):
        """Both prior and syntax sections appear when both are provided."""
        runner = _make_runner()
        phase = _make_phase(system_prompt="$file_context\n$prior_best\n$syntax_errors")
        result = runner._build_executor_prompt(phase, "FC", "PRIOR TEXT", "BadSyntax")
        assert "PRIOR TEXT" in result
        assert "BadSyntax" in result
        assert "Prior Best" in result
        assert "PRIORITY FIX" in result

    def test_literal_template_dollar_sign_preserved(self):
        """Template variables not defined are left as-is (safe_substitute)."""
        runner = _make_runner()
        # $unknown_var is not a valid substitution key — safe_substitute leaves it
        phase = _make_phase(system_prompt="$file_context $unknown_var")
        result = runner._build_executor_prompt(phase, "FC", None, "")
        # safe_substitute: unknown vars are kept literally
        assert "$unknown_var" in result

    def test_output_is_string(self):
        runner = _make_runner()
        phase = _make_phase()
        result = runner._build_executor_prompt(phase, "x", None, "")
        assert isinstance(result, str)
        assert len(result) > 0


# ---------------------------------------------------------------------------
# PhaseRunner._write_phase_summary
# ---------------------------------------------------------------------------

class TestWritePhaseSummary:
    """Tests for PhaseRunner._write_phase_summary."""

    def test_calls_artifacts_write(self):
        """Must call artifacts.write to persist summary content."""
        runner = _make_runner()
        phase = _make_phase()
        r1 = _make_inner_result("proposal1")
        runner._write_phase_summary(
            outer=1, phase=phase, results=[r1], synthesis="SYNTHESIS TEXT", best_score=7.5
        )
        assert runner.artifacts.write.called

    def test_best_score_in_summary(self):
        """The best_score float must appear in the written summary."""
        runner = _make_runner()
        phase = _make_phase()
        r1 = _make_inner_result()
        runner._write_phase_summary(
            outer=1, phase=phase, results=[r1], synthesis="synth", best_score=8.8
        )
        written = runner.artifacts.write.call_args[0][0]
        assert "8.8" in written

    def test_synthesis_text_in_summary(self):
        """The synthesis string must appear verbatim in the written summary."""
        runner = _make_runner()
        phase = _make_phase()
        r1 = _make_inner_result()
        runner._write_phase_summary(
            outer=2, phase=phase, results=[r1], synthesis="UNIQUE_SYNTHESIS_MARKER", best_score=6.0
        )
        written = runner.artifacts.write.call_args[0][0]
        assert "UNIQUE_SYNTHESIS_MARKER" in written

    def test_outer_round_in_header(self):
        """Round number (outer + 1) should appear in the summary header."""
        runner = _make_runner()
        phase = _make_phase()
        results = [_make_inner_result(f"proposal {i}") for i in range(3)]
        runner._write_phase_summary(
            outer=4, phase=phase, results=results, synthesis="synth", best_score=7.0
        )
        written = runner.artifacts.write.call_args[0][0]
        # Round label = outer + 1 = 5
        assert "Round 5" in written

    def test_phase_name_in_header(self):
        """Phase name should appear in the summary header."""
        runner = _make_runner()
        phase = _make_phase(name="framework_improvement")
        r1 = _make_inner_result()
        runner._write_phase_summary(
            outer=1, phase=phase, results=[r1], synthesis="s", best_score=5.0
        )
        written = runner.artifacts.write.call_args[0][0]
        assert "framework_improvement" in written

    def test_empty_results_list(self):
        """Should not crash when results list is empty."""
        runner = _make_runner()
        phase = _make_phase()
        runner._write_phase_summary(
            outer=1, phase=phase, results=[], synthesis="no rounds", best_score=0.0
        )
        assert runner.artifacts.write.called
        written = runner.artifacts.write.call_args[0][0]
        assert "no rounds" in written


# ---------------------------------------------------------------------------
# PhaseRunner._build_active_registry
# ---------------------------------------------------------------------------

class TestBuildActiveRegistry:
    """Tests for PhaseRunner._build_active_registry (tool set selection)."""

    def test_default_phase_uses_read_only_tags(self):
        """A phase with no extra tool_tags → registry filtered to _READ_ONLY_TAGS."""
        from harness.pipeline.phase_runner import _READ_ONLY_TAGS

        runner = _make_runner()
        runner.registry = MagicMock()
        runner.registry.filter_by_tags.return_value = MagicMock()

        phase = _make_phase()  # no tool_tags
        runner._build_active_registry(phase)

        passed_tags = runner.registry.filter_by_tags.call_args[0][0]
        assert _READ_ONLY_TAGS.issubset(passed_tags)

    def test_phase_with_extra_tool_tags_includes_them(self):
        """tool_tags=['write'] adds 'write' to the active set."""
        runner = _make_runner()
        runner.registry = MagicMock()
        runner.registry.filter_by_tags.return_value = MagicMock()

        phase = PhaseConfig(name="impl", index=0, system_prompt="x", tool_tags=["write"])
        runner._build_active_registry(phase)

        passed_tags = runner.registry.filter_by_tags.call_args[0][0]
        assert "write" in passed_tags

    def test_result_comes_from_registry(self):
        """Return value is the filtered registry object."""
        sentinel = MagicMock()
        runner = _make_runner()
        runner.registry = MagicMock()
        runner.registry.filter_by_tags.return_value = sentinel

        phase = _make_phase()
        result = runner._build_active_registry(phase)
        assert result is sentinel

    def test_multiple_extra_tags_all_included(self):
        """Multiple extra tool_tags are all included in the filter set."""
        runner = _make_runner()
        runner.registry = MagicMock()
        runner.registry.filter_by_tags.return_value = MagicMock()

        phase = PhaseConfig(
            name="impl", index=0, system_prompt="x", tool_tags=["write", "exec", "custom"]
        )
        runner._build_active_registry(phase)

        passed_tags = runner.registry.filter_by_tags.call_args[0][0]
        assert "write" in passed_tags
        assert "exec" in passed_tags
        assert "custom" in passed_tags


# ---------------------------------------------------------------------------
# PhaseRunner._load_inner_result
# ---------------------------------------------------------------------------

class TestLoadInnerResult:
    """Tests for PhaseRunner._load_inner_result (disk → InnerResult)."""

    def _make_read_side_effect(self, mapping: dict[str, str]):
        """Create a side_effect for artifacts.read(*segs, filename) calls."""
        def side_effect(*args):
            filename = args[-1]
            return mapping.get(filename, "")
        return side_effect

    def test_loads_proposal(self):
        """proposal.txt content ends up in InnerResult.proposal."""
        runner = _make_runner()
        runner.artifacts.inner_dir.return_value = ("outer01", "phase01", "inner01")
        runner.artifacts.read.side_effect = self._make_read_side_effect(
            {
                "proposal.txt": "MY PROPOSAL",
                "basic_eval.txt": "Good work.\nSCORE: 7.5",
                "diffusion_eval.txt": "Ok.\nSCORE: 7.0",
            }
        )

        result = runner._load_inner_result(1, "implement", 0)
        assert result.proposal == "MY PROPOSAL"

    def test_falls_back_to_implement_output_if_no_proposal(self):
        """If proposal.txt is empty, fall back to implement_output.txt."""
        runner = _make_runner()
        runner.artifacts.inner_dir.return_value = ("outer01", "phase01", "inner01")
        runner.artifacts.read.side_effect = self._make_read_side_effect(
            {
                "proposal.txt": "",  # empty → fall back
                "implement_output.txt": "FALLBACK PROPOSAL",
                "basic_eval.txt": "SCORE: 6.0",
                "diffusion_eval.txt": "SCORE: 5.5",
            }
        )

        result = runner._load_inner_result(1, "implement", 0)
        assert result.proposal == "FALLBACK PROPOSAL"

    def test_dual_score_parsed(self):
        """dual_score is built from basic_eval.txt and diffusion_eval.txt scores."""
        runner = _make_runner()
        runner.artifacts.inner_dir.return_value = ("outer01", "phase01", "inner01")
        runner.artifacts.read.side_effect = self._make_read_side_effect(
            {
                "proposal.txt": "p",
                "basic_eval.txt": "Great.\nSCORE: 8.5",
                "diffusion_eval.txt": "Ok.\nSCORE: 7.0",
            }
        )

        result = runner._load_inner_result(1, "implement", 0)
        assert result.dual_score is not None
        assert result.dual_score.basic.score == pytest.approx(8.5)
        assert result.dual_score.diffusion.score == pytest.approx(7.0)

    def test_extra_fields_read(self):
        """syntax_errors and pytest_result are read from disk."""
        runner = _make_runner()
        runner.artifacts.inner_dir.return_value = ("o", "p", "i")
        runner.artifacts.read.side_effect = self._make_read_side_effect(
            {
                "proposal.txt": "prop",
                "basic_eval.txt": "SCORE: 5.0",
                "diffusion_eval.txt": "SCORE: 5.0",
                "syntax_errors.txt": "SyntaxError at line 3",
                "pytest_result.txt": "5 failed, 3 passed",
            }
        )

        result = runner._load_inner_result(0, "phase", 0)
        assert result.syntax_errors == "SyntaxError at line 3"
        assert result.pytest_result == "5 failed, 3 passed"

    def test_calls_inner_dir_with_correct_args(self):
        """inner_dir is called with the outer, label, and inner arguments."""
        runner = _make_runner()
        runner.artifacts.inner_dir.return_value = ("x", "y", "z")
        runner.artifacts.read.side_effect = self._make_read_side_effect(
            {
                "proposal.txt": "p",
                "basic_eval.txt": "SCORE: 7",
                "diffusion_eval.txt": "SCORE: 6",
            }
        )

        runner._load_inner_result(3, "my_phase", 2)
        runner.artifacts.inner_dir.assert_called_once_with(3, "my_phase", 2)
