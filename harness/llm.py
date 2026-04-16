"""Backward-compatibility shim — code moved to harness.core.llm.

All imports from ``harness.llm`` continue to work via this re-export.
New code should import from ``harness.core.llm`` directly.
"""
# ruff: noqa: F401, F403
from harness.core.llm import (
    LLM,
    LLMResponse,
    Message,
    _call_with_retry,
    _estimate_conversation_chars,
    _prune_conversation_tool_outputs,
    _summarise_tool_input,
)

__all__ = [
    "LLM",
    "LLMResponse",
    "Message",
    "_call_with_retry",
    "_estimate_conversation_chars",
    "_prune_conversation_tool_outputs",
    "_summarise_tool_input",
]
