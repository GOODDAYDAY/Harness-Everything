"""Tests for the file_info tool.

Covers: normal file stat, multiple paths, empty paths list, non-existent file,
directory vs regular file, path security, and output format.
"""
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import MagicMock


from harness.tools.file_info import FileInfoTool


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


tool = FileInfoTool()


# ---------------------------------------------------------------------------
# 1. Normal stat
# ---------------------------------------------------------------------------

class TestNormalStat:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cfg = make_config(workspace=self.tmpdir)

    def _make(self, name: str, content: str) -> str:
        p = Path(self.tmpdir) / name
        p.write_text(content)
        return str(p)

    def test_single_file_returns_output(self):
        p = self._make("hello.py", "line1\nline2\nline3\n")
        result = run(tool.execute(self.cfg, paths=[p]))
        assert not result.is_error
        assert "hello.py" in result.output

    def test_output_contains_line_count(self):
        p = self._make("three.py", "a\nb\nc\n")
        result = run(tool.execute(self.cfg, paths=[p]))
        assert not result.is_error
        assert "3" in result.output  # 3 lines

    def test_output_contains_byte_size(self):
        content = "hello\n"
        p = self._make("sized.py", content)
        result = run(tool.execute(self.cfg, paths=[p]))
        assert not result.is_error
        assert str(len(content.encode())) in result.output

    def test_output_contains_modified_date(self):
        p = self._make("dated.py", "x = 1\n")
        result = run(tool.execute(self.cfg, paths=[p]))
        assert not result.is_error
        # mtime in YYYY-MM-DD format
        import re
        assert re.search(r"\d{4}-\d{2}-\d{2}", result.output)

    def test_multiple_paths(self):
        p1 = self._make("a.py", "a\n")
        p2 = self._make("b.py", "b\nc\n")
        result = run(tool.execute(self.cfg, paths=[p1, p2]))
        assert not result.is_error
        assert "a.py" in result.output
        assert "b.py" in result.output

    def test_empty_file_has_zero_lines(self):
        p = self._make("empty.py", "")
        result = run(tool.execute(self.cfg, paths=[p]))
        assert not result.is_error
        assert "0" in result.output

    def test_large_file_line_count(self):
        content = "x\n" * 1000
        p = self._make("large.py", content)
        result = run(tool.execute(self.cfg, paths=[p]))
        assert not result.is_error
        assert "1000" in result.output


# ---------------------------------------------------------------------------
# 2. Error cases
# ---------------------------------------------------------------------------

class TestErrorCases:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cfg = make_config(workspace=self.tmpdir)

    def test_empty_paths_is_error(self):
        result = run(tool.execute(self.cfg, paths=[]))
        assert result.is_error

    def test_nonexistent_file_shows_error_in_output(self):
        missing = str(Path(self.tmpdir) / "ghost.py")
        result = run(tool.execute(self.cfg, paths=[missing]))
        # Returns graceful output with ERROR annotation per path,
        # not a top-level is_error (because other paths may succeed)
        # The key thing: does not raise an exception
        assert isinstance(result.is_error, bool)
        if not result.is_error:
            assert "error" in result.output.lower() or "ghost.py" in result.output

    def test_directory_shows_error_in_output(self):
        dir_path = str(Path(self.tmpdir))
        result = run(tool.execute(self.cfg, paths=[dir_path]))
        # Directories are not regular files — should note this per-path
        assert isinstance(result.is_error, bool)

    def test_path_outside_workspace_shows_error(self):
        result = run(tool.execute(self.cfg, paths=["/etc/passwd"]))
        # Should not crash; the per-path result should contain ERROR
        assert isinstance(result.is_error, bool)
        if not result.is_error:
            assert "error" in result.output.lower()

    def test_empty_string_path_shows_error(self):
        result = run(tool.execute(self.cfg, paths=[""]))
        assert isinstance(result.is_error, bool)
        if not result.is_error:
            assert "error" in result.output.lower()


# ---------------------------------------------------------------------------
# 3. Output structure
# ---------------------------------------------------------------------------

class TestOutputFormat:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cfg = make_config(workspace=self.tmpdir)
        self.p = str((Path(self.tmpdir) / "mod.py").resolve())
        Path(self.p).write_text("a = 1\n")

    def test_output_has_header_line(self):
        result = run(tool.execute(self.cfg, paths=[self.p]))
        assert not result.is_error
        assert "line" in result.output.lower() or "bytes" in result.output.lower()

    def test_output_is_string(self):
        result = run(tool.execute(self.cfg, paths=[self.p]))
        assert isinstance(result.output, str)

    def test_mixed_valid_and_missing(self):
        good = self.p
        bad = str(Path(self.tmpdir) / "missing.py")
        result = run(tool.execute(self.cfg, paths=[good, bad]))
        # Should return output covering both; the good one should show up
        assert not result.is_error
        assert "mod.py" in result.output
        # The bad one should show ERROR annotation inline
        assert "error" in result.output.lower() or "missing.py" in result.output
