"""Discussion — LLM-backed multi-turn conversation about an improvement proposal.

Manages conversation context (proposal + diagnostic data + chat history) and
tracks proposal modifications requested by the operator.  Uses the harness
LLM client standalone, independent of AgentLoop.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

_SYSTEM_PROMPT_TEMPLATE = """\
You are discussing an improvement proposal for a software project.

## Original Proposal

{proposal}

## Diagnostic Data

The following data was collected during the diagnostic phase (database queries,
log analysis, code inspection results).  Use it to answer the operator's
questions with specific numbers and evidence.

{diagnostic_context}

## Instructions

- Answer questions grounded in the diagnostic data above.
- If the operator requests changes to the proposal (e.g., "skip module X",
  "focus on error handling"), acknowledge the change and present the revised
  proposal clearly.
- When presenting a revised proposal, use the same structure as the original
  (Findings, Proposed Actions, Rationale, Expected Impact, Risk Assessment).
- If asked "what's the current plan?", show the latest version of the proposal.
- Be concise.  The operator is technical and does not need hand-holding.
"""

_MAX_CONTEXT_CHARS: int = 100_000
_PROPOSAL_UPDATE_MARKER = "## Proposed Actions"


class Discussion:
    """Multi-turn conversation manager for proposal review and modification.

    Holds the original proposal, diagnostic context, and full chat history.
    Detects when the LLM's response contains a revised proposal and updates
    the current_proposal accordingly.
    """

    def __init__(
        self,
        proposal: str,
        diagnostic_context: str,
        llm: Any,
    ) -> None:
        self._original_proposal = proposal
        self._current_proposal = proposal
        self._llm = llm
        self._system_prompt = self._build_system_prompt(proposal, diagnostic_context)
        self._messages: list[dict[str, Any]] = []
        log.info(
            "Discussion created, proposal_len=%d, context_len=%d",
            len(proposal),
            len(diagnostic_context),
        )

    @property
    def current_proposal(self) -> str:
        """The latest version of the proposal, incorporating operator modifications."""
        return self._current_proposal

    async def respond(self, user_message: str) -> str:
        """Process an operator message and return the LLM's response.

        If the response contains a revised proposal (detected by the presence
        of '## Proposed Actions'), the current_proposal is updated.
        """
        # 1. Append user message to history
        self._append_user_message(user_message)

        # 2. Call LLM with full conversation context
        response_text = await self._call_llm()

        # 3. Append assistant response to history
        self._append_assistant_message(response_text)

        # 4. Check if the response contains a revised proposal
        self._check_for_proposal_update(response_text)

        return response_text

    # ══════════════════════════════════════════════════════════════════════
    #  Private methods
    # ══════════════════════════════════════════════════════════════════════

    def _build_system_prompt(self, proposal: str, diagnostic_context: str) -> str:
        """Construct the system prompt with proposal and diagnostic data."""
        truncated_context = diagnostic_context[:_MAX_CONTEXT_CHARS]
        if len(diagnostic_context) > _MAX_CONTEXT_CHARS:
            truncated_context += f"\n\n... (truncated, {len(diagnostic_context)} total chars)"
            log.warning(
                "Diagnostic context truncated from %d to %d chars",
                len(diagnostic_context),
                _MAX_CONTEXT_CHARS,
            )
        prompt = _SYSTEM_PROMPT_TEMPLATE.format(
            proposal=proposal,
            diagnostic_context=truncated_context,
        )
        log.debug("System prompt built, len=%d", len(prompt))
        return prompt

    def _append_user_message(self, text: str) -> None:
        """Add a user message to the conversation history."""
        self._messages.append({"role": "user", "content": text})
        log.debug("User message appended, history_len=%d", len(self._messages))

    def _append_assistant_message(self, text: str) -> None:
        """Add an assistant message to the conversation history."""
        self._messages.append({"role": "assistant", "content": text})
        log.debug("Assistant message appended, history_len=%d", len(self._messages))

    async def _call_llm(self) -> str:
        """Call the LLM with the current conversation context."""
        log.debug("Calling LLM for discussion, messages=%d", len(self._messages))
        response = await self._llm.call(
            self._messages,
            system=self._system_prompt,
        )
        log.info("LLM response received, text_len=%d", len(response.text))
        return response.text

    def _check_for_proposal_update(self, response_text: str) -> None:
        """Detect if the LLM response contains a revised proposal.

        Uses the presence of '## Proposed Actions' as a signal that the
        response includes a full revised proposal.  This is a heuristic;
        the operator can always ask 'what's the current plan?' to verify.
        """
        if _PROPOSAL_UPDATE_MARKER in response_text:
            self._current_proposal = response_text
            log.info("Proposal updated from discussion response")
