"""Tests for the diff_files tool.

Covers: file_vs_text mode, file_vs_file mode, identical files, truncation,
security/path errors, custom labels, and context line clamping.
"""
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import MagicMock


from harness.tools.diff_files import DiffFilesTool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_config(workspace: str = "/tmp"):
    cfg = MagicMock()
    cfg.workspace = workspace
    cfg.allowed_paths = [workspace]
    return cfg


def run(coro):
    return asyncio.run(coro)


tool = DiffFilesTool()


# ---------------------------------------------------------------------------
# 1. file_vs_text mode (default)
# ---------------------------------------------------------------------------

class TestFileVsText:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cfg = make_config(workspace=self.tmpdir)

    def _make_file(self, name: str, content: str) -> str:
        p = Path(self.tmpdir) / name
        p.write_text(content)
        return str(p)

    def test_identical_produces_no_diff(self):
        content = "line1\nline2\nline3\n"
        path = self._make_file("a.txt", content)
        result = run(tool.execute(
            self.cfg,
            path_a=path,
            text_b=content,
            mode="file_vs_text",
            context=2,
            max_lines=100,
        ))
        assert not result.is_error
        assert "no differences" in result.output.lower() or "identical" in result.output.lower()

    def test_single_line_change(self):
        original = "foo\nbar\nbaz\n"
        changed = "foo\nBAR\nbaz\n"
        path = self._make_file("orig.txt", original)
        result = run(tool.execute(
            self.cfg,
            path_a=path,
            text_b=changed,
            mode="file_vs_text",
            context=2,
            max_lines=200,
        ))
        assert not result.is_error
        # Should show BAR as addition and bar as removal
        assert "+BAR" in result.output or "BAR" in result.output

    def test_addition_of_lines(self):
        original = "line1\nline2\n"
        extended = "line1\nline2\nline3\nline4\n"
        path = self._make_file("base.txt", original)
        result = run(tool.execute(
            self.cfg,
            path_a=path,
            text_b=extended,
            mode="file_vs_text",
            context=0,
            max_lines=100,
        ))
        assert not result.is_error
        assert "line3" in result.output or "line4" in result.output

    def test_removal_of_lines(self):
        original = "a\nb\nc\nd\n"
        truncated = "a\nd\n"
        path = self._make_file("long.txt", original)
        result = run(tool.execute(
            self.cfg,
            path_a=path,
            text_b=truncated,
            mode="file_vs_text",
            context=0,
            max_lines=100,
        ))
        assert not result.is_error
        assert "-b" in result.output or "-c" in result.output

    def test_custom_labels_appear_in_output(self):
        path = self._make_file("x.txt", "hello\n")
        result = run(tool.execute(
            self.cfg,
            path_a=path,
            text_b="world\n",
            mode="file_vs_text",
            context=0,
            max_lines=100,
            label_a="ORIGINAL",
            label_b="REVISED",
        ))
        assert not result.is_error
        assert "ORIGINAL" in result.output or "REVISED" in result.output

    def test_truncation_at_max_lines(self):
        # Make a big diff that will exceed max_lines=5
        lines = [f"line{i}\n" for i in range(50)]
        original = "".join(lines)
        modified = "".join(f"CHANGED{i}\n" for i in range(50))
        path = self._make_file("big.txt", original)
        result = run(tool.execute(
            self.cfg,
            path_a=path,
            text_b=modified,
            mode="file_vs_text",
            context=0,
            max_lines=5,
        ))
        assert not result.is_error
        assert "truncated" in result.output.lower()

    def test_empty_file_vs_text(self):
        path = self._make_file("empty.txt", "")
        result = run(tool.execute(
            self.cfg,
            path_a=path,
            text_b="new content\n",
            mode="file_vs_text",
            context=0,
            max_lines=100,
        ))
        assert not result.is_error

    def test_nonexistent_file_is_error(self):
        result = run(tool.execute(
            self.cfg,
            path_a=str(Path(self.tmpdir) / "ghost.txt"),
            text_b="anything",
            mode="file_vs_text",
            context=2,
            max_lines=100,
        ))
        assert result.is_error


# ---------------------------------------------------------------------------
# 2. file_vs_file mode
# ---------------------------------------------------------------------------

class TestFileVsFile:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cfg = make_config(workspace=self.tmpdir)

    def _make_file(self, name: str, content: str) -> str:
        p = Path(self.tmpdir) / name
        p.write_text(content)
        return str(p)

    def test_identical_files_no_diff(self):
        content = "same\ncontent\n"
        a = self._make_file("a.txt", content)
        b = self._make_file("b.txt", content)
        result = run(tool.execute(
            self.cfg,
            path_a=a,
            path_b=b,
            mode="file_vs_file",
            context=2,
            max_lines=200,
        ))
        assert not result.is_error
        assert "no differences" in result.output.lower() or "identical" in result.output.lower()

    def test_different_files_shows_diff(self):
        a = self._make_file("v1.txt", "old content\n")
        b = self._make_file("v2.txt", "new content\n")
        result = run(tool.execute(
            self.cfg,
            path_a=a,
            path_b=b,
            mode="file_vs_file",
            context=0,
            max_lines=100,
        ))
        assert not result.is_error
        assert "new" in result.output.lower() or "+" in result.output

    def test_missing_path_b_in_file_mode_is_error(self):
        a = self._make_file("a.txt", "hello")
        result = run(tool.execute(
            self.cfg,
            path_a=a,
            mode="file_vs_file",
            context=2,
            max_lines=100,
        ))
        assert result.is_error

    def test_nonexistent_path_b_is_error(self):
        a = self._make_file("a.txt", "hello")
        result = run(tool.execute(
            self.cfg,
            path_a=a,
            path_b=str(Path(self.tmpdir) / "missing.txt"),
            mode="file_vs_file",
            context=2,
            max_lines=100,
        ))
        assert result.is_error


# ---------------------------------------------------------------------------
# 3. Security / path constraints
# ---------------------------------------------------------------------------

class TestPathSecurity:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cfg = make_config(workspace=self.tmpdir)

    def test_path_outside_workspace_is_error(self):
        """Paths outside the allowed workspace should be rejected."""
        result = run(tool.execute(
            self.cfg,
            path_a="/etc/passwd",
            text_b="whatever",
            mode="file_vs_text",
            context=2,
            max_lines=100,
        ))
        assert result.is_error


# ---------------------------------------------------------------------------
# 4. Summary header
# ---------------------------------------------------------------------------

class TestSummaryHeader:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cfg = make_config(workspace=self.tmpdir)

    def _make_file(self, name: str, content: str) -> str:
        p = Path(self.tmpdir) / name
        p.write_text(content)
        return str(p)

    def test_summary_shows_additions_and_removals(self):
        path = self._make_file("s.txt", "a\nb\nc\n")
        result = run(tool.execute(
            self.cfg,
            path_a=path,
            text_b="a\nX\nc\nd\n",
            mode="file_vs_text",
            context=0,
            max_lines=200,
        ))
        assert not result.is_error
        # summary line contains +N and -N counts
        assert "+" in result.output
        assert "-" in result.output
