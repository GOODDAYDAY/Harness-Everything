"""Abstract base for all tools."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from harness.config import HarnessConfig


@dataclass
class ToolResult:
    """Uniform result returned by every tool execution."""

    output: str = ""
    error: str = ""
    is_error: bool = False

    def to_api(self) -> dict[str, Any]:
        """Format as a tool_result content block for the Claude API."""
        text = self.error if self.is_error else self.output
        return {"type": "text", "text": text}


class Tool(ABC):
    """Base class for all harness tools.

    Subclasses must define *name*, *description*, and implement
    *input_schema()* and *execute()*.
    """

    name: str
    description: str

    # Set to True if this tool operates on file paths that should be checked
    # against allowed_paths in config.
    requires_path_check: bool = False

    @abstractmethod
    def input_schema(self) -> dict[str, Any]:
        """Return JSON Schema for the tool input."""

    @abstractmethod
    async def execute(self, config: HarnessConfig, **params: Any) -> ToolResult:
        """Run the tool and return a ToolResult."""

    def api_schema(self) -> dict[str, Any]:
        """Export as a tool definition for the Claude API."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema(),
        }

    # ---- helpers ----

    def _check_path(self, config: HarnessConfig, path: str) -> ToolResult | None:
        """Return a ToolResult error if *path* is outside allowed dirs, else None.

        Rejects null bytes before any Path operation — a null byte in a path
        string causes undefined behaviour on some OSes and can be used to
        truncate the path at the OS level, bypassing prefix checks.
        """
        if "\x00" in path:
            return ToolResult(
                error=f"PERMISSION ERROR: path contains null byte: {path!r}",
                is_error=True,
            )
        if not config.is_path_allowed(path):
            return ToolResult(
                error=f"Path not allowed: {path}  (allowed: {config.allowed_paths})",
                is_error=True,
            )
        return None
