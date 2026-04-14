"""Verification hooks — pluggable post-execution checks."""

from __future__ import annotations

import asyncio
import glob as glob_mod
import py_compile
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.config import HarnessConfig


@dataclass
class HookResult:
    """Outcome of a verification hook."""

    passed: bool
    output: str
    errors: str = ""


class VerificationHook(ABC):
    """Base class for post-execution verification hooks."""

    name: str

    @abstractmethod
    async def run(self, config: HarnessConfig, context: dict[str, Any]) -> HookResult:
        """Run the hook.

        ``context`` carries phase/round metadata and may include keys like
        ``"inner_dir"``, ``"phase"``, ``"outer"``, etc.
        """


class SyntaxCheckHook(VerificationHook):
    """Run ``py_compile`` on files matching configured glob patterns."""

    name = "syntax_check"

    def __init__(self, patterns: list[str] | None = None) -> None:
        self.patterns = patterns or ["**/*.py"]

    async def run(self, config: HarnessConfig, context: dict[str, Any]) -> HookResult:
        errors: list[str] = []
        for pattern in self.patterns:
            for path_str in glob_mod.glob(
                pattern, recursive=True, root_dir=config.workspace
            ):
                full_path = str(Path(config.workspace) / path_str)
                try:
                    py_compile.compile(full_path, doraise=True)
                except py_compile.PyCompileError as e:
                    errors.append(f"{path_str}: {e.msg}")
                except Exception:
                    pass

        if errors:
            error_text = "\n".join(errors)
            return HookResult(passed=False, output="", errors=error_text)
        return HookResult(passed=True, output="All syntax checks passed")


class PytestHook(VerificationHook):
    """Run ``pytest`` on a configured test directory."""

    name = "pytest"

    def __init__(self, test_path: str = "tests/", timeout: int = 120) -> None:
        self.test_path = test_path
        self.timeout = timeout

    async def run(self, config: HarnessConfig, context: dict[str, Any]) -> HookResult:
        try:
            proc = await asyncio.create_subprocess_exec(
                "python", "-m", "pytest", self.test_path, "-v", "--tb=short",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=config.workspace,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout
            )
        except asyncio.TimeoutError:
            return HookResult(passed=False, output="", errors="pytest timed out")
        except FileNotFoundError:
            return HookResult(passed=False, output="", errors="pytest not found")
        except Exception as e:
            return HookResult(passed=False, output="", errors=str(e))

        out = stdout.decode(errors="replace") + stderr.decode(errors="replace")
        passed = proc.returncode == 0
        return HookResult(passed=passed, output=out, errors="" if passed else out)


class GitCommitHook(VerificationHook):
    """Conditionally commit changes in configured repos."""

    name = "git_commit"

    def __init__(self, repos: list[str] | None = None) -> None:
        self.repos = repos or []

    async def run(self, config: HarnessConfig, context: dict[str, Any]) -> HookResult:
        outer = context.get("outer", 0)
        phase_name = context.get("phase_name", "unknown")
        commit_msg = f"harness: R{outer + 1} {phase_name}"

        results: list[str] = []
        all_passed = True

        for repo in self.repos:
            repo_path = Path(config.workspace) / repo
            if not repo_path.is_dir():
                results.append(f"{repo}: directory not found, skipped")
                continue

            try:
                # git add -A
                add = subprocess.run(
                    ["git", "add", "-A"],
                    cwd=repo_path, capture_output=True, text=True, timeout=30,
                )
                if add.returncode != 0:
                    results.append(f"{repo}: git add failed: {add.stderr.strip()}")
                    all_passed = False
                    continue

                # git commit
                commit = subprocess.run(
                    ["git", "commit", "--allow-empty", "-m", commit_msg],
                    cwd=repo_path, capture_output=True, text=True, timeout=30,
                )
                if commit.returncode == 0:
                    results.append(f"{repo}: committed")
                else:
                    results.append(f"{repo}: commit failed: {commit.stderr.strip()}")
                    all_passed = False
            except Exception as e:
                results.append(f"{repo}: error: {e}")
                all_passed = False

        output = "\n".join(results)
        return HookResult(passed=all_passed, output=output)


def build_hooks(phase_config: Any) -> list[VerificationHook]:
    """Build verification hooks from a PhaseConfig."""
    hooks: list[VerificationHook] = []

    if phase_config.syntax_check_patterns:
        hooks.append(SyntaxCheckHook(phase_config.syntax_check_patterns))

    if phase_config.run_tests:
        hooks.append(PytestHook(phase_config.test_path))

    if phase_config.commit_on_success and phase_config.commit_repos:
        hooks.append(GitCommitHook(phase_config.commit_repos))

    return hooks
