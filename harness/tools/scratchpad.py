"""scratchpad — persistent notes the LLM can write to survive context pruning.

The LLM loop in ``harness/core/llm.py`` intercepts scratchpad calls and
injects collected notes at the top of the system prompt on every
subsequent turn — so they outlive the conversation-pruning threshold that
otherwise evicts old tool results. This ``execute`` method is the fallback
path for callers that bypass the interceptor (tests, direct registry use);
it echoes the note back so nothing fails.
"""

from __future__ import annotations

from typing import Any

from harness.core.config import HarnessConfig
from harness.tools.base import Tool, ToolResult


class ScratchpadTool(Tool):
    name = "scratchpad"
    description = (
        "Save an important finding as a persistent note. "
        "Notes survive conversation pruning and are re-injected into your "
        "system prompt on every turn. "
        "Use for: file locations, key function signatures, decisions made, "
        "bugs found, design constraints. "
        "Do NOT re-read files to recall information you've already seen — "
        "save notes here instead. "
        "Notes are per-cycle; a new cycle starts fresh."
    )
    tags = frozenset({"analysis"})

    MAX_NOTE_CHARS = 2000

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "note": {
                    "type": "string",
                    "description": (
                        f"The note to save (max {self.MAX_NOTE_CHARS} chars). "
                        "Be specific: 'app.py:247 task_planner cache-miss path' "
                        "beats 'found bug somewhere'."
                    ),
                },
            },
            "required": ["note"],
        }

    async def execute(
        self, config: HarnessConfig, *, note: str,
    ) -> ToolResult:
        note = (note or "").strip()
        if not note:
            return ToolResult(error="note cannot be empty", is_error=True)
        if len(note) > self.MAX_NOTE_CHARS:
            note = note[: self.MAX_NOTE_CHARS] + "… [truncated]"
        return ToolResult(
            output=f"[scratchpad] note saved ({len(note)} chars): {note[:80]}…",
            metadata={"note": note},
        )
