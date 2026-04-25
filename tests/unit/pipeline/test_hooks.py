"""Tests for harness.pipeline.hooks — pure-function helpers and hook constructors.

NOTE: ImportSmokeHook.run() is a protected method (safety net for the cycle
runner) and is NOT tested here per project hard rules.
"""
from __future__ import annotations


import pytest

from harness.core.config import HarnessConfig
from harness.pipeline.hooks import (
    GitCommitHook,
    HookResult,
    ImportSmokeHook,
    PytestHook,
    StaticCheckHook,
    SyntaxCheckHook,
    build_hooks,
)
from harness.pipeline.phase import PhaseConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _phase_config(**kwargs) -> PhaseConfig:
    """Minimal PhaseConfig factory."""
    defaults = dict(
        name="test_phase",
        index=0,
        system_prompt="sp",
        falsifiable_criterion="fc",
        glob_patterns=[],
    )
    defaults.update(kwargs)
    return PhaseConfig(**defaults)


# ---------------------------------------------------------------------------
# HookResult dataclass
# ---------------------------------------------------------------------------

class TestHookResult:
    """Tests for the HookResult dataclass."""

    def test_passed_true(self):
        r = HookResult(passed=True, output="ok")
        assert r.passed is True
        assert r.output == "ok"
        assert r.errors == ""

    def test_passed_false_with_errors(self):
        r = HookResult(passed=False, output="", errors="syntax error in foo.py")
        assert r.passed is False
        assert r.errors == "syntax error in foo.py"

    def test_default_errors_is_empty(self):
        r = HookResult(passed=True, output="fine")
        assert r.errors == ""

    def test_output_field_required(self):
        """HookResult requires an output argument."""
        r = HookResult(passed=True, output="")
        assert r.output == ""


# ---------------------------------------------------------------------------
# Hook constructors & attributes
# ---------------------------------------------------------------------------

class TestSyntaxCheckHook:
    def test_name(self):
        h = SyntaxCheckHook(["**/*.py"])
        assert h.name == "syntax_check"

    def test_gates_commit(self):
        h = SyntaxCheckHook(["**/*.py"])
        assert h.gates_commit is True

    def test_patterns_stored(self):
        patterns = ["src/**/*.py", "tests/**/*.py"]
        h = SyntaxCheckHook(patterns)
        assert h.patterns == patterns

    def test_default_patterns(self):
        h = SyntaxCheckHook()
        assert h.patterns == ["**/*.py"]


class TestStaticCheckHook:
    def test_name(self):
        h = StaticCheckHook()
        assert h.name == "static_check"

    def test_gates_commit(self):
        h = StaticCheckHook()
        assert h.gates_commit is True


class TestPytestHook:
    def test_name(self):
        h = PytestHook("tests/")
        assert h.name == "pytest"

    def test_gates_commit_false(self):
        """PytestHook does NOT gate commits by default."""
        h = PytestHook("tests/")
        assert h.gates_commit is False

    def test_test_path_stored(self):
        h = PytestHook("tests/unit/")
        assert h.test_path == "tests/unit/"


class TestGitCommitHook:
    def test_repos_stored(self):
        h = GitCommitHook(repos=[".", "./sub"])
        assert h.repos == [".", "./sub"]

    def test_rich_metadata_flag(self):
        h = GitCommitHook(repos=["."], rich_metadata=True)
        assert h.rich_metadata is True

    def test_rich_metadata_default_false(self):
        h = GitCommitHook(repos=["."])
        assert h.rich_metadata is False


class TestImportSmokeHook:
    def test_modules_stored(self):
        h = ImportSmokeHook(modules=["harness", "harness.core"])
        assert h.modules == ["harness", "harness.core"]

    def test_smoke_calls_stored(self):
        h = ImportSmokeHook(modules=[], smoke_calls=["import os"])
        assert h.smoke_calls == ["import os"]

    def test_default_smoke_calls_empty(self):
        """smoke_calls defaults to empty list."""
        h = ImportSmokeHook(modules=[])
        assert h.smoke_calls == []


# ---------------------------------------------------------------------------
# build_hooks
# ---------------------------------------------------------------------------

class TestBuildHooks:
    """Tests for build_hooks(phase_config) — verifies correct hook selection."""

    def test_no_hooks_when_nothing_configured(self):
        """Default PhaseConfig produces no hooks."""
        pc = _phase_config()
        assert build_hooks(pc) == []

    def test_syntax_check_hook_added(self):
        pc = _phase_config(syntax_check_patterns=["**/*.py"])
        hooks = build_hooks(pc)
        names = [h.name for h in hooks]
        assert "syntax_check" in names

    def test_syntax_check_hook_type(self):
        pc = _phase_config(syntax_check_patterns=["src/**/*.py"])
        hooks = build_hooks(pc)
        syntax_hooks = [h for h in hooks if isinstance(h, SyntaxCheckHook)]
        assert len(syntax_hooks) == 1

    def test_pytest_hook_added_when_run_tests(self):
        pc = _phase_config(run_tests=True, test_path="tests/")
        hooks = build_hooks(pc)
        names = [h.name for h in hooks]
        assert "pytest" in names

    def test_pytest_hook_not_added_without_run_tests(self):
        pc = _phase_config(run_tests=False, test_path="tests/")
        hooks = build_hooks(pc)
        names = [h.name for h in hooks]
        assert "pytest" not in names

    def test_commit_adds_static_and_git_hooks(self):
        """commit_on_success=True always includes StaticCheckHook and GitCommitHook."""
        pc = _phase_config(commit_on_success=True, commit_repos=["."])
        hooks = build_hooks(pc)
        names = [h.name for h in hooks]
        assert "static_check" in names
        assert any(isinstance(h, GitCommitHook) for h in hooks)

    def test_commit_false_no_git_hook(self):
        pc = _phase_config(commit_on_success=False)
        hooks = build_hooks(pc)
        assert not any(isinstance(h, GitCommitHook) for h in hooks)

    def test_import_smoke_hook_added(self):
        pc = _phase_config(import_smoke_modules=["harness.core"])
        hooks = build_hooks(pc)
        assert any(isinstance(h, ImportSmokeHook) for h in hooks)

    def test_import_smoke_not_added_without_modules(self):
        pc = _phase_config(import_smoke_modules=[])
        hooks = build_hooks(pc)
        assert not any(isinstance(h, ImportSmokeHook) for h in hooks)

    def test_all_hooks_together(self):
        """All hooks are added when all options enabled."""
        pc = _phase_config(
            syntax_check_patterns=["**/*.py"],
            run_tests=True, test_path="tests/",
            import_smoke_modules=["harness"],
            commit_on_success=True, commit_repos=["."],
        )
        hooks = build_hooks(pc)
        names = [h.name for h in hooks]
        assert "syntax_check" in names
        assert "pytest" in names
        assert "static_check" in names
        assert any(isinstance(h, GitCommitHook) for h in hooks)
        assert any(isinstance(h, ImportSmokeHook) for h in hooks)

    def test_returns_list(self):
        pc = _phase_config()
        assert isinstance(build_hooks(pc), list)


# ---------------------------------------------------------------------------
# SyntaxCheckHook.run() — integration (uses tmpdir)
# ---------------------------------------------------------------------------

class TestSyntaxCheckHookRun:
    """Integration tests for SyntaxCheckHook.run()."""

    @pytest.mark.asyncio
    async def test_passes_on_valid_python(self, tmp_path):
        (tmp_path / "good.py").write_text("x = 1\n")
        config = HarnessConfig(workspace=str(tmp_path))
        hook = SyntaxCheckHook(["**/*.py"])
        result = await hook.run(config, {})
        assert result.passed is True
        assert result.errors == ""

    @pytest.mark.asyncio
    async def test_fails_on_syntax_error(self, tmp_path):
        (tmp_path / "bad.py").write_text("def broken(\n")
        config = HarnessConfig(workspace=str(tmp_path))
        hook = SyntaxCheckHook(["**/*.py"])
        result = await hook.run(config, {})
        assert result.passed is False
        assert result.errors

    @pytest.mark.asyncio
    async def test_passes_on_empty_directory(self, tmp_path):
        """No .py files → trivially passes."""
        config = HarnessConfig(workspace=str(tmp_path))
        hook = SyntaxCheckHook(["**/*.py"])
        result = await hook.run(config, {})
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_multiple_files_one_bad(self, tmp_path):
        """One bad file among good files → overall failure."""
        (tmp_path / "good.py").write_text("y = 2\n")
        (tmp_path / "bad.py").write_text("def broken(\n")
        config = HarnessConfig(workspace=str(tmp_path))
        hook = SyntaxCheckHook(["**/*.py"])
        result = await hook.run(config, {})
        assert result.passed is False


# ---------------------------------------------------------------------------
# StaticCheckHook.run() — integration (uses tmpdir)
# ---------------------------------------------------------------------------

class TestStaticCheckHookRun:
    """Integration tests for StaticCheckHook.run()."""

    @pytest.mark.asyncio
    async def test_passes_on_clean_file(self, tmp_path):
        (tmp_path / "clean.py").write_text("x = 1\n")
        config = HarnessConfig(workspace=str(tmp_path))
        context = {"files_changed": ["clean.py"]}
        hook = StaticCheckHook()
        result = await hook.run(config, context)
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_fails_on_unused_import(self, tmp_path):
        (tmp_path / "unused.py").write_text("import os\nimport sys\nx = 1\n")
        config = HarnessConfig(workspace=str(tmp_path))
        context = {"files_changed": ["unused.py"]}
        hook = StaticCheckHook()
        result = await hook.run(config, context)
        assert result.passed is False
        assert "F401" in result.errors

    @pytest.mark.asyncio
    async def test_no_files_changed_passes(self, tmp_path):
        """With no changed files, nothing to check → passes."""
        config = HarnessConfig(workspace=str(tmp_path))
        context = {"files_changed": []}
        hook = StaticCheckHook()
        result = await hook.run(config, context)
        assert result.passed is True
