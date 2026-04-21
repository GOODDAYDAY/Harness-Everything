"""Agent mode — fully autonomous single-LLM runtime.

Third runtime alongside ``simple`` (one-shot task) and ``pipeline``
(multi-phase iterative improvement). See ``agent_loop.py`` for the core
loop and ``config/agent_example.json`` for a minimal configuration.
"""

from harness.agent.agent_loop import AgentConfig, AgentLoop, AgentResult

__all__ = ["AgentConfig", "AgentLoop", "AgentResult"]
