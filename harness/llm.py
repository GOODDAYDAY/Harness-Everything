"""Thin async wrapper around the Anthropic Claude API."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import asyncio
import os

import anthropic
from anthropic._exceptions import OverloadedError

from harness.config import HarnessConfig
from harness.tools.base import ToolResult
from harness.tools.registry import ToolRegistry

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Transient-error retry policy
# ---------------------------------------------------------------------------
# These errors are safe to retry because the request never reached the model
# (rate limit, overload, connection drop, 5xx).  We use exponential backoff
# with jitter so that a fleet of parallel calls doesn't hammer the API in lock
# step after a momentary overload event.
_RETRYABLE_EXCEPTIONS = (
    OverloadedError,                      # HTTP 529 — Claude overloaded
    anthropic.RateLimitError,             # HTTP 429 — rate limit hit
    anthropic.InternalServerError,        # HTTP 500 — transient server error
    anthropic.APIConnectionError,         # network-level failure (no response)
    anthropic.APITimeoutError,            # SDK-level timeout wrapper
)

_MAX_RETRIES: int = 4           # up to 4 retries (5 total attempts)
_INITIAL_DELAY: float = 2.0     # seconds before first retry
_BACKOFF_FACTOR: float = 2.0    # each retry waits 2× longer
_MAX_DELAY: float = 60.0        # cap the wait so we don't stall for minutes


async def _call_with_retry(coro_factory, *, max_retries: int = _MAX_RETRIES) -> Any:
    """Execute ``coro_factory()`` with exponential-backoff retry on transient errors.

    ``coro_factory`` must be a zero-argument callable that returns a fresh
    coroutine each time — a coroutine object cannot be awaited twice.

    Raises the last exception when all retries are exhausted, or immediately
    for non-retryable errors (auth failures, bad requests, etc.).
    """
    delay = _INITIAL_DELAY
    last_exc: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            return await coro_factory()
        except _RETRYABLE_EXCEPTIONS as exc:
            last_exc = exc
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
            import random
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
        # Support custom base_url and auth token via env vars
        kwargs: dict[str, Any] = {}
        base_url = os.environ.get("ANTHROPIC_BASE_URL")
        if base_url:
            kwargs["base_url"] = base_url
        auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")
        if auth_token:
            kwargs["api_key"] = auth_token
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

        # Log usage when available (usage object may not exist in all SDK versions)
        usage = getattr(resp, "usage", None)
        if usage is not None:
            log.debug(
                "LLM call: model=%s stop=%s in_tok=%s out_tok=%s latency=%.1fs",
                self.config.model,
                resp.stop_reason,
                getattr(usage, "input_tokens", "?"),
                getattr(usage, "output_tokens", "?"),
                elapsed,
            )
        else:
            log.debug(
                "LLM call: model=%s stop=%s latency=%.1fs",
                self.config.model,
                resp.stop_reason,
                elapsed,
            )

        return LLMResponse(
            text="\n".join(text_parts),
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

        for turn in range(max_turns):
            resp = await self.call(conversation, system=system, tools=tools_schema)
            log.debug(
                "tool_loop turn=%d stop=%s calls=%d total_tools=%d",
                turn + 1,
                resp.stop_reason,
                len(resp.tool_calls),
                len(execution_log),
            )

            # Build assistant message content (text + tool_use blocks)
            assistant_content: list[dict[str, Any]] = []
            if resp.text:
                assistant_content.append({"type": "text", "text": resp.text})
            for tc in resp.tool_calls:
                assistant_content.append(
                    {"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["input"]}
                )
            conversation.append({"role": "assistant", "content": assistant_content})

            if not resp.tool_calls:
                elapsed = time.monotonic() - loop_start
                log.info(
                    "tool_loop done: turns=%d tool_calls=%d elapsed=%.1fs",
                    turn + 1,
                    len(execution_log),
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
                    {"tool": tc["name"], "input": tc["input"], "output": result.output or result.error}
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

        elapsed = time.monotonic() - loop_start
        log.warning(
            "tool_loop hit max_turns=%d after %.1fs (%d tool calls)",
            max_turns,
            elapsed,
            len(execution_log),
        )
        return "(max tool turns reached)", execution_log
