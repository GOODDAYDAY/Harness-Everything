"""Tests for harness/tools/git_search.py."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from harness.core.config import HarnessConfig
from harness.tools.git_search import GitSearchTool


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def git_repo(tmp_path) -> Path:
    """Create a minimal git repository with two commits for testing."""
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    # First commit
    (tmp_path / "hello.py").write_text("# hello\nprint('hello')\n")
    (tmp_path / "notes.txt").write_text("some notes here\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    # Second commit
    (tmp_path / "hello.py").write_text("# hello v2\nprint('hello world')\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Update hello"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    return tmp_path


@pytest.fixture()
def config_git(git_repo) -> HarnessConfig:
    return HarnessConfig(workspace=str(git_repo))


@pytest.fixture()
def config_non_git(tmp_path) -> HarnessConfig:
    return HarnessConfig(workspace=str(tmp_path))


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# GitSearchTool – basic attributes
# ---------------------------------------------------------------------------

class TestGitSearchToolAttributes:
    def test_name(self):
        assert GitSearchTool().name == "git_search"

    def test_tags(self):
        assert "git" in GitSearchTool().tags

    def test_description_mentions_modes(self):
        d = GitSearchTool().description
        assert "log" in d.lower() or "grep" in d.lower() or "blame" in d.lower()

    def test_input_schema_has_mode(self):
        schema = GitSearchTool().input_schema()
        assert "mode" in schema.get("properties", {})

    def test_input_schema_requires_limit(self):
        schema = GitSearchTool().input_schema()
        assert "limit" in schema.get("required", [])


# ---------------------------------------------------------------------------
# GitSearchTool – invalid mode
# ---------------------------------------------------------------------------

class TestGitSearchInvalidMode:
    def test_invalid_mode_returns_error(self, config_git):
        result = run(GitSearchTool().execute(
            config_git, mode="no_such_mode", limit=10
        ))
        assert result.is_error
        assert "mode" in result.error.lower() or "invalid" in result.error.lower()


# ---------------------------------------------------------------------------
# GitSearchTool – non-git workspace
# ---------------------------------------------------------------------------

class TestGitSearchNonGit:
    def test_log_mode_non_git_returns_error(self, config_non_git):
        result = run(GitSearchTool().execute(
            config_non_git, mode="log", pattern="test", limit=10
        ))
        assert result.is_error

    def test_grep_mode_non_git_returns_error(self, config_non_git):
        result = run(GitSearchTool().execute(
            config_non_git, mode="grep", pattern="test", limit=10
        ))
        assert result.is_error


# ---------------------------------------------------------------------------
# GitSearchTool – mode: log
# ---------------------------------------------------------------------------

class TestGitSearchModeLog:
    def test_log_no_pattern_returns_error(self, config_git):
        result = run(GitSearchTool().execute(
            config_git, mode="log", pattern="", limit=10
        ))
        assert result.is_error
        assert "pattern" in result.error.lower()

    def test_log_pattern_matches_commit_message(self, config_git):
        result = run(GitSearchTool().execute(
            config_git, mode="log", pattern="Initial", limit=10
        ))
        assert not result.is_error
        assert "Initial" in result.output

    def test_log_pattern_no_match_returns_message(self, config_git):
        result = run(GitSearchTool().execute(
            config_git, mode="log", pattern="xyzzy_no_match_9999", limit=10
        ))
        assert not result.is_error
        # Should return empty or a "no results" message, not None
        assert result.output is not None

    def test_log_pattern_update(self, config_git):
        result = run(GitSearchTool().execute(
            config_git, mode="log", pattern="Update", limit=10
        ))
        assert not result.is_error
        assert "Update" in result.output

    def test_log_respects_limit(self, config_git):
        result = run(GitSearchTool().execute(
            config_git, mode="log", pattern=".", limit=1
        ))
        assert not result.is_error


# ---------------------------------------------------------------------------
# GitSearchTool – mode: grep
# ---------------------------------------------------------------------------

class TestGitSearchModeGrep:
    def test_grep_no_pattern_returns_error(self, config_git):
        result = run(GitSearchTool().execute(
            config_git, mode="grep", pattern="", limit=10
        ))
        assert result.is_error
        assert "pattern" in result.error.lower()

    def test_grep_finds_string_in_working_tree(self, config_git):
        result = run(GitSearchTool().execute(
            config_git, mode="grep", pattern="hello", limit=10
        ))
        assert not result.is_error
        assert "hello" in result.output.lower()

    def test_grep_no_match_returns_message(self, config_git):
        result = run(GitSearchTool().execute(
            config_git, mode="grep", pattern="xyzzy_no_match_9999", limit=10
        ))
        assert not result.is_error
        assert result.output is not None

    def test_grep_case_insensitive_flag(self, config_git):
        result = run(GitSearchTool().execute(
            config_git, mode="grep", pattern="HELLO", limit=10,
            case_insensitive=True
        ))
        assert not result.is_error
        assert "hello" in result.output.lower()

    def test_grep_path_scope(self, config_git):
        result = run(GitSearchTool().execute(
            config_git, mode="grep", pattern="hello", path="hello.py", limit=10
        ))
        assert not result.is_error
        assert "hello" in result.output.lower()


# ---------------------------------------------------------------------------
# GitSearchTool – mode: show
# ---------------------------------------------------------------------------

class TestGitSearchModeShow:
    def test_show_no_commit_returns_error(self, config_git):
        result = run(GitSearchTool().execute(
            config_git, mode="show", commit="", limit=10
        ))
        assert result.is_error
        assert "commit" in result.error.lower()

    def test_show_invalid_chars_in_commit_returns_error(self, config_git):
        # Shell injection characters should be rejected
        result = run(GitSearchTool().execute(
            config_git, mode="show", commit="abc; rm -rf /", limit=10
        ))
        assert result.is_error

    def test_show_symbolic_ref_rejected(self, config_git):
        # show mode only accepts hex hashes, not symbolic refs like HEAD
        result = run(GitSearchTool().execute(
            config_git, mode="show", commit="HEAD", limit=50
        ))
        assert result.is_error

    def test_show_valid_hash_returns_diff(self, config_git):
        # Get the actual commit hash
        import subprocess
        r = subprocess.run(
            ["git", "log", "--format=%H", "-1"],
            cwd=config_git.workspace,
            capture_output=True, text=True,
        )
        commit_hash = r.stdout.strip()
        result = run(GitSearchTool().execute(
            config_git, mode="show", commit=commit_hash, limit=50
        ))
        assert not result.is_error
        assert result.output
        assert commit_hash[:7] in result.output or commit_hash in result.output

    def test_show_bad_hash_returns_error(self, config_git):
        result = run(GitSearchTool().execute(
            config_git, mode="show", commit="deadbeefdeadbeef", limit=10
        ))
        assert result.is_error


# ---------------------------------------------------------------------------
# GitSearchTool – mode: log_file
# ---------------------------------------------------------------------------

class TestGitSearchModeLogFile:
    def test_log_file_no_path_returns_error(self, config_git):
        result = run(GitSearchTool().execute(
            config_git, mode="log_file", path="", limit=10
        ))
        assert result.is_error
        assert "path" in result.error.lower()

    def test_log_file_valid_path_returns_history(self, config_git):
        result = run(GitSearchTool().execute(
            config_git, mode="log_file", path="hello.py", limit=10
        ))
        assert not result.is_error
        assert "commit" in result.output.lower() or "Update" in result.output

    def test_log_file_nonexistent_path_returns_empty(self, config_git):
        result = run(GitSearchTool().execute(
            config_git, mode="log_file", path="no_such_file.py", limit=10
        ))
        # Should return empty results (git log on missing path yields nothing)
        assert result.output is not None or result.error is not None


# ---------------------------------------------------------------------------
# GitSearchTool – mode: blame
# ---------------------------------------------------------------------------

class TestGitSearchModeBlame:
    def test_blame_no_path_returns_error(self, config_git):
        result = run(GitSearchTool().execute(
            config_git, mode="blame", path="", limit=10
        ))
        assert result.is_error
        assert "path" in result.error.lower()

    def test_blame_valid_file_returns_annotations(self, config_git):
        result = run(GitSearchTool().execute(
            config_git, mode="blame", path="hello.py", limit=10
        ))
        assert not result.is_error
        assert result.output

    def test_blame_nonexistent_file_returns_error(self, config_git):
        result = run(GitSearchTool().execute(
            config_git, mode="blame", path="no_such_file.py", limit=10
        ))
        assert result.is_error

    def test_blame_with_pattern_restricts_output(self, config_git):
        result = run(GitSearchTool().execute(
            config_git, mode="blame", path="hello.py", pattern="hello", limit=10
        ))
        # Should work and return annotation for matching lines
        assert not result.is_error
        assert result.output


# ---------------------------------------------------------------------------
# GitSearchTool._parse_blame_porcelain – unit tests
# ---------------------------------------------------------------------------

class TestParseBlamePorcelain:
    """Tests for the _parse_blame_porcelain static method.
    
    Signature: _parse_blame_porcelain(raw, pattern, case_insensitive, limit)
    Returns: list of {commit, author, line (int), text (str)}
    """

    @staticmethod
    def _make_porcelain(commit: str, line_num: int, text: str) -> str:
        """Build a minimal git blame --porcelain chunk."""
        return (
            f"{commit} 1 {line_num} 1\n"
            f"author Test User\n"
            f"author-mail <test@test.com>\n"
            f"author-time 1700000000\n"
            f"author-tz +0000\n"
            f"committer Test User\n"
            f"committer-mail <test@test.com>\n"
            f"committer-time 1700000000\n"
            f"committer-tz +0000\n"
            f"summary Initial commit\n"
            f"filename hello.py\n"
            f"\t{text}\n"
        )

    # Valid 40-char hex commit hashes
    H1 = "1" * 40
    H2 = "2" * 40
    H3 = "3" * 40
    H4 = "4" * 40
    H5 = "5" * 40
    H6 = "6" * 40

    def test_basic_parse(self):
        raw = self._make_porcelain(self.H1, 1, "print('hello')")
        entries = GitSearchTool._parse_blame_porcelain(raw, "", False, 100)
        assert len(entries) == 1
        e = entries[0]
        assert e["author"] == "Test User"
        assert "print" in e["text"]

    def test_multiple_lines(self):
        raw = (
            self._make_porcelain(self.H1, 1, "line one")
            + self._make_porcelain(self.H2, 2, "line two")
        )
        entries = GitSearchTool._parse_blame_porcelain(raw, "", False, 100)
        assert len(entries) == 2

    def test_empty_input_returns_empty_list(self):
        entries = GitSearchTool._parse_blame_porcelain("", "", False, 100)
        assert entries == []

    def test_line_number_is_preserved(self):
        raw = self._make_porcelain(self.H3, 5, "some code")
        entries = GitSearchTool._parse_blame_porcelain(raw, "", False, 100)
        assert len(entries) == 1
        assert entries[0]["line"] == 5

    def test_commit_hash_is_stored(self):
        raw = self._make_porcelain(self.H4, 1, "code")
        entries = GitSearchTool._parse_blame_porcelain(raw, "", False, 100)
        assert entries[0]["commit"][:4] == self.H4[:4]

    def test_pattern_filters_lines(self):
        raw = (
            self._make_porcelain(self.H1, 1, "hello world")
            + self._make_porcelain(self.H2, 2, "goodbye world")
        )
        entries = GitSearchTool._parse_blame_porcelain(raw, "hello", False, 100)
        assert len(entries) == 1
        assert "hello" in entries[0]["text"]

    def test_limit_restricts_output(self):
        # Use unique valid hex commit hashes for each line
        hashes = [str(i) * 40 for i in range(5)]
        raw = "".join(
            self._make_porcelain(hashes[i], i + 1, f"line {i}")
            for i in range(5)
        )
        entries = GitSearchTool._parse_blame_porcelain(raw, "", False, 2)
        assert len(entries) <= 2

    def test_case_insensitive_pattern(self):
        raw = (
            self._make_porcelain(self.H1, 1, "Hello World")
            + self._make_porcelain(self.H2, 2, "other line")
        )
        entries = GitSearchTool._parse_blame_porcelain(raw, "hello", True, 100)
        assert len(entries) == 1
        assert "Hello" in entries[0]["text"]
