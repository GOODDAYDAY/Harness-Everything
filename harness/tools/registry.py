"""ToolRegistry — manages tools and dispatches execution."""

from __future__ import annotations

import logging
from typing import Any

from harness.config import HarnessConfig
from harness.tools.base import Tool, ToolResult

log = logging.getLogger(__name__)


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
        """Look up and execute a tool by name.

        Error categories returned to the caller (and visible to the LLM):

        * ``SCHEMA ERROR``     — ``TypeError`` raised during dispatch, which
          means a required parameter is missing or has the wrong type.  The
          LLM should fix its parameter values, not retry with the same call.
        * ``PERMISSION ERROR`` — ``PermissionError`` / ``OSError`` with errno
          EACCES or EPERM; usually means the path is outside allowed_paths or
          the process lacks filesystem rights.  The LLM should inspect the
          path and check ``config.allowed_paths``.
        * ``TOOL ERROR``       — any other exception; includes I/O failures,
          subprocess errors, and unexpected conditions.  The LLM should read
          the message and decide whether to retry or report under ISSUES.

        Keeping these categories distinct prevents the LLM from treating a
        ``TypeError`` caused by a missing required argument as a permission
        problem and wasting tool turns adjusting the wrong thing.
        """
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(error=f"Unknown tool: {name!r}", is_error=True)
        try:
            return await tool.execute(config, **params)
        except TypeError as exc:
            # Most common cause: the LLM omitted a required parameter or passed
            # a value of the wrong type.  Surfacing this as a schema error gives
            # the model an accurate signal to fix its next call rather than
            # retrying with the same (broken) arguments.
            msg = (
                f"SCHEMA ERROR calling {name!r}: {exc}. "
                f"Check that all required parameters are present and correctly "
                f"typed per the tool's JSON schema.  "
                f"Required params: {_required_params(tool)}"
            )
            log.warning("registry: schema error in tool %r: %s", name, exc)
            return ToolResult(error=msg, is_error=True)
        except PermissionError as exc:
            msg = (
                f"PERMISSION ERROR in {name!r}: {exc}. "
                f"The path may be outside the allowed directories.  "
                f"Allowed paths: {config.allowed_paths}"
            )
            log.warning("registry: permission error in tool %r: %s", name, exc)
            return ToolResult(error=msg, is_error=True)
        except Exception as exc:
            msg = f"TOOL ERROR in {name!r} — {type(exc).__name__}: {exc}"
            log.warning("registry: unexpected error in tool %r: %s", name, exc)
            return ToolResult(error=msg, is_error=True)


def _required_params(tool: Tool) -> list[str]:
    """Return the list of required parameter names from the tool's JSON schema.

    Used in SCHEMA ERROR messages so the LLM knows exactly which parameters
    it must supply.  Returns an empty list when the schema is absent or
    malformed (fail-safe — the error message is still useful without it).
    """
    try:
        schema = tool.input_schema()
        return schema.get("required", [])
    except Exception:
        return []
