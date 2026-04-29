"""Thin async wrapper around the Anthropic Claude API."""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any

import anthropic
from anthropic._exceptions import OverloadedError

from harness.core.config import HarnessConfig
from harness.tools.base import ToolResult
from harness.tools.registry import ToolRegistry

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Transient-error retry policy
# ---------------------------------------------------------------------------
# These errors are safe to retry because the request never reached the model
# (rate limit, overload, connection drop, 5xx).  We use exponential backoff
# with jitter so that a fleet of parallel calls doesn't hit the API in lock
# step after a momentary overload event.
_RETRYABLE_EXCEPTIONS = (
    OverloadedError,                      # HTTP 529 — Claude overloaded
    anthropic.RateLimitError,             # HTTP 429 — rate limit hit
    anthropic.InternalServerError,        # HTTP 500 — transient server error
    anthropic.APIConnectionError,         # network-level failure (no response)
    anthropic.APITimeoutError,            # SDK-level timeout wrapper
    asyncio.TimeoutError,                 # asyncio.wait_for wall hit (slow network /
                                          # overloaded model exceeding per-call timeout)
)

_MAX_RETRIES: int = 4           # up to 4 retries (5 total attempts)
_INITIAL_DELAY: float = 2.0     # seconds before first retry
_BACKOFF_FACTOR: float = 2.0    # each retry waits 2× longer
_MAX_DELAY: float = 60.0        # cap the wait so we don't stall for minutes

# A single tool turn taking longer than this is unusual and worth warning about
# so operators can spot hung or unexpectedly slow tool calls.
_TURN_STALL_WARN_SECS: float = 90.0

# ---------------------------------------------------------------------------
# Conversation-history pruning
# ---------------------------------------------------------------------------
# Long executor loops (30 turns, read_file outputs up to 2 000 lines each)
# can push the conversation well past 200 K chars (~50 K tokens).  Claude's
# context window is 200 K tokens, but each subsequent API call re-sends the
# *entire* conversation, so token costs and latency grow linearly with turns.
# Worse, if the conversation overflows the model's context window, the API
# returns a cryptic HTTP 400 "prompt too long" error that looks identical to
# a schema error — operators lose hours diagnosing the wrong thing.
#
# Strategy: after every tool-result batch, estimate the conversation's total
# character size.  When it exceeds _CONV_PRUNE_THRESHOLD_CHARS, truncate the
# text *content* of older tool-result messages (keeping the 4 most recent
# assistant+user pairs intact) until the total drops to _CONV_PRUNE_TARGET_CHARS.
# The structural integrity required by the Anthropic API (every tool_use block
# must have a matching tool_result block with the same ID) is preserved because
# we only shorten the text inside existing tool_result content blocks — we never
# remove or reorder messages.
#
# Heuristic: 4 chars ≈ 1 token.  The threshold is set at 600 K chars (~150 K
# tokens) — comfortably inside the 200 K-token context window while leaving
# headroom for the system prompt, the final answer, and the tools schema.
_CONV_PRUNE_THRESHOLD_CHARS: int = 300_000   # trigger pruning above this total
_CONV_PRUNE_TARGET_CHARS: int = 200_000      # prune down to this target
# Number of *trailing* message pairs (assistant + user) kept verbatim.
# Keeping the most recent turns intact ensures the model still sees the fresh
# tool output it just received; only older outputs are compressed.
_CONV_PRUNE_KEEP_RECENT_PAIRS: int = 3

# Tools whose outputs are predominantly navigational / exploratory and carry
# low signal for future reasoning.  When pruning is triggered, these are
# stubbed even when their output is only modestly long (> 200 chars), whereas
# all other tools use a more generous threshold (> 500 chars).
_LOW_SIGNAL_PRUNE_TOOLS: frozenset[str] = frozenset({
    "grep_search",
    "glob_search",
    "todo_scan",
    "list_directory",
    "tree",
    "tool_discovery",
    "git_log",
    "git_status",
    "git_diff",
})

# Tools whose outputs are always high-signal and should be preserved with a
# much more generous stub threshold.  These include test runners (whose pass/
# fail output is critical for planning the next step), Python evaluation
# (whose return values and tracebacks are decisive), and lint checks (whose
# error lists drive immediate follow-up edits).  They get a 2 000-char
# threshold — only compacted if truly enormous.
# NOTE: "bash" is deliberately excluded: bash is also used to read files (cat,
# head, etc.) which produces low-value output, so it stays at the 500-char
# default and relies on the _HIGH_SIGNAL_PATTERNS filter to preserve key lines.
_HIGH_SIGNAL_TOOLS: frozenset[str] = frozenset({
    "test_runner",
    "python_eval",
    "lint_check",
})

# Medium-signal tools produce output (shell commands, code reading, compilation
# runs) that is worth preserving longer than the 500-char default before
# compaction / reactive pruning.  lint_check and test_runner are already in
# _HIGH_SIGNAL_TOOLS so they are excluded here.
_MEDIUM_SIGNAL_TOOLS: frozenset[str] = frozenset({
    "bash",
    # Code-reading tools produce function bodies and structural analysis that
    # agents need to recall when making edits.  500-char default threshold
    # compacts a 30-line function body; 1500-char threshold keeps most of them
    # intact across multiple turns, reducing forced re-reads.
    "symbol_extractor",
    "code_analysis",
    "cross_reference",
    "call_graph",
    "data_flow",
    # Import-graph output is used to reason about circular dependencies and
    # module structure when reorganising imports; worth preserving longer than
    # the 500-char default.
    "dependency_analyzer",
    # diff_files produces unified diffs of file changes (before/after).  These
    # are used to verify edits and plan follow-up fixes, so they are worth
    # preserving longer than the 500-char default.
    "diff_files",
    # feature_search and project_map produce structured code-search results;
    # they are frequently consulted when planning edits so they warrant the
    # same 1 500-char threshold as other code-reading tools.
    "feature_search",
    "project_map",
})

# Minimum character count for a non-tool-call LLM response to be considered
# plausible.  Responses shorter than this with no tool calls almost always
# indicate truncation, a stop-sequence mis-fire, or a context-overflow
# condition — not a valid empty answer.  A warning is logged so operators can
# diagnose silent failures before they corrupt scores or produce empty output.
_SHORT_RESPONSE_CHARS: int = 50

# ---------------------------------------------------------------------------
# execution_log — full output, no truncation
# ---------------------------------------------------------------------------
# Each tool call's output is stored in execution_log for artifact writing
# (tool_log.json).  Previous versions capped output at 4 000 chars to save
# memory, but this discarded ~34% of tool output data — making post-run
# analysis incomplete.  Now we keep full output so tool_log.json is a
# faithful archive of every tool interaction.

# Maximum scratchpad notes kept in memory.  Older notes are evicted when
# this cap is reached; the most recent entries are retained.
_SCRATCHPAD_MAX_NOTES: int = 30


async def _call_with_retry(coro_factory, *, max_retries: int = _MAX_RETRIES) -> Any:
    """Execute ``coro_factory()`` with exponential-backoff retry on transient errors.

    ``coro_factory`` must be a zero-argument callable that returns a fresh
    coroutine each time — a coroutine object cannot be awaited twice.

    Raises the last exception when all retries are exhausted, or immediately
    for non-retryable errors (auth failures, bad requests, etc.).
    """
    delay = _INITIAL_DELAY

    for attempt in range(max_retries + 1):
        try:
            return await coro_factory()
        except _RETRYABLE_EXCEPTIONS as exc:
            if attempt == max_retries:
                log.error(
                    "LLM call failed after %d attempt(s): %s: %s",
                    attempt + 1,
                    type(exc).__name__,
                    exc,
                )
                raise
            # Jitter: ±20% of the nominal delay to spread retries across
            # concurrent calls that hit the same overload window.
            jitter = delay * 0.2 * (2 * random.random() - 1)
            wait = min(delay + jitter, _MAX_DELAY)
            log.warning(
                "LLM transient error (attempt %d/%d): %s — retrying in %.1fs",
                attempt + 1,
                max_retries + 1,
                type(exc).__name__,
                wait,
            )
            await asyncio.sleep(wait)
            delay = min(delay * _BACKOFF_FACTOR, _MAX_DELAY)
        except Exception:
            # Non-retryable — propagate immediately
            raise

    raise RuntimeError("unreachable")  # pragma: no cover


# ---------------------------------------------------------------------------
# Conversation-history pruning helpers
# ---------------------------------------------------------------------------


def _estimate_conversation_chars(conversation: list[dict[str, Any]]) -> int:
    """Return a cheap character-count estimate for the entire conversation.

    Walks every message's content field and sums the lengths of all text
    strings found in it.  Handles both string content (plain user/assistant
    turns) and list content (mixed text + tool_use + tool_result blocks).

    This is intentionally an *undercount* — JSON field names, IDs, and
    non-text bytes are ignored — so the trigger threshold should include
    a safety margin.  The important thing is detecting runaway growth, not
    precision accounting.
    """
    total = 0
    for msg in conversation:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                # text blocks and tool_use input (which can be large JSON)
                text_val = block.get("text", "")
                if isinstance(text_val, str):
                    total += len(text_val)
                # tool_result content is a list of sub-blocks
                sub_content = block.get("content", "")
                if isinstance(sub_content, str):
                    total += len(sub_content)
                elif isinstance(sub_content, list):
                    for sub in sub_content:
                        if isinstance(sub, dict):
                            sub_text = sub.get("text", "")
                            if isinstance(sub_text, str):
                                total += len(sub_text)
                            # Count image blocks by their base64 data size
                            if sub.get("type") == "image":
                                source = sub.get("source", {})
                                if isinstance(source, dict):
                                    total += len(source.get("data", ""))
    return total


def _prune_conversation_tool_outputs(
    conversation: list[dict[str, Any]],
    target_chars: int,
    keep_recent_pairs: int,
) -> tuple[list[dict[str, Any]], int, int]:
    """Truncate tool-result text in older turns to bring conversation under *target_chars*.

    Preserves the *keep_recent_pairs* most recent assistant+user (tool-result)
    message pairs verbatim so the model still sees fresh context.  Only
    tool-result content blocks in older user-role messages are truncated.

    SAFETY INVARIANTS (verified by design, not runtime checks):
    1. System prompt is NEVER at risk.  It is passed as the ``system`` keyword
       argument directly to the Anthropic API, separate from ``messages``.  It
       is not present in the ``conversation`` list at all — so this function
       physically cannot touch it.
    2. Plain-text initial user messages are NEVER pruned.  The candidate index
       list is built by selecting only user messages whose ``content`` is a
       *list* containing at least one ``{"type": "tool_result"}`` block.  A
       plain-text opening turn has ``content`` as a bare string, so it is
       excluded from pruning candidates before the loop even starts.
    3. No messages are removed or reordered.  The Anthropic API requires that
       every ``tool_use`` block has a matching ``tool_result`` block with the
       same ``tool_use_id``.  Removing messages would break this invariant.
       This function only shortens text inside existing tool_result sub-blocks,
       keeping the structural pairing intact.

    The Anthropic API requires structural integrity: every tool_use block must
    have a matching tool_result block with the same ID.  This function never
    removes or reorders messages — it only shortens text inside existing
    tool_result content blocks, so structural integrity is always maintained.

    Args:
        conversation:       The current conversation list (mutated in-place).
        target_chars:       Desired total character count after pruning.
        keep_recent_pairs:  Number of trailing assistant+user pairs to skip.

    Returns:
        (pruned_conversation, messages_pruned, chars_removed) for logging.
    """
    # Identify indices of user messages that contain tool_result blocks.
    # We walk in *forward* order so we can skip the most recent N pairs.
    tool_result_indices: list[int] = []
    for i, msg in enumerate(conversation):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if not isinstance(content, list):
            continue
        if any(
            isinstance(b, dict) and b.get("type") == "tool_result"
            for b in content
        ):
            tool_result_indices.append(i)

    # The most recent `keep_recent_pairs` entries are protected.
    protected_indices: set[int] = set(tool_result_indices[-keep_recent_pairs:])

    # Build a mapping from tool_use_id → tool_name by scanning all assistant
    # messages.  This lets us apply tool-specific pruning thresholds and
    # produce better stubs via _make_compact_stub.
    tool_id_to_name: dict[str, str] = {}
    for msg in conversation:
        if msg.get("role") != "assistant":
            continue
        for block in msg.get("content", []):
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            bid = block.get("id", "")
            bname = block.get("name", "")
            if bid and bname:
                tool_id_to_name[bid] = bname

    msgs_pruned = 0
    chars_removed = 0

    for idx in tool_result_indices:
        if idx in protected_indices:
            continue
        current = _estimate_conversation_chars(conversation)
        if current <= target_chars:
            break

        content = conversation[idx]["content"]
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            # Look up tool name; fall back to generic label if unknown.
            tool_use_id = block.get("tool_use_id", "")
            tool_name = tool_id_to_name.get(tool_use_id, "tool")
            # Low-signal navigational tools (grep, glob, list, etc.) are stubbed
            # aggressively; medium-signal tools (bash, symbol_extractor, …) get
            # a moderately generous threshold; high-signal tools (test_runner,
            # python_eval, lint_check) get the most generous threshold so the
            # model retains critical context (test failures, eval errors, etc.).
            if tool_name in _HIGH_SIGNAL_TOOLS:
                threshold = 2_000
            elif tool_name in _MEDIUM_SIGNAL_TOOLS:
                threshold = 1_500
            elif tool_name in _LOW_SIGNAL_PRUNE_TOOLS:
                threshold = 200
            else:
                threshold = 500
            sub_content = block.get("content", [])
            if not isinstance(sub_content, list):
                continue
            for sub in sub_content:
                if not isinstance(sub, dict) or sub.get("type") != "text":
                    continue
                original_text = sub.get("text", "")
                original_len = len(original_text)
                # Content-aware override: a bash output that looks like a test
                # or compile run is treated as high-signal (threshold 2000)
                # even though bash is normally medium-signal (1500).
                effective_threshold = threshold
                if (
                    tool_name == "bash"
                    and threshold < 2_000
                    and _bash_is_test_output(original_text)
                ):
                    effective_threshold = 2_000
                if original_len <= effective_threshold:
                    continue  # already small enough
                # Use the same compact-stub logic as proactive compaction so
                # the LLM sees a consistent, signal-preserving summary format.
                # Pass tool-specific max_signal_lines/max_line_chars to match
                # the behaviour of _compact_old_tool_results (previously the
                # default values were used, giving fewer signal lines for
                # high-signal tools than the proactive path did).
                _is_high = (
                    tool_name in _HIGH_SIGNAL_TOOLS
                    or (tool_name == "bash" and _bash_is_test_output(original_text))
                )
                if _is_high:
                    _max_sig, _max_line = 15, 300
                elif tool_name in _MEDIUM_SIGNAL_TOOLS:
                    _max_sig, _max_line = 12, 250
                elif tool_name in _LOW_SIGNAL_PRUNE_TOOLS:
                    # Aggressively compact: file-lists and search results are
                    # rarely re-read in full; 5 signal lines is plenty.
                    _max_sig, _max_line = 5, 120
                else:
                    _max_sig, _max_line = 8, 200
                stub = _make_compact_stub(
                    tool_name, original_text,
                    max_signal_lines=_max_sig,
                    max_line_chars=_max_line,
                )
                if len(stub) >= original_len:
                    continue  # no space saving — leave original
                sub["text"] = stub
                chars_removed += original_len - len(stub)
                msgs_pruned += 1

    return conversation, msgs_pruned, chars_removed


# ---------------------------------------------------------------------------
# Per-loop file-read cache
# ---------------------------------------------------------------------------
# Tool loops commonly read the same file multiple times (e.g. the LLM reads a
# file, edits it, then re-reads to verify — but it also re-reads unchanged
# files just because they fell off its attention).  Caching read_file results
# within a single tool loop avoids injecting duplicate multi-KB tool results
# into the conversation, which directly reduces input-token growth per turn.

_WRITE_TOOLS = frozenset({
    "write_file", "edit_file", "file_patch", "find_replace",
    # Batch variants — each carries a list of paths rather than a single
    # `path`; their mutated paths are extracted below.
    "batch_edit", "batch_write",
})

# Tools that don't mutate shared state and can be executed in parallel within
# a single LLM turn. Pure-function / read-filesystem / search / analysis
# tools only — anything that writes files, runs shell commands, or hits the
# network must stay sequential.
_READ_ONLY_TOOL_NAMES = frozenset({
    # file / directory reads
    "batch_read", "read_file", "tree", "list_directory", "diff_files",
    # search / grep
    "grep_search", "glob_search", "feature_search", "todo_scan",
    # git reads (no --write)
    "git_status", "git_diff", "git_log", "git_search",
    # static analysis (no side effects)
    "code_analysis", "symbol_extractor", "cross_reference",
    "data_flow", "call_graph", "dependency_analyzer",
    # misc read/transform
    "json_transform", "tool_discovery", "scratchpad",
    # new analysis tools (no side effects)
    "file_info", "lint_check", "project_map", "context_budget",
    # skills (read-only lookup)
    "skill_lookup",
})


from harness.tools.path_utils import extract_written_paths  # noqa: E402  (after TYPE_CHECKING guard)


class _CachedToolRegistry:
    """Thin wrapper around ToolRegistry that caches file-read results.

    Cache lifetime = one ``call_with_tools()`` invocation.  Writes to the
    same path invalidate the cache entry so the next read sees fresh content.
    Both ``read_file`` (single path) and ``batch_read`` (multi-path) are
    de-duplicated; in the batch case we filter out paths the agent has
    already seen this turn and only fetch the remainder, so a 10-path
    batch_read after a 5-path batch_read only does 5 disk reads.
    """

    def __init__(self, inner: ToolRegistry) -> None:
        self._inner = inner
        # Full cache: (path, offset, limit) → ToolResult for single reads.
        self._cache: dict[tuple[str, int, int], ToolResult] = {}
        # Paths invalidated by a write; next read from disk re-hydrates.
        self._dirty_paths: set[str] = set()
        # Per-turn "already seen" path map used by batch_read to skip
        # re-fetching paths (with the same offset/limit) the agent has
        # already pulled into context.  Keyed by path; value is the set
        # of (offset, limit) pairs that have been fetched.
        self._read_seen: dict[str, set[tuple[int, int]]] = {}

    @property
    def _tools(self) -> dict:          # forward for to_api_schema()
        return self._inner._tools      # noqa: SLF001

    def to_api_schema(self) -> list[dict[str, Any]]:
        return self._inner.to_api_schema()

    async def execute(
        self,
        name: str,
        config: HarnessConfig,
        params: dict[str, Any],
    ) -> ToolResult:
        # Invalidate cache on writes *before* execution so that a failed
        # write still clears the stale entry. Both single- and batch-write
        # tools go through here; extract_written_paths normalises the shape.
        if name in _WRITE_TOOLS:
            for path_str in extract_written_paths(name, params):
                self._dirty_paths.add(path_str)
                self._read_seen.pop(path_str, None)

        if name == "read_file":
            path_str = str(params.get("path", ""))
            offset = int(params.get("offset", 1))
            limit = int(params.get("limit", 2000))
            key = (path_str, offset, limit)
            if key in self._cache and path_str not in self._dirty_paths:
                log.debug("file cache HIT: %s offset=%d limit=%d", path_str, offset, limit)
                return self._cache[key]
            result = await self._inner.execute(name, config, params)
            if not result.is_error:
                self._cache[key] = result
                self._read_seen.setdefault(path_str, set()).add((offset, limit))
                # If the path was dirty, a fresh read clears the dirty flag
                self._dirty_paths.discard(path_str)
            return result

        if name == "batch_read":
            raw_paths = params.get("paths") or []
            if isinstance(raw_paths, list) and raw_paths:
                b_offset = int(params.get("offset", 1))
                b_limit = int(params.get("limit", 2000))
                already = [
                    p for p in raw_paths
                    if isinstance(p, str)
                    and (b_offset, b_limit) in self._read_seen.get(p, set())
                    and p not in self._dirty_paths
                ]
                to_fetch = [
                    p for p in raw_paths
                    if isinstance(p, str)
                    and (b_offset, b_limit) not in self._read_seen.get(p, set())
                ]
                # All paths already seen — short-circuit with a hint so
                # the LLM's context isn't re-filled with content it already
                # has in the conversation (or should have saved via scratchpad).
                if already and not to_fetch:
                    hint = (
                        f"[batch_read cache] All {len(already)} path(s) were "
                        "already read earlier in this turn. Review your "
                        "earlier tool results or scratchpad notes — do NOT "
                        "re-read files to recall content. "
                        "To read a DIFFERENT section of the same file, use a "
                        "different offset or limit.\n"
                        f"Paths: {', '.join(already)}"
                    )
                    return ToolResult(output=hint, metadata={"cache_hit_all": True})
                # Partial overlap — fetch only the uncached subset; prepend
                # a note that explains which paths were skipped.
                if already and to_fetch:
                    fetch_params = {**params, "paths": to_fetch}
                    result = await self._inner.execute(name, config, fetch_params)
                    if not result.is_error:
                        for p in to_fetch:
                            self._read_seen.setdefault(p, set()).add((b_offset, b_limit))
                            self._dirty_paths.discard(p)
                        note = (
                            f"[Note] {len(already)} path(s) omitted (already "
                            f"read earlier this turn): {', '.join(already)}\n\n"
                        )
                        return ToolResult(
                            output=note + result.output,
                            metadata={**(result.metadata or {}), "cache_skipped": already},
                        )
                    return result
                # Nothing cached — full fetch as usual.
                result = await self._inner.execute(name, config, params)
                if not result.is_error:
                    for p in to_fetch:
                        self._read_seen.setdefault(p, set()).add((b_offset, b_limit))
                        self._dirty_paths.discard(p)
                return result
            # Malformed paths — let the tool report the error.
            return await self._inner.execute(name, config, params)

        return await self._inner.execute(name, config, params)


# ---------------------------------------------------------------------------
# Proactive tool-result compaction
# ---------------------------------------------------------------------------
# Instead of waiting until the conversation hits 600 K chars (the old
# _prune_conversation_tool_outputs threshold), we proactively compact old
# tool results after every turn once the conversation exceeds a modest
# number of message pairs.  Old results are replaced with one-line summaries
# that preserve *what* tool was called but drop the multi-KB output.

_COMPACT_MIN_TURNS: int = 6       # keep first N turns fully intact
_COMPACT_KEEP_RECENT: int = 3     # always keep last N assistant+user pairs
_COMPACT_MIN_TEXT_LEN: int = 500  # only compact results above this size
_COMPACT_PREVIEW_CHARS: int = 300  # chars of preview to keep in compact stub

# Tools whose output is pure listings (file paths, tree nodes, log lines).
# For these, showing a 300-char preview adds zero signal — the LLM already
# knows what it searched for from the tool call.  Instead we show a
# count of non-blank lines (≈ number of matches / files) as a 1-line summary.
_LIST_OUTPUT_TOOLS: frozenset[str] = frozenset({
    "glob_search",
    "list_directory",
    "tree",
    "git_log",
})

# These tools have useful context at the top of their output (search term,
# diff headers, etc.) but don't need 300 chars.  Use a shorter preview.
_SHORT_PREVIEW_CHARS: int = 100
_SHORT_PREVIEW_TOOLS: frozenset[str] = frozenset({
    "grep_search",
    "git_status",
    "git_diff",
    "tool_discovery",
    "todo_scan",
    "feature_search",
})

# Keywords that indicate high-signal output; their lines are preserved.
# All patterns must be lowercase — they are matched against text.lower().
# Chosen to match real test/evaluation output without over-matching prose.
# - Avoid "result" (too broad: matches "tool_result", "result_dict", etc.).
# - Avoid bare "pass" (matches "password", "bypass"); use "passed" instead.
# - "score" alone matches docstrings; keep it since evaluator output says "score N"
#   and matching is substring-based so it’s hard to avoid without regex.
_HIGH_SIGNAL_PATTERNS = (
    # Test / CI output (lowercase; matching is case-insensitive via .lower())
    "passed", "failed", "error",
    "warning",
    # Exception / stack traces
    "exception", "traceback", "assertionerror", "typeerror", "valueerror",
    # Additional Python runtime/import errors (common in bash compile-check output)
    "nameerror", "attributeerror", "syntaxerror", "importerror",
    "modulenotfounderror", "keyerror", "indexerror", "runtimeerror",
    "filenotfounderror",
    # Evaluation / scoring markers
    "score", "verdict",
    # Assertion keywords
    "assert ", "assert:",
    # Severity / status words
    "critical", "fatal",
    # Unicode pass/fail indicators (not lowercased — symbols; test has exemption)
    "✓", "✗", "✘",
)

# Patterns that indicate a bash output is from a test/compile run and should be
# treated as high-signal (same retention threshold as test_runner / lint_check).
# Checked case-insensitively on the combined text.
_BASH_TEST_PATTERNS = (
    "passed",       # pytest: "3 passed", "all 7 passed"
    "failed",       # pytest: "1 failed"
    "::test_",      # pytest test IDs
    "====",         # pytest section separator (===== FAILURES =====)
    "----",         # pytest section separator (----- traceback -----)
    "short test summary",  # pytest -q summary line
    "syntaxerror",  # Python compilation error
    "traceback (",  # Python traceback header
    "exit code:",   # [exit code: N] markers
    "ruff check",   # ruff linter invocation
    " error[",      # ruff/mypy error format: "error[E302]"
    "warning[",     # ruff/mypy warning format
    "import error", # import failures
    ".pyc",         # compiled Python
)

# Regex for pytest / test_runner summary lines such as:
#   "42 passed in 1.30s"  "3 failed, 1 error"  "1 passed, 2 warnings"
# Used by _make_compact_stub to surface the verdict at the top of the stub.
_PYTEST_SUMMARY_RE = re.compile(r"\d+\s+(?:passed|failed|error)", re.IGNORECASE)


def _bash_is_test_output(text: str) -> bool:
    """Return True when a bash tool output looks like a test/compile/lint run.

    When True, callers should treat the output as high-signal and use the same
    retention threshold as test_runner / lint_check (2000 chars) rather than
    the default medium-signal bash threshold (1500 chars).
    """
    if not text:
        return False
    lower = text.lower()
    return any(pat in lower for pat in _BASH_TEST_PATTERNS)


def _make_compact_stub(
    tool_name: str,
    text: str,
    max_signal_lines: int = 8,
    max_line_chars: int = 200,
) -> str:
    """Return a compact, information-preserving stub for a large tool result.

    The stub always includes:
    - The original character count so the LLM knows how much was dropped.
    - A preview of the first *_COMPACT_PREVIEW_CHARS* characters (truncated at
      a newline boundary where possible) so the LLM can recall the context.
    - Any high-signal lines (errors, scores, verdicts, pass/fail indicators)
      found anywhere in the full text, deduplicated and capped at *max_signal_lines*.

    Callers may pass higher *max_signal_lines* for tools that are expected to
    produce many independent error/signal entries (e.g. bash running a test suite).

    This is much more useful than a blank ``[tool: N chars, compacted]`` stub
    because the LLM can see *what* the result was about and whether it
    succeeded, without having to re-read the full output.
    """
    n = len(text)
    lines_all = text.splitlines()

    # --- Determine preview strategy based on tool type ---
    # List-output tools (glob_search, list_directory, tree, git_log) produce
    # pure file-path / log-line listings.  Their first 300 chars are noise;
    # a simple non-blank-line count is far more useful.
    # Short-preview tools have useful context at the top but don't need 300 chars.
    if tool_name in _LIST_OUTPUT_TOOLS:
        preview = ""
        non_blank = sum(1 for ln in lines_all if ln.strip())
        count_label = {
            "glob_search": "files",
            "list_directory": "entries",
            "tree": "nodes",
            "git_log": "commits",
        }.get(tool_name, "lines")
        count_line = f"{non_blank} {count_label} listed"
    else:
        count_line = ""
        preview_chars = _SHORT_PREVIEW_CHARS if tool_name in _SHORT_PREVIEW_TOOLS else _COMPACT_PREVIEW_CHARS
        preview_raw = text[:preview_chars]
        last_nl = preview_raw.rfind("\n")
        if last_nl > 20:
            preview = preview_raw[:last_nl].rstrip()
        else:
            preview = preview_raw.rstrip()

    # --- High-signal lines: any line containing a keyword (case-insensitive) ---
    signal_lines: list[str] = []
    seen: set[str] = set()
    for line in lines_all:
        stripped = line.strip()
        if not stripped or stripped in seen:
            continue
        lower_line = stripped.lower()
        if any(kw in lower_line for kw in _HIGH_SIGNAL_PATTERNS):
            # Skip lines that are part of the preview (avoid duplication).
            # Truncate very long lines so a single verbose line can't bloat
            # the stub (e.g. one-liner output with 3000 chars on a single line).
            truncated = stripped[:max_line_chars]
            if truncated not in preview:
                signal_lines.append(truncated)
                seen.add(truncated)
            if len(signal_lines) >= max_signal_lines:
                break

    # --- test_runner / pytest: extract the final summary line prominently ---
    # Pytest summary looks like "3 passed, 1 failed in 0.12s" or "42 passed in 1.3s"
    # or "5 failed, 2 errors".  Placing it at the top of the stub makes the
    # verdict instantly visible without reading through the signal lines.
    summary_line: str = ""
    if tool_name in ("test_runner", "bash"):
        for line in reversed(lines_all):
            stripped_l = line.strip()
            if _PYTEST_SUMMARY_RE.search(stripped_l):
                summary_line = stripped_l[:max_line_chars]
                break

    parts = [f"[{tool_name}: {n} chars, compacted]"]
    if summary_line:
        parts.append(f"summary: {summary_line}")
    if count_line:
        parts.append(count_line)
    if preview:
        parts.append(f"preview: {preview}")
    if signal_lines:
        parts.append("signal: " + " | ".join(signal_lines))
    return "\n".join(parts)


def _compact_old_tool_results(conversation: list[dict[str, Any]]) -> int:
    """Replace old tool-result text with compact, signal-preserving stubs.

    Returns the number of blocks compacted.
    """
    # Find tool_result message indices (same logic as _prune_conversation_tool_outputs)
    tr_indices: list[int] = []
    for i, msg in enumerate(conversation):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if not isinstance(content, list):
            continue
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            tr_indices.append(i)

    if len(tr_indices) <= _COMPACT_KEEP_RECENT:
        return 0

    protected = set(tr_indices[-_COMPACT_KEEP_RECENT:])
    compacted = 0

    for idx in tr_indices:
        if idx in protected:
            continue
        content = conversation[idx]["content"]
        # Find the preceding assistant message to extract tool names
        tool_names: dict[str, str] = {}  # tool_use_id -> name
        if idx > 0:
            prev = conversation[idx - 1]
            if prev.get("role") == "assistant" and isinstance(prev.get("content"), list):
                for b in prev["content"]:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        tool_names[b.get("id", "")] = b.get("name", "tool")

        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            sub_content = block.get("content", [])
            if not isinstance(sub_content, list):
                continue
            # Strip image blocks from old tool results — they are the
            # largest context consumers (~100KB each).  Replace with a
            # small text placeholder so the LLM knows an image was here.
            orig_len = len(sub_content)
            filtered = [
                s for s in sub_content
                if not (isinstance(s, dict) and s.get("type") == "image")
            ]
            if len(filtered) < orig_len:
                n_removed = orig_len - len(filtered)
                filtered.insert(0, {
                    "type": "text",
                    "text": f"[{n_removed} image(s) removed from old result]",
                })
                block["content"] = filtered
                compacted += n_removed
                sub_content = filtered
            for sub in sub_content:
                if not isinstance(sub, dict) or sub.get("type") != "text":
                    continue
                text = sub.get("text", "")
                tool_id = block.get("tool_use_id", "")
                tool_name = tool_names.get(tool_id, "tool")

                # Choose compaction threshold by tool type:
                # - High-signal tools (test_runner, python_eval): larger threshold,
                #   more signal lines — their output is worth keeping longer.
                # - Medium-signal tools (bash, lint_check): higher threshold than
                #   default because they often contain compilation/test output.
                # - Low-signal tools (grep, glob, etc.): aggressively compact.
                # - Default: _COMPACT_MIN_TEXT_LEN (500).
                # Content-aware override: bash outputs that look like test/compile
                # runs get the same high-signal treatment as test_runner.
                _treat_as_high = (
                    tool_name in _HIGH_SIGNAL_TOOLS
                    or (tool_name == "bash" and _bash_is_test_output(text))
                )
                if _treat_as_high:
                    min_len = 2000
                    max_signal = 15
                    max_line = 300
                elif tool_name in _MEDIUM_SIGNAL_TOOLS:
                    min_len = 1500
                    max_signal = 12
                    max_line = 250
                elif tool_name in _LOW_SIGNAL_PRUNE_TOOLS:
                    # Aggressively compact: file-lists and search results older
                    # than _COMPACT_KEEP_RECENT turns are rarely re-read.
                    # Threshold of 200 chars captures even modest outputs.
                    min_len = 200
                    max_signal = 5
                    max_line = 120
                else:
                    min_len = _COMPACT_MIN_TEXT_LEN  # 500 default
                    max_signal = 8
                    max_line = 200

                if len(text) < min_len:
                    continue
                # Build a compact, signal-preserving summary.
                # Only replace if the stub is actually shorter than the original;
                # header overhead can make stubs larger for very short inputs.
                stub = _make_compact_stub(
                    tool_name, text,
                    max_signal_lines=max_signal,
                    max_line_chars=max_line,
                )
                if len(stub) < len(text):
                    sub["text"] = stub
                    compacted += 1

    return compacted


def _summarise_tool_input(tool_name: str, params: dict[str, Any]) -> str:
    """Return a short, human-readable summary of a tool call's key parameters.

    Used in log lines — keeps them to one line without truncating silently.
    """
    # File-path tools: show the path
    for key in ("path", "source", "destination"):
        if key in params and params[key]:
            val = str(params[key])
            if len(val) > 80:
                val = "…" + val[-77:]
            return f"({val})"
    # Bash: show the command (truncated)
    if tool_name == "bash" and "command" in params:
        cmd = str(params["command"])
        if len(cmd) > 80:
            cmd = cmd[:77] + "…"
        return f"($ {cmd})"
    # Search tools: show pattern
    if "pattern" in params:
        return f"(pattern={params['pattern']!r})"
    # Fallback: first key=value pair
    if params:
        k, v = next(iter(params.items()))
        v_str = str(v)
        if len(v_str) > 60:
            v_str = v_str[:57] + "…"
        return f"({k}={v_str!r})"
    return ""


@dataclass
class Message:
    role: str  # "user" | "assistant"
    content: Any  # str or list[content blocks]


@dataclass
class LLMResponse:
    """Parsed response from one API call."""

    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str = ""
    raw: Any = None


class LLM:
    """Async Claude API client with optional tool_use loop."""

    def __init__(self, config: HarnessConfig) -> None:
        self.config = config
        kwargs: dict[str, Any] = {}
        # Config fields take priority; fall back to env vars only if config is empty.
        base_url = config.base_url or os.environ.get("HARNESS_BASE_URL") or ""
        if base_url:
            kwargs["base_url"] = base_url
        api_key = config.api_key or os.environ.get("HARNESS_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY") or ""
        if api_key:
            kwargs["api_key"] = api_key
        log.info("LLM client: base_url=%s model=%s", base_url or "(default)", config.model)
        self.client = anthropic.AsyncAnthropic(**kwargs)
        # API-concurrency cap: clamp the configured value into [1, 20] so a
        # misconfigured 0 doesn't deadlock every caller and an accidentally-huge
        # value doesn't ignore the underlying provider's rate limit.
        _max_cc = max(1, min(int(getattr(config, "max_concurrent_llm_calls", 4)), 20))
        self._api_semaphore = asyncio.Semaphore(_max_cc)

    async def call(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        timeout: int = 300,
    ) -> LLMResponse:
        """Single API call — returns parsed LLMResponse.

        Args:
            timeout: Per-call timeout in seconds (default 300 = 5 min).
                     Raises asyncio.TimeoutError on expiry.
        """
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": max_tokens or self.config.max_tokens,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools
        # DeepSeek V4 models have thinking mode enabled by default.
        # The harness doesn't pass thinking blocks back to the API, which
        # causes a 400 error on subsequent turns. Disable thinking explicitly.
        if (self.config.model or "").lower().startswith("deepseek"):
            kwargs["thinking"] = {"type": "disabled"}

        t0 = time.monotonic()
        # Gate every API call through the per-process semaphore so the
        # provider's rate limit is respected even when multiple concurrent
        # tasks (dual evaluators, parallel tool calls) fire simultaneously.
        async with self._api_semaphore:
            resp = await _call_with_retry(
                lambda: asyncio.wait_for(
                    self.client.messages.create(**kwargs),
                    timeout=timeout,
                )
            )
        elapsed = time.monotonic() - t0

        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []

        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    {"id": block.id, "name": block.name, "input": block.input}
                )

        # Log usage when available (usage object may not exist in all SDK versions).
        # Claude prompt-caching returns cache_read_input_tokens and
        # cache_creation_input_tokens in the usage object; log these when present
        # so operators can see cache hit rates without consulting the API dashboard.
        usage = getattr(resp, "usage", None)
        if usage is not None:
            in_tok = getattr(usage, "input_tokens", None)
            out_tok = getattr(usage, "output_tokens", None)
            cache_read = getattr(usage, "cache_read_input_tokens", None)
            cache_create = getattr(usage, "cache_creation_input_tokens", None)

            # Build a compact token summary string, omitting cache fields when
            # they are absent or zero (keeps logs clean for non-caching models).
            token_parts = [
                f"in_tok={in_tok if in_tok is not None else '?'}",
                f"out_tok={out_tok if out_tok is not None else '?'}",
            ]
            if cache_read:
                token_parts.append(f"cache_read={cache_read}")
            if cache_create:
                token_parts.append(f"cache_create={cache_create}")
            token_str = " ".join(token_parts)

            log.info(
                "LLM call: model=%s stop=%s %s latency=%.1fs",
                self.config.model,
                resp.stop_reason,
                token_str,
                elapsed,
            )
        else:
            log.info(
                "LLM call: model=%s stop=%s latency=%.1fs",
                self.config.model,
                resp.stop_reason,
                elapsed,
            )

        response_text = "\n".join(text_parts)

        # Warn when the model returns a very short text response with no tool
        # calls.  Responses shorter than _SHORT_RESPONSE_CHARS that contain no
        # tool calls usually indicate a truncated or failed generation — the
        # model ran out of tokens, was cut off by a stop sequence, or received
        # a context that left nothing meaningful to say.  These near-empty
        # responses look like valid LLMResponse objects but carry no useful
        # content; scoring them later (e.g. in parse_score) silently returns 0.
        if len(response_text) < _SHORT_RESPONSE_CHARS and not tool_calls:
            log.warning(
                "LLM call: suspiciously short response (%d chars, no tool calls) — "
                "possible truncation or failed generation "
                "(stop=%r, model=%s)",
                len(response_text),
                resp.stop_reason,
                self.config.model,
            )

        return LLMResponse(
            text=response_text,
            tool_calls=tool_calls,
            stop_reason=resp.stop_reason,
            raw=resp,
        )

    async def call_with_tools(
        self,
        messages: list[dict[str, Any]],
        registry: ToolRegistry,
        *,
        system: str = "",
        max_turns: int = 30,
    ) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """Run a full tool_use agent loop.

        Returns ``(final_text, execution_log, llm_calls, conversation, raw_conversation)``
        where:

        - *execution_log* — list of tool call records
        - *llm_calls* — per-turn LLM API metadata (tokens, latency, model)
        - *conversation* — full message list at loop exit (post-pruning / compaction)
        - *raw_conversation* — full message list without any compaction (original tool outputs)
        """
        cached_registry = _CachedToolRegistry(registry)
        tools_schema = cached_registry.to_api_schema()
        conversation = list(messages)
        raw_conversation: list[dict[str, Any]] = copy.deepcopy(messages)
        execution_log: list[dict[str, Any]] = []
        llm_calls: list[dict[str, Any]] = []
        loop_start = time.monotonic()
        total_in_tokens: int = 0
        total_out_tokens: int = 0
        # Emit a mid-loop WARNING when cumulative output tokens cross this
        # threshold — a strong signal of a runaway or unproductive tool loop.
        # Set relative to max_tokens so it scales with the configured budget.
        _TOKEN_SPEND_WARN = self.config.max_tokens * 4

        # Accumulated scratchpad notes — the LLM can call the scratchpad tool
        # to save findings that survive conversation pruning. We re-inject
        # them into the system prompt on every turn.
        scratchpad_notes: list[str] = []

        for turn in range(max_turns):
            turn_start = time.monotonic()
            # Build the effective system prompt — original + any scratchpad
            # notes accumulated this loop. The notes sit at the top because
            # that's the highest-attention region across models and survives
            # any future mid-conversation truncation.
            if scratchpad_notes:
                notes_block = (
                    f"## Your Exploration Notes ({len(scratchpad_notes)} entr"
                    f"{'ies' if len(scratchpad_notes) != 1 else 'y'})\n"
                    + "\n".join(f"- {n}" for n in scratchpad_notes)
                    + "\n\n"
                )
                effective_system = notes_block + (system or "")
            else:
                effective_system = system
            resp = await self.call(conversation, system=effective_system, tools=tools_schema)
            turn_elapsed = time.monotonic() - turn_start

            # Accumulate token counts and capture per-turn LLM metadata
            turn_meta: dict[str, Any] = {
                "turn": turn + 1,
                "model": self.config.model,
                "stop_reason": resp.stop_reason,
                "elapsed_s": round(turn_elapsed, 2),
                "tool_calls": len(resp.tool_calls),
                "text_len": len(resp.text) if resp.text else 0,
                "text_preview": (resp.text or "")[:300],
            }
            if resp.raw is not None:
                _u = getattr(resp.raw, "usage", None)
                if _u is not None:
                    in_tok = getattr(_u, "input_tokens", 0) or 0
                    out_tok = getattr(_u, "output_tokens", 0) or 0
                    total_in_tokens += in_tok
                    total_out_tokens += out_tok
                    turn_meta["input_tokens"] = in_tok
                    turn_meta["output_tokens"] = out_tok
                    cache_read = getattr(_u, "cache_read_input_tokens", None)
                    cache_create = getattr(_u, "cache_creation_input_tokens", None)
                    if cache_read:
                        turn_meta["cache_read_input_tokens"] = cache_read
                    if cache_create:
                        turn_meta["cache_creation_input_tokens"] = cache_create
                # Release the full Anthropic Message object — usage has been
                # extracted and nothing else needs the raw response.
                resp.raw = None
            llm_calls.append(turn_meta)

            log.debug(
                "tool_loop turn=%d stop=%s calls=%d total_tools=%d "
                "in_tok=%d out_tok=%d elapsed=%.1fs",
                turn + 1,
                resp.stop_reason,
                len(resp.tool_calls),
                len(execution_log),
                total_in_tokens,
                total_out_tokens,
                turn_elapsed,
            )

            # Warn early when cumulative output-token spend exceeds the budget
            # threshold — helps operators abort loops that are clearly spinning.
            if total_out_tokens > _TOKEN_SPEND_WARN:
                log.warning(
                    "tool_loop turn=%d: cumulative out_tok=%d exceeds "
                    "spend-warn threshold=%d (max_tokens=%d × 4) — "
                    "loop may be unproductive; consider lowering max_tool_turns",
                    turn + 1,
                    total_out_tokens,
                    _TOKEN_SPEND_WARN,
                    self.config.max_tokens,
                )

            # Warn on unexpectedly slow turns (model overload, very large contexts)
            # so operators can spot degraded performance before the full loop times out.
            if turn_elapsed > _TURN_STALL_WARN_SECS:
                log.warning(
                    "tool_loop turn=%d took %.1fs (> %.0f s threshold) — "
                    "model may be overloaded or context window is very large",
                    turn + 1,
                    turn_elapsed,
                    _TURN_STALL_WARN_SECS,
                )

            # Build assistant message content (text + tool_use blocks).
            # The Anthropic API rejects messages whose content array is empty
            # (HTTP 400 "messages must have non-empty content").  This can
            # happen when the model produces neither text nor tool calls — an
            # unusual but observed condition on certain stop-reason edge cases.
            # Guard: if both resp.text and resp.tool_calls are absent, inject a
            # synthetic text block so the conversation stays valid and the loop
            # can continue (or exit cleanly on the next iteration check below).
            assistant_content: list[dict[str, Any]] = []
            if resp.text:
                assistant_content.append({"type": "text", "text": resp.text})
            for tc in resp.tool_calls:
                assistant_content.append(
                    {"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["input"]}
                )
            if not assistant_content:
                # Synthesise a minimal placeholder so the message is never empty.
                # Log at WARNING so operators can see this degenerate condition.
                log.warning(
                    "tool_loop turn=%d: assistant returned empty content "
                    "(stop_reason=%r) — injecting placeholder to keep conversation valid",
                    turn + 1,
                    resp.stop_reason,
                )
                assistant_content.append({"type": "text", "text": "(no output)"})
            conversation.append({"role": "assistant", "content": assistant_content})
            raw_conversation.append({"role": "assistant", "content": copy.deepcopy(assistant_content)})

            if not resp.tool_calls:
                elapsed = time.monotonic() - loop_start
                log.info(
                    "tool_loop done: turns=%d tool_calls=%d "
                    "total_in_tok=%d total_out_tok=%d elapsed=%.1fs",
                    turn + 1,
                    len(execution_log),
                    total_in_tokens,
                    total_out_tokens,
                    elapsed,
                )
                return resp.text, execution_log, llm_calls, conversation, raw_conversation

            # Classify this turn's tool calls into three lanes:
            #   scratchpad  — intercepted here (no I/O); the note gets saved
            #                 and re-injected into the system prompt next turn
            #   read-only   — pure reads/search/analysis; run in parallel via
            #                 asyncio.gather
            #   mutating    — edits / writes / bash / subprocess; run serially
            #                 because order and exclusive filesystem access matter
            # Results are keyed by the original tool_use index so the returned
            # tool_result blocks preserve the order the API expects.
            results_by_index: dict[int, tuple[ToolResult, dict[str, Any]]] = {}

            read_only_tasks: list[tuple[int, dict[str, Any]]] = []
            mutating_calls: list[tuple[int, dict[str, Any]]] = []

            for idx, tc in enumerate(resp.tool_calls):
                name = tc["name"]
                if name == "scratchpad":
                    # Handle inline — no I/O, no registry call.
                    note_raw = (tc.get("input") or {}).get("note", "")
                    note = str(note_raw).strip() if note_raw is not None else ""
                    if not note:
                        result = ToolResult(
                            error="note cannot be empty", is_error=True
                        )
                    else:
                        # Clip to the same cap the tool declares so we don't
                        # silently grow the system prompt without bound.
                        cap = 2000
                        if len(note) > cap:
                            note = note[:cap] + "… [truncated]"
                        scratchpad_notes.append(note)
                        if len(scratchpad_notes) > _SCRATCHPAD_MAX_NOTES:
                            scratchpad_notes = scratchpad_notes[-_SCRATCHPAD_MAX_NOTES:]
                        result = ToolResult(
                            output=(
                                f"[scratchpad] saved ({len(note)} chars). "
                                f"Will be re-injected into your system prompt "
                                f"on every subsequent turn this loop."
                            ),
                            metadata={"note": note},
                        )
                    results_by_index[idx] = (result, tc)
                elif name == "context_budget":
                    # Handle inline — inject live loop stats.
                    pct_turns = (
                        f"{turn + 1}/{max_turns} ({100 * (turn + 1) // max_turns}%)"
                        if max_turns > 0 else f"{turn + 1}/?"
                    )
                    budget_lines = [
                        f"Turn: {pct_turns}",
                        f"Input tokens used:  {total_in_tokens:,}",
                        f"Output tokens used: {total_out_tokens:,}",
                        f"Total tool calls:   {len(execution_log)}",
                        f"Scratchpad notes:   {len(scratchpad_notes)}",
                    ]
                    result = ToolResult(
                        output="\n".join(budget_lines),
                        metadata={
                            "turn": turn + 1,
                            "max_turns": max_turns,
                            "input_tokens": total_in_tokens,
                            "output_tokens": total_out_tokens,
                            "tool_calls": len(execution_log),
                            "scratchpad_notes": len(scratchpad_notes),
                        },
                    )
                    results_by_index[idx] = (result, tc)
                elif name in _READ_ONLY_TOOL_NAMES:
                    read_only_tasks.append((idx, tc))
                else:
                    mutating_calls.append((idx, tc))

            # Read-only batch: run in parallel.
            if read_only_tasks:
                _ro_t0 = time.monotonic()
                coros = [
                    cached_registry.execute(
                        tc["name"], self.config, tc["input"],
                    )
                    for _, tc in read_only_tasks
                ]
                ro_results = await asyncio.gather(*coros, return_exceptions=True)
                for (idx, tc), res in zip(read_only_tasks, ro_results):
                    if isinstance(res, BaseException):
                        res = ToolResult(
                            error=f"{type(res).__name__}: {res}",
                            is_error=True,
                        )
                    results_by_index[idx] = (res, tc)
                _ro_elapsed = time.monotonic() - _ro_t0
                log.info(
                    "  parallel_tools: %d read-only in %.2fs",
                    len(read_only_tasks), _ro_elapsed,
                )

            # Mutating calls: run sequentially — filesystem mutations and
            # shell commands cannot be safely interleaved with each other.
            if mutating_calls:
                _mu_t0 = time.monotonic()
                for idx, tc in mutating_calls:
                    try:
                        res = await cached_registry.execute(
                            tc["name"], self.config, tc["input"],
                        )
                    except BaseException as exc:
                        res = ToolResult(
                            error=f"{type(exc).__name__}: {exc}",
                            is_error=True,
                        )
                    results_by_index[idx] = (res, tc)
                _mu_elapsed = time.monotonic() - _mu_t0
                if len(mutating_calls) > 1:
                    log.info(
                        "  sequential_tools: %d mutating in %.2fs",
                        len(mutating_calls), _mu_elapsed,
                    )

            # Emit results IN ORIGINAL ORDER — Anthropic requires tool_result
            # blocks to match tool_use block order 1:1.
            tool_results: list[dict[str, Any]] = []
            for idx in range(len(resp.tool_calls)):
                result, tc = results_by_index[idx]
                call_detail = _summarise_tool_input(tc["name"], tc["input"])
                if result.is_error:
                    log.warning(
                        "  ✗ tool=%s %s → %s",
                        tc["name"],
                        call_detail,
                        (result.error or "")[:120],
                    )
                else:
                    log.info(
                        "  ✓ tool=%s %s",
                        tc["name"],
                        call_detail,
                    )
                raw_output = result.output or result.error
                execution_log.append(
                    {
                        "tool": tc["name"],
                        "input": tc["input"],
                        "output": raw_output,
                        "duration_ms": round(result.elapsed_s * 1000),
                        "is_error": result.is_error,
                    }
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tc["id"],
                        "content": result.to_api(),
                        "is_error": result.is_error,
                    }
                )
            conversation.append({"role": "user", "content": tool_results})
            raw_conversation.append({"role": "user", "content": copy.deepcopy(tool_results)})

            # Prune old tool-result text when the conversation grows large.
            # This prevents context-window overflow (HTTP 400 "prompt too long")
            # on long executor loops and caps per-turn input-token costs.
            # We check *after* appending so the estimate includes the fresh results.
            conv_chars = _estimate_conversation_chars(conversation)
            if conv_chars > _CONV_PRUNE_THRESHOLD_CHARS:
                conversation, n_pruned, n_removed = _prune_conversation_tool_outputs(
                    conversation,
                    target_chars=_CONV_PRUNE_TARGET_CHARS,
                    keep_recent_pairs=_CONV_PRUNE_KEEP_RECENT_PAIRS,
                )
                log.warning(
                    "tool_loop turn=%d: conversation size %d chars exceeds "
                    "threshold %d — pruned %d tool-result block(s), "
                    "removed %d chars (new estimate: ~%d chars)",
                    turn + 1,
                    conv_chars,
                    _CONV_PRUNE_THRESHOLD_CHARS,
                    n_pruned,
                    n_removed,
                    conv_chars - n_removed,
                )

            # Proactive compaction: replace old tool-result text with one-line
            # summaries once the conversation has enough turns.  This runs
            # *every* turn (after the initial warm-up) and is cheap — it only
            # scans message metadata, not the full text.
            if turn >= _COMPACT_MIN_TURNS:
                n_compacted = _compact_old_tool_results(conversation)
                if n_compacted:
                    log.info(
                        "tool_loop turn=%d: compacted %d old tool-result block(s)",
                        turn + 1,
                        n_compacted,
                    )

        elapsed = time.monotonic() - loop_start
        log.warning(
            "tool_loop hit max_turns=%d after %.1fs (%d tool calls, "
            "total_in_tok=%d total_out_tok=%d)",
            max_turns,
            elapsed,
            len(execution_log),
            total_in_tokens,
            total_out_tokens,
        )
        # Emit a structured summary that the Evaluator can parse with
        # _extract_executor_status().  The bare string "(max tool turns reached)"
        # previously returned here lacked a STATUS: line, so the evaluator
        # treated the run as STATUS: DONE and could issue a false PASS verdict
        # for an incomplete execution.  Adding STATUS: PARTIAL ensures the
        # evaluator's existing PARTIAL-override logic fires and the next
        # planner iteration receives the "execution was incomplete" feedback
        # block rather than a silent acceptance.
        #
        # COMPLETED/SKIPPED are left as "unknown" because we genuinely don't
        # know which steps finished — the tool loop was cut off mid-flight.
        # The ISSUES field names the root cause so the planner can act on it.
        partial_summary = (
            f"COMPLETED: unknown (tool loop was cut off)\n"
            f"SKIPPED: unknown (tool loop was cut off)\n"
            f"ISSUES: tool loop exhausted max_tool_turns={max_turns} after "
            f"{len(execution_log)} tool call(s) in {elapsed:.1f}s — "
            f"the plan was not fully executed.  "
            f"Reduce plan scope or raise max_tool_turns in HarnessConfig.\n"
            f"STATUS: PARTIAL"
        )
        return partial_summary, execution_log, llm_calls, conversation, raw_conversation
