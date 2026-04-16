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
_CONV_PRUNE_THRESHOLD_CHARS: int = 600_000   # trigger pruning above this total
_CONV_PRUNE_TARGET_CHARS: int = 400_000      # prune down to this target
# Number of *trailing* message pairs (assistant + user) kept verbatim.
# Keeping the most recent turns intact ensures the model still sees the fresh
# tool output it just received; only older outputs are compressed.
_CONV_PRUNE_KEEP_RECENT_PAIRS: int = 4

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
        tools_schema = registry.to_api_schema()
        conversation = list(messages)
        execution_log: list[dict[str, Any]] = []
        loop_start = time.monotonic()
        total_in_tokens: int = 0
        total_out_tokens: int = 0
        # Emit a mid-loop WARNING when cumulative output tokens cross this
        # threshold — a strong signal of a runaway or unproductive tool loop.
        # Set relative to max_tokens so it scales with the configured budget.
        _TOKEN_SPEND_WARN = self.config.max_tokens * 4

        for turn in range(max_turns):
            turn_start = time.monotonic()
            resp = await self.call(conversation, system=system, tools=tools_schema)
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

            # Execute tools and build tool_result message
            tool_results: list[dict[str, Any]] = []
            for tc in resp.tool_calls:
                result: ToolResult = await registry.execute(
                    tc["name"], self.config, tc["input"]
                )
                # Log a concise one-liner per tool call with key path/command info
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
