"""Tests for harness/tools/git.py."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from harness.core.config import HarnessConfig
from harness.tools.git import GitStatusTool, GitDiffTool, GitLogTool, _run_git


@pytest.fixture()
def git_repo(tmp_path) -> Path:
    """Create a minimal git repository for testing."""
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    (tmp_path / "README.md").write_text("# Test Repo")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    return tmp_path


@pytest.fixture()
def config_git(git_repo) -> HarnessConfig:
    return HarnessConfig(workspace=str(git_repo))


@pytest.fixture()
def config_non_git(tmp_path) -> HarnessConfig:
    return HarnessConfig(workspace=str(tmp_path))


# ---------------------------------------------------------------------------
# _run_git helper
# ---------------------------------------------------------------------------

class TestRunGit:
    def test_successful_git_command(self, config_git):
        result = asyncio.run(_run_git(config_git, "status", "--short"))
        assert not result.is_error

    def test_failed_git_command_in_non_git_dir(self, config_non_git):
        result = asyncio.run(_run_git(config_non_git, "status", "--short"))
        assert result.is_error

    def test_empty_output_returns_placeholder(self, config_git):
        # git diff on a clean repo returns empty output
        result = asyncio.run(_run_git(config_git, "diff"))
        assert not result.is_error
        assert result.output == "(empty)"

    def test_invalid_subcommand_returns_error(self, config_git):
        result = asyncio.run(_run_git(config_git, "no-such-git-subcommand-xyz"))
        assert result.is_error


# ---------------------------------------------------------------------------
# GitStatusTool
# ---------------------------------------------------------------------------

class TestGitStatusTool:
    def test_name(self):
        assert GitStatusTool().name == "git_status"

    def test_tags(self):
        assert "git" in GitStatusTool().tags

    def test_clean_repo_returns_no_error(self, config_git):
        result = asyncio.run(GitStatusTool().execute(config_git))
        assert not result.is_error

    def test_dirty_repo_shows_changes(self, config_git, git_repo):
        (git_repo / "new_file.py").write_text("x = 1")
        result = asyncio.run(GitStatusTool().execute(config_git))
        assert not result.is_error
        assert "new_file.py" in result.output

    def test_non_git_dir_returns_error(self, config_non_git):
        result = asyncio.run(GitStatusTool().execute(config_non_git))
        assert result.is_error

    def test_input_schema(self):
        schema = GitStatusTool().input_schema()
        assert schema["type"] == "object"
        assert "properties" in schema


# ---------------------------------------------------------------------------
# GitDiffTool
# ---------------------------------------------------------------------------

class TestGitDiffTool:
    def test_name(self):
        assert GitDiffTool().name == "git_diff"

    def test_clean_repo_no_diff(self, config_git):
        result = asyncio.run(GitDiffTool().execute(config_git))
        assert not result.is_error
        assert result.output == "(empty)"

    def test_unstaged_changes_visible(self, config_git, git_repo):
        f = git_repo / "README.md"
        f.write_text("# Modified")
        result = asyncio.run(GitDiffTool().execute(config_git))
        assert not result.is_error
        assert "README" in result.output or "Modified" in result.output

    def test_staged_only(self, config_git, git_repo):
        f = git_repo / "README.md"
        f.write_text("# Staged change")
        subprocess.run(["git", "add", "."], cwd=git_repo, check=True, capture_output=True)
        result = asyncio.run(GitDiffTool().execute(config_git, staged=True))
        assert not result.is_error
        assert "Staged" in result.output

    def test_path_filter(self, config_git, git_repo):
        (git_repo / "other.py").write_text("y = 2")
        (git_repo / "README.md").write_text("# Modified")
        result = asyncio.run(
            GitDiffTool().execute(config_git, path="README.md")
        )
        assert not result.is_error
        assert "other.py" not in result.output

    def test_input_schema_has_staged_and_path(self):
        schema = GitDiffTool().input_schema()
        props = schema["properties"]
        assert "staged" in props
        assert "path" in props


# ---------------------------------------------------------------------------
# GitLogTool
# ---------------------------------------------------------------------------

class TestGitLogTool:
    def test_name(self):
        assert GitLogTool().name == "git_log"

    def test_count_required(self):
        schema = GitLogTool().input_schema()
        assert "count" in schema.get("required", [])

    def test_log_returns_commits(self, config_git):
        result = asyncio.run(GitLogTool().execute(config_git, count=5))
        assert not result.is_error
        assert "Initial commit" in result.output

    def test_oneline_format(self, config_git):
        result = asyncio.run(GitLogTool().execute(config_git, count=1, oneline=True))
        assert not result.is_error
        lines = [line for line in result.output.strip().splitlines() if line]
        assert len(lines) >= 1

    def test_full_format(self, config_git):
        result = asyncio.run(GitLogTool().execute(config_git, count=1, oneline=False))
        assert not result.is_error
        assert "commit" in result.output.lower() or "Author" in result.output

    def test_non_git_dir_returns_error(self, config_non_git):
        result = asyncio.run(GitLogTool().execute(config_non_git, count=5))
        assert result.is_error
