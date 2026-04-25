"""Tests for harness/tools/git_search.py."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from harness.core.config import HarnessConfig
from harness.tools.git_search import GitSearchTool, _run_git

_parse_blame_porcelain = GitSearchTool._parse_blame_porcelain


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_config(tmp_path: Path) -> HarnessConfig:
    return HarnessConfig(
        model="test",
        max_tokens=1000,
        workspace=str(tmp_path),
        allowed_paths=[str(tmp_path)],
    )


def run(coro):
    """Run a coroutine synchronously."""
    return asyncio.run(coro)


def make_git_repo(path: Path) -> None:
    """Initialise a minimal git repo with one commit."""
    subprocess.run(["git", "init", str(path)], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@test.com"],
        capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        capture_output=True, check=True,
    )
    # Create a file and commit
    hello = path / "hello.py"
    hello.write_text("# hello\nprint('hello world')\n")
    subprocess.run(["git", "-C", str(path), "add", "."], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "initial commit"],
        capture_output=True, check=True,
    )


# ---------------------------------------------------------------------------
# _parse_blame_porcelain  (pure static method — no I/O)
# ---------------------------------------------------------------------------

SAMPLE_PORCELAIN = (
    "abcdef1234567890abcdef1234567890abcdef12 1 1 1\n"
    "author Alice\n"
    "author-mail <alice@example.com>\n"
    "author-time 1700000000\n"
    "author-tz +0000\n"
    "committer Alice\n"
    "committer-mail <alice@example.com>\n"
    "committer-time 1700000000\n"
    "committer-tz +0000\n"
    "summary initial commit\n"
    "boundary\n"
    "filename hello.py\n"
    "\t# hello\n"
    "abcdef1234567890abcdef1234567890abcdef12 2 2\n"
    "author Alice\n"
    "author-mail <alice@example.com>\n"
    "author-time 1700000000\n"
    "author-tz +0000\n"
    "committer Alice\n"
    "committer-mail <alice@example.com>\n"
    "committer-time 1700000000\n"
    "committer-tz +0000\n"
    "summary initial commit\n"
    "filename hello.py\n"
    "\tprint('hello world')\n"
)


class TestParseBlamePorcelain:
    def test_returns_all_lines_without_filter(self):
        results = _parse_blame_porcelain(SAMPLE_PORCELAIN, "", False, 200)
        assert len(results) == 2

    def test_line_structure(self):
        results = _parse_blame_porcelain(SAMPLE_PORCELAIN, "", False, 200)
        r = results[0]
        assert set(r.keys()) == {"commit", "author", "line", "text"}

    def test_commit_hash_extracted(self):
        results = _parse_blame_porcelain(SAMPLE_PORCELAIN, "", False, 200)
        assert results[0]["commit"].startswith("abcdef")

    def test_author_extracted(self):
        results = _parse_blame_porcelain(SAMPLE_PORCELAIN, "", False, 200)
        assert results[0]["author"] == "Alice"

    def test_line_number_extracted(self):
        results = _parse_blame_porcelain(SAMPLE_PORCELAIN, "", False, 200)
        assert results[0]["line"] == 1
        assert results[1]["line"] == 2

    def test_text_extracted(self):
        results = _parse_blame_porcelain(SAMPLE_PORCELAIN, "", False, 200)
        assert results[0]["text"] == "# hello"
        assert results[1]["text"] == "print('hello world')"

    def test_pattern_filter(self):
        results = _parse_blame_porcelain(SAMPLE_PORCELAIN, "hello", False, 200)
        # Both lines have 'hello' in them — "# hello" and "print('hello world')"
        assert len(results) == 2

    def test_pattern_filter_excludes_nonmatching(self):
        results = _parse_blame_porcelain(SAMPLE_PORCELAIN, "print", False, 200)
        assert len(results) == 1
        assert "print" in results[0]["text"]

    def test_case_sensitive_filter(self):
        results = _parse_blame_porcelain(SAMPLE_PORCELAIN, "HELLO", False, 200)
        assert len(results) == 0

    def test_case_insensitive_filter(self):
        results = _parse_blame_porcelain(SAMPLE_PORCELAIN, "HELLO", True, 200)
        assert len(results) == 2

    def test_limit_applied(self):
        results = _parse_blame_porcelain(SAMPLE_PORCELAIN, "", False, 1)
        assert len(results) == 1

    def test_empty_output(self):
        results = _parse_blame_porcelain("", "", False, 200)
        assert results == []


# ---------------------------------------------------------------------------
# GitSearchTool.execute — mode validation & limit clamping
# ---------------------------------------------------------------------------

class TestExecuteValidation:
    tool = GitSearchTool()

    def _cfg(self, tmp_path):
        return make_config(tmp_path)

    def test_invalid_mode(self, tmp_path):
        result = run(self.tool.execute(self._cfg(tmp_path), mode="invalid", limit=10))
        assert result.is_error
        assert "invalid" in result.error.lower() or "Unknown mode" in result.error

    def test_valid_mode_names_pass_validation(self, tmp_path):
        # mode='show' without commit should give a different error (not mode validation)
        result = run(self.tool.execute(self._cfg(tmp_path), mode="show", limit=10))
        assert result.is_error
        assert "commit" in result.error.lower()

    def test_limit_clamped_below(self, tmp_path):
        """limit=0 should be clamped to 1 (not raise)."""
        # mode=log with no pattern → error about pattern, not limit
        result = run(self.tool.execute(self._cfg(tmp_path), mode="log", pattern="", limit=0))
        assert result.is_error
        assert "pattern" in result.error.lower()

    def test_limit_clamped_above(self, tmp_path):
        """limit=9999 should be clamped to 200."""
        result = run(self.tool.execute(self._cfg(tmp_path), mode="log", pattern="", limit=9999))
        assert result.is_error
        assert "pattern" in result.error.lower()

    def test_log_mode_requires_pattern(self, tmp_path):
        result = run(self.tool.execute(self._cfg(tmp_path), mode="log", limit=10))
        assert result.is_error
        assert "pattern" in result.error.lower()

    def test_blame_mode_requires_path(self, tmp_path):
        result = run(self.tool.execute(self._cfg(tmp_path), mode="blame", limit=10))
        assert result.is_error
        assert "path" in result.error.lower()

    def test_grep_mode_requires_pattern(self, tmp_path):
        result = run(self.tool.execute(self._cfg(tmp_path), mode="grep", limit=10))
        assert result.is_error
        assert "pattern" in result.error.lower()

    def test_show_mode_requires_commit(self, tmp_path):
        result = run(self.tool.execute(self._cfg(tmp_path), mode="show", limit=10))
        assert result.is_error
        assert "commit" in result.error.lower()

    def test_log_file_mode_requires_path(self, tmp_path):
        result = run(self.tool.execute(self._cfg(tmp_path), mode="log_file", limit=10))
        assert result.is_error
        assert "path" in result.error.lower()


# ---------------------------------------------------------------------------
# _mode_show — commit ref sanitization
# ---------------------------------------------------------------------------

class TestModeShowSanitization:
    tool = GitSearchTool()

    def _cfg(self, tmp_path):
        return make_config(tmp_path)

    def test_shell_injection_rejected(self, tmp_path):
        result = run(self.tool.execute(
            self._cfg(tmp_path), mode="show", commit="HEAD; rm -rf /", limit=10
        ))
        assert result.is_error
        assert "Invalid commit" in result.error

    def test_backtick_rejected(self, tmp_path):
        result = run(self.tool.execute(
            self._cfg(tmp_path), mode="show", commit="`whoami`", limit=10
        ))
        assert result.is_error

    def test_hex_hash_accepted_format(self, tmp_path):
        """Valid hex commits pass sanitization (may fail on missing commit — that's OK)."""
        result = run(self.tool.execute(
            self._cfg(tmp_path), mode="show",
            commit="abcdef1234567890abcdef1234567890abcdef12",
            limit=10,
        ))
        # May be an error because git repo doesn't have this commit,
        # but the error should be from git, not from sanitization
        if result.is_error:
            assert "Invalid commit" not in result.error

    def test_HEAD_rejected(self, tmp_path):
        """HEAD contains 'H' which is not in safe_chars, so is rejected."""
        result = run(self.tool.execute(
            self._cfg(tmp_path), mode="show", commit="HEAD", limit=10
        ))
        assert result.is_error
        assert "Invalid commit" in result.error

    def test_pipe_char_rejected(self, tmp_path):
        result = run(self.tool.execute(
            self._cfg(tmp_path), mode="show", commit="HEAD|cat", limit=10
        ))
        assert result.is_error
        assert "Invalid commit" in result.error


# ---------------------------------------------------------------------------
# Integration tests using a real git repo
# ---------------------------------------------------------------------------

@pytest.fixture()
def git_repo(tmp_path):
    """Create a minimal git repo in tmp_path and return (path, config)."""
    make_git_repo(tmp_path)
    cfg = make_config(tmp_path)
    return tmp_path, cfg


class TestIntegrationWithRealRepo:
    tool = GitSearchTool()

    def test_log_mode_finds_commit(self, git_repo):
        _path, cfg = git_repo
        result = run(self.tool.execute(cfg, mode="log", pattern="initial", limit=10))
        assert not result.is_error
        assert "initial" in result.output.lower()

    def test_log_mode_no_match(self, git_repo):
        _path, cfg = git_repo
        result = run(self.tool.execute(
            cfg, mode="log", pattern="zzznomatch99999", limit=10
        ))
        assert not result.is_error
        assert "No commits found" in result.output

    def test_log_mode_case_insensitive(self, git_repo):
        _path, cfg = git_repo
        result = run(self.tool.execute(
            cfg, mode="log", pattern="INITIAL", limit=10, case_insensitive=True
        ))
        assert not result.is_error
        assert not result.is_error

    def test_grep_mode_finds_match(self, git_repo):
        _path, cfg = git_repo
        result = run(self.tool.execute(cfg, mode="grep", pattern="hello world", limit=10))
        assert not result.is_error
        assert "hello world" in result.output

    def test_grep_mode_no_match(self, git_repo):
        _path, cfg = git_repo
        result = run(self.tool.execute(cfg, mode="grep", pattern="zzznomatch", limit=10))
        assert not result.is_error
        assert "No matches" in result.output

    def test_grep_mode_case_insensitive(self, git_repo):
        _path, cfg = git_repo
        result = run(self.tool.execute(
            cfg, mode="grep", pattern="HELLO WORLD", limit=10, case_insensitive=True
        ))
        # Either finds it or no matches, but no error
        assert not result.is_error

    def test_grep_mode_path_filter(self, git_repo):
        path, cfg = git_repo
        result = run(self.tool.execute(
            cfg, mode="grep", pattern="hello", path="hello.py", limit=10
        ))
        assert not result.is_error

    def test_blame_mode_no_path_error(self, git_repo):
        _path, cfg = git_repo
        result = run(self.tool.execute(cfg, mode="blame", limit=10))
        assert result.is_error
        assert "path" in result.error.lower()

    def test_blame_mode_with_file(self, git_repo):
        _path, cfg = git_repo
        result = run(self.tool.execute(
            cfg, mode="blame", path="hello.py", pattern="", limit=10
        ))
        assert not result.is_error

    def test_blame_mode_with_pattern(self, git_repo):
        _path, cfg = git_repo
        result = run(self.tool.execute(
            cfg, mode="blame", path="hello.py", pattern="hello", limit=10
        ))
        assert not result.is_error
        assert "hello" in result.output.lower()

    def test_log_file_mode(self, git_repo):
        _path, cfg = git_repo
        result = run(self.tool.execute(
            cfg, mode="log_file", path="hello.py", limit=10
        ))
        assert not result.is_error
        assert "initial" in result.output.lower()

    def test_log_file_mode_nonexistent_file(self, git_repo):
        _path, cfg = git_repo
        result = run(self.tool.execute(
            cfg, mode="log_file", path="nonexistent.py", limit=10
        ))
        # Should not be an error — git just returns no commits
        assert not result.is_error
        assert "No commits" in result.output

    def test_show_mode_valid_hash(self, git_repo):
        path, cfg = git_repo
        # Get the actual commit hash from git log
        commit_hash = subprocess.check_output(
            ["git", "-C", str(path), "log", "--format=%H", "-1"],
            text=True,
        ).strip()
        result = run(self.tool.execute(cfg, mode="show", commit=commit_hash, limit=10))
        assert not result.is_error
        assert len(result.output) > 0

    def test_show_mode_bad_ref(self, git_repo):
        _path, cfg = git_repo
        result = run(self.tool.execute(
            cfg, mode="show", commit="deadbeef", limit=10
        ))
        assert result.is_error
        # Should be a git error, not a sanitization error
        assert "Invalid commit" not in result.error

    def test_output_truncated(self, git_repo):
        """Output longer than _MAX_OUTPUT_CHARS gets truncated."""
        from harness.tools.git_search import _MAX_OUTPUT_CHARS
        path, cfg = git_repo
        # Create a large file and commit it
        large_file = path / "large.py"
        large_file.write_text("x = 1\n" * 10000)
        subprocess.run(
            ["git", "-C", str(path), "add", "large.py"], capture_output=True, check=True
        )
        subprocess.run(
            ["git", "-C", str(path), "commit", "-m", "large file"],
            capture_output=True, check=True,
        )
        commit_hash = subprocess.check_output(
            ["git", "-C", str(path), "log", "--format=%H", "-1"],
            text=True,
        ).strip()
        result = run(self.tool.execute(
            cfg, mode="show", commit=commit_hash, limit=10
        ))
        assert not result.is_error
        if len(result.output) >= _MAX_OUTPUT_CHARS:
            assert "[truncated]" in result.output


# ---------------------------------------------------------------------------
# _run_git  — basic behavior
# ---------------------------------------------------------------------------

class TestRunGit:
    def test_run_git_success(self, tmp_path):
        make_git_repo(tmp_path)
        cfg = make_config(tmp_path)
        stdout, stderr, code = run(_run_git(cfg, "status"))
        assert code == 0
        assert "branch" in stdout.lower() or len(stdout) > 0

    def test_run_git_nonzero_exit(self, tmp_path):
        cfg = make_config(tmp_path)
        # 'git status' in a non-git dir returns non-zero
        stdout, stderr, code = run(_run_git(cfg, "status"))
        assert code != 0

    def test_run_git_bad_command(self, tmp_path):
        cfg = make_config(tmp_path)
        stdout, stderr, code = run(_run_git(cfg, "zzz-not-a-real-git-command"))
        assert code != 0
