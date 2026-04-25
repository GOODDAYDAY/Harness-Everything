"""Unit tests for harness.tools.context_budget.

Covers:
  - Fallback execute path returns informational message
  - Schema has no required params
  - Name and description correctness
"""

import asyncio
from unittest.mock import Mock


from harness.core.config import HarnessConfig
from harness.tools.context_budget import ContextBudgetTool


def _run(coro):
    return asyncio.run(coro)


class TestContextBudgetFallback:
    def test_fallback_returns_info_message(self):
        tool = ContextBudgetTool()
        cfg = Mock(spec=HarnessConfig)
        cfg.workspace = "/tmp"
        cfg.allowed_paths = ["/tmp"]
        result = _run(tool.execute(cfg))
        assert not result.is_error
        assert "intercepted" in result.output.lower() or "tool loop" in result.output.lower()


class TestContextBudgetSchema:
    def test_schema_no_required(self):
        tool = ContextBudgetTool()
        schema = tool.input_schema()
        assert "required" not in schema or schema.get("required") == []

    def test_name(self):
        tool = ContextBudgetTool()
        assert tool.name == "context_budget"

    def test_description_mentions_token(self):
        tool = ContextBudgetTool()
        assert "token" in tool.description.lower()
