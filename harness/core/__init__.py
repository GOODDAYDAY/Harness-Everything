"""harness.core — configuration and LLM client.

Re-exports the public API so that both old-style imports
(``from harness.config import HarnessConfig``) and new-style imports
(``from harness.core import HarnessConfig``) work transparently.
"""

from harness.core.config import (
    DualEvaluatorConfig,
    EvaluatorConfig,
    HarnessConfig,
    PipelineConfig,
    PlannerConfig,
)
from harness.core.llm import LLM, LLMResponse, Message

__all__ = [
    "DualEvaluatorConfig",
    "EvaluatorConfig",
    "HarnessConfig",
    "LLM",
    "LLMResponse",
    "Message",
    "PipelineConfig",
    "PlannerConfig",
]
