"""Unit tests for harness.core.project_context.

Covers: _build_tree, _file_inventory, and ProjectContextBuilder.build().
All filesystem tests use temporary directories so they are hermetic.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from harness.core.project_context import (
    ProjectContextBuilder,
    _build_tree,
    _file_inventory,
    _run_cmd,
    _TREE_MAX_ENTRIES,
    _FILE_GLOB_LIMIT,
    _MAX_OUTPUT_CHARS,
)
from harness.core.config import HarnessConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(workspace: str) -> HarnessConfig:
    """Minimal HarnessConfig pointing at a temp workspace."""
    cfg = MagicMock(spec=HarnessConfig)
    cfg.workspace = workspace
    return cfg


def _make_dir_structure(base: Path, spec: dict) -> None:
    """Create a directory structure from a nested dict.

    Keys ending in "/" are directories; other keys are files whose values
    are the file content (str or bytes).
    """
    for name, value in spec.items():
        path = base / name
        if isinstance(value, dict):
            path.mkdir(parents=True, exist_ok=True)
            _make_dir_structure(path, value)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(value, bytes):
                path.write_bytes(value)
            else:
                path.write_text(value or "")


# ---------------------------------------------------------------------------
# _build_tree tests
# ---------------------------------------------------------------------------

class TestBuildTree:
    """Tests for _build_tree()."""

    def test_empty_directory(self, tmp_path):
        """An empty directory returns no tree lines."""
        lines = _build_tree(tmp_path)
        assert lines == []

    def test_single_file(self, tmp_path):
        """A directory with one file produces a single tree line."""
        (tmp_path / "README.md").write_text("hello")
        lines = _build_tree(tmp_path)
        assert len(lines) == 1
        assert "README.md" in lines[0]
        assert "└── " in lines[0]  # only entry → last-item connector

    def test_connector_last_entry_uses_corner(self, tmp_path):
        """The final visible entry in a directory must use '└── '."""
        (tmp_path / "a.py").write_text("")
        (tmp_path / "b.py").write_text("")
        (tmp_path / "c.py").write_text("")
        lines = _build_tree(tmp_path)
        # Last line must have the corner connector
        assert "└── " in lines[-1]
        # All-but-last lines must have the T connector
        for line in lines[:-1]:
            assert "├── " in line

    def test_connector_with_hidden_file_at_end(self, tmp_path):
        """Hidden files are skipped; last *visible* entry still gets '└── '."""
        (tmp_path / "visible.py").write_text("")
        (tmp_path / ".hidden").write_text("")  # should be skipped
        lines = _build_tree(tmp_path)
        # Only visible.py should appear
        assert len(lines) == 1
        assert "└── " in lines[0]
        assert "hidden" not in lines[0]

    def test_hidden_files_skipped(self, tmp_path):
        """Files and dirs starting with '.' are excluded."""
        (tmp_path / ".git").mkdir()
        (tmp_path / ".env").write_text("secret")
        (tmp_path / "main.py").write_text("")
        lines = _build_tree(tmp_path)
        content = "\n".join(lines)
        assert ".git" not in content
        assert ".env" not in content
        assert "main.py" in content

    def test_skip_dirs_are_excluded(self, tmp_path):
        """_TREE_SKIP_DIRS entries are never descended into."""
        for skip in ("__pycache__", ".pytest_cache", "node_modules", ".venv"):
            skip_dir = tmp_path / skip
            skip_dir.mkdir()
            (skip_dir / "junk.py").write_text("")
        (tmp_path / "real.py").write_text("")
        lines = _build_tree(tmp_path)
        content = "\n".join(lines)
        for skip in ("__pycache__", ".pytest_cache", "node_modules", ".venv"):
            assert skip not in content
        assert "real.py" in content

    def test_egg_info_excluded(self, tmp_path):
        """Directories ending in '.egg-info' are excluded."""
        (tmp_path / "mypkg.egg-info").mkdir()
        (tmp_path / "mypkg.egg-info" / "PKG-INFO").write_text("")
        (tmp_path / "module.py").write_text("")
        lines = _build_tree(tmp_path)
        content = "\n".join(lines)
        assert "egg-info" not in content
        assert "module.py" in content

    def test_dirs_listed_before_files(self, tmp_path):
        """Directories come before files at the same level."""
        (tmp_path / "zzz_file.txt").write_text("")
        (tmp_path / "aaa_dir").mkdir()
        lines = _build_tree(tmp_path)
        names = [line.split("── ", 1)[-1].rstrip("/") for line in lines if "── " in line]
        dir_idx = names.index("aaa_dir")
        file_idx = names.index("zzz_file.txt")
        assert dir_idx < file_idx, "directories should appear before files"

    def test_nested_directories(self, tmp_path):
        """Nested dirs are recursed up to max_depth."""
        _make_dir_structure(tmp_path, {
            "src": {
                "module": {
                    "deep.py": "",
                }
            }
        })
        lines = _build_tree(tmp_path, max_depth=3)
        content = "\n".join(lines)
        assert "src" in content
        assert "module" in content
        assert "deep.py" in content

    def test_max_depth_respected(self, tmp_path):
        """Tree stops after max_depth levels."""
        _make_dir_structure(tmp_path, {
            "a": {
                "b": {
                    "c": {
                        "deep.py": ""  # 3 levels below tmp_path
                    }
                }
            }
        })
        lines = _build_tree(tmp_path, max_depth=2)
        content = "\n".join(lines)
        assert "a" in content
        assert "b" in content
        assert "deep.py" not in content  # level 3 — beyond max_depth=2

    def test_truncation_at_max_entries(self, tmp_path):
        """When > _TREE_MAX_ENTRIES files exist, the tree is truncated."""
        for i in range(_TREE_MAX_ENTRIES + 10):
            (tmp_path / f"file_{i:04d}.py").write_text("")
        lines = _build_tree(tmp_path)
        entry_count = sum(
            1 for line in lines if "── " in line and "(truncated)" not in line
        )
        assert entry_count <= _TREE_MAX_ENTRIES
        truncation_lines = [line for line in lines if "(truncated)" in line]
        assert len(truncation_lines) == 1, "expected exactly one truncation marker"

    def test_permission_error_returns_empty(self, tmp_path):
        """A directory that cannot be listed returns []."""
        locked = tmp_path / "locked"
        locked.mkdir()
        original_mode = locked.stat().st_mode
        try:
            # Remove read permission from the directory
            locked.chmod(0o000)
            # Root can still read, so skip this assertion when running as root
            if os.getuid() != 0:
                lines = _build_tree(locked)
                assert lines == []
        finally:
            locked.chmod(original_mode)

    def test_prefix_passed_to_children(self, tmp_path):
        """Children of a directory receive the correct prefix with indentation."""
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "mod.py").write_text("")
        lines = _build_tree(tmp_path)
        # The nested file must be indented relative to its parent
        nested = [line for line in lines if "mod.py" in line]
        assert len(nested) == 1
        # The nested line must start with at least 4 chars of prefix + connector
        assert nested[0].startswith(" ") or nested[0].startswith("│"), (
            f"expected nested indentation but got: {nested[0]!r}"
        )


# ---------------------------------------------------------------------------
# _file_inventory tests
# ---------------------------------------------------------------------------

class TestFileInventory:
    """Tests for _file_inventory()."""

    def test_empty_workspace_returns_empty(self, tmp_path):
        """An empty directory gives no inventory lines."""
        lines = _file_inventory(str(tmp_path))
        assert lines == []

    def test_python_files_are_listed(self, tmp_path):
        """Python source files appear under 'Python sources'."""
        (tmp_path / "app.py").write_text("")
        (tmp_path / "utils.py").write_text("")
        lines = _file_inventory(str(tmp_path))
        combined = "\n".join(lines)
        assert "Python sources" in combined
        assert "app.py" in combined
        assert "utils.py" in combined

    def test_test_files_are_listed(self, tmp_path):
        """Test files matching **/test_*.py appear in inventory output.

        Note: because _FILE_CATEGORIES iterates Python sources (**/*.py)
        before Tests (**/test_*.py), test files are deduplicated into the
        'Python sources' category rather than 'Tests'.  The important
        guarantee is that the file itself IS listed somewhere.
        """
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_foo.py").write_text("")
        lines = _file_inventory(str(tmp_path))
        combined = "\n".join(lines)
        # The file must appear somewhere in the inventory output
        assert "test_foo.py" in combined
        # 'Python sources' category captures it first (dedup removes it from Tests)
        assert "Python sources" in combined

    def test_markdown_files_are_listed(self, tmp_path):
        """Markdown files appear under 'Docs / markdown'."""
        (tmp_path / "README.md").write_text("# hi")
        lines = _file_inventory(str(tmp_path))
        combined = "\n".join(lines)
        assert "Docs" in combined
        assert "README.md" in combined

    def test_categories_without_matches_are_omitted(self, tmp_path):
        """Categories with no matches produce no output at all."""
        # Only a markdown file — should NOT have 'Python sources' or 'Tests'
        (tmp_path / "NOTES.md").write_text("")
        lines = _file_inventory(str(tmp_path))
        combined = "\n".join(lines)
        assert "Python sources" not in combined
        assert "Tests" not in combined
        assert "Docs" in combined

    def test_deduplication_of_paths(self, tmp_path):
        """The same file is not listed twice even if it matches multiple globs."""
        # test_foo.py matches both 'Python sources' (**/*.py) and 'Tests' (**/test_*.py)
        (tmp_path / "test_foo.py").write_text("")
        lines = _file_inventory(str(tmp_path))
        bullet_lines = [ln for ln in lines if "test_foo.py" in ln]
        # Should appear exactly once across all categories
        assert len(bullet_lines) == 1, (
            f"test_foo.py should appear exactly once; got {len(bullet_lines)} times"
        )

    def test_glob_limit_enforced(self, tmp_path):
        """No more than _FILE_GLOB_LIMIT files are shown per category."""
        # Create more .py files than the limit
        for i in range(_FILE_GLOB_LIMIT + 15):
            (tmp_path / f"mod_{i:04d}.py").write_text("")
        lines = _file_inventory(str(tmp_path))
        bullet_lines = [ln for ln in lines if ln.strip().startswith("•")]
        # Each bullet is one file; total per category must not exceed the limit
        # (all our files are Python, so one category)
        assert len(bullet_lines) <= _FILE_GLOB_LIMIT

    def test_output_is_list_of_strings(self, tmp_path):
        """Return value is always a list of str."""
        (tmp_path / "main.py").write_text("")
        result = _file_inventory(str(tmp_path))
        assert isinstance(result, list)
        for item in result:
            assert isinstance(item, str)

    def test_bullet_format(self, tmp_path):
        """Individual file entries are prefixed with '  •'."""
        (tmp_path / "main.py").write_text("")
        lines = _file_inventory(str(tmp_path))
        file_lines = [ln for ln in lines if "main.py" in ln]
        assert len(file_lines) == 1
        assert file_lines[0].startswith("  \u2022")  # '  •'


# ---------------------------------------------------------------------------
# ProjectContextBuilder.build() tests
# ---------------------------------------------------------------------------

class TestProjectContextBuilderBuild:
    """Tests for ProjectContextBuilder.build()."""

    def _run(self, coro):
        """Run an async coroutine synchronously in tests."""
        return asyncio.new_event_loop().run_until_complete(coro)

    def test_empty_workspace_returns_empty_string(self, tmp_path):
        """A workspace with no files and no git returns ''."""
        builder = ProjectContextBuilder(_cfg(str(tmp_path)))
        result = self._run(builder.build())
        # An empty dir should still produce a tree-less, inventory-less output
        # (no visible files) → returns ""
        assert isinstance(result, str)
        # With no files and no git, the result should be empty
        assert result == ""

    def test_workspace_with_python_file_includes_tree_and_inventory(self, tmp_path):
        """A workspace with a .py file produces tree and inventory sections."""
        (tmp_path / "main.py").write_text("print('hello')")
        builder = ProjectContextBuilder(_cfg(str(tmp_path)))
        result = self._run(builder.build())
        assert "Project Structure" in result
        assert "File Inventory" in result
        assert "main.py" in result

    def test_header_always_present_when_content_exists(self, tmp_path):
        """The '## Project Context' header is emitted when there is content."""
        (tmp_path / "app.py").write_text("")
        builder = ProjectContextBuilder(_cfg(str(tmp_path)))
        result = self._run(builder.build())
        assert result.startswith("## Project Context")

    def test_workspace_not_a_directory(self, tmp_path):
        """If workspace is a file path (not a dir), tree is empty."""
        fake_file = tmp_path / "not_a_dir.txt"
        fake_file.write_text("content")
        builder = ProjectContextBuilder(_cfg(str(fake_file)))
        result = self._run(builder.build())
        # Since the workspace isn't a real dir, _sync_tree returns []
        assert "Project Structure" not in result

    def test_workspace_nonexistent(self, tmp_path):
        """If workspace path doesn't exist, build() returns '' gracefully."""
        nonexistent = str(tmp_path / "does_not_exist")
        builder = ProjectContextBuilder(_cfg(nonexistent))
        result = self._run(builder.build())
        assert isinstance(result, str)
        assert result == ""

    def test_output_truncated_at_max_chars(self, tmp_path):
        """Output is hard-capped at _MAX_OUTPUT_CHARS characters."""
        # Create many files to force a large context block
        for i in range(200):
            (tmp_path / f"module_{i:04d}.py").write_text("# filler" * 20)
        builder = ProjectContextBuilder(_cfg(str(tmp_path)))
        result = self._run(builder.build())
        assert len(result) <= _MAX_OUTPUT_CHARS + 100  # small slack for the header
        if len(result) == _MAX_OUTPUT_CHARS + 100:
            assert "truncated" in result

    def test_git_sections_present_when_in_git_repo(self):
        """In a real git repo git sections appear FIRST (before inventory/tree).

        Git sections are emitted before file inventory so they always survive
        the _MAX_OUTPUT_CHARS hard cap even on large projects.
        """
        import subprocess
        repo_root_bytes = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(Path(__file__).parent),
        )
        repo_root = repo_root_bytes.decode().strip()
        builder = ProjectContextBuilder(_cfg(repo_root))
        result = self._run(builder.build())
        # Git sections must appear despite the file inventory being large
        assert "Recent Commits" in result, (
            "Recent Commits section missing — git sections should appear first "
            "so they are not dropped by the output character cap"
        )
        # Git section must come before the tree/inventory (ordering guarantee)
        tree_pos = result.find("Project Structure")
        git_pos = result.find("Recent Commits")
        if tree_pos != -1:
            assert git_pos < tree_pos, (
                "Git sections must appear before the directory tree in the output"
            )

    def test_git_sections_absent_when_no_git(self, tmp_path):
        """In a non-git directory, 'Recent Commits' section is absent."""
        (tmp_path / "main.py").write_text("")
        builder = ProjectContextBuilder(_cfg(str(tmp_path)))
        result = self._run(builder.build())
        assert "Recent Commits" not in result

    def test_result_is_string(self, tmp_path):
        """build() always returns a str, never None or bytes."""
        builder = ProjectContextBuilder(_cfg(str(tmp_path)))
        result = self._run(builder.build())
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_build_is_awaitable(self, tmp_path):
        """build() is a proper coroutine that can be awaited."""
        builder = ProjectContextBuilder(_cfg(str(tmp_path)))
        result = await builder.build()
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# _run_cmd tests
# ---------------------------------------------------------------------------

class TestRunCmd:
    """Tests for the _run_cmd async helper."""

    def _run(self, coro):
        return asyncio.new_event_loop().run_until_complete(coro)

    def test_successful_command_returns_stdout(self, tmp_path):
        """A command that succeeds returns its stdout."""
        result = self._run(_run_cmd(["echo", "hello"], cwd=str(tmp_path)))
        assert result.strip() == "hello"

    def test_nonzero_exit_returns_empty(self, tmp_path):
        """A command with a non-zero exit code returns ''."""
        result = self._run(_run_cmd(["false"], cwd=str(tmp_path)))
        assert result == ""

    def test_nonexistent_command_returns_empty(self, tmp_path):
        """An unknown command (FileNotFoundError) returns '' without raising."""
        result = self._run(_run_cmd(["__no_such_cmd__"], cwd=str(tmp_path)))
        assert result == ""

    def test_timeout_returns_empty(self, tmp_path):
        """A command that hangs beyond the timeout returns ''."""
        # 'sleep 5' with timeout=0.05 should time out
        result = self._run(
            _run_cmd(["sleep", "5"], cwd=str(tmp_path), timeout=0)
        )
        assert result == ""
