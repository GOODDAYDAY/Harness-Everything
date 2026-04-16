"""DualEvaluator — two independent evaluators that never see each other's output.

Unlike ThreeWayResolver (which merges perspectives), this keeps evaluators
isolated to prevent groupthink.  Scores are combined numerically.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Literal

from harness.core.llm import LLM
from harness.pipeline.phase import DualScore, ScoreItem
from harness.prompts import dual_evaluator as default_prompts

log = logging.getLogger(__name__)


_SCORE_MIN: float = 0.0
_SCORE_MAX: float = 10.0

# Strict pattern: "SCORE: N" on its own line (anchored).  Preferred over loose
# because evaluators are instructed to place the authoritative score last on
# its own line.  The loose fallback handles older/custom prompts that don't
# follow the anchored format.
_STRICT_RE = re.compile(r"^\s*SCORE:\s*(\d+(?:\.\d+)?)\s*$", re.MULTILINE)
_STRICT_UNANCHORED_RE = re.compile(r"SCORE:\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_LOOSE_RE  = re.compile(r"SCORE[:\s]+(\d+(?:\.\d+)?)", re.IGNORECASE)

# Mode header injected into the evaluation user message so evaluators know
# whether they are reviewing a text proposal or an implement-mode code change.
_MODE_HEADERS: dict[str, str] = {
    "debate": (
        "## EVALUATION MODE: DEBATE (TEXT PROPOSAL)\n"
        "You are reviewing a **text proposal** (plan / recommendation).\n\n"
        "KEY DIFFERENCES FROM IMPLEMENT MODE:\n"
        "1. Evaluate the **reasoning quality**, not executed code\n"
        "2. Assess **specificity** of proposed changes (file/function names)\n"
        "3. Check **completeness** against the task requirements\n"
        "4. Do NOT penalize for lack of tool calls or execution results\n"
        "5. Focus on whether the plan would work IF implemented correctly\n\n"
        "CRITICAL: A good debate proposal must name concrete code entities "
        "(files, functions, classes) from the source context. "
        "Vague proposals score ≤4 on Specificity.\n\n"
        "**STRUCTURED OUTPUT REQUIREMENTS:**\n"
        "1. Use exact section headers from your system prompt\n"
        "2. Place SCORE: X.X on its own line at the very end\n"
        "3. Reference specific file::function in all findings\n"
        "4. Provide numbered, actionable feedback items\n"
        "5. Include concrete calibration anchors in your scoring\n\n"
    ),
    "implement": (
        "## EVALUATION MODE: IMPLEMENT (EXECUTED CODE)\n"
        "You are reviewing an **executed code change** (implement round).\n\n"
        "KEY DIFFERENCES FROM DEBATE MODE:\n"
        "1. Evaluate the **actual code state** after execution\n"
        "2. Check **correctness of edits** (syntax, logic, test results)\n"
        "3. Verify **tool call success/failure** from execution logs\n"
        "4. The proposal text is for context only; CODE STATE is authoritative\n"
        "5. Penalize missing tests, syntax errors, broken functionality\n\n"
        "CRITICAL: Look for concrete evidence in the changed files and test results. "
        "A plan that looks good but produces broken code scores ≤5 on Correctness.\n\n"
        "**STRUCTURED OUTPUT REQUIREMENTS:**\n"
        "1. Use exact section headers from your system prompt\n"
        "2. Place SCORE: X.X on its own line at the very end\n"
        "3. Reference specific file::function in all findings\n"
        "4. Provide numbered, actionable feedback items\n"
        "5. Include concrete calibration anchors in your scoring\n"
        "6. Focus on executed code, not the proposal text\n\n"
    ),
}


def extract_structured_feedback(text: str, evaluator_type: str = "basic") -> dict[str, Any]:
    """Extract structured feedback from evaluator output.
    
    Args:
        text: Evaluator output text
        evaluator_type: "basic" or "diffusion"
    
    Returns:
        Dictionary with extracted structured data
    """
    result = {
        "score": None,
        "analysis": {},
        "defect": None,
        "feedback_items": [],
        "improvement_suggestion": None,
    }
    
    # Extract score
    score_match = _STRICT_RE.findall(text)
    if score_match:
        result["score"] = float(score_match[-1])
    
    # Extract analysis dimensions
    if "ANALYSIS:" in text:
        analysis_section = text.split("ANALYSIS:")[1].split("\n\n")[0]
        # Look for dimension scores
        dimension_pattern = r"([A-D])\.\s+([^:]+):\s*(\d+(?:\.\d+)?)"
        matches = re.findall(dimension_pattern, analysis_section)
        for letter, dimension, score in matches:
            result["analysis"][dimension.strip()] = float(score)
    
    # Extract defect/risk
    if evaluator_type == "basic" and "TOP DEFECT:" in text:
        defect_section = text.split("TOP DEFECT:")[1].split("\n")[0].strip()
        if defect_section.lower() != "none":
            result["defect"] = defect_section
    elif evaluator_type == "diffusion" and "KEY RISK:" in text:
        risk_section = text.split("KEY RISK:")[1].split("\n")[0].strip()
        if risk_section.lower() != "none":
            result["defect"] = risk_section
    
    # Extract feedback items
    feedback_section_name = "ACTIONABLE FEEDBACK:" if evaluator_type == "basic" else "ACTIONABLE MITIGATIONS:"
    if feedback_section_name in text:
        feedback_section = text.split(feedback_section_name)[1].split("\n\n")[0]
        # Extract numbered items
        item_pattern = r"^\s*(\d+)\.\s+(.+)$"
        for line in feedback_section.split('\n'):
            match = re.match(item_pattern, line.strip())
            if match:
                result["feedback_items"].append(match.group(2).strip())
    
    # Extract improvement suggestion
    if "WHAT WOULD MAKE THIS 10/10:" in text:
        improvement = text.split("WHAT WOULD MAKE THIS 10/10:")[1].split("\n")[0].strip()
        if improvement.lower() != "already perfect":
            result["improvement_suggestion"] = improvement
    
    return result


def validate_evaluator_output(text: str, evaluator_type: str = "basic", mode: str | None = None) -> tuple[bool, list[str]]:
    """Validate evaluator output structure and return (is_valid, issues).
    
    Args:
        text: Evaluator output text to validate
        evaluator_type: "basic" or "diffusion" to check appropriate structure
        mode: Optional mode ("debate" or "implement") to validate mode-specific content
    
    Returns:
        Tuple of (is_valid, list_of_issues)
    """
    issues = []
    
    # Check for required sections based on evaluator type
    if evaluator_type == "basic":
        required_sections = ["ANALYSIS:", "TOP DEFECT:", "SCORE:"]
        optional_sections = ["DELTA VS PRIOR BEST:", "ACTIONABLE FEEDBACK:", "WHAT WOULD MAKE THIS 10/10:"]
        defect_section = "TOP DEFECT:"
        feedback_section = "ACTIONABLE FEEDBACK:"
    else:  # diffusion
        required_sections = ["ANALYSIS:", "KEY RISK:", "SCORE:"]
        optional_sections = ["DELTA VS PRIOR BEST:", "ACTIONABLE MITIGATIONS:", "WHAT WOULD MAKE THIS 10/10:"]
        defect_section = "KEY RISK:"
        feedback_section = "ACTIONABLE MITIGATIONS:"
    
    # Check for required sections
    for section in required_sections:
        if section not in text:
            issues.append(f"Missing required section: {section}")
    
    # Check for SCORE format - must be on its own line
    score_lines = [line for line in text.split('\n') if line.strip().startswith('SCORE:')]
    if score_lines:
        score_line = score_lines[-1].strip()
        # Check for proper SCORE: X.X format
        if not re.match(r'^SCORE:\s*\d+(?:\.\d+)?\s*$', score_line):
            issues.append(f"SCORE line malformed: '{score_line}' - expected 'SCORE: X.X'")
        # Check that score is the last thing in the output (most reliable)
        if not text.strip().endswith(score_line):
            issues.append("SCORE should be the last line of the output for reliable parsing")
    else:
        issues.append("No SCORE line found")
    
    # Check ANALYSIS section has proper structure
    if "ANALYSIS:" in text:
        analysis_section = text.split("ANALYSIS:")[1].split("\n\n")[0]
        # Check for dimension scores
        if evaluator_type == "basic":
            dimensions = ["A. Correctness", "B. Completeness", "C. Specificity", "D. Architecture fit"]
        else:
            dimensions = ["A. Caller impact", "B. Maintenance debt", "C. Emergent behaviour", "D. Rollback safety"]
        
        for dim in dimensions:
            if dim not in analysis_section:
                issues.append(f"ANALYSIS missing dimension: {dim}")
    
    # Check defect/risk section has concrete reference
    if defect_section in text:
        defect_text = text.split(defect_section)[1].split("\n")[0].strip()
        if defect_text.lower() != "none":
            # Should contain file::function reference
            if "::" not in defect_text:
                issues.append(f"{defect_section} should reference file::function, got: '{defect_text}'")
    
    # Check for structured feedback if present
    if feedback_section in text:
        feedback_text = text.split(feedback_section)[1].split("\n\n")[0]
        # Check for numbered items
        lines = [line.strip() for line in feedback_text.split('\n') if line.strip()]
        numbered_items = [line for line in lines if re.match(r'^\d+\.\s', line)]
        if not numbered_items:
            issues.append(f"{feedback_section} should contain numbered items (1., 2., etc.)")
    
    # Check WHAT WOULD MAKE THIS 10/10 section if present
    if "WHAT WOULD MAKE THIS 10/10:" in text:
        ten_text = text.split("WHAT WOULD MAKE THIS 10/10:")[1].split("\n")[0].strip()
        if ten_text.lower() != "already perfect" and len(ten_text) < 10:
            issues.append("WHAT WOULD MAKE THIS 10/10: should provide concrete improvement")
    
    # Mode-specific validation
    if mode and mode in _MODE_HEADERS:
        mode_header = _MODE_HEADERS[mode]
        # Check for mode-appropriate content
        if mode == "debate":
            # Debate mode should mention text proposals or planning
            if "text proposal" not in text.lower() and "planning round" not in text.lower():
                issues.append("Debate mode output should mention 'text proposal' or 'planning round'")
        elif mode == "implement":
            # Implement mode should mention executed code or code state
            if "executed code" not in text.lower() and "code state" not in text.lower():
                issues.append("Implement mode output should mention 'executed code' or 'code state'")
    
    is_valid = len(issues) == 0
    return is_valid, issues


def parse_score(
    text: str,
    pattern: str = r"SCORE[=:\s]+(\d+(?:\.\d+)?)",
) -> float:
    """Extract a numeric score from evaluator output and clamp it to [0, 10].

    Extraction strategy (three-tier):
    1. **Strict anchored** — search for ``^SCORE: N$`` (anchored to line boundaries).
       Takes the **last** strict match.  This reliably captures the
       authoritative final score placed at the end of the output, ignoring
       any inline arithmetic lines such as ``SCORE = (A×0.4)+… = 6.0``.
    2. **Strict unanchored** — search for ``SCORE: N`` (not anchored) as fallback.
    3. **Loose fallback** — if no strict match, apply the caller-supplied
       ``pattern`` (default: ``SCORE[=:\\s]+N``).  Takes the last match.

    Returns 0.0 and logs a warning when no match is found.
    Logs a warning when the extracted value is outside [0, 10].
    """
    # Clean the text - remove markdown code blocks if present
    clean_text = text
    if "```" in text:
        # Remove code blocks entirely by joining non-code parts
        parts = text.split("```")
        # Keep only parts outside code blocks (even-indexed parts)
        non_code_parts = [parts[i] for i in range(0, len(parts), 2)]
        clean_text = "".join(non_code_parts)
    
    # Three-tier extraction strategy
    
    # 1. Strict anchored pattern (anchored to line boundaries)
    strict_anchored = _STRICT_RE.findall(clean_text)
    if strict_anchored:
        raw = float(strict_anchored[-1])
        log.debug("parse_score: found strict anchored SCORE: %.2f", raw)
    else:
        # 2. Strict unanchored pattern (not anchored)
        strict_unanchored = _STRICT_UNANCHORED_RE.findall(clean_text)
        if strict_unanchored:
            raw = float(strict_unanchored[-1])
            log.debug("parse_score: found strict unanchored SCORE: %.2f", raw)
        else:
            # 3. Loose fallback pattern
            loose = re.findall(pattern, clean_text, re.IGNORECASE)
            if not loose:
                log.warning(
                    "parse_score: no score token found in evaluator output (first 500 chars):\n%.500s",
                    clean_text,
                )
                return 0.0
            raw = float(loose[-1])
            log.debug("parse_score: found loose SCORE: %.2f", raw)

    clamped = max(_SCORE_MIN, min(_SCORE_MAX, raw))
    if clamped != raw:
        log.warning(
            "parse_score: raw value %.2f is outside [%.0f, %.0f] — clamped to %.2f",
            raw, _SCORE_MIN, _SCORE_MAX, clamped,
        )
    return clamped


class DualEvaluator:
    """Run two evaluators in parallel, each blind to the other's output."""

    def __init__(self, llm: LLM) -> None:
        self.llm = llm

    async def evaluate(
        self,
        subject: str,
        context: str,
        *,
        mode: Literal["debate", "implement"] = "debate",
        basic_system: str = "",
        diffusion_system: str = "",
        score_pattern: str = r"SCORE[:\s]+(\d+(?:\.\d+)?)",
    ) -> DualScore:
        """Run both evaluators in parallel and return combined scores.

        Args:
            subject: What to evaluate (proposal text, code state, etc.).
            context: Source files, architecture constraints, etc.
            mode: ``"debate"`` for text proposals, ``"implement"`` for executed
                code changes.  Selects the appropriate default system prompts
                and prepends a mode header to the evaluation user message so
                evaluators apply the correct rubric.
            basic_system: Override system prompt for the basic evaluator.
            diffusion_system: Override system prompt for the diffusion evaluator.
            score_pattern: Regex to extract numeric score from evaluator output
                (used as the loose fallback in parse_score).
        """
        basic_sys = basic_system or default_prompts.BASIC_SYSTEM
        diffusion_sys = diffusion_system or default_prompts.DIFFUSION_SYSTEM

        # Prepend a mode header so evaluators adapt their rubric to whether
        # they are reviewing a text proposal or an executed code change.
        mode_header = _MODE_HEADERS.get(mode, _MODE_HEADERS["debate"])

        # Build user messages (identical structure, different system prompts)
        messages = [
            {
                "role": "user",
                "content": (
                    f"{mode_header}"
                    f"## Subject to Evaluate\n\n{subject}\n\n"
                    f"## Source Context\n\n{context}"
                ),
            }
        ]

        # Run in parallel — key: neither sees the other's output.
        # Wrap coroutines in Tasks so that if one raises, we can explicitly
        # cancel the other rather than leaving it as an abandoned background
        # task that continues consuming API quota and logs an unhandled
        # "Task exception was never retrieved" warning.
        basic_task = asyncio.ensure_future(self.llm.call(list(messages), system=basic_sys))
        diffusion_task = asyncio.ensure_future(self.llm.call(list(messages), system=diffusion_sys))

        try:
            basic_resp, diffusion_resp = await asyncio.gather(basic_task, diffusion_task)
        except Exception:
            # Cancel whichever task is still running so it does not linger
            # as a background coroutine consuming API quota.
            for t in (basic_task, diffusion_task):
                if not t.done():
                    t.cancel()
            raise

        basic_score = parse_score(basic_resp.text, score_pattern)
        diffusion_score = parse_score(diffusion_resp.text, score_pattern)
        
        # Validate output structure
        basic_valid, basic_issues = validate_evaluator_output(basic_resp.text, "basic")
        diffusion_valid, diffusion_issues = validate_evaluator_output(diffusion_resp.text, "diffusion")
        
        if not basic_valid:
            log.warning(
                "Basic evaluator output structure issues: %s",
                "; ".join(basic_issues)
            )
        
        if not diffusion_valid:
            log.warning(
                "Diffusion evaluator output structure issues: %s",
                "; ".join(diffusion_issues)
            )

        # Extract structured feedback
        basic_feedback = extract_structured_feedback(basic_resp.text, "basic")
        diffusion_feedback = extract_structured_feedback(diffusion_resp.text, "diffusion")
        
        log.info(
            "DualEvaluator[%s]: basic=%.1f (valid=%s, feedback=%d items) diffusion=%.1f (valid=%s, feedback=%d items) combined=%.1f",
            mode, basic_score, basic_valid, len(basic_feedback.get("feedback_items", [])),
            diffusion_score, diffusion_valid, len(diffusion_feedback.get("feedback_items", [])),
            basic_score + diffusion_score,
        )
        
        # Log key findings for debugging
        if basic_feedback.get("defect"):
            log.debug("Basic evaluator top defect: %s", basic_feedback["defect"])
        if diffusion_feedback.get("defect"):
            log.debug("Diffusion evaluator key risk: %s", diffusion_feedback["defect"])

        return DualScore(
            basic=ScoreItem(basic_score, basic_resp.text),
            diffusion=ScoreItem(diffusion_score, diffusion_resp.text),
        )
