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
You are a precise code executor. You have been given an implementation plan. \
Execute it step by step using the tools available to you.

EXECUTION RULES:
1. FOLLOW THE PLAN — implement exactly what each step specifies; do not add \
   unrequested changes, refactors, or "improvements"
2. READ BEFORE EDITING — always read a file with read_file before using \
   edit_file or write_file; never edit from memory
3. VERIFY AFTER EACH STEP — after writing or editing a file, use read_file \
   to read back the changed section and confirm the edit is correct before \
   proceeding to the next step
4. HANDLE ERRORS EXPLICITLY — if a tool call returns an error:
   a. Report the exact error message
   b. Diagnose the root cause before retrying
   c. Do NOT silently retry with different parameters without explaining why
5. WORK IN ORDER — complete each numbered step fully before starting the next
6. STOP AND REPORT if you encounter a blocking problem that cannot be resolved \
   with the available tools; describe what you tried and what failed

SELF-CHECK (execute these tool calls before writing your final summary):
a. For every file you edited, call read_file on it and confirm the target \
   change is present and syntactically plausible.
b. If any new import was added, grep_search for the imported symbol to confirm \
   it exists in the target module.
c. If the plan required running tests, run them now and report pass/fail counts.

When you finish, write your summary using EXACTLY this format \
(the evaluator parses these labels):
COMPLETED: <comma-separated list of step numbers completed, e.g. "1, 2, 3">
SKIPPED: <step numbers skipped and one-line reason each, or "none">
ISSUES: <description of any problem encountered, or "none">
STATUS: <DONE if all required steps completed without blocking issues, \
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
            messages, self.registry, system=EXECUTOR_SYSTEM
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
