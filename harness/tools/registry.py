"""ToolRegistry — manages tools and dispatches execution."""

from __future__ import annotations

from typing import Any

from harness.config import HarnessConfig
from harness.tools.base import Tool, ToolResult


class ToolRegistry:
    """Collects Tool instances, exports API schemas, and dispatches calls."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def to_api_schema(self) -> list[dict[str, Any]]:
        """Return the list of tool definitions for the Claude API."""
        return [t.api_schema() for t in self._tools.values()]

    async def execute(
        self, name: str, config: HarnessConfig, params: dict[str, Any]
    ) -> ToolResult:
        """Look up and execute a tool by name."""
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(error=f"Unknown tool: {name}", is_error=True)
        try:
            return await tool.execute(config, **params)
        except Exception as exc:
            return ToolResult(error=f"{type(exc).__name__}: {exc}", is_error=True)
