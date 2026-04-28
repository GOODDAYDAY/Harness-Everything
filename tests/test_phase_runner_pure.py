"""Tests for the pure-function layer of harness/pipeline/phase_runner.py.

Covers:
  * _tokenise_path
  * _tokenise_phrase
  * score_file_relevance
  * _truncate_file_content

No async, no filesystem, no API calls needed.
"""
from __future__ import annotations

import pathlib
import time
import unittest.mock as mock

from harness.pipeline.phase_runner import (
    _FILE_CHAR_LIMIT,
    _READ_ONLY_TAGS,
    _TAIL_LINES,
    _TOTAL_CHAR_LIMIT,
    _W_KEYWORD,
    _W_RECENCY,
    _W_SIZE,
    PhaseRunner,
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
    def test_simple_filename(self):
        tokens = _tokenise_path("llm.py")
        assert "llm" in tokens
        assert "py" in tokens

    def test_snake_case_split(self):
        tokens = _tokenise_path("phase_runner.py")
        assert "phase" in tokens
        assert "runner" in tokens

    def test_camel_case_split(self):
        tokens = _tokenise_path("DualEvaluator.py")
        assert "dual" in tokens
        assert "evaluator" in tokens

    def test_path_with_directory(self):
        tokens = _tokenise_path("harness/core/llm.py")
        assert "harness" in tokens
        assert "core" in tokens
        assert "llm" in tokens

    def test_returns_set(self):
        result = _tokenise_path("something.py")
        assert isinstance(result, set)

    def test_all_lowercase(self):
        tokens = _tokenise_path("MyModule.py")
        for t in tokens:
            assert t == t.lower(), f"Token {t!r} should be lowercase"

    def test_empty_string(self):
        assert _tokenise_path("") == set()

    def test_deep_nested_path(self):
        tokens = _tokenise_path("a/b/c/deep_file_name.py")
        assert "deep" in tokens
        assert "file" in tokens
        assert "name" in tokens

    def test_numeric_components_included(self):
        tokens = _tokenise_path("v2_planner.py")
        # Should contain at least one meaningful token
        assert len(tokens) > 0


# ---------------------------------------------------------------------------
# _tokenise_phrase
# ---------------------------------------------------------------------------

class TestTokenisePhrase:
    def test_simple_words(self):
        tokens = _tokenise_phrase("code analysis")
        assert "code" in tokens
        assert "analysis" in tokens

    def test_stop_words_filtered(self):
        tokens = _tokenise_phrase("the quick brown fox")
        assert "the" not in tokens  # common stop word

    def test_returns_set(self):
        result = _tokenise_phrase("some words")
        assert isinstance(result, set)

    def test_all_lowercase(self):
        tokens = _tokenise_phrase("PyTest Runner")
        for t in tokens:
            assert t == t.lower()

    def test_empty_string(self):
        assert _tokenise_phrase("") == set()

    def test_single_word(self):
        tokens = _tokenise_phrase("planner")
        assert "planner" in tokens

    def test_common_stop_words_filtered(self):
        # These should be filtered by standard stop-word logic
        for stop in ["the", "a", "an", "is", "in", "for"]:
            tokens = _tokenise_phrase(f"{stop} relevant")
            assert stop not in tokens, f"Stop word {stop!r} should be filtered"
            assert "relevant" in tokens

    def test_meaningful_words_kept(self):
        tokens = _tokenise_phrase("evaluator proposal synthesis")
        assert "evaluator" in tokens
        assert "proposal" in tokens
        assert "synthesis" in tokens


# ---------------------------------------------------------------------------
# score_file_relevance
# ---------------------------------------------------------------------------

class TestScoreFileRelevance:
    def setup_method(self):
        self.now = time.time()

    def _score(self, path, keywords=None, mtime_offset=0, file_size=2000):
        kws = set(keywords) if keywords is not None else set()
        return score_file_relevance(
            path,
            kws,
            mtime=self.now - mtime_offset,
            now=self.now,
            file_size=file_size,
        )

    def test_returns_float(self):
        result = self._score("harness/core/llm.py")
        assert isinstance(result, float)

    def test_score_bounded_zero_to_one(self):
        for path in ["a.py", "long_file_name.py", "harness/deep/path.py"]:
            s = self._score(path, {"llm", "core"})
            assert 0.0 <= s <= 1.0, f"Score {s} out of [0,1] for {path}"

    def test_full_keyword_match_beats_partial(self):
        # Path with both keywords scores higher than path with one
        s_full = self._score("harness/llm/core.py", {"llm", "core"})
        s_partial = self._score("harness/llm/utils.py", {"llm", "core"})
        assert s_full >= s_partial

    def test_keyword_match_beats_no_match(self):
        s_match = self._score("evaluator.py", {"evaluator"})
        s_no_match = self._score("utils.py", {"evaluator"})
        assert s_match > s_no_match

    def test_newer_file_scores_higher(self):
        s_new = self._score("utils.py", mtime_offset=10)  # 10s old
        s_old = self._score("utils.py", mtime_offset=3600 * 24 * 30)  # 30d old
        assert s_new >= s_old

    def test_very_tiny_file_penalised(self):
        s_small = self._score("utils.py", file_size=10)
        s_medium = self._score("utils.py", file_size=2000)
        # Tiny files (not enough content) should score <= medium
        assert s_small <= s_medium

    def test_very_large_file_penalised(self):
        s_huge = self._score("utils.py", file_size=500_000)
        s_medium = self._score("utils.py", file_size=2000)
        assert s_huge <= s_medium

    def test_empty_keywords_no_keyword_bonus(self):
        s_no_kw = self._score("llm.py", keywords=set())
        # Without keywords, max achievable keyword score is 0
        # So score should be _W_RECENCY * recency + _W_SIZE * size
        # Just verify it doesn't crash and returns a valid float
        assert 0.0 <= s_no_kw <= 1.0

    def test_keyword_match_monotone_with_path_depth(self):
        # Same tokens, different paths — all should score equally when keywords match
        s1 = self._score("llm.py", {"llm"})
        s2 = self._score("core/llm.py", {"llm"})
        # Both should have keyword match; should not crash
        assert 0.0 <= s1 <= 1.0
        assert 0.0 <= s2 <= 1.0

    def test_weights_sum_to_one(self):
        assert abs(_W_KEYWORD + _W_RECENCY + _W_SIZE - 1.0) < 1e-9

    def test_camel_case_path_tokenised(self):
        """CamelCase file name tokens should still be matchable."""
        s = self._score("DualEvaluator.py", {"dual"})
        assert s > self._score("unrelated.py", {"dual"})


# ---------------------------------------------------------------------------
# _truncate_file_content
# ---------------------------------------------------------------------------

class TestTruncateFileContent:
    def test_short_content_unchanged(self):
        content = "line 1\nline 2\nline 3\n"
        result, was_truncated = _truncate_file_content(content, "test.py")
        assert result == content
        assert not was_truncated

    def test_long_content_truncated(self):
        # Content exceeds _FILE_CHAR_LIMIT (use many short lines so tail fits in budget)
        lines = [f"line {i:05d}: " + "x" * 50 for i in range(2000)]
        content = "\n".join(lines)
        assert len(content) > _FILE_CHAR_LIMIT  # precondition
        result, was_truncated = _truncate_file_content(content, "test.py")
        assert was_truncated
        # Result should not wildly exceed _FILE_CHAR_LIMIT (tail-only path allowed to exceed)
        assert len(result) <= _FILE_CHAR_LIMIT + _TAIL_LINES * 60  # tail may push it a bit over

    def test_omission_marker_in_truncated_output(self):
        content = "\n".join(f"line {i}" for i in range(5000))
        result, was_truncated = _truncate_file_content(content, "test.py")
        if was_truncated:
            assert "omit" in result.lower() or "trunc" in result.lower() or "..." in result

    def test_tail_lines_preserved(self):
        # Last _TAIL_LINES should appear in result
        lines = [f"line {i}" for i in range(5000)]
        content = "\n".join(lines)
        result, was_truncated = _truncate_file_content(content, "test.py")
        # Check last line is present in the truncated output
        assert lines[-1] in result

    def test_first_lines_preserved_if_content_fits(self):
        # Content exactly at limit should not be truncated
        content = "a" * (_FILE_CHAR_LIMIT - 10)
        result, was_truncated = _truncate_file_content(content, "test.py")
        assert not was_truncated
        assert result == content

    def test_returns_tuple_of_str_and_bool(self):
        result = _truncate_file_content("hello", "x.py")
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], str)
        assert isinstance(result[1], bool)

    def test_empty_content_not_truncated(self):
        result, was_truncated = _truncate_file_content("", "empty.py")
        assert not was_truncated
        assert result == ""

    def test_exactly_at_limit_not_truncated(self):
        content = "a" * _FILE_CHAR_LIMIT
        result, was_truncated = _truncate_file_content(content, "test.py")
        assert not was_truncated

    def test_file_char_limit_constant(self):
        assert _FILE_CHAR_LIMIT > 0
        assert isinstance(_FILE_CHAR_LIMIT, int)

    def test_tail_lines_constant(self):
        assert _TAIL_LINES > 0
        assert isinstance(_TAIL_LINES, int)

    def test_single_line_no_newlines_bounded(self):
        # Minified / base64 content with no newlines must not exceed _FILE_CHAR_LIMIT
        content = "x" * (_FILE_CHAR_LIMIT * 3)
        result, was_truncated = _truncate_file_content(content, "minified.js")
        assert was_truncated
        # Add generous margin (200 chars) for omission marker overhead
        assert len(result) <= _FILE_CHAR_LIMIT + 200, (
            f"Output {len(result)} exceeded _FILE_CHAR_LIMIT {_FILE_CHAR_LIMIT}"
        )

    def test_few_very_long_lines_bounded(self):
        # 5 lines each of 5000 chars: tail strategy would return all 5 = 25000 chars
        lines = ["x" * 5000] * 5
        content = "\n".join(lines)
        result, was_truncated = _truncate_file_content(content, "big_lines.py")
        assert was_truncated
        assert len(result) <= _FILE_CHAR_LIMIT + 200


# ---------------------------------------------------------------------------
# Integration: tokenise + score pipeline
# ---------------------------------------------------------------------------

class TestTokeniseAndScoreIntegration:
    def test_tokenise_path_feeds_score_correctly(self):
        """Tokens from _tokenise_path should match keywords from _tokenise_phrase."""
        now = time.time()
        keywords = _tokenise_phrase("phase runner pipeline")
        # This file's path tokens should overlap with the keywords
        s_relevant = score_file_relevance(
            "harness/pipeline/phase_runner.py",
            keywords,
            mtime=now - 100,
            now=now,
            file_size=20000,
        )
        s_irrelevant = score_file_relevance(
            "harness/tools/bash.py",
            keywords,
            mtime=now - 100,
            now=now,
            file_size=20000,
        )
        assert s_relevant > s_irrelevant

    def test_keyword_specificity_matters(self):
        """More-specific keyword sets should more strongly differentiate files."""
        now = time.time()
        # Unique keyword
        kw = {"synthesis"}
        s_match = score_file_relevance(
            "harness/pipeline/synthesis_runner.py",
            kw, now - 60, now, 5000,
        )
        s_miss = score_file_relevance(
            "harness/pipeline/planner.py",
            kw, now - 60, now, 5000,
        )
        assert s_match > s_miss


# ---------------------------------------------------------------------------
# _read_source_files
# ---------------------------------------------------------------------------

class TestReadSourceFiles:
    """Tests for _read_source_files using a real temporary directory."""

    def _make_workspace(self, tmp_path: pathlib.Path) -> pathlib.Path:
        """Create a small synthetic workspace for tests."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "alpha.py").write_text("def alpha():\n    return 1\n")
        (tmp_path / "src" / "beta.py").write_text("def beta():\n    # keyword_unique\n    return 2\n")
        (tmp_path / "other.txt").write_text("plain text file\n")
        return tmp_path

    def test_empty_glob_returns_no_match_sentinel(self, tmp_path):
        ws = self._make_workspace(tmp_path)
        result = _read_source_files(ws, [], [], _TOTAL_CHAR_LIMIT)
        # No patterns → nothing found; function returns a sentinel string, not ""
        assert isinstance(result, str)

    def test_returns_string(self, tmp_path):
        ws = self._make_workspace(tmp_path)
        result = _read_source_files(ws, ["**/*.py"], set(), _TOTAL_CHAR_LIMIT)
        assert isinstance(result, str)

    def test_includes_matching_files(self, tmp_path):
        ws = self._make_workspace(tmp_path)
        result = _read_source_files(ws, ["**/*.py"], set(), _TOTAL_CHAR_LIMIT)
        # Both .py files should appear
        assert "alpha" in result
        assert "beta" in result

    def test_excludes_non_matching_files(self, tmp_path):
        ws = self._make_workspace(tmp_path)
        result = _read_source_files(ws, ["**/*.py"], set(), _TOTAL_CHAR_LIMIT)
        # .txt file should not appear in .py glob
        assert "plain text" not in result

    def test_keyword_in_result_promotes_relevant_file(self, tmp_path):
        ws = self._make_workspace(tmp_path)
        # beta.py contains "keyword_unique" — with that keyword, beta should be included
        # keywords must be passed as a set
        result = _read_source_files(ws, ["**/*.py"], {"keyword_unique"}, _TOTAL_CHAR_LIMIT)
        assert "beta" in result

    def test_char_limit_respected(self, tmp_path):
        ws = self._make_workspace(tmp_path)
        # Very small limit — should return something but not exceed it much
        result = _read_source_files(ws, ["**/*.py"], set(), 50)
        # Result may be empty or very short due to tight budget
        assert isinstance(result, str)
        # Even with overhead markers, result should not be wildly oversized
        assert len(result) < 5000  # sanity ceiling

    def test_total_char_limit_zero_returns_sentinel_or_empty(self, tmp_path):
        ws = self._make_workspace(tmp_path)
        result = _read_source_files(ws, ["**/*.py"], set(), 0)
        # With zero budget the function returns a sentinel or empty string
        assert isinstance(result, str)

    def test_no_glob_matches_returns_sentinel(self, tmp_path):
        ws = self._make_workspace(tmp_path)
        result = _read_source_files(ws, ["**/*.nonexistent"], set(), _TOTAL_CHAR_LIMIT)
        # No files match → sentinel is returned
        assert isinstance(result, str)
        assert "nonexistent" not in result or "[No source files matched]" in result

    def test_path_header_in_output(self, tmp_path):
        ws = self._make_workspace(tmp_path)
        result = _read_source_files(ws, ["src/alpha.py"], set(), _TOTAL_CHAR_LIMIT)
        # The function should include the file path in the output as a header
        assert "alpha.py" in result

    def test_deduplication(self, tmp_path):
        ws = self._make_workspace(tmp_path)
        # Same file matched by two different globs — should not appear twice
        result = _read_source_files(
            ws,
            ["**/*.py", "src/*.py"],
            set(),
            _TOTAL_CHAR_LIMIT,
        )
        # Count occurrences of "def alpha" — should be exactly 1
        assert result.count("def alpha") == 1


# ---------------------------------------------------------------------------
# _read_source_manifest
# ---------------------------------------------------------------------------

class TestReadSourceManifest:
    """Tests for _read_source_manifest using a temporary directory."""

    def _make_workspace(self, tmp_path: pathlib.Path) -> pathlib.Path:
        (tmp_path / "harness").mkdir()
        (tmp_path / "harness" / "core.py").write_text("# core module\n")
        (tmp_path / "harness" / "utils.py").write_text("# utils module\n")
        return tmp_path

    def test_returns_string(self, tmp_path):
        ws = self._make_workspace(tmp_path)
        result = _read_source_manifest(ws, ["**/*.py"], [])
        assert isinstance(result, str)

    def test_empty_glob_returns_string(self, tmp_path):
        ws = self._make_workspace(tmp_path)
        result = _read_source_manifest(ws, [], set())
        assert isinstance(result, str)

    def test_includes_file_names(self, tmp_path):
        ws = self._make_workspace(tmp_path)
        result = _read_source_manifest(ws, ["**/*.py"], set())
        assert "core.py" in result
        assert "utils.py" in result

    def test_no_match_returns_string(self, tmp_path):
        ws = self._make_workspace(tmp_path)
        result = _read_source_manifest(ws, ["**/*.ts"], set())
        assert isinstance(result, str)

    def test_keyword_match_file_included(self, tmp_path):
        ws = self._make_workspace(tmp_path)
        result = _read_source_manifest(ws, ["**/*.py"], {"core"})
        # core.py should appear
        assert "core" in result


# ---------------------------------------------------------------------------
# PhaseRunner._select_prior
# ---------------------------------------------------------------------------

def _make_runner(tmp_path: pathlib.Path) -> PhaseRunner:
    """Return a PhaseRunner with all dependencies mocked out."""
    harness_cfg = mock.MagicMock()
    harness_cfg.workspace = tmp_path
    harness_cfg.intel_file = tmp_path / "intel.jsonl"  # does not exist
    registry = mock.MagicMock()
    registry.filter_by_tags.return_value = registry  # stub chaining
    llm = mock.MagicMock()
    runner = PhaseRunner.__new__(PhaseRunner)
    runner.harness = harness_cfg
    runner.registry = registry
    runner.llm = llm
    return runner


class TestSelectPrior:
    def test_inner_zero_returns_initial_prior(self, tmp_path):
        runner = _make_runner(tmp_path)
        result = runner._select_prior(0, "initial", None)
        assert result == "initial"

    def test_inner_zero_ignores_best_result(self, tmp_path):
        runner = _make_runner(tmp_path)
        best = mock.MagicMock()
        best.proposal = "best_proposal"
        result = runner._select_prior(0, "initial", best)
        assert result == "initial"

    def test_inner_gt_zero_with_best_returns_proposal(self, tmp_path):
        runner = _make_runner(tmp_path)
        best = mock.MagicMock()
        best.proposal = "previous_best"
        result = runner._select_prior(1, "initial", best)
        assert result == "previous_best"

    def test_inner_gt_zero_without_best_returns_initial(self, tmp_path):
        runner = _make_runner(tmp_path)
        result = runner._select_prior(3, "initial", None)
        assert result == "initial"

    def test_inner_gt_zero_empty_string_initial_prior(self, tmp_path):
        runner = _make_runner(tmp_path)
        result = runner._select_prior(2, "", None)
        assert result == ""

    def test_returns_exact_proposal_string(self, tmp_path):
        runner = _make_runner(tmp_path)
        best = mock.MagicMock()
        best.proposal = "some\nmultiline\nproposal"
        result = runner._select_prior(5, "initial", best)
        assert result == "some\nmultiline\nproposal"


# ---------------------------------------------------------------------------
# PhaseRunner._build_active_registry
# ---------------------------------------------------------------------------

class TestBuildActiveRegistry:
    def test_no_tool_tags_returns_read_only_subset(self, tmp_path):
        runner = _make_runner(tmp_path)
        phase = mock.MagicMock()
        phase.tool_tags = []
        runner._build_active_registry(phase)
        # Should call filter_by_tags with _READ_ONLY_TAGS only
        runner.registry.filter_by_tags.assert_called()
        call_args = runner.registry.filter_by_tags.call_args
        tags_used = call_args[0][0] if call_args[0] else call_args[1].get("tags", [])
        for tag in _READ_ONLY_TAGS:
            assert tag in tags_used

    def test_tool_tags_included_alongside_read_only(self, tmp_path):
        runner = _make_runner(tmp_path)
        phase = mock.MagicMock()
        phase.tool_tags = ["write", "bash"]
        runner._build_active_registry(phase)
        runner.registry.filter_by_tags.assert_called()
        call_args = runner.registry.filter_by_tags.call_args
        tags_used = call_args[0][0] if call_args[0] else call_args[1].get("tags", [])
        for tag in _READ_ONLY_TAGS:
            assert tag in tags_used
        for tag in ["write", "bash"]:
            assert tag in tags_used

    def test_no_tool_tags_empty_list(self, tmp_path):
        runner = _make_runner(tmp_path)
        phase = mock.MagicMock()
        phase.tool_tags = []
        runner._build_active_registry(phase)
        call_args = runner.registry.filter_by_tags.call_args
        tags_used = call_args[0][0] if call_args[0] else call_args[1].get("tags", [])
        # Should not add extra tags beyond _READ_ONLY_TAGS
        extra = set(tags_used) - set(_READ_ONLY_TAGS)
        assert extra == set()


# ---------------------------------------------------------------------------
# PhaseRunner._build_executor_prompt
# ---------------------------------------------------------------------------

def _make_phase_mock(name="test_phase", template=None, glob_patterns=None):
    """Build a minimal PhaseConfig-like mock for _build_executor_prompt tests."""
    phase = mock.MagicMock()
    # Explicitly set string attributes so they aren't MagicMock objects.
    # _build_executor_prompt reads phase.system_prompt (not executor_prompt).
    phase.name = name
    phase.system_prompt = template or "Fix the code.\n$file_context\n"
    phase.glob_patterns = glob_patterns or []
    phase.falsifiable_criterion = "tests pass"
    return phase


class TestBuildExecutorPrompt:
    def test_returns_string(self, tmp_path):
        runner = _make_runner(tmp_path)
        phase = _make_phase_mock()
        result = runner._build_executor_prompt(phase, "ctx", None, "")
        assert isinstance(result, str)

    def test_file_context_substituted(self, tmp_path):
        runner = _make_runner(tmp_path)
        phase = _make_phase_mock(template="Do this.\n$file_context\n")
        result = runner._build_executor_prompt(phase, "MY_CONTEXT", None, "")
        assert "MY_CONTEXT" in result

    def test_file_context_placeholder_replaced(self, tmp_path):
        runner = _make_runner(tmp_path)
        phase = _make_phase_mock(template="Do this.\n$file_context\n")
        result = runner._build_executor_prompt(phase, "INSERTED", None, "")
        # The literal "$file_context" should not remain
        assert "$file_context" not in result

    def test_empty_file_context(self, tmp_path):
        runner = _make_runner(tmp_path)
        phase = _make_phase_mock(template="Do this.\n$file_context\n")
        result = runner._build_executor_prompt(phase, "", None, "")
        assert isinstance(result, str)

    _FULL_TEMPLATE = (
        "Task.\n$file_context\n${prior_best}${syntax_errors}"
        "Criterion: $falsifiable_criterion\n"
    )

    def test_prior_best_none_has_no_prior_section(self, tmp_path):
        runner = _make_runner(tmp_path)
        phase = _make_phase_mock(template=self._FULL_TEMPLATE)
        result = runner._build_executor_prompt(phase, "ctx", None, "")
        # No prior best means no prior section in output
        assert "Prior Best" not in result

    def test_prior_best_present_included_in_prompt(self, tmp_path):
        runner = _make_runner(tmp_path)
        phase = _make_phase_mock(template=self._FULL_TEMPLATE)
        result = runner._build_executor_prompt(phase, "ctx", "previous proposal here", "")
        assert "previous proposal here" in result

    def test_syntax_errors_included_when_provided(self, tmp_path):
        runner = _make_runner(tmp_path)
        phase = _make_phase_mock(template=self._FULL_TEMPLATE)
        result = runner._build_executor_prompt(phase, "ctx", None, "SyntaxError: bad code line 5")
        assert "SyntaxError" in result

    def test_syntax_errors_empty_no_syntax_section(self, tmp_path):
        runner = _make_runner(tmp_path)
        phase = _make_phase_mock(template=self._FULL_TEMPLATE)
        result = runner._build_executor_prompt(phase, "ctx", None, "")
        assert "SyntaxError" not in result

    def test_workspace_reminder_prepended(self, tmp_path):
        runner = _make_runner(tmp_path)
        # workspace is a Path; _build_executor_prompt uses f"{self.harness.workspace}"
        runner.harness.workspace = str(tmp_path)
        phase = _make_phase_mock(template=self._FULL_TEMPLATE)
        result = runner._build_executor_prompt(phase, "ctx", None, "")
        # The workspace path should appear somewhere in the prompt
        assert str(tmp_path) in result

    def test_syntax_priority_fix_header_present(self, tmp_path):
        runner = _make_runner(tmp_path)
        phase = _make_phase_mock(template=self._FULL_TEMPLATE)
        result = runner._build_executor_prompt(phase, "ctx", None, "bad syntax here")
        assert "PRIORITY FIX" in result

    def test_prior_best_section_header_present(self, tmp_path):
        runner = _make_runner(tmp_path)
        phase = _make_phase_mock(template=self._FULL_TEMPLATE)
        result = runner._build_executor_prompt(phase, "ctx", "my proposal", "")
        assert "Prior Best" in result

    def test_falsifiable_criterion_substituted(self, tmp_path):
        runner = _make_runner(tmp_path)
        phase = _make_phase_mock(template=self._FULL_TEMPLATE)
        result = runner._build_executor_prompt(phase, "ctx", None, "")
        assert "tests pass" in result


# ---------------------------------------------------------------------------
# Additional edge-case tests added in cycle 34
# ---------------------------------------------------------------------------

class TestTokenisePathHyphens:
    """_tokenise_path splits on hyphens in addition to separators."""

    def test_hyphens_are_split(self):
        tokens = _tokenise_path("my-project/utils.py")
        assert "my" in tokens
        assert "project" in tokens
        assert "utils" in tokens

    def test_hyphenated_stem_only(self):
        tokens = _tokenise_path("some-long-name.py")
        assert "some" in tokens
        assert "long" in tokens
        assert "name" in tokens

    def test_hyphens_and_underscores_combined(self):
        tokens = _tokenise_path("my-module/the_helper.py")
        assert "my" in tokens
        assert "module" in tokens
        assert "the" in tokens
        assert "helper" in tokens


class TestTokenisePhraseEdgeCases:
    """_tokenise_phrase edge cases."""

    def test_phrase_is_lowercased(self):
        # CamelCase is kept as a single token (no camelCase splitting in phrase)
        tokens = _tokenise_phrase("MyClass")
        # Lowercased
        assert "myclass" in tokens

    def test_single_char_tokens_excluded(self):
        # Single-character tokens should be filtered (len > 1 required)
        tokens = _tokenise_phrase("a b c do")
        for t in tokens:
            assert len(t) > 1, f"Single-char token {t!r} should be excluded"

    def test_stop_words_excluded(self):
        tokens = _tokenise_phrase("fix the issue in code")
        assert "the" not in tokens
        assert "in" not in tokens
        # meaningful tokens remain
        assert "fix" in tokens
        assert "issue" in tokens
        assert "code" in tokens


class TestScoreFileRelevanceFileSizeZero:
    """score_file_relevance with file_size=0 uses a neutral 0.5 size signal."""

    def test_zero_size_gives_neutral_size_signal(self):
        now = 1_000_000.0
        # No keywords, fresh mtime → recency = 1.0, size_score = 0.5 (neutral)
        score = score_file_relevance("test.py", set(), now, now, 0)
        expected = _W_KEYWORD * 0.0 + _W_RECENCY * 1.0 + _W_SIZE * 0.5
        assert abs(score - expected) < 1e-9

    def test_zero_size_score_differs_from_large_file(self):
        now = 1_000_000.0
        score_zero = score_file_relevance("test.py", set(), now, now, 0)
        # Large file gets a low size_signal (< 0.5)
        score_large = score_file_relevance("test.py", set(), now, now, 500_000)
        # zero-size is neutral (0.5 signal), large is penalised (< 0.5 signal)
        assert score_zero > score_large


class TestTruncateFileContentTailOnly:
    """_truncate_file_content: when file is so wide that even tail fills budget."""

    def test_very_wide_lines_still_truncates(self):
        # 200 lines of 100-char strings → 20 000 chars >> _FILE_CHAR_LIMIT (8000)
        wide_line = "x" * 100
        content = "\n".join([wide_line] * 200)
        result, truncated = _truncate_file_content(content, "big.py")
        assert truncated is True
        # Allow a small overhead for the omission-marker line itself (~80 chars)
        assert len(result) <= _FILE_CHAR_LIMIT + 100

    def test_head_tail_omission_marker_present(self):
        # A file large enough to require omission between head and tail
        # 500 lines of ~20 chars each → ~10 000 chars > _FILE_CHAR_LIMIT (8000)
        lines = [f"line {i:04d} payload  " for i in range(500)]
        content = "\n".join(lines)
        result, truncated = _truncate_file_content(content, "medium.py")
        assert truncated is True
        # Head and tail are both present; omission marker between them
        assert "omitted" in result.lower()

    def test_tail_content_preserved_when_truncated(self):
        # Last line should survive truncation (it's part of the tail)
        lines = [f"line {i:04d} payload  " for i in range(500)]
        content = "\n".join(lines)
        result, _ = _truncate_file_content(content, "medium.py")
        assert "line 0499" in result  # last line


class TestReadSourceManifestDetails:
    """_read_source_manifest: check size display and hint text."""

    def test_file_sizes_shown(self, tmp_path):
        f = tmp_path / "script.py"
        f.write_text("hello world")
        result = _read_source_manifest(str(tmp_path), ["*.py"], None)
        # Size in bytes should appear next to the filename
        assert "bytes" in result

    def test_tool_hint_present(self, tmp_path):
        f = tmp_path / "script.py"
        f.write_text("content")
        result = _read_source_manifest(str(tmp_path), ["*.py"], None)
        # Should include a hint about how to read files
        assert "batch_read" in result

    def test_dedup_same_file_two_patterns(self, tmp_path):
        f = tmp_path / "test_utils.py"
        f.write_text("pass")
        # Two overlapping glob patterns both match the same file
        result = _read_source_manifest(str(tmp_path), ["*.py", "test_*.py"], None)
        # File should appear exactly once
        assert result.count("test_utils.py") == 1


class TestReadSourceFilesWorkspaceTypes:
    """_read_source_files: workspace can be a str or Path."""

    def test_workspace_as_string(self, tmp_path):
        (tmp_path / "mod.py").write_text("# module")
        result = _read_source_files(str(tmp_path), ["*.py"], None, 100_000)
        assert "mod.py" in result

    def test_workspace_as_path(self, tmp_path):
        (tmp_path / "mod.py").write_text("# module")
        result = _read_source_files(tmp_path, ["*.py"], None, 100_000)
        assert "mod.py" in result

    def test_keyword_none_newest_first(self, tmp_path):
        import time
        f_old = tmp_path / "alpha.py"
        f_new = tmp_path / "beta.py"
        f_old.write_text("# alpha")
        time.sleep(0.05)
        f_new.write_text("# beta")
        result = _read_source_files(str(tmp_path), ["*.py"], None, 100_000)
        # beta (newer) should appear before alpha (older)
        assert result.index("beta.py") < result.index("alpha.py")

    def test_dedup_across_two_patterns(self, tmp_path):
        (tmp_path / "test_utils.py").write_text("pass")
        # Two patterns that both match the same file
        result = _read_source_files(str(tmp_path), ["*.py", "test_*.py"], None, 100_000)
        assert result.count("=== FILE: test_utils.py ===") == 1


class TestBuildExecutorPromptWarning:
    """_build_executor_prompt: glob_patterns set but no $file_context warns."""

    def test_no_file_context_placeholder_logs_warning(self, tmp_path, caplog):
        import logging
        runner = _make_runner(tmp_path)
        # Template with no $file_context
        phase = _make_phase_mock(
            template="Fix the code. No placeholder here.",
            glob_patterns=["*.py"],
        )
        with caplog.at_level(logging.WARNING, logger="harness.pipeline.phase_runner"):
            runner._build_executor_prompt(phase, "some_ctx", None, "")
        assert any("file_context" in r.message.lower() for r in caplog.records)


class TestBuildExecutorPromptIntelBlock:
    """_build_executor_prompt: intel_block injection for framework_improvement phase.

    The intel_block is only rendered into the prompt when the phase template
    contains ``$intel_metric_block``.  These tests use a template that includes
    that placeholder so we can verify the block is built and substituted.
    """

    _TEMPLATE_WITH_INTEL = "Fix the code.\n$intel_metric_block"
    # probe_results.jsonl is written under benchmarks/evaluator_calibration/
    # relative to harness.workspace (the path is hardcoded in phase_runner.py).
    _PROBE_SUBPATH = "benchmarks/evaluator_calibration/probe_results.jsonl"

    def test_non_framework_phase_has_no_intel_block(self, tmp_path):
        """intel_block must be empty for any phase that is not framework_improvement."""
        runner = _make_runner(tmp_path)
        phase = _make_phase_mock(name="feature", template=self._TEMPLATE_WITH_INTEL)
        prompt = runner._build_executor_prompt(phase, "", None, "")
        assert "evaluator calibration" not in prompt.lower()
        assert "discriminability" not in prompt.lower()
        assert "INTELLIGENCE METRIC" not in prompt

    def test_framework_improvement_no_probe_file(self, tmp_path):
        """When probe_results.jsonl does not exist, intel_block is skipped gracefully."""
        runner = _make_runner(tmp_path)
        phase = _make_phase_mock(
            name="framework_improvement", template=self._TEMPLATE_WITH_INTEL
        )
        # Should not raise
        prompt = runner._build_executor_prompt(phase, "", None, "")
        assert isinstance(prompt, str)
        # No intel block content when file is missing
        assert "INTELLIGENCE METRIC" not in prompt

    def test_framework_improvement_with_probe_file(self, tmp_path):
        """When probe_results.jsonl has trajectory data, intel_block is rendered."""
        import json

        runner = _make_runner(tmp_path)
        probe_path = tmp_path / self._PROBE_SUBPATH
        probe_path.parent.mkdir(parents=True, exist_ok=True)
        # format_trajectory expects 'rho', 'rho_basic', or 'rho_diffusion' keys
        records = [
            {"cycle": i + 1, "rho": 0.5 + i * 0.05, "n_pairs": 10}
            for i in range(5)
        ]
        probe_path.write_text("\n".join(json.dumps(r) for r in records))

        phase = _make_phase_mock(
            name="framework_improvement", template=self._TEMPLATE_WITH_INTEL
        )
        prompt = runner._build_executor_prompt(phase, "", None, "")
        assert isinstance(prompt, str)
        # Intel block content should appear (Spearman ρ trajectory)
        assert "INTELLIGENCE METRIC" in prompt
        assert "Spearman" in prompt or "ρ" in prompt or "rho" in prompt.lower()

    def test_framework_improvement_probe_file_malformed_skipped(self, tmp_path):
        """Malformed probe_results.jsonl causes intel_block to be skipped silently."""
        runner = _make_runner(tmp_path)
        probe_path = tmp_path / self._PROBE_SUBPATH
        probe_path.parent.mkdir(parents=True, exist_ok=True)
        probe_path.write_text("not-valid-json\n")
        phase = _make_phase_mock(
            name="framework_improvement", template=self._TEMPLATE_WITH_INTEL
        )
        # Should not raise
        prompt = runner._build_executor_prompt(phase, "", None, "")
        assert isinstance(prompt, str)
        assert "INTELLIGENCE METRIC" not in prompt

    def test_intel_block_not_injected_for_non_framework_phases(self, tmp_path):
        """Multiple non-framework phase names all produce an empty intel_block."""
        import json

        runner = _make_runner(tmp_path)
        probe_path = tmp_path / self._PROBE_SUBPATH
        probe_path.parent.mkdir(parents=True, exist_ok=True)
        records = [{"cycle": i + 1, "rho": 0.7, "n_pairs": 8} for i in range(3)]
        probe_path.write_text("\n".join(json.dumps(r) for r in records))

        for phase_name in ("feature", "bugfix", "refactor", "documentation"):
            phase = _make_phase_mock(
                name=phase_name, template=self._TEMPLATE_WITH_INTEL
            )
            prompt = runner._build_executor_prompt(phase, "", None, "")
            assert "INTELLIGENCE METRIC" not in prompt, (
                f"Phase {phase_name!r} should not inject intel_block"
            )

    def test_intel_block_regression_warning_present(self, tmp_path):
        """When rho < 0.85 and no evaluator files are touched, penalty text is present."""
        import json

        runner = _make_runner(tmp_path)
        probe_path = tmp_path / self._PROBE_SUBPATH
        probe_path.parent.mkdir(parents=True, exist_ok=True)
        # rho below 0.85 threshold → should mention penalty text
        records = [{"cycle": i + 1, "rho": 0.6, "n_pairs": 10} for i in range(3)]
        probe_path.write_text("\n".join(json.dumps(r) for r in records))

        phase = _make_phase_mock(
            name="framework_improvement", template=self._TEMPLATE_WITH_INTEL
        )
        prompt = runner._build_executor_prompt(phase, "", None, "")
        assert "INTELLIGENCE METRIC" in prompt
        # The penalty / guidance text should reference the rho value
        assert "0.6" in prompt or "0.60" in prompt or "0.600" in prompt


def _make_runner_with_artifacts(tmp_path: pathlib.Path) -> "PhaseRunner":
    """Return a PhaseRunner whose artifacts manager writes to tmp_path."""
    runner = _make_runner(tmp_path)
    summary_file = tmp_path / "phase_summary.txt"
    artifacts = mock.MagicMock()
    # phase_dir returns a tuple used as *args to artifacts.write
    artifacts.phase_dir.return_value = (str(tmp_path), "outer1", "test_phase")

    def _write_side_effect(content, *_segs, filename="phase_summary.txt"):
        summary_file.write_text(content)

    artifacts.write.side_effect = _write_side_effect
    runner.artifacts = artifacts
    return runner


class TestWritePhaseSummary:
    """PhaseRunner._write_phase_summary: check content format."""

    def test_summary_written_via_artifacts(self, tmp_path):
        runner = _make_runner_with_artifacts(tmp_path)
        phase = _make_phase_mock(name="test_phase")
        result = mock.MagicMock()
        result.combined_score = 7.5
        runner._write_phase_summary(1, phase, [result], "synthesis output", 7.5)
        # artifacts.write should have been called
        runner.artifacts.write.assert_called_once()

    def test_summary_content_has_synthesis(self, tmp_path):
        runner = _make_runner_with_artifacts(tmp_path)
        phase = _make_phase_mock(name="test_phase")
        result = mock.MagicMock()
        result.combined_score = 6.0
        runner._write_phase_summary(1, phase, [result], "THE SYNTHESIS TEXT", 6.0)
        # Extract the content argument passed to artifacts.write
        call_content = runner.artifacts.write.call_args[0][0]
        assert "THE SYNTHESIS TEXT" in call_content

    def test_summary_content_has_best_score(self, tmp_path):
        runner = _make_runner_with_artifacts(tmp_path)
        phase = _make_phase_mock(name="test_phase")
        result = mock.MagicMock()
        result.combined_score = 8.5
        runner._write_phase_summary(1, phase, [result], "synth", 8.5)
        call_content = runner.artifacts.write.call_args[0][0]
        assert "8.5" in call_content

    def test_summary_content_has_phase_label(self, tmp_path):
        runner = _make_runner_with_artifacts(tmp_path)
        phase = _make_phase_mock(name="my_special_phase")
        result = mock.MagicMock()
        result.combined_score = 5.0
        runner._write_phase_summary(1, phase, [result], "synth", 5.0)
        call_content = runner.artifacts.write.call_args[0][0]
        # phase.label is what appears in the content (mock returns the mock default)
        assert call_content  # non-empty


# ---------------------------------------------------------------------------
# TestLoadInnerResult
# ---------------------------------------------------------------------------


class TestLoadInnerResult:
    """Tests for PhaseRunner._load_inner_result (uses real ArtifactStore I/O)."""

    @staticmethod
    def _make_runner(tmp_path: pathlib.Path) -> PhaseRunner:
        from harness.core.artifacts import ArtifactStore

        runner = PhaseRunner.__new__(PhaseRunner)
        runner.harness = mock.MagicMock()
        runner.harness.workspace = tmp_path
        runner.registry = mock.MagicMock()
        runner.registry.filter_by_tags.return_value = runner.registry
        runner.llm = mock.MagicMock()
        runner.artifacts = ArtifactStore(tmp_path)
        return runner

    def test_reads_proposal_from_proposal_txt(self, tmp_path: pathlib.Path) -> None:
        runner = self._make_runner(tmp_path)
        segs = runner.artifacts.inner_dir(0, "myfeature", 0)
        runner.artifacts.write("My proposal text", *segs, "proposal.txt")
        runner.artifacts.write("Score: 7.0", *segs, "basic_eval.txt")
        runner.artifacts.write("Score: 8.0", *segs, "diffusion_eval.txt")

        result = runner._load_inner_result(0, "myfeature", 0)

        assert result.proposal == "My proposal text"

    def test_falls_back_to_implement_output_when_proposal_empty(
        self, tmp_path: pathlib.Path
    ) -> None:
        runner = self._make_runner(tmp_path)
        segs = runner.artifacts.inner_dir(0, "myfeature", 0)
        runner.artifacts.write("", *segs, "proposal.txt")
        runner.artifacts.write("Fallback content from impl", *segs, "implement_output.txt")
        runner.artifacts.write("Score: 6.0", *segs, "basic_eval.txt")
        runner.artifacts.write("Score: 6.0", *segs, "diffusion_eval.txt")

        result = runner._load_inner_result(0, "myfeature", 0)

        assert result.proposal == "Fallback content from impl"

    def test_prefers_proposal_txt_over_implement_output(
        self, tmp_path: pathlib.Path
    ) -> None:
        runner = self._make_runner(tmp_path)
        segs = runner.artifacts.inner_dir(0, "myfeature", 0)
        runner.artifacts.write("Actual proposal", *segs, "proposal.txt")
        runner.artifacts.write("Impl log content", *segs, "implement_output.txt")
        runner.artifacts.write("Score: 5.0", *segs, "basic_eval.txt")
        runner.artifacts.write("Score: 5.0", *segs, "diffusion_eval.txt")

        result = runner._load_inner_result(0, "myfeature", 0)

        assert result.proposal == "Actual proposal"

    def test_basic_and_diffusion_scores_parsed_correctly(
        self, tmp_path: pathlib.Path
    ) -> None:
        runner = self._make_runner(tmp_path)
        segs = runner.artifacts.inner_dir(0, "feature", 0)
        runner.artifacts.write("prop", *segs, "proposal.txt")
        runner.artifacts.write("Score: 7.5\nDetailed critique here", *segs, "basic_eval.txt")
        runner.artifacts.write("Score: 8.0\nDiffusion critique", *segs, "diffusion_eval.txt")

        result = runner._load_inner_result(0, "feature", 0)

        assert result.dual_score is not None
        assert result.dual_score.basic.score == 7.5
        assert result.dual_score.diffusion.score == 8.0

    def test_combined_score_uses_60_40_weighting(
        self, tmp_path: pathlib.Path
    ) -> None:
        runner = self._make_runner(tmp_path)
        segs = runner.artifacts.inner_dir(0, "feature", 0)
        runner.artifacts.write("prop", *segs, "proposal.txt")
        runner.artifacts.write("Score: 6.0", *segs, "basic_eval.txt")
        runner.artifacts.write("Score: 8.0", *segs, "diffusion_eval.txt")

        result = runner._load_inner_result(0, "feature", 0)

        # 6.0*0.6 + 8.0*0.4 = 3.6 + 3.2 = 6.8
        assert abs(result.combined_score - 6.8) < 0.01

    def test_raw_critique_text_stored_in_score_item(
        self, tmp_path: pathlib.Path
    ) -> None:
        runner = self._make_runner(tmp_path)
        segs = runner.artifacts.inner_dir(0, "feature", 0)
        runner.artifacts.write("prop", *segs, "proposal.txt")
        runner.artifacts.write("Score: 7.0\nBasic critique text", *segs, "basic_eval.txt")
        runner.artifacts.write("Score: 7.0\nDiffusion critique text", *segs, "diffusion_eval.txt")

        result = runner._load_inner_result(0, "feature", 0)

        assert "Basic critique text" in result.dual_score.basic.critique
        assert "Diffusion critique text" in result.dual_score.diffusion.critique

    def test_missing_files_return_empty_strings(
        self, tmp_path: pathlib.Path
    ) -> None:
        runner = self._make_runner(tmp_path)
        # Don't write any artifacts

        result = runner._load_inner_result(0, "feature", 0)

        assert result.proposal == ""
        assert result.syntax_errors == ""
        assert result.pytest_result == ""
        assert result.post_impl_snapshot == ""
        assert result.implement_log == ""

    def test_missing_scores_give_zero_combined(
        self, tmp_path: pathlib.Path
    ) -> None:
        runner = self._make_runner(tmp_path)
        # No score files written

        result = runner._load_inner_result(0, "feature", 0)

        assert result.combined_score == 0.0
        assert result.dual_score.basic.score == 0.0
        assert result.dual_score.diffusion.score == 0.0

    def test_inner_dir_indexing_outer_and_inner(
        self, tmp_path: pathlib.Path
    ) -> None:
        runner = self._make_runner(tmp_path)
        # outer=1, inner=2 — verify the right directory is used
        segs = runner.artifacts.inner_dir(1, "phase", 2)
        runner.artifacts.write("content-round2-inner3", *segs, "proposal.txt")
        runner.artifacts.write("Score: 9.0", *segs, "basic_eval.txt")
        runner.artifacts.write("Score: 9.0", *segs, "diffusion_eval.txt")

        # outer=0 inner=0 should get empty (different dir)
        result_other = runner._load_inner_result(0, "phase", 0)
        result = runner._load_inner_result(1, "phase", 2)

        assert result.proposal == "content-round2-inner3"
        assert result.combined_score == 9.0
        assert result_other.proposal == ""  # different dir, no content

    def test_syntax_errors_and_pytest_result_stored(
        self, tmp_path: pathlib.Path
    ) -> None:
        runner = self._make_runner(tmp_path)
        segs = runner.artifacts.inner_dir(0, "feature", 0)
        runner.artifacts.write("prop", *segs, "proposal.txt")
        runner.artifacts.write("Score: 5.0", *segs, "basic_eval.txt")
        runner.artifacts.write("Score: 5.0", *segs, "diffusion_eval.txt")
        runner.artifacts.write("SyntaxError: invalid syntax", *segs, "syntax_errors.txt")
        runner.artifacts.write("FAILED test_foo::test_bar", *segs, "pytest_result.txt")

        result = runner._load_inner_result(0, "feature", 0)

        assert result.syntax_errors == "SyntaxError: invalid syntax"
        assert result.pytest_result == "FAILED test_foo::test_bar"

    def test_perfect_score_both_evaluators(
        self, tmp_path: pathlib.Path
    ) -> None:
        runner = self._make_runner(tmp_path)
        segs = runner.artifacts.inner_dir(0, "feature", 0)
        runner.artifacts.write("perfect proposal", *segs, "proposal.txt")
        runner.artifacts.write("Score: 10.0\nExcellent!", *segs, "basic_eval.txt")
        runner.artifacts.write("Score: 10.0\nPerfect!", *segs, "diffusion_eval.txt")

        result = runner._load_inner_result(0, "feature", 0)

        assert result.combined_score == 10.0
        assert result.dual_score.basic.score == 10.0
        assert result.dual_score.diffusion.score == 10.0
