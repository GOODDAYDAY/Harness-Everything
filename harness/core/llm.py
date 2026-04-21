"""Thin async wrapper around the Anthropic Claude API."""

from __future__ import annotations

import asyncio
import logging
import os
import random
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
# Raised from 150K/100K on 2026-04-21 to give long Agent-mode cycles
# (hundreds of tool turns) more working memory before older tool results
# get truncated. 300K chars ≈ 75K tokens, well inside a 200K-token context
# window once you subtract system prompt + tools schema + safety margin.
# Number of *trailing* message pairs (assistant + user) kept verbatim.
# Keeping the most recent turns intact ensures the model still sees the fresh
# tool output it just received; only older outputs are compressed.
_CONV_PRUNE_KEEP_RECENT_PAIRS: int = 3

# Minimum character count for a non-tool-call LLM response to be considered
# plausible.  Responses shorter than this with no tool calls almost always
# indicate truncation, a stop-sequence mis-fire, or a context-overflow
# condition — not a valid empty answer.  A warning is logged so operators can
# diagnose silent failures before they corrupt scores or produce empty output.
_SHORT_RESPONSE_CHARS: int = 50


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
            sub_content = block.get("content", [])
            if not isinstance(sub_content, list):
                continue
            for sub in sub_content:
                if not isinstance(sub, dict) or sub.get("type") != "text":
                    continue
                original_text = sub.get("text", "")
                original_len = len(original_text)
                if original_len <= 200:
                    continue  # already tiny — not worth truncating
                stub = f"[pruned — {original_len} chars, turn index {idx}]"
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
})


def _paths_written_by(name: str, params: dict[str, Any]) -> list[str]:
    """Extract the set of paths a write-class tool will mutate.

    Single-path tools carry a ``path`` key; batch variants carry lists.
    Returns an empty list for tools that don't match either shape so
    callers can uniformly iterate without special-casing.
    """
    if name in {"write_file", "edit_file", "file_patch", "find_replace"}:
        p = params.get("path")
        return [str(p)] if p else []
    if name == "batch_edit":
        return [
            str(e["path"]) for e in (params.get("edits") or [])
            if isinstance(e, dict) and e.get("path")
        ]
    if name == "batch_write":
        return [
            str(f["path"]) for f in (params.get("files") or [])
            if isinstance(f, dict) and f.get("path")
        ]
    return []


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
        # Per-turn "already seen" path set used by batch_read to skip
        # re-fetching paths the agent has already pulled into context.
        self._read_seen: set[str] = set()

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
        # tools go through here; _paths_written_by normalises the shape.
        if name in _WRITE_TOOLS:
            for path_str in _paths_written_by(name, params):
                self._dirty_paths.add(path_str)
                self._read_seen.discard(path_str)

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
                self._read_seen.add(path_str)
                # If the path was dirty, a fresh read clears the dirty flag
                self._dirty_paths.discard(path_str)
            return result

        if name == "batch_read":
            raw_paths = params.get("paths") or []
            if isinstance(raw_paths, list) and raw_paths:
                already = [
                    p for p in raw_paths
                    if isinstance(p, str)
                    and p in self._read_seen
                    and p not in self._dirty_paths
                ]
                to_fetch = [
                    p for p in raw_paths
                    if isinstance(p, str) and p not in self._read_seen
                ]
                # All paths already seen — short-circuit with a hint so
                # the LLM's context isn't re-filled with content it already
                # has in the conversation (or should have saved via scratchpad).
                if already and not to_fetch:
                    hint = (
                        f"[batch_read cache] All {len(already)} path(s) were "
                        "already read earlier in this turn. Review your "
                        "earlier tool results or scratchpad notes — do NOT "
                        "re-read files to recall content.\n"
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
                            self._read_seen.add(p)
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
                        self._read_seen.add(p)
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


def _compact_old_tool_results(conversation: list[dict[str, Any]]) -> int:
    """Replace old tool-result text with one-line summaries.

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
            for sub in sub_content:
                if not isinstance(sub, dict) or sub.get("type") != "text":
                    continue
                text = sub.get("text", "")
                if len(text) < _COMPACT_MIN_TEXT_LEN:
                    continue
                # Build a compact summary
                tool_id = block.get("tool_use_id", "")
                tool_name = tool_names.get(tool_id, "tool")
                sub["text"] = f"[{tool_name}: {len(text)} chars, compacted]"
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

        t0 = time.monotonic()
        # Gate every API call through the per-process semaphore so the
        # provider's rate limit is respected even when multiple pipeline
        # tasks (debate parallel rounds, dual evaluators, planner three-way)
        # fire simultaneously.
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
    ) -> tuple[str, list[dict[str, Any]]]:
        """Run a full tool_use agent loop.

        Returns (final_text, execution_log) where execution_log is a list of
        {"tool": name, "input": {...}, "output": "..."} dicts.
        """
        cached_registry = _CachedToolRegistry(registry)
        tools_schema = cached_registry.to_api_schema()
        conversation = list(messages)
        execution_log: list[dict[str, Any]] = []
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

            # Accumulate token counts for the end-of-loop summary
            if resp.raw is not None:
                _u = getattr(resp.raw, "usage", None)
                if _u is not None:
                    total_in_tokens += getattr(_u, "input_tokens", 0) or 0
                    total_out_tokens += getattr(_u, "output_tokens", 0) or 0

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
                return resp.text, execution_log

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
                        result = ToolResult(
                            output=(
                                f"[scratchpad] saved ({len(note)} chars). "
                                f"Will be re-injected into your system prompt "
                                f"on every subsequent turn this loop."
                            ),
                            metadata={"note": note},
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
                execution_log.append(
                    {
                        "tool": tc["name"],
                        "input": tc["input"],
                        "output": result.output or result.error,
                        "duration_ms": round(result.elapsed_s * 1000),
                        "is_error": result.is_error,
                    }
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tc["id"],
                        "content": [result.to_api()],
                        "is_error": result.is_error,
                    }
                )
            conversation.append({"role": "user", "content": tool_results})

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
        return partial_summary, execution_log
