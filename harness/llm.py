"""Thin async wrapper around the Anthropic Claude API."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import asyncio
import os

import anthropic

from harness.config import HarnessConfig
from harness.tools.base import ToolResult
from harness.tools.registry import ToolRegistry

log = logging.getLogger(__name__)


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

        resp = await asyncio.wait_for(
            self.client.messages.create(**kwargs),
            timeout=timeout,
        )

        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []

        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    {"id": block.id, "name": block.name, "input": block.input}
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

        for turn in range(max_turns):
            resp = await self.call(conversation, system=system, tools=tools_schema)
            log.debug("Turn %d: stop_reason=%s, tool_calls=%d", turn, resp.stop_reason, len(resp.tool_calls))

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
                return resp.text, execution_log

            # Execute tools and build tool_result message
            tool_results: list[dict[str, Any]] = []
            for tc in resp.tool_calls:
                result: ToolResult = await registry.execute(
                    tc["name"], self.config, tc["input"]
                )
                execution_log.append(
                    {"tool": tc["name"], "input": tc["input"], "output": result.output or result.error}
                )
                log.info("  tool=%s  error=%s", tc["name"], result.is_error)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tc["id"],
                        "content": [result.to_api()],
                        "is_error": result.is_error,
                    }
                )
            conversation.append({"role": "user", "content": tool_results})

        return "(max tool turns reached)", execution_log
