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

Rules:
- Follow the plan exactly — do not add extra changes
- Read files before editing them
- After making changes, verify they look correct
- If a step fails, report the error clearly instead of guessing
- Work through the plan in order
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
