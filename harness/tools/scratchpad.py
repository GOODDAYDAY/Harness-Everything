"""scratchpad — persistent notes the LLM can write to survive context pruning.

This tool is **intercepted by the LLM loop** (``call_with_tools`` in
``harness/core/llm.py``) before it reaches the normal registry dispatch.
The interceptor extracts the ``note`` argument, appends it to a per-cycle
notes list, and injects the collected notes at the top of the *system
prompt* on subsequent turns — so they survive the conversation-pruning
threshold (300K chars) that otherwise evicts old tool results.

Why do this?
------------
Long agent cycles (200 tool turns) routinely exceed the pruning threshold.
When that happens, older tool results are truncated and the agent loses
access to file contents it read early on. Without scratchpad, the agent's
only recourse is to re-read the same files — wasteful and slow. With
scratchpad, the agent is told:

    "Save your key findings here; do NOT re-read files to recall them."

The interceptor handles the I/O — this ``execute`` method is a fallback
that runs only when something bypasses the interceptor (unit tests, or a
future code path that calls the registry directly). It simply echoes the
note back so calls don't fail.
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

    # Practical cap so a single note can't bloat the system prompt.
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
        # Fallback path (interceptor in llm.py normally handles this).
        # Just echo the note truncated to the cap; the interceptor will
        # override this when it's active.
        if not isinstance(note, str):
            return ToolResult(
                error=f"note must be a string, got {type(note).__name__}",
                is_error=True,
            )
        note = note.strip()
        if not note:
            return ToolResult(error="note cannot be empty", is_error=True)
        if len(note) > self.MAX_NOTE_CHARS:
            note = note[: self.MAX_NOTE_CHARS] + "… [truncated]"
        return ToolResult(
            output=f"[scratchpad] note saved ({len(note)} chars): {note[:80]}…",
            metadata={"note": note},
        )
