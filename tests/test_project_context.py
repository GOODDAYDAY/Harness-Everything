"""Tests for harness/core/project_context.py

Focuses on the two pure functions that are the core of the module:
  _build_tree - recursively builds a tree listing
  _file_inventory - globs for key file categories
"""
from __future__ import annotations

from pathlib import Path

from harness.core.project_context import (
    _TREE_MAX_ENTRIES,
    _build_tree,
    _file_inventory,
)


# ---------------------------------------------------------------------------
# _build_tree
# ---------------------------------------------------------------------------

class TestBuildTree:
    def test_empty_directory(self, tmp_path):
        lines = _build_tree(tmp_path)
        assert lines == []

    def test_single_file(self, tmp_path):
        (tmp_path / "readme.md").write_text("")
        lines = _build_tree(tmp_path)
        assert len(lines) == 1
        assert "readme.md" in lines[0]

    def test_single_dir(self, tmp_path):
        (tmp_path / "subdir").mkdir()
        lines = _build_tree(tmp_path)
        assert any("subdir" in ln for ln in lines)

    def test_last_item_uses_corner_connector(self, tmp_path):
        (tmp_path / "aaa.py").write_text("")
        (tmp_path / "zzz.py").write_text("")
        lines = _build_tree(tmp_path)
        assert lines[0].startswith("├── ")
        assert lines[-1].startswith("└── ")

    def test_hidden_files_excluded(self, tmp_path):
        (tmp_path / ".hidden").write_text("")
        (tmp_path / "visible.py").write_text("")
        lines = _build_tree(tmp_path)
        assert not any(".hidden" in ln for ln in lines)
        assert any("visible.py" in ln for ln in lines)

    def test_pycache_excluded(self, tmp_path):
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "mycode.py").write_text("")
        lines = _build_tree(tmp_path)
        assert not any("__pycache__" in ln for ln in lines)

    def test_venv_excluded(self, tmp_path):
        (tmp_path / ".venv").mkdir()
        (tmp_path / "app.py").write_text("")
        lines = _build_tree(tmp_path)
        assert not any(".venv" in ln for ln in lines)

    def test_node_modules_excluded(self, tmp_path):
        (tmp_path / "node_modules").mkdir()
        lines = _build_tree(tmp_path)
        assert not any("node_modules" in ln for ln in lines)

    def test_recursive_into_subdirs(self, tmp_path):
        sub = tmp_path / "pkg"
        sub.mkdir()
        (sub / "mod.py").write_text("")
        lines = _build_tree(tmp_path)
        assert any("mod.py" in ln for ln in lines)

    def test_depth_limit_respected(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c" / "d"
        deep.mkdir(parents=True)
        (deep / "deep_file.py").write_text("")
        lines = _build_tree(tmp_path, max_depth=2)
        assert not any("deep_file.py" in ln for ln in lines)

    def test_truncation_at_max_entries(self, tmp_path):
        for i in range(_TREE_MAX_ENTRIES + 10):
            (tmp_path / f"file_{i:04d}.py").write_text("")
        lines = _build_tree(tmp_path)
        assert any("truncated" in ln for ln in lines)
        assert len(lines) <= _TREE_MAX_ENTRIES + 2

    def test_dirs_listed_before_files(self, tmp_path):
        """Directories should appear before files at the same level."""
        (tmp_path / "z_file.py").write_text("")
        (tmp_path / "a_dir").mkdir()
        lines = _build_tree(tmp_path)
        dir_idx = next(i for i, ln in enumerate(lines) if "a_dir" in ln)
        file_idx = next(i for i, ln in enumerate(lines) if "z_file.py" in ln)
        assert dir_idx < file_idx

    def test_egg_info_dirs_excluded(self, tmp_path):
        (tmp_path / "mypackage.egg-info").mkdir()
        (tmp_path / "src.py").write_text("")
        lines = _build_tree(tmp_path)
        assert not any("egg-info" in ln for ln in lines)

    def test_trailing_slash_on_dirs(self, tmp_path):
        (tmp_path / "mydir").mkdir()
        lines = _build_tree(tmp_path)
        dir_line = next(ln for ln in lines if "mydir" in ln)
        assert "mydir/" in dir_line

    def test_files_no_trailing_slash(self, tmp_path):
        (tmp_path / "file.py").write_text("")
        lines = _build_tree(tmp_path)
        file_line = next(ln for ln in lines if "file.py" in ln)
        assert not file_line.rstrip().endswith("/")

    def test_permission_error_returns_empty(self, tmp_path, monkeypatch):
        """If iterdir raises PermissionError, return empty list."""
        original_iterdir = Path.iterdir

        def bad_iterdir(self):
            if self == tmp_path:
                raise PermissionError("no access")
            return original_iterdir(self)

        monkeypatch.setattr(Path, "iterdir", bad_iterdir)
        lines = _build_tree(tmp_path)
        assert lines == []


# ---------------------------------------------------------------------------
# _file_inventory
# ---------------------------------------------------------------------------

class TestFileInventory:
    def test_empty_workspace(self, tmp_path):
        result = _file_inventory(str(tmp_path))
        assert isinstance(result, list)
        assert result == []

    def test_py_files_discovered(self, tmp_path):
        (tmp_path / "app.py").write_text("")
        (tmp_path / "utils.py").write_text("")
        result = _file_inventory(str(tmp_path))
        assert any("Python sources" in ln for ln in result)
        assert any("app.py" in ln for ln in result)

    def test_py_file_appears_in_results(self, tmp_path):
        (tmp_path / "test_foo.py").write_text("")
        result = _file_inventory(str(tmp_path))
        assert any("test_foo.py" in ln for ln in result)

    def test_md_files_discovered(self, tmp_path):
        (tmp_path / "README.md").write_text("")
        result = _file_inventory(str(tmp_path))
        assert any("README.md" in ln for ln in result)

    def test_bullets_have_bullet_marker(self, tmp_path):
        (tmp_path / "foo.py").write_text("")
        result = _file_inventory(str(tmp_path))
        file_lines = [ln for ln in result if ln.startswith("  •")]
        assert len(file_lines) >= 1

    def test_deduplication(self, tmp_path):
        """A file should not appear more than once across all categories."""
        (tmp_path / "test_foo.py").write_text("")
        result = _file_inventory(str(tmp_path))
        bullet_lines = [ln for ln in result if ln.strip().startswith("•")]
        occurrences = sum(1 for ln in bullet_lines if "test_foo.py" in ln)
        assert occurrences == 1

    def test_no_categories_when_no_matching_files(self, tmp_path):
        (tmp_path / "data.csv").write_text("")
        result = _file_inventory(str(tmp_path))
        assert result == []

    def test_result_is_list_of_strings(self, tmp_path):
        (tmp_path / "a.py").write_text("")
        result = _file_inventory(str(tmp_path))
        assert all(isinstance(ln, str) for ln in result)

    def test_files_sorted_alphabetically(self, tmp_path):
        (tmp_path / "zzz.py").write_text("")
        (tmp_path / "aaa.py").write_text("")
        result = _file_inventory(str(tmp_path))
        file_lines = [ln.strip() for ln in result if ln.strip().startswith("•")]
        names = [ln.replace("• ", "") for ln in file_lines]
        assert names == sorted(names)
