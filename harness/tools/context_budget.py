"""context_budget — report current token usage and remaining budget.

This tool is **intercepted** by the LLM tool loop in ``harness/core/llm.py``
(same pattern as the scratchpad tool). The loop injects live token counts,
turn numbers, and scratchpad stats that only it has access to.

The ``execute`` method below is a fallback for direct registry calls
(tests, scripts) and returns a stub response.
"""

from __future__ import annotations

from typing import Any

from harness.core.config import HarnessConfig
from harness.tools.base import Tool, ToolResult


class ContextBudgetTool(Tool):
    name = "context_budget"
    description = (
        "Check your current token usage, remaining budget, and tool turn count. "
        "Use this to decide how much data to request in subsequent reads. "
        "Returns: input tokens used, output tokens used, current turn number, "
        "max turns allowed, scratchpad note count. "
        "No parameters needed — just call it. "
        "Rule of thumb: if turn > 80% of max_turns, start wrapping up — "
        "finish the current edit, run final tests, then commit."
    )
    tags = frozenset({"analysis"})

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
        }

    async def execute(self, config: HarnessConfig) -> ToolResult:
        # Fallback when called outside the tool loop (tests, scripts).
        # The real implementation is in core/llm.py tool_loop where this
        # tool name is intercepted and live stats are injected.
        return ToolResult(
            output=(
                "[context_budget] This tool is intercepted by the tool loop. "
                "When called outside the loop (e.g. in tests), stats are unavailable."
            )
        )
