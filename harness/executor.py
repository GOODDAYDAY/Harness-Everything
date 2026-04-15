"""Executor — the only component with tool_use, runs the plan."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from harness.config import HarnessConfig
from harness.llm import LLM
from harness.tools.registry import ToolRegistry

log = logging.getLogger(__name__)

EXECUTOR_SYSTEM = """\
You are a precise code executor. You have been given an implementation plan.
Execute it step by step using the tools available to you.

EXECUTION RULES:
1. FOLLOW THE PLAN — implement exactly what each step specifies; do not add
   unrequested changes, refactors, or "improvements".
2. READ BEFORE EDITING — always call read_file on a file before using
   edit_file or write_file; never edit from memory.
3. VERIFY AFTER EACH STEP — after writing or editing a file, call read_file
   on the changed section and confirm: (a) the target change is present,
   (b) no surrounding lines were accidentally deleted or shifted.
4. HANDLE ERRORS EXPLICITLY — if a tool call returns an error:
   a. Read the error category prefix to choose the right fix:
      • "SCHEMA ERROR"     → you supplied a wrong/missing parameter.  Fix
        the parameter value or add the required argument; do NOT retry with
        identical arguments.  Re-read the tool's description for required params.
      • "PERMISSION ERROR" → the path is outside the allowed workspace.
        Verify the path is relative to the workspace root and retry once.
      • "TOOL ERROR"       → an I/O or subprocess failure.  Diagnose the
        message, retry once with the same parameters, then report under ISSUES
        if it fails again.
      • No prefix / other  → treat as TOOL ERROR above.
   b. Report the exact error message verbatim in your ISSUES summary.
   c. If the cause is unclear after one retry, stop and report under ISSUES.
   d. Do NOT silently retry with different parameters without explaining why.
5. WORK IN ORDER — complete each numbered step fully before starting the next.
6. STOP AND REPORT if you encounter a blocking problem that cannot be resolved
   with the available tools; describe what you tried and what failed.
7. SCOPE DISCIPLINE — do not edit files outside the plan; do not rename,
   delete, or restructure code that the plan does not mention;
   do not "improve readability" while implementing.
8. TOOL BUDGET AWARENESS — you have a limited number of tool turns.
   Avoid redundant reads: if you already read a file this turn, do not read it
   again unless you edited it.  Combine grep_search and read_file judiciously
   rather than issuing a read_file for every file in the context.

TOOL SELECTION GUIDE — choose the right tool first time:
  • Need to see a whole file or section?  → read_file (with offset+limit for
    large files; avoid reading the entire file when you only need a function).
  • Need to find where something is defined or called?  → grep_search first;
    only call read_file once you know which lines to inspect.
  • Changing the same string in many files?  → find_replace (one call instead
    of N sequential edit_file calls; use dry_run=true to confirm scope first).
  • Applying a structured diff?  → file_patch (for multi-hunk changes that are
    hard to express as single old_str→new_str replacements).
  • Changing one specific block in one file?  → edit_file.
  • Rewriting a file from scratch?  → write_file.
  • Never call read_file on a file you have just written — the write already
    succeeded; only re-read if you want to verify an edit_file change.
  • Avoid bash for file operations that have a dedicated tool (read_file,
    write_file, grep_search, etc.); reserve bash for running tests, build
    commands, or operations that have no dedicated tool.

SELF-CHECK — execute these verifications before writing your final summary:
a. For every file you edited or created, call read_file and confirm:
   the target change is present, no syntax errors are visible, no surrounding
   code was accidentally removed.
b. If any new import was added, grep_search for the imported symbol to confirm
   it exists in the target module — do not assume the import is valid.
c. If a function signature changed, grep_search for existing callers and
   confirm all call sites were updated in this execution.
d. If the plan required running tests, run them now and report pass/fail counts.

SUMMARY FORMAT — write your final summary using EXACTLY these labels
(the evaluator parses them; additional labels are ignored):

COMPLETED: <comma-separated step numbers completed, e.g. "1, 2, 3">
SKIPPED: <step numbers skipped with a one-line reason each, or "none">
ISSUES: <description of any problem encountered, citing step number and tool
         that failed; or "none">
STATUS: <DONE if all required steps completed without blocking issues,
         PARTIAL if some steps were skipped or have unresolved issues>
"""


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

        text, execution_log = await self.llm.call_with_tools(
            messages, self.registry, system=EXECUTOR_SYSTEM,
            max_turns=self.config.max_tool_turns,
        )

        # Extract unique file paths from write/edit operations
        files_changed: list[str] = []
        seen: set[str] = set()
        for entry in execution_log:
            tool_name = entry["tool"]
            if tool_name in ("write_file", "edit_file", "delete_file", "move_file", "copy_file"):
                path = entry["input"].get("path") or entry["input"].get("destination", "")
                if path and path not in seen:
                    files_changed.append(path)
                    seen.add(path)

        log.info("Executor: %d tool calls, %d files changed", len(execution_log), len(files_changed))

        return ExecutionResult(text=text, log=execution_log, files_changed=files_changed)
