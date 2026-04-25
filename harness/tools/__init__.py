"""Built-in tools — import this module to get all default tools.

Tool registry structure
-----------------------
* ``DEFAULT_TOOLS``  — always registered when building a registry with no filter.
  These are the workhorse file/search/code tools that every agent task needs.
* ``OPTIONAL_TOOLS`` — high-cost or scope-expanding tools that are NOT registered
  by default.  Opt in via ``HarnessConfig.extra_tools = ["web_search"]`` or by
  passing ``extra_tools=["web_search", "http_request"]`` to ``build_registry()``.
  Keeping them optional caps the API schema size for tasks that don't need them
  and prevents accidental network access in offline/air-gapped environments.
* ``ALL_TOOLS``      — union of both lists; exported for tools that need the full
  catalogue (e.g. admin scripts, test fixtures, tool name validation).

Current optional tools
~~~~~~~~~~~~~~~~~~~~~~
* ``web_search``   — DuckDuckGo search + page fetch; network access required.
* ``http_request`` — Generic HTTP client (GET/POST/etc.); outbound network
                     access required; kept optional to prevent unintentional
                     network calls in air-gapped or restricted environments.
* ``git_search``   — Git history/blame/grep search; high schema cost for a
                     specialised capability, so kept optional to reduce per-call
                     schema size for tasks that don't need git-history lookups.
* ``read_file`` / ``edit_file`` / ``write_file``
                   — Single-file variants, superseded by ``batch_read`` /
                     ``batch_edit`` / ``batch_write`` respectively. Kept
                     available as opt-in when a specific integration needs
                     one-at-a-time semantics; default pipelines/agents use
                     the batch variants so a multi-file change costs one
                     LLM round-trip instead of N.

Current default tools
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
All other tools in this module (no network access required) plus the new
``batch_read`` / ``batch_edit`` / ``batch_write`` / ``scratchpad`` tools.
"""

import logging

from harness.tools.base import Tool
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
from harness.tools.data_flow import DataFlowTool
from harness.tools.feature_search import FeatureSearchTool
from harness.tools.call_graph import CallGraphTool
from harness.tools.dependency_analyzer import DependencyAnalyzerTool
from harness.tools.http_client import HttpRequestTool
from harness.tools.json_transform import JsonTransformTool
from harness.tools.discovery import ToolDiscoveryTool
from harness.tools.git_search import GitSearchTool
from harness.tools.todo_scan import TodoScanTool
from harness.tools.batch_read import BatchReadTool
from harness.tools.batch_edit import BatchEditTool
from harness.tools.batch_write import BatchWriteTool
from harness.tools.scratchpad import ScratchpadTool
from harness.tools.file_info import FileInfoTool
from harness.tools.lint_check import LintCheckTool
from harness.tools.context_budget import ContextBudgetTool
from harness.tools.project_map import ProjectMapTool
from harness.tools.ast_rename import AstRenameTool

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default tool set — registered automatically by build_registry()
# ---------------------------------------------------------------------------

DEFAULT_TOOLS: list[Tool] = [
    # --- BATCH FILE TOOLS (primary — listed first so LLM prefers these) ---
    BatchReadTool(),
    BatchEditTool(),
    BatchWriteTool(),
    EditFileTool(),
    ScratchpadTool(),
    # --- SEARCH & ANALYSIS (high-signal, read-only) ---
    GrepSearchTool(),
    GlobSearchTool(),
    SymbolExtractorTool(),
    CodeAnalysisTool(),
    CrossReferenceTool(),
    FeatureSearchTool(),
    ProjectMapTool(),
    FileInfoTool(),
    # --- TESTING & VERIFICATION ---
    TestRunnerTool(),     # prefer over bash("pytest ...")
    LintCheckTool(),      # prefer over bash("ruff ...")
    ContextBudgetTool(),
    # --- FILE / DIR OPS ---
    DeleteFileTool(),
    MoveFileTool(),
    CopyFileTool(),
    ListDirectoryTool(),
    CreateDirectoryTool(),
    TreeTool(),
    FilePatchTool(),
    FindReplaceTool(),
    DiffFilesTool(),
    # --- GIT ---
    GitStatusTool(),
    GitDiffTool(),
    GitLogTool(),
    # --- SPECIALIZED ---
    DataFlowTool(),
    CallGraphTool(),
    DependencyAnalyzerTool(),
    PythonEvalTool(),
    JsonTransformTool(),
    AstRenameTool(),
    ToolDiscoveryTool(),
    TodoScanTool(),
    # --- BASH (last — use only for builds, installs, and commands that
    #     have no dedicated tool; NEVER for reading source files) ---
    BashTool(),
]

# ---------------------------------------------------------------------------
# Optional tool set — NOT registered by default; opt in via extra_tools
# ---------------------------------------------------------------------------
# Rationale: these tools require outbound network access.  Including them
# unconditionally would (a) add schema weight to every LLM call and (b)
# allow network calls in air-gapped or restricted environments.
# To enable: set HarnessConfig.extra_tools = ["web_search", "http_request"]
# in your config, or pass extra_tools=[...] to build_registry().
# ---------------------------------------------------------------------------

OPTIONAL_TOOLS: list[Tool] = [
    WebSearchTool(),    # DuckDuckGo search + page fetch; needs network access
    HttpRequestTool(),  # Generic HTTP client (GET/POST/etc.); needs network access
    GitSearchTool(),    # Git history/blame/grep; high schema cost, opt in via extra_tools
    ReadFileTool(),     # Superseded by batch_read; opt in via extra_tools=["read_file"]
    WriteFileTool(),    # Superseded by batch_write; opt in via extra_tools=["write_file"]
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
