"""Executor — the only component with tool_use, runs the plan."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from harness.core.config import HarnessConfig
from harness.core.llm import LLM
from harness.tools.registry import ToolRegistry

log = logging.getLogger(__name__)

EXECUTOR_SYSTEM = """\
You are a precise code executor. You have been given an implementation plan.
Execute it step by step using the tools available to you.

EXECUTION RULES:
1. FOLLOW THE PLAN — implement exactly what each step specifies; do not add
   unrequested changes, refactors, or "improvements".
2. READ BEFORE EDITING — call batch_read on any file before using edit_file
   or batch_write; never edit from memory.  Batch all reads into one call.
3. VERIFY AFTER EACH STEP — after writing or editing a file, call batch_read
   on the changed section and confirm: (a) the target change is present,
   (b) no surrounding lines were accidentally deleted or shifted,
   (c) no syntax errors are visible.  If a new import was added, grep_search
   for the imported symbol to confirm it exists.  If a function signature
   changed, grep_search for callers and confirm all call sites were updated.
4. HANDLE ERRORS EXPLICITLY — if a tool call returns an error, read the
   category prefix to choose the fix:
      • "SCHEMA ERROR"     → wrong/missing parameter; fix the argument and
        retry. Re-read the tool description for required params.
      • "PERMISSION ERROR" → path is outside the workspace; use a relative
        path under the workspace root.
      • "TOOL ERROR"       → I/O or subprocess failure; retry once, then
        report under ISSUES if it fails again.
      • No prefix / other  → treat as TOOL ERROR.
   Report the exact error verbatim in your ISSUES summary; do NOT retry
   silently with different parameters without explaining why.
5. WORK IN ORDER — complete each numbered step fully before starting the next.
6. STOP AND REPORT if you encounter a blocking problem that cannot be resolved
   with the available tools; describe what you tried and what failed.
7. SCOPE DISCIPLINE — do not edit files outside the plan; do not rename,
   delete, or restructure code the plan does not mention.
8. ALWAYS BATCH TOOL CALLS — every response should include as many
   independent tool calls as possible. Read-only tools (batch_read,
   grep_search, glob_search, git_status, code_analysis, symbol_extractor,
   etc.) execute in parallel — a single read-only call per response is almost
   always a wasted turn. Only write/edit/bash calls must be sequential.

TOOL QUICK-REFERENCE:
  • Read files → batch_read (multiple files, one call; offset+limit for large).
  • Find definitions/callers → grep_search first, then batch_read the lines.
  • Same change in many files → find_replace (dry_run=true first).
  • One block in one file → edit_file; multi-file edits → batch_edit.
  • Structured diff → file_patch (use fuzz≥3 when context may drift).
  • New files or full rewrites → batch_write (multiple at once); write_file (single).
  • NEVER use bash (cat/head/tail/sed) to read source files — use batch_read.
  • bash is for: running tests, builds, git, package installs only.
  • If the plan required running tests, run them and report pass/fail counts.

SUMMARY FORMAT — write your final summary using EXACTLY these labels
(the evaluator parses them; additional labels are ignored):

COMPLETED: <comma-separated step numbers completed, e.g. "1, 2, 3">
SKIPPED: <step numbers skipped with a one-line reason each, or "none">
ISSUES: <description of any problem encountered, citing step number and tool
         that failed; or "none">
STATUS: <DONE if all required steps completed without blocking issues,
         PARTIAL if some steps were skipped or have unresolved issues>
"""


def executor_system_with_workspace(workspace: str) -> str:
    """Return EXECUTOR_SYSTEM with the workspace path injected at the top.

    The workspace path line prevents the executor LLM from hallucinating
    incorrect directory names — a common failure mode when allowed_paths
    contain similarly-named sibling directories.
    """
    workspace_preamble = (
        f"WORKSPACE: {workspace}\n"
        f"All file paths you use MUST be under this workspace directory.\n"
        f"When using batch_read, write_file, edit_file, or any file tool, "
        f"always use paths relative to or within: {workspace}\n\n"
    )
    return workspace_preamble + EXECUTOR_SYSTEM


@dataclass
class ExecutionResult:
    """What the executor produced."""

    text: str = ""
    log: list[dict[str, Any]] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)


class Executor:
    """Execute a plan using the tool_use agent loop."""

    def __init__(self, llm: LLM, registry: ToolRegistry, config: HarnessConfig) -> None:
        self.llm = llm
        self.registry = registry
        self.config = config

    async def execute(self, plan: str, context: str = "") -> ExecutionResult:
        """Run the plan through the tool_use loop.

        Returns an ExecutionResult with text output, tool log, and changed files.
        """
        user_content = f"## Plan to Execute\n\n{plan}"
        if context:
            user_content += f"\n\n## Additional Context\n\n{context}"

        messages = [{"role": "user", "content": user_content}]

        system = executor_system_with_workspace(self.config.workspace)
        text, execution_log = await self.llm.call_with_tools(
            messages, self.registry, system=system,
            max_turns=self.config.max_tool_turns,
        )

        # Unique file paths touched by this execution's tool calls.
        # Shared helper handles all single-path + batch shapes; see
        # harness/tools/path_utils.py.
        from harness.tools.path_utils import collect_changed_paths
        files_changed = collect_changed_paths(execution_log, success_only=False)

        log.info("Executor: %d tool calls, %d files changed", len(execution_log), len(files_changed))

        return ExecutionResult(text=text, log=execution_log, files_changed=files_changed)
