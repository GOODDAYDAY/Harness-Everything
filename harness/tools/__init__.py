"""Built-in tools — import this module to get all default tools."""

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
]


def build_registry(
    allowed_tools: list[str] | None = None,
) -> ToolRegistry:
    """Create a ToolRegistry pre-loaded with built-in tools.

    If *allowed_tools* is provided, only tools whose names appear in the list
    are registered.
    """
    registry = ToolRegistry()
    for tool in ALL_TOOLS:
        if allowed_tools and tool.name not in allowed_tools:
            continue
        registry.register(tool)
    return registry
