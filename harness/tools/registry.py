"""ToolRegistry — manages tools and dispatches execution."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from harness.core.config import HarnessConfig
from harness.tools.base import Tool, ToolResult

log = logging.getLogger(__name__)

# LLMs frequently send alternative parameter names that differ from the tool's
# JSON schema.  Rather than wasting tool turns on a TypeError → retry loop, we
# normalise the most common aliases *before* dispatch.  Keys = wrong name the
# LLM sends, values = correct name the tool schema expects.
_PARAM_ALIASES: dict[str, str] = {
    "file_content": "content",     # write_file: LLM says file_content
    "file_path": "path",           # many tools: LLM says file_path
    "filename": "path",            # many tools: LLM says filename
    "filepath": "path",            # many tools: LLM says filepath
    "text": "content",             # write_file: LLM says text
    "old_string": "old_str",       # edit_file: LLM says old_string
    "new_string": "new_str",       # edit_file: LLM says new_string
    "old_text": "old_str",         # edit_file: LLM says old_text
    "new_text": "new_str",         # edit_file: LLM says new_text
    "directory": "path",           # list_directory / tree: LLM says directory
    "dir": "path",                 # list_directory / tree: LLM says dir
    "pattern": "glob",             # grep_search: LLM says pattern
    # NOTE: "query" maps to "glob" (grep_search-specific).  Do NOT use "query"
    # as a primary parameter name in new tools — use "concept", "term", etc.
    # to avoid silent alias rewriting before dispatch.
    "query": "glob",               # grep_search: LLM says query
    "search": "regex",             # grep_search: LLM says search
    "cmd": "command",              # bash: LLM says cmd
}


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

        # Enforce tool allowlist: if config.allowed_tools is non-empty, only
        # tools explicitly listed there may be executed.  This is checked at
        # dispatch time (not only at registration) so that a registry built
        # with all tools still respects a restrictive config at runtime.
        if config.allowed_tools and name not in config.allowed_tools:
            log.warning("registry: tool %r blocked by allowed_tools allowlist", name)
            return ToolResult(
                error=(
                    f"PERMISSION ERROR: tool {name!r} is not in the allowed_tools list.  "
                    f"Allowed: {config.allowed_tools}"
                ),
                is_error=True,
            )

        # Normalise common parameter aliases so the LLM's first attempt
        # succeeds instead of burning a tool turn on a TypeError retry.
        params = _normalise_params(tool, params)

        # Reject parameters that are not in the tool's schema.  This catches
        # hallucinated parameter names early and gives the LLM a precise error
        # rather than a cryptic TypeError from the tool's execute() signature.
        if err := _check_unknown_params(tool, params):
            return err

        _t0 = time.monotonic()
        try:
            result = await tool.execute(config, **params)
            _duration_ms = round((time.monotonic() - _t0) * 1000)
            result.elapsed_s = round(_duration_ms / 1000, 4)
            log.info(
                "TOOL_TRACE %s",
                json.dumps({
                    "name": name,
                    "success": not result.is_error,
                    "duration_ms": _duration_ms,
                }),
            )
            return result
        except TypeError as exc:
            _duration_ms = round((time.monotonic() - _t0) * 1000)
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
            log.warning(
                "tool_call: name=%r schema_error duration_ms=%d — %s",
                name, _duration_ms, exc,
            )
            log.info(
                "TOOL_TRACE %s",
                json.dumps({"name": name, "success": False, "duration_ms": _duration_ms, "error_type": "schema"}),
            )
            return ToolResult(error=msg, is_error=True)
        except PermissionError as exc:
            _duration_ms = round((time.monotonic() - _t0) * 1000)
            msg = (
                f"PERMISSION ERROR in {name!r}: {exc}. "
                f"The path may be outside the allowed directories.  "
                f"Allowed paths: {config.allowed_paths}"
            )
            log.warning(
                "tool_call: name=%r permission_error duration_ms=%d — %s",
                name, _duration_ms, exc,
            )
            log.info(
                "TOOL_TRACE %s",
                json.dumps({"name": name, "success": False, "duration_ms": _duration_ms, "error_type": "permission"}),
            )
            return ToolResult(error=msg, is_error=True)
        except Exception as exc:
            _duration_ms = round((time.monotonic() - _t0) * 1000)
            msg = f"TOOL ERROR in {name!r} — {type(exc).__name__}: {exc}"
            log.warning(
                "tool_call: name=%r tool_error duration_ms=%d — %s",
                name, _duration_ms, exc,
            )
            log.info(
                "TOOL_TRACE %s",
                json.dumps({"name": name, "success": False, "duration_ms": _duration_ms, "error_type": "tool"}),
            )
            return ToolResult(error=msg, is_error=True)


def _normalise_params(tool: Tool, params: dict[str, Any]) -> dict[str, Any]:
    """Map common LLM parameter-name mistakes to the tool's actual schema names.

    Only renames a key when:
    1. The key is in ``_PARAM_ALIASES``, AND
    2. The alias target is a known parameter in the tool's schema, AND
    3. The target name is not already present in ``params``
       (don't clobber an explicitly provided correct param).

    Returns a *new* dict — the original is not mutated.
    """
    try:
        schema_props = set(tool.input_schema().get("properties", {}).keys())
    except Exception:
        return params  # can't introspect schema → pass through unchanged

    out = dict(params)
    for wrong_name, right_name in _PARAM_ALIASES.items():
        if (
            wrong_name in out
            and right_name in schema_props
            and right_name not in out
        ):
            out[right_name] = out.pop(wrong_name)
            log.debug(
                "registry: alias %r → %r for tool %s",
                wrong_name, right_name, tool.name,
            )
    return out


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


def _check_unknown_params(
    tool: Tool, params: dict[str, Any]
) -> ToolResult | None:
    """Return a SCHEMA ERROR ToolResult if *params* contains keys not in the tool's schema.

    Called after alias normalisation so that already-corrected keys are not
    reported as unknown.  Returns None when all keys are valid or when the
    schema cannot be introspected (fail-open to avoid breaking tools with
    unusual schemas).
    """
    try:
        schema_props = set(tool.input_schema().get("properties", {}).keys())
    except Exception:
        return None  # can't introspect — pass through
    unknown = set(params) - schema_props
    if unknown:
        msg = (
            f"SCHEMA ERROR calling {tool.name!r}: unexpected parameter(s) {sorted(unknown)}.  "
            f"Known parameters: {sorted(schema_props)}"
        )
        log.warning("registry: unknown params %s for tool %r", sorted(unknown), tool.name)
        return ToolResult(error=msg, is_error=True)
    return None
