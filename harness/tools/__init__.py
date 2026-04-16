"""Built-in tools — import this module to get all default tools.

Tool registry structure
-----------------------
* ``DEFAULT_TOOLS``  — always registered when building a registry with no filter.
  These are the workhorse file/search/code tools that every agent task needs.
* ``OPTIONAL_TOOLS`` — high-cost or scope-expanding tools that are NOT registered
  by default.  Opt in via ``HarnessConfig.extra_tools = ["web_search"]`` or by
  passing ``extra_tools=["web_search"]`` to ``build_registry()``.  Keeping them
  optional caps the API schema size for tasks that don't need them (web_search
  adds ~1 KB to every LLM call's tool list) and prevents accidental network
  access in offline/air-gapped environments.
* ``ALL_TOOLS``      — union of both lists; exported for tools that need the full
  catalogue (e.g. admin scripts, test fixtures, tool name validation).

Current optional tools
~~~~~~~~~~~~~~~~~~~~~~
* ``web_search``  — DuckDuckGo search + page fetch; network access required.

Current default tools (27 of 27)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
All other tools in this module.
"""

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
from harness.tools.diff_files import DiffFilesTool
from harness.tools.cross_reference import CrossReferenceTool
from harness.tools.semantic_search import SemanticSearchTool
from harness.tools.data_flow import DataFlowTool
from harness.tools.feature_search import FeatureSearchTool
from harness.tools.call_graph import CallGraphTool

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default tool set — registered automatically by build_registry()
# ---------------------------------------------------------------------------

DEFAULT_TOOLS: list[Tool] = [
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
    SymbolExtractorTool(),
    PythonEvalTool(),
    FindReplaceTool(),
    DiffFilesTool(),
    CrossReferenceTool(),
    SemanticSearchTool(),
    DataFlowTool(),
    FeatureSearchTool(),
    CallGraphTool(),
]

# ---------------------------------------------------------------------------
# Optional tool set — NOT registered by default; opt in via extra_tools
# ---------------------------------------------------------------------------
# Rationale: these tools either require network access (web_search) or have
# a large schema footprint.  Including them unconditionally would add ~1 KB
# to every LLM call's tool list even for purely local tasks.
# To enable: set HarnessConfig.extra_tools = ["web_search"] in your config.
# ---------------------------------------------------------------------------

OPTIONAL_TOOLS: list[Tool] = [
    WebSearchTool(),  # DuckDuckGo search + page fetch; needs network access
]

# ---------------------------------------------------------------------------
# ALL_TOOLS — union for catalogue queries, validation, and test fixtures
# ---------------------------------------------------------------------------

ALL_TOOLS: list[Tool] = DEFAULT_TOOLS + OPTIONAL_TOOLS

# Unified name → instance map for both default and optional tools.
# Used by build_registry() to resolve extra_tools names and by callers that
# need to instantiate a specific tool by name at runtime.
_ALL_TOOLS_BY_NAME: dict[str, Tool] = {t.name: t for t in ALL_TOOLS}


def build_registry(
    allowed_tools: list[str] | None = None,
    extra_tools: list[str] | None = None,
) -> ToolRegistry:
    """Create a ToolRegistry pre-loaded with built-in tools.

    Args:
        allowed_tools: If provided, only tools whose names appear in this list
            are registered from ``DEFAULT_TOOLS``.  An empty list is treated the
            same as ``None`` (all default tools included) to match the
            ``HarnessConfig`` convention where an empty list means "no filter".
            Note: this filter applies to DEFAULT_TOOLS only; tools requested via
            ``extra_tools`` are always added regardless of this list.
        extra_tools: Additional tool names to look up from ``_ALL_TOOLS_BY_NAME``
            (which covers both DEFAULT_TOOLS and OPTIONAL_TOOLS) and add to the
            registry.  Unknown names are logged as warnings and skipped rather
            than raising so that a misconfigured ``extra_tools`` entry does not
            abort the whole run.
            Example: ``extra_tools=["web_search"]`` enables network search.

    Returns:
        A populated :class:`ToolRegistry` ready for use.
    """
    registry = ToolRegistry()

    # Register default tools (subject to allowed_tools filter)
    for tool in DEFAULT_TOOLS:
        if allowed_tools and tool.name not in allowed_tools:
            continue
        registry.register(tool)

    # Register optional / extra tools (not subject to allowed_tools filter —
    # if a caller explicitly requests a tool by name, respect that intent).
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
        # Don't double-register if it was already included via DEFAULT_TOOLS above
        if registry.get(name) is None:
            registry.register(tool_instance)
            log.debug("extra_tools: registered %r", name)

    return registry
