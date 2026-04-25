"""Unit tests for harness.tools.find_replace.

Covers:
  - _preview_matches helper (pure function, no I/O)
  - FindReplaceTool: basic usage, dry_run, literal mode, case-insensitive,
    multiline, count=1, max_files_changed cap, no-match path, file_glob,
    error output format.
"""

from __future__ import annotations

import asyncio
import re
from unittest.mock import Mock

from harness.tools.find_replace import FindReplaceTool, _preview_matches


# ---------------------------------------------------------------------------
# _preview_matches
# ---------------------------------------------------------------------------

class TestPreviewMatches:
    def test_basic_match_format(self):
        pattern = re.compile(r"foo")
        text = "line one foo\nline two bar\nline three foo again"
        previews = _preview_matches(text, pattern, max_lines=10)
        assert len(previews) == 2
        assert "L1: line one foo" in previews[0]
        assert "L3: line three foo again" in previews[1]

    def test_no_match_returns_empty(self):
        pattern = re.compile(r"xyz_not_present")
        text = "hello world\nfoo bar"
        assert _preview_matches(text, pattern, max_lines=10) == []

    def test_max_lines_cap(self):
        pattern = re.compile(r"x")
        text = "\n".join(f"x{i}" for i in range(20))
        previews = _preview_matches(text, pattern, max_lines=5)
        assert len(previews) == 5

    def test_long_line_truncated(self):
        pattern = re.compile(r"start")
        long_line = "start " + "a" * 200
        previews = _preview_matches(long_line, pattern, max_lines=10)
        assert len(previews) == 1
        # truncated at ~120 chars
        assert len(previews[0]) < 180
        assert "\u2026" in previews[0]  # ellipsis appended

    def test_line_numbers_one_based(self):
        pattern = re.compile(r"match")
        text = "skip\nskip\nmatch here"
        previews = _preview_matches(text, pattern, max_lines=10)
        assert len(previews) == 1
        assert "L3:" in previews[0]


# ---------------------------------------------------------------------------
# FindReplaceTool helpers
# ---------------------------------------------------------------------------

def _make_tool() -> FindReplaceTool:
    return FindReplaceTool()


def _run(tool, tmp_path, **kwargs) -> str:
    """Run the tool synchronously using execute() with a minimal mock config."""
    config = Mock()
    config.workspace = str(tmp_path)
    config.phase_scope = None
    config.phase_edit_globs = None      # disable phase-scope path restriction
    config.phases = []
    config.homoglyph_blocklist = {}     # prevent Mock iteration error in security.py
    config.allowed_paths = [tmp_path]   # make path validation succeed
    result = asyncio.run(tool.execute(config, **kwargs))
    return result.output


# ---------------------------------------------------------------------------
# FindReplaceTool: core behaviour
# ---------------------------------------------------------------------------

class TestFindReplaceTool:
    def test_name(self):
        assert _make_tool().name == "find_replace"

    def test_schema_required_fields(self):
        schema = _make_tool().input_schema()
        required = set(schema.get("required", []))
        assert "pattern" in required
        assert "replacement" in required

    # -- actual replacement -------------------------------------------------

    def test_basic_regex_replace(self, tmp_path):
        p = tmp_path / "file.py"
        p.write_text("x = old_value\ny = old_value\n")
        out = _run(
            _make_tool(), tmp_path,
            pattern="old_value", replacement="new_value",
            file_glob="**/*.py", path="",
        )
        assert "1 file(s) changed" in out
        assert p.read_text() == "x = new_value\ny = new_value\n"

    def test_literal_mode(self, tmp_path):
        # Literal: the pattern string is not treated as a regex.
        # 'x.y' literally means 'x.y' not 'x<any>y'.
        p = tmp_path / "file.py"
        p.write_text("x = x.y and xzy\n")
        out = _run(
            _make_tool(), tmp_path,
            pattern="x.y", replacement="REPLACED",
            literal=True, file_glob="**/*.py", path="",
        )
        assert "1 file(s) changed" in out
        text = p.read_text()
        assert "REPLACED" in text
        assert "xzy" in text  # 'xzy' must NOT be replaced

    def test_case_insensitive_flag(self, tmp_path):
        p = tmp_path / "f.py"
        p.write_text("Foo foo FOO\n")
        _run(
            _make_tool(), tmp_path,
            pattern="foo", replacement="BAR",
            case_insensitive=True, file_glob="**/*.py", path="",
        )
        assert p.read_text() == "BAR BAR BAR\n"

    def test_count_one_replaces_first_occurrence_only(self, tmp_path):
        p = tmp_path / "f.py"
        p.write_text("a = old\nb = old\nc = old\n")
        _run(
            _make_tool(), tmp_path,
            pattern="old", replacement="new",
            count=1, file_glob="**/*.py", path="",
        )
        text = p.read_text()
        assert text.count("new") == 1
        assert text.count("old") == 2

    def test_multiline_caret_anchor(self, tmp_path):
        p = tmp_path / "f.py"
        p.write_text("start_here\nno_match\nstart_second\n")
        _run(
            _make_tool(), tmp_path,
            pattern="^start", replacement="BEGIN",
            multiline=True, file_glob="**/*.py", path="",
        )
        text = p.read_text()
        assert text.count("BEGIN") == 2

    # -- dry_run ------------------------------------------------------------

    def test_dry_run_does_not_write(self, tmp_path):
        p = tmp_path / "f.py"
        original = "x = old\n"
        p.write_text(original)
        out = _run(
            _make_tool(), tmp_path,
            pattern="old", replacement="new",
            dry_run=True, file_glob="**/*.py", path="",
        )
        assert "DRY RUN" in out
        assert p.read_text() == original  # file unchanged

    def test_dry_run_shows_preview_lines(self, tmp_path):
        p = tmp_path / "f.py"
        p.write_text("x = old_val  # important\n")
        out = _run(
            _make_tool(), tmp_path,
            pattern="old_val", replacement="new_val",
            dry_run=True, file_glob="**/*.py", path="",
        )
        # dry-run should include line preview
        assert "L1:" in out

    # -- no match -----------------------------------------------------------

    def test_no_match_reports_zero_files(self, tmp_path):
        (tmp_path / "f.py").write_text("nothing interesting here\n")
        out = _run(
            _make_tool(), tmp_path,
            pattern="__completely_absent_token__",
            replacement="x",
            file_glob="**/*.py", path="",
        )
        assert "No matches found" in out

    # -- max_files_changed cap ----------------------------------------------

    def test_max_files_changed_cap_respected(self, tmp_path):
        # Create 5 files each containing the pattern.
        for i in range(5):
            (tmp_path / f"f{i}.py").write_text(f"x = old_{i}\n")
        out = _run(
            _make_tool(), tmp_path,
            pattern="old_", replacement="new_",
            max_files_changed=2,
            file_glob="**/*.py", path="",
        )
        # Warning about cap in output
        assert "cap" in out.lower() or "max_files_changed" in out
        # Only 2 files should have been written
        changed = sum(
            1 for i in range(5)
            if "new_" in (tmp_path / f"f{i}.py").read_text()
        )
        assert changed == 2

    # -- file_glob ----------------------------------------------------------

    def test_file_glob_limits_search(self, tmp_path):
        py_file = tmp_path / "source.py"
        txt_file = tmp_path / "notes.txt"
        py_file.write_text("x = OLD\n")
        txt_file.write_text("x = OLD\n")
        _run(
            _make_tool(), tmp_path,
            pattern="OLD", replacement="NEW",
            file_glob="**/*.py", path="",
        )
        assert "NEW" in py_file.read_text()
        assert "OLD" in txt_file.read_text()  # .txt must be untouched

    # -- path sub-directory scope -------------------------------------------

    def test_path_scope_limits_to_subdir(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        inside = sub / "inside.py"
        outside = tmp_path / "outside.py"
        inside.write_text("x = OLD\n")
        outside.write_text("x = OLD\n")
        _run(
            _make_tool(), tmp_path,
            pattern="OLD", replacement="NEW",
            file_glob="**/*.py", path="sub",
        )
        assert "NEW" in inside.read_text()
        assert "OLD" in outside.read_text()  # outside sub/ must be untouched

    # -- output format ------------------------------------------------------

    def test_output_contains_pattern_and_replacement(self, tmp_path):
        p = tmp_path / "f.py"
        p.write_text("x = old_name\n")
        out = _run(
            _make_tool(), tmp_path,
            pattern="old_name", replacement="new_name",
            file_glob="**/*.py", path="",
        )
        assert "old_name" in out
        assert "new_name" in out

    def test_output_shows_substitution_count(self, tmp_path):
        p = tmp_path / "f.py"
        p.write_text("a\nb\na\n")  # 2 occurrences of 'a'
        out = _run(
            _make_tool(), tmp_path,
            pattern="^a$", replacement="Z",
            multiline=True, file_glob="**/*.py", path="",
        )
        assert "2 substitution" in out
