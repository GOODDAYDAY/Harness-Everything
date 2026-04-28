"""Tests for the file_patch tool.

Covers: single-file patch (bare hunk with path arg), multi-file patch
(git diff format), dry_run mode, fuzz tolerance, empty patch, no path
for bare hunk, and security guards.
"""
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import MagicMock


from harness.tools.file_patch import FilePatchTool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_config(workspace: str):
    cfg = MagicMock()
    cfg.workspace = workspace
    cfg.allowed_paths = [workspace]
    # phase_scope is used by _check_phase_scope; must be None or empty to not restrict
    cfg.phase_scope = None
    return cfg


def run(coro):
    return asyncio.run(coro)


tool = FilePatchTool()


# ---------------------------------------------------------------------------
# 1. Single-file bare hunk (requires path=)
# ---------------------------------------------------------------------------

class TestSingleFilePatch:
    ORIGINAL = "line1\nline2\nline3\n"

    # Patch that changes 'line2' -> 'LINE2'
    PATCH_CHANGE = """@@ -1,3 +1,3 @@
 line1
-line2
+LINE2
 line3
"""
    # Patch that adds 'line4' after line3
    PATCH_ADD = """@@ -1,3 +1,4 @@
 line1
 line2
 line3
+line4
"""
    # Patch that removes line2
    PATCH_REMOVE = """@@ -1,3 +1,2 @@
 line1
-line2
 line3
"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cfg = make_config(workspace=self.tmpdir)

    def _make(self, name: str, content: str) -> str:
        p = Path(self.tmpdir) / name
        p.write_text(content)
        return str(p)

    def test_change_line(self):
        p = self._make("f.py", self.ORIGINAL)
        result = run(tool.execute(self.cfg, patch=self.PATCH_CHANGE, path=p))
        assert not result.is_error
        assert "LINE2" in Path(p).read_text()

    def test_add_line(self):
        p = self._make("f.py", self.ORIGINAL)
        result = run(tool.execute(self.cfg, patch=self.PATCH_ADD, path=p))
        assert not result.is_error
        assert "line4" in Path(p).read_text()

    def test_remove_line(self):
        p = self._make("f.py", self.ORIGINAL)
        result = run(tool.execute(self.cfg, patch=self.PATCH_REMOVE, path=p))
        assert not result.is_error
        content = Path(p).read_text()
        assert "line2" not in content
        assert "line1" in content
        assert "line3" in content

    def test_dry_run_does_not_write(self):
        p = self._make("f.py", self.ORIGINAL)
        result = run(tool.execute(self.cfg, patch=self.PATCH_CHANGE, path=p, dry_run=True))
        assert not result.is_error
        # File should still contain original content
        assert "line2" in Path(p).read_text()
        assert "LINE2" not in Path(p).read_text()

    def test_dry_run_shows_dry_run_in_output(self):
        p = self._make("f.py", self.ORIGINAL)
        result = run(tool.execute(self.cfg, patch=self.PATCH_CHANGE, path=p, dry_run=True))
        assert "DRY RUN" in result.output or "dry" in result.output.lower()

    def test_output_shows_hunk_count(self):
        p = self._make("f.py", self.ORIGINAL)
        result = run(tool.execute(self.cfg, patch=self.PATCH_CHANGE, path=p))
        assert not result.is_error
        assert "Hunk" in result.output or "hunk" in result.output.lower()

    def test_bare_hunk_without_path_is_error(self):
        result = run(tool.execute(self.cfg, patch=self.PATCH_CHANGE))
        assert result.is_error

    def test_empty_patch_is_error(self):
        p = self._make("f.py", self.ORIGINAL)
        result = run(tool.execute(self.cfg, patch="   ", path=p))
        assert result.is_error

    def test_no_matching_context_is_error(self):
        p = self._make("f.py", "completely different content\n")
        result = run(tool.execute(self.cfg, patch=self.PATCH_CHANGE, path=p, fuzz=0))
        assert result.is_error

    def test_fuzz_tolerance_applies_offset_patch(self):
        # Original has 2 extra lines at the top, offsetting line2 to line4.
        # With fuzz=3, the hunk should still apply.
        shifted = "prefix1\nprefix2\nline1\nline2\nline3\n"
        p = self._make("g.py", shifted)
        result = run(tool.execute(self.cfg, patch=self.PATCH_CHANGE, path=p, fuzz=3))
        assert not result.is_error
        assert "LINE2" in Path(p).read_text()


# ---------------------------------------------------------------------------
# 2. Multi-file patch (git diff format)
# ---------------------------------------------------------------------------

class TestMultiFilePatch:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cfg = make_config(workspace=self.tmpdir)

    def _make_rel(self, name: str, content: str) -> str:
        p = Path(self.tmpdir) / name
        p.write_text(content)
        return name  # return relative path

    def _make_abs(self, name: str, content: str) -> str:
        p = Path(self.tmpdir) / name
        p.write_text(content)
        return str(p)

    def test_multi_file_patch_applies_both(self):
        self._make_rel("a.py", "old_a\n")
        self._make_rel("b.py", "old_b\n")
        patch = (
            "--- a/a.py\n+++ b/a.py\n"
            "@@ -1 +1 @@\n-old_a\n+new_a\n"
            "--- a/b.py\n+++ b/b.py\n"
            "@@ -1 +1 @@\n-old_b\n+new_b\n"
        )
        result = run(tool.execute(self.cfg, patch=patch))
        assert not result.is_error
        assert "new_a" in (Path(self.tmpdir) / "a.py").read_text()
        assert "new_b" in (Path(self.tmpdir) / "b.py").read_text()

    def test_multi_file_dry_run(self):
        self._make_rel("z.py", "orig\n")
        patch = (
            "--- a/z.py\n+++ b/z.py\n"
            "@@ -1 +1 @@\n-orig\n+changed\n"
        )
        result = run(tool.execute(self.cfg, patch=patch, dry_run=True))
        assert not result.is_error
        # File not changed
        assert (Path(self.tmpdir) / "z.py").read_text() == "orig\n"

    def test_multi_file_one_fails_other_succeeds(self):
        """When one file's hunk fails to match, the other should still be applied."""
        self._make_rel("ok.py", "hello\n")
        # bad.py has content that doesn't match the hunk
        self._make_rel("bad.py", "totally different\n")
        patch = (
            "--- a/ok.py\n+++ b/ok.py\n"
            "@@ -1 +1 @@\n-hello\n+HELLO\n"
            "--- a/bad.py\n+++ b/bad.py\n"
            "@@ -1 +1 @@\n-hello\n+HELLO\n"  # won't match
        )
        result = run(tool.execute(self.cfg, patch=patch, fuzz=0))
        # ok.py should be patched, bad.py should fail
        ok_content = (Path(self.tmpdir) / "ok.py").read_text()
        assert "HELLO" in ok_content
        # Result should contain error mention for bad.py
        assert "bad.py" in result.output or "bad.py" in (result.error or "")


# ---------------------------------------------------------------------------
# 3. Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cfg = make_config(workspace=self.tmpdir)

    def test_create_new_file_via_patch(self):
        """Patch with old_count=0 creates a new file."""
        new_file = str(Path(self.tmpdir) / "brand_new.py")
        patch = "@@ -0,0 +1,2 @@\n+line1\n+line2\n"
        result = run(tool.execute(self.cfg, patch=patch, path=new_file))
        assert not result.is_error
        assert Path(new_file).exists()
        assert "line1" in Path(new_file).read_text()

    def test_patch_with_no_newline_at_end(self):
        p = Path(self.tmpdir) / "nonl.py"
        p.write_text("a\nb")  # no trailing newline
        patch = "@@ -1,2 +1,2 @@\n a\n-b\n+B"
        result = run(tool.execute(self.cfg, patch=patch, path=str(p)))
        assert not result.is_error
        assert "B" in p.read_text()

    def test_path_outside_workspace_is_error(self):
        patch = "@@ -1 +1 @@\n-x\n+y\n"
        result = run(tool.execute(self.cfg, patch=patch, path="/etc/hosts"))
        assert result.is_error

    def test_invalid_hunk_header_is_error(self):
        p = Path(self.tmpdir) / "x.py"
        p.write_text("hello\n")
        # No valid @@ lines
        patch = "this is not a valid patch at all"
        result = run(tool.execute(self.cfg, patch=patch, path=str(p)))
        assert result.is_error

    def test_line_count_summary_in_output(self):
        p = Path(self.tmpdir) / "lc.py"
        p.write_text("a\nb\nc\n")
        patch = "@@ -1,3 +1,4 @@\n a\n b\n c\n+d\n"
        result = run(tool.execute(self.cfg, patch=patch, path=str(p)))
        assert not result.is_error
        assert "→" in result.output or "->" in result.output or "Lines" in result.output
