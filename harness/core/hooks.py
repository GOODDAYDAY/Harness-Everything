"""Verification hooks — pluggable post-execution checks."""

from __future__ import annotations

import asyncio
import glob as glob_mod
import os
import py_compile
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.core.config import HarnessConfig


@dataclass
class HookResult:
    """Outcome of a verification hook."""

    passed: bool
    output: str
    errors: str = ""


class VerificationHook(ABC):
    """Base class for post-execution verification hooks."""

    name: str
    # When True, failure of this hook suppresses all subsequent hook
    # executions in the same phase. Leave False for advisory checks whose
    # failure should still allow the commit (e.g. pytest when the phase's
    # job is to add new, initially-failing tests).
    gates_commit: bool = False

    @abstractmethod
    async def run(self, config: HarnessConfig, context: dict[str, Any]) -> HookResult:
        """Run the hook.

        ``context`` carries phase/round metadata and may include keys like
        ``"inner_dir"``, ``"phase"``, ``"outer"``, etc.
        """


class SyntaxCheckHook(VerificationHook):
    """Run ``py_compile`` on files matching configured glob patterns."""

    name = "syntax_check"
    gates_commit = True

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


class ImportSmokeHook(VerificationHook):
    """Verify a fresh subprocess can import the configured modules.

    Cheap, deterministic, offline — catches ImportError/SyntaxError introduced
    by the preceding phase before the change is committed. Intended for
    self-improvement runs where the harness edits its own source and a
    broken module would prevent the next round from running at all.

    Runs in a subprocess so the parent's already-imported modules don't mask
    a freshly-broken import.
    """

    name = "import_smoke"
    gates_commit = True

    def __init__(
        self,
        modules: list[str] | None = None,
        smoke_calls: list[str] | None = None,
        timeout: int = 30,
    ) -> None:
        # Default covers the self-improvement case; override via config for
        # projects with different import entry points.
        self.modules = modules or [
            "harness.core.config",
            "harness.agent",
            "harness.tools",
        ]
        # smoke_calls are arbitrary Python statements executed after the
        # imports. They are how we catch runtime-only NameErrors that hide
        # inside function bodies (see 2026-04-19 validate_calibration_anchors
        # incident: bare `import` succeeded, but the next evaluator call blew
        # up because a referenced helper was never defined). Each string must
        # use fully qualified names — e.g. "harness.evaluation.dual_evaluator.
        # validate_evaluator_output('DELTA\\nSCORE: 5', 'basic', 'debate')".
        self.smoke_calls = smoke_calls or []
        self.timeout = timeout

    async def run(self, config: HarnessConfig, context: dict[str, Any]) -> HookResult:
        if not self.modules and not self.smoke_calls:
            log.warning("import_smoke: no modules or calls configured, skipping")
            return HookResult(passed=True, output="(no modules to check)", errors="")
        import_stmts = "\n".join(f"import {m}" for m in self.modules)
        call_stmts = "\n".join(self.smoke_calls)
        # build_registry() exercises tool class loading, which is the most
        # common place a refactor breaks imports without hitting top-level
        # module imports directly.  Skip when running against an external
        # project (modules list was overridden, harness may not be importable).
        harness_src_root = str(Path(__file__).resolve().parents[2])
        ws_is_harness = str(Path(config.workspace).resolve()) == harness_src_root
        registry_check = (
            "from harness.tools import build_registry\n"
            "build_registry()\n"
        ) if ws_is_harness else ""
        script = (
            f"{import_stmts}\n"
            f"{registry_check}"
            f"{call_stmts}\n"
        )
        # Use sys.executable so the smoke runs under the same interpreter
        # (and thus the same venv / installed packages) as the harness itself.
        # Hardcoding "python" fails inside venv-based deployments where the
        # bare name is not on PATH.
        # The harness package may not be pip-installed; ensure the source
        # root (parent of the top-level ``harness/`` package) is on
        # PYTHONPATH so the subprocess can import it even when cwd is the
        # target workspace (which won't contain ``harness/``).
        env = {**os.environ}
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{harness_src_root}:{existing}" if existing else harness_src_root

        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-c", script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=config.workspace,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout
            )
        except asyncio.TimeoutError:
            if proc is not None:
                proc.kill()
                await proc.wait()
            return HookResult(
                passed=False, output="", errors="import smoke timed out"
            )
        except FileNotFoundError:
            return HookResult(
                passed=False, output="",
                errors=f"interpreter not found: {sys.executable}",
            )
        except Exception as e:
            return HookResult(passed=False, output="", errors=str(e))

        if proc.returncode == 0:
            return HookResult(
                passed=True,
                output=f"import smoke OK ({len(self.modules)} modules)",
            )
        err = stderr.decode(errors="replace") + stdout.decode(errors="replace")
        return HookResult(passed=False, output="", errors=err[:2000])


class StaticCheckHook(VerificationHook):
    """Run ruff (preferred) or pyflakes over changed files.

    Targets static correctness errors that slip past ``py_compile`` but blow
    up at runtime: most importantly F821 ("undefined name"), which caught
    the ``validate_calibration_anchors`` incident 2026-04-19 where the LLM
    wrote a call to a helper it never defined. We include F811 (redefined
    name) and F401 (unused import) as cheap extras — both reliable signals
    of a merge artefact or a hallucinated import.

    Behaviour with missing tools:
      * Neither ``ruff`` nor ``pyflakes`` importable -> log a WARNING, pass
        the hook. The build does not block. This preserves local-dev
        ergonomics and avoids a chicken-and-egg on fresh installs.
      * Tool present and reports errors -> fail the hook (gates the commit).

    Scope: checks only the files changed in this phase, sourced from the
    hook context (``context["files_changed"]``). Falls back to a no-op if
    no changed files are provided — we don't want to re-scan the whole
    tree on every commit.
    """

    name = "static_check"
    gates_commit = True

    # F821 is the critical one. F811 and F401 are cheap, high-signal extras.
    RUFF_RULES = "F821,F811,F401"

    def __init__(self, timeout: int = 20) -> None:
        self.timeout = timeout

    async def _run_tool(
        self, argv: list[str], cwd: str,
    ) -> tuple[int | None, str]:
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout
            )
        except asyncio.TimeoutError:
            if proc is not None:
                proc.kill()
                await proc.wait()
            return None, "static check timed out"
        except FileNotFoundError:
            return None, "tool not found"
        except Exception as e:
            return None, str(e)
        combined = stdout.decode(errors="replace") + stderr.decode(errors="replace")
        return proc.returncode, combined

    async def run(self, config: HarnessConfig, context: dict[str, Any]) -> HookResult:
        changed: list[str] = [
            f for f in context.get("files_changed", [])
            if isinstance(f, str) and f.endswith(".py")
        ]
        if not changed:
            return HookResult(
                passed=True, output="static_check: no python files changed"
            )

        # Probe ruff first (faster, richer), fall back to pyflakes. Use
        # sys.executable -m so we hit the same interpreter / venv as the
        # harness itself — avoids "works on my machine" with a global ruff.
        probe_rc, _ = await self._run_tool(
            [sys.executable, "-m", "ruff", "--version"], config.workspace
        )
        if probe_rc == 0:
            rc, output = await self._run_tool(
                [
                    sys.executable, "-m", "ruff", "check",
                    "--select", self.RUFF_RULES,
                    "--no-cache",
                    *changed,
                ],
                config.workspace,
            )
            if rc == 0:
                return HookResult(
                    passed=True,
                    output=f"static_check (ruff {self.RUFF_RULES}): clean on {len(changed)} file(s)",
                )
            return HookResult(
                passed=False, output="",
                errors=f"ruff reported issues:\n{output[:1500]}",
            )

        probe_rc, _ = await self._run_tool(
            [sys.executable, "-m", "pyflakes", "--version"], config.workspace
        )
        if probe_rc == 0:
            rc, output = await self._run_tool(
                [sys.executable, "-m", "pyflakes", *changed],
                config.workspace,
            )
            if rc == 0:
                return HookResult(
                    passed=True,
                    output=f"static_check (pyflakes): clean on {len(changed)} file(s)",
                )
            return HookResult(
                passed=False, output="",
                errors=f"pyflakes reported issues:\n{output[:1500]}",
            )

        # Neither tool available. Warn but don't block — see class docstring.
        log.warning("static_check: neither ruff nor pyflakes available, checks SKIPPED")
        return HookResult(
            passed=True,
            output=(
                "static_check: neither ruff nor pyflakes is installed under "
                f"{sys.executable}; static checks SKIPPED. Install ruff to "
                "restore this guard (pip install ruff)."
            ),
            errors="SKIPPED: no linting tool available",
        )


class PytestHook(VerificationHook):
    """Run ``pytest`` on a configured test directory."""

    name = "pytest"

    def __init__(self, test_path: str = "tests/", timeout: int = 120) -> None:
        self.test_path = test_path
        self.timeout = timeout

    async def run(self, config: HarnessConfig, context: dict[str, Any]) -> HookResult:
        # Declare proc before the try so it is always in scope in the except
        # block, even when create_subprocess_exec itself raises.
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pytest", self.test_path, "-v", "--tb=short",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=config.workspace,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout
            )
        except asyncio.TimeoutError:
            # Kill the child process and reap it so the OS does not keep a
            # zombie entry and asyncio does not warn about an unclosed transport.
            if proc is not None:
                proc.kill()
                await proc.wait()
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

    def __init__(
        self, repos: list[str] | None = None, rich_metadata: bool = False
    ) -> None:
        self.repos = repos or []
        self.rich_metadata = rich_metadata

    async def run(self, config: HarnessConfig, context: dict[str, Any]) -> HookResult:
        outer = context.get("outer", 0)
        phase_name = context.get("phase_name", "unknown")

        if self.rich_metadata:
            score = context.get("best_score", 0.0)
            changes = context.get("changes_summary", "")
            files = context.get("files_changed", [])
            basic = context.get("basic_critique", "")
            diffusion = context.get("diffusion_critique", "")
            tools = context.get("tool_summary", "")
            n_inner = context.get("inner_rounds_run", 0)
            all_scores = context.get("all_scores", [])

            commit_msg = f"[harness] R{outer + 1} {phase_name} [score={score:.1f}]"
            body_parts: list[str] = []
            if changes:
                body_parts.append(f"Summary: {changes}")
            if files:
                body_parts.append("Files modified:")
                for f in files[:10]:
                    body_parts.append(f"  - {f}")
                if len(files) > 10:
                    body_parts.append(f"  ... and {len(files) - 10} more")
            if n_inner > 0:
                scores_str = ", ".join(f"{s:.1f}" for s in all_scores)
                body_parts.append(f"Inner rounds: {n_inner} (scores: {scores_str})")
            if tools:
                body_parts.append(f"Tool usage: {tools}")
            if basic:
                body_parts.append(f"Basic evaluator: {basic[:300]}")
            if diffusion:
                body_parts.append(f"Diffusion evaluator: {diffusion[:300]}")
            if body_parts:
                commit_msg += "\n\n" + "\n".join(body_parts)
        else:
            commit_msg = f"[harness] R{outer + 1} {phase_name}"

        results: list[str] = []
        all_passed = True

        for repo in self.repos:
            repo_path = Path(config.workspace) / repo
            if not repo_path.is_dir():
                results.append(f"{repo}: directory not found, skipped")
                continue

            try:
                # git add -A (async)
                add_proc = await asyncio.create_subprocess_exec(
                    "git", "add", "-A",
                    cwd=repo_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(add_proc.communicate(), timeout=30)
                if add_proc.returncode != 0:
                    results.append(f"{repo}: git add failed: {stderr.decode().strip()}")
                    all_passed = False
                    continue

                # git commit (async)
                commit_proc = await asyncio.create_subprocess_exec(
                    "git", "commit", "--allow-empty", "-m", commit_msg,
                    cwd=repo_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(commit_proc.communicate(), timeout=30)
                if commit_proc.returncode == 0:
                    results.append(f"{repo}: committed")
                else:
                    results.append(f"{repo}: commit failed: {stderr.decode().strip()}")
                    all_passed = False
            except asyncio.TimeoutError:
                results.append(f"{repo}: git command timed out")
                all_passed = False
            except Exception as e:
                results.append(f"{repo}: error: {e}")
                all_passed = False

        output = "\n".join(results)
        return HookResult(passed=all_passed, output=output)
