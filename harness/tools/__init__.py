"""Built-in tools — import this module to get all default tools."""

import logging

from harness.tools.base import Tool, ToolResult
from harness.tools.registry import ToolRegistry
from harness.tools.file_read import ReadFileTool
from harness.tools.file_write import WriteFileTool
from harness.tools.file_edit import EditFileTool
from harness.tools.file_ops import DeleteFileTool, MoveFileTool, CopyFileTool
from harness.tools.directory import ListDirectoryTool, CreateDirectoryTool, TreeTool
from harness.tools.search_glob import GlobSearchTool
from harness.tools.search_grep import GrepSearchTool
from harness.tools.bash import BashTool
from harness.tools.git import GitStatusTool, GitDiffTool, GitLogTool
from harness.tools.code_analysis import CodeAnalysisTool
from harness.tools.file_patch import FilePatchTool
from harness.tools.test_runner import TestRunnerTool
from harness.tools.web_search import WebSearchTool
from harness.tools.symbol_extractor import SymbolExtractorTool
from harness.tools.python_eval import PythonEvalTool
from harness.tools.find_replace import FindReplaceTool

log = logging.getLogger(__name__)

ALL_TOOLS: list[Tool] = [
    ReadFileTool(),
    WriteFileTool(),
    EditFileTool(),
    DeleteFileTool(),
    MoveFileTool(),
    CopyFileTool(),
    ListDirectoryTool(),
    CreateDirectoryTool(),
    TreeTool(),
    GlobSearchTool(),
    GrepSearchTool(),
    BashTool(),
    GitStatusTool(),
    GitDiffTool(),
    GitLogTool(),
    CodeAnalysisTool(),
    FilePatchTool(),
    TestRunnerTool(),
    WebSearchTool(),
    SymbolExtractorTool(),
    PythonEvalTool(),
    FindReplaceTool(),
]

# Map of every tool name → tool class for on-demand instantiation.
# Consumers can register additional tools at runtime without modifying ALL_TOOLS.
_ALL_TOOLS_BY_NAME: dict[str, Tool] = {t.name: t for t in ALL_TOOLS}


def build_registry(
    allowed_tools: list[str] | None = None,
    extra_tools: list[str] | None = None,
) -> ToolRegistry:
    """Create a ToolRegistry pre-loaded with built-in tools.

    Args:
        allowed_tools: If provided, only tools whose names appear in this list
            are registered from ``ALL_TOOLS``.  An empty list is treated the
            same as ``None`` (all tools included) to match the ``HarnessConfig``
            convention where an empty list means "no filter".
        extra_tools: Additional tool names to look up from ``_ALL_TOOLS_BY_NAME``
            and add to the registry regardless of ``allowed_tools``.  Unknown
            names are logged as warnings and skipped rather than raising so that
            a misconfigured ``extra_tools`` entry does not abort the whole run.

    Returns:
        A populated :class:`ToolRegistry` ready for use.
    """
    registry = ToolRegistry()
    for tool in ALL_TOOLS:
        if allowed_tools and tool.name not in allowed_tools:
            continue
        registry.register(tool)

    for name in (extra_tools or []):
        tool_instance = _ALL_TOOLS_BY_NAME.get(name)
        if tool_instance is None:
            log.warning(
                "extra_tools: %r is not a known tool name — skipping.  "
                "Known names: %s",
                name,
                sorted(_ALL_TOOLS_BY_NAME),
            )
            continue
        # Don't double-register if it was already included above
        if registry.get(name) is None:
            registry.register(tool_instance)
            log.debug("extra_tools: registered %r", name)

    return registry
