"""Tests for the todo_scan tool.

Covers: tag detection, filtering by tag, sort_by modes,
include_context, max_results clamping, empty files, invalid inputs.
"""
import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock


from harness.tools.todo_scan import TodoScanTool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_config(workspace: str):
    cfg = MagicMock()
    cfg.workspace = workspace
    cfg.allowed_paths = [workspace]
    return cfg


def run(coro):
    return asyncio.run(coro)


tool = TodoScanTool()


# ---------------------------------------------------------------------------
# Fixture source files
# ---------------------------------------------------------------------------

SAMPLE_SRC = """# Normal comment
def foo():
    # TODO: implement this properly
    pass

def bar():
    # FIXME: this is broken
    # HACK: temporary workaround
    x = 1

def baz():
    # NOTE: this is intentional
    # BUG: known crash on empty input
    pass
"""

CLEAN_SRC = """def clean():
    # just a regular comment, nothing to flag
    return 42
"""

XXX_SRC = """# XXX: remove this before release
CONSTANT = 99
"""


# ---------------------------------------------------------------------------
# 1. Basic detection
# ---------------------------------------------------------------------------

class TestBasicDetection:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cfg = make_config(workspace=self.tmpdir)

    def _write(self, name: str, src: str) -> Path:
        p = Path(self.tmpdir) / name
        p.write_text(src)
        return p

    def test_finds_all_tags_by_default(self):
        self._write("sample.py", SAMPLE_SRC)
        result = run(tool.execute(self.cfg, max_results=100))
        assert not result.is_error
        data = json.loads(result.output)
        assert data["total_found"] >= 5  # TODO, FIXME, HACK, NOTE, BUG

    def test_finds_todo_tag(self):
        self._write("a.py", SAMPLE_SRC)
        result = run(tool.execute(self.cfg, max_results=100, tags=["TODO"]))
        assert not result.is_error
        data = json.loads(result.output)
        assert data["by_tag"].get("TODO", 0) >= 1

    def test_finds_fixme_tag(self):
        self._write("a.py", SAMPLE_SRC)
        result = run(tool.execute(self.cfg, max_results=100, tags=["FIXME"]))
        assert not result.is_error
        data = json.loads(result.output)
        assert data["by_tag"].get("FIXME", 0) >= 1

    def test_finds_xxx_tag(self):
        self._write("xxx.py", XXX_SRC)
        result = run(tool.execute(self.cfg, max_results=100, tags=["XXX"]))
        assert not result.is_error
        data = json.loads(result.output)
        assert data["by_tag"].get("XXX", 0) >= 1

    def test_clean_file_returns_zero(self):
        self._write("clean.py", CLEAN_SRC)
        result = run(tool.execute(self.cfg, max_results=100))
        assert not result.is_error
        data = json.loads(result.output)
        assert data["total_found"] == 0

    def test_empty_file_no_crash(self):
        self._write("empty.py", "")
        result = run(tool.execute(self.cfg, max_results=100))
        assert not result.is_error
        data = json.loads(result.output)
        assert data["total_found"] == 0


# ---------------------------------------------------------------------------
# 2. Tag filtering
# ---------------------------------------------------------------------------

class TestTagFiltering:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cfg = make_config(workspace=self.tmpdir)
        (Path(self.tmpdir) / "s.py").write_text(SAMPLE_SRC)

    def test_single_tag_filter_excludes_others(self):
        result = run(tool.execute(self.cfg, max_results=100, tags=["TODO"]))
        data = json.loads(result.output)
        # Only TODO should appear in by_tag
        for tag in ["FIXME", "HACK", "NOTE", "BUG"]:
            assert data["by_tag"].get(tag, 0) == 0

    def test_multiple_tag_filter(self):
        result = run(tool.execute(self.cfg, max_results=100, tags=["TODO", "FIXME"]))
        data = json.loads(result.output)
        assert data["by_tag"].get("TODO", 0) >= 1
        assert data["by_tag"].get("FIXME", 0) >= 1
        assert data["by_tag"].get("HACK", 0) == 0

    def test_case_insensitive_custom_tag(self):
        """Tags passed in lowercase should still be found (normalised to upper)."""
        result = run(tool.execute(self.cfg, max_results=100, tags=["todo"]))
        data = json.loads(result.output)
        assert data["by_tag"].get("TODO", 0) >= 1

    def test_empty_tags_falls_back_to_defaults(self):
        """Empty tags list is treated as 'use all default tags' — not an error."""
        result = run(tool.execute(self.cfg, max_results=100, tags=[]))
        # Tool falls back to the full default tag set; should return results
        assert not result.is_error
        data = json.loads(result.output)
        assert data["total_found"] >= 1


# ---------------------------------------------------------------------------
# 3. sort_by modes
# ---------------------------------------------------------------------------

class TestSortBy:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cfg = make_config(workspace=self.tmpdir)
        (Path(self.tmpdir) / "s.py").write_text(SAMPLE_SRC)
        (Path(self.tmpdir) / "x.py").write_text(XXX_SRC)

    def test_sort_by_file(self):
        result = run(tool.execute(self.cfg, max_results=100, sort_by="file"))
        assert not result.is_error
        data = json.loads(result.output)
        files = [r["file"] for r in data["results"]]
        assert files == sorted(files)  # should be in file alphabetical order

    def test_sort_by_tag(self):
        result = run(tool.execute(self.cfg, max_results=100, sort_by="tag"))
        assert not result.is_error
        data = json.loads(result.output)
        tags = [r["tag"] for r in data["results"]]
        assert tags == sorted(tags)

    def test_sort_by_line(self):
        result = run(tool.execute(self.cfg, max_results=100, sort_by="line"))
        assert not result.is_error

    def test_invalid_sort_by_is_error(self):
        result = run(tool.execute(self.cfg, max_results=100, sort_by="invalid"))
        assert result.is_error


# ---------------------------------------------------------------------------
# 4. include_context
# ---------------------------------------------------------------------------

class TestIncludeContext:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cfg = make_config(workspace=self.tmpdir)
        (Path(self.tmpdir) / "s.py").write_text(SAMPLE_SRC)

    def test_context_fields_present_when_true(self):
        result = run(tool.execute(
            self.cfg, max_results=100, tags=["TODO"], include_context=True
        ))
        assert not result.is_error
        data = json.loads(result.output)
        assert data["total_found"] >= 1
        hit = data["results"][0]
        # When include_context=True, context_before and context_after should be present
        assert "context_before" in hit or "context_after" in hit

    def test_context_fields_absent_when_false(self):
        result = run(tool.execute(
            self.cfg, max_results=100, tags=["TODO"], include_context=False
        ))
        assert not result.is_error
        data = json.loads(result.output)
        assert data["total_found"] >= 1
        hit = data["results"][0]
        assert "context_before" not in hit
        assert "context_after" not in hit


# ---------------------------------------------------------------------------
# 5. max_results clamping
# ---------------------------------------------------------------------------

class TestMaxResults:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cfg = make_config(workspace=self.tmpdir)
        # Write a file with 10 TODO items
        lines = "\n".join(f"# TODO: item {i}" for i in range(10))
        (Path(self.tmpdir) / "many.py").write_text(lines)

    def test_max_results_limits_output(self):
        result = run(tool.execute(self.cfg, max_results=3))
        assert not result.is_error
        data = json.loads(result.output)
        assert len(data["results"]) <= 3

    def test_max_results_full_when_not_exceeded(self):
        result = run(tool.execute(self.cfg, max_results=100))
        assert not result.is_error
        data = json.loads(result.output)
        assert data["total_found"] == 10

    def test_truncated_flag_set_when_capped(self):
        result = run(tool.execute(self.cfg, max_results=3))
        data = json.loads(result.output)
        assert data["truncated"] is True

    def test_truncated_false_when_not_capped(self):
        result = run(tool.execute(self.cfg, max_results=100))
        data = json.loads(result.output)
        assert data["truncated"] is False


# ---------------------------------------------------------------------------
# 6. Output structure
# ---------------------------------------------------------------------------

class TestOutputStructure:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cfg = make_config(workspace=self.tmpdir)
        (Path(self.tmpdir) / "s.py").write_text(SAMPLE_SRC)

    def test_output_is_valid_json(self):
        result = run(tool.execute(self.cfg, max_results=100))
        assert not result.is_error
        data = json.loads(result.output)  # must not raise
        assert isinstance(data, dict)

    def test_output_has_required_fields(self):
        result = run(tool.execute(self.cfg, max_results=100))
        data = json.loads(result.output)
        for field in ["root", "files_scanned", "total_found", "by_tag", "results"]:
            assert field in data, f"Missing field: {field}"

    def test_each_result_has_required_fields(self):
        result = run(tool.execute(self.cfg, max_results=100))
        data = json.loads(result.output)
        for hit in data["results"]:
            assert "file" in hit
            assert "line" in hit
            assert "tag" in hit
            assert "text" in hit

    def test_result_line_number_is_int(self):
        result = run(tool.execute(self.cfg, max_results=100))
        data = json.loads(result.output)
        for hit in data["results"]:
            assert isinstance(hit["line"], int)
            assert hit["line"] >= 1

    def test_result_tag_is_uppercase(self):
        result = run(tool.execute(self.cfg, max_results=100))
        data = json.loads(result.output)
        for hit in data["results"]:
            assert hit["tag"] == hit["tag"].upper()

    def test_files_scanned_counts_py_files(self):
        result = run(tool.execute(self.cfg, max_results=100))
        data = json.loads(result.output)
        assert data["files_scanned"] >= 1


# ---------------------------------------------------------------------------
# 7. file_glob filtering
# ---------------------------------------------------------------------------

class TestFileGlob:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cfg = make_config(workspace=self.tmpdir)

    def test_file_glob_restricts_scan(self):
        # Put TODO in a .txt file and a .py file
        (Path(self.tmpdir) / "notes.txt").write_text("# TODO: do something\n")
        (Path(self.tmpdir) / "code.py").write_text("# FIXME: fix this\n")
        # Scan only .py files (default)
        result = run(tool.execute(self.cfg, max_results=100, file_glob="**/*.py"))
        assert not result.is_error
        data = json.loads(result.output)
        # Only FIXME (from .py) should appear
        assert data["by_tag"].get("FIXME", 0) >= 1
        assert data["by_tag"].get("TODO", 0) == 0

    def test_file_glob_txt_finds_txt_file(self):
        (Path(self.tmpdir) / "notes.txt").write_text("# TODO: do something\n")
        result = run(tool.execute(self.cfg, max_results=100, file_glob="**/*.txt", tags=["TODO"]))
        assert not result.is_error
        data = json.loads(result.output)
        assert data["by_tag"].get("TODO", 0) >= 1
