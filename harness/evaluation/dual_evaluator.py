"""DualEvaluator — two independent evaluators that never see each other's output.

Unlike ThreeWayResolver (which merges perspectives), this keeps evaluators
isolated to prevent groupthink.  Scores are combined numerically.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Literal

from harness.core.llm import LLM
from harness.prompts import dual_evaluator as default_prompts


@dataclass
class ScoreItem:
    """A single evaluator's score and critique."""

    score: float
    critique: str


@dataclass
class DualScore:
    """Result from dual-isolated evaluation."""

    basic: ScoreItem
    diffusion: ScoreItem

    @property
    def combined(self) -> float:
        """Combined score (weighted average of basic and diffusion, 0-10).

        Uses 60% weight for basic score (detailed correctness evaluation) and
        40% weight for diffusion score (system-level impact evaluation).
        The result is clamped to the [0.0, 10.0] range.
        """
        if not (0.0 <= self.basic.score <= 10.0):
            raise ValueError(f"Basic score {self.basic.score} is outside valid range [0.0, 10.0]")
        if not (0.0 <= self.diffusion.score <= 10.0):
            raise ValueError(f"Diffusion score {self.diffusion.score} is outside valid range [0.0, 10.0]")
        weighted_score = 0.6 * self.basic.score + 0.4 * self.diffusion.score
        return max(0.0, min(10.0, weighted_score))

log = logging.getLogger(__name__)


_SCORE_MIN: float = 0.0
_SCORE_MAX: float = 10.0

# Structured score patterns with improved validation
# 1. Strict anchored: "SCORE: N" on its own line (preferred format)
_STRICT_RE = re.compile(r"^\s*SCORE:\s+(\d+(?:\.\d+)?)(?:\s+.*)?$", re.MULTILINE)
# 2. Strict unanchored: "SCORE: N" anywhere (case-insensitive)
_STRICT_UNANCHORED_RE = re.compile(r"SCORE:\s+(\d+(?:\.\d+)?)", re.IGNORECASE)
# 3. Loose pattern: "SCORE N" or "SCORE=N" variations
_LOOSE_RE = re.compile(r"SCORE[:\s=]+(\d+(?:\.\d+)?)", re.IGNORECASE)
# Inline code spans that contain SCORE patterns — used to suppress false positives.
# Only strips spans where a SCORE-like value appears inside the backticks.
_INLINE_SCORE_RE = re.compile(r"`[^`\n]*\bSCORE[^`\n]*`", re.IGNORECASE)
# 4. Enhanced pattern with score range validation: "SCORE: 7.5/10" or "SCORE: 8 (out of 10)"
_ENHANCED_RE = re.compile(
    r"(?:SCORE|Score|score)[:\s=]+"
    r"(\d+(?:\.\d+)?)"
    r"(?:\s*/\s*10|\s*\(out of\s*10\)|\s*of\s*10)?",
    re.IGNORECASE
)
# 5. Final score pattern: "FINAL SCORE: N" (explicit final score)
_FINAL_SCORE_RE = re.compile(r"FINAL\s+SCORE[:\s=]+(\d+(?:\.\d+)?)", re.IGNORECASE)

# Mode header injected into the evaluation user message so evaluators know
# whether they are reviewing a text proposal or an implement-mode code change.
# Mode headers are prepended to every evaluator user message so the LLM knows
# whether it is reviewing a text proposal (debate) or executed code (implement).
# Keep these SHORT — calibration anchors, scoring guidance, and output format
# requirements are already in the system prompt (BASIC_SYSTEM / DIFFUSION_SYSTEM).
_MODE_HEADERS: dict[str, str] = {
    "debate": (
        "## EVALUATION MODE: DEBATE (TEXT PROPOSAL)\n"
        "You are reviewing a **text proposal** (plan / recommendation), NOT executed code.\n"
        "- Evaluate reasoning quality and specificity of proposed changes\n"
        "- Do NOT penalize for lack of tool calls or execution results\n"
        "- Assess whether the plan names concrete files/functions and would work if implemented\n\n"
    ),
    "implement": (
        "## EVALUATION MODE: IMPLEMENT (EXECUTED CODE)\n"
        "You are reviewing an **executed code change**, NOT a proposal.\n"
        "- Evaluate the actual code state; the proposal text is context only\n"
        "- Check correctness, syntax, test results, and tool call success/failure\n"
        "- Penalize missing tests, syntax errors, and broken functionality\n\n"
    ),
}


def format_critique_from_feedback(feedback_dict: dict[str, Any]) -> str:
    """Format structured feedback dictionary into a readable critique string.
    
    Args:
        feedback_dict: The structured feedback dictionary from extract_structured_feedback
        
    Returns:
        A formatted critique string
    """
    if not feedback_dict:
        return "No feedback available"
    
    parts = []
    
    # Add score if available
    score = feedback_dict.get("score")
    if score is not None:
        parts.append(f"Score: {score:.1f}")
    
    # Add feedback items
    feedback_items = feedback_dict.get("feedback_items", [])
    if feedback_items:
        parts.append("Feedback:")
        for item in feedback_items:
            parts.append(f"  • {item}")
    
    # Add improvement suggestion
    improvement = feedback_dict.get("improvement_suggestion")
    if improvement:
        parts.append(f"Improvement suggestion: {improvement}")
    
    # Add defect if present
    defect = feedback_dict.get("defect")
    if defect:
        parts.append(f"Critical defect: {defect}")
    
    # Add analysis summary
    analysis = feedback_dict.get("analysis", {})
    if analysis:
        parts.append("Analysis:")
        for dimension, score_val in analysis.items():
            parts.append(f"  • {dimension}: {float(score_val):.1f}")
    
    return "\n".join(parts)


def extract_structured_feedback(text: str, evaluator_type: str = "basic", context: dict[str, Any] | None = None, mode: str | None = None) -> dict[str, Any]:
    """Extract structured feedback from evaluator output text.

    Returns a dict with keys:
        - "score": float or None
        - "delta": str or None
        - "analysis": dict mapping dimension names to scores
        - "defect": str or None — top defect/risk text
        - "feedback_items": list of actionable feedback strings
        - "improvement_suggestion": str or None
        - "warnings": list of str from validate_score_calibration
        - "calibration_anchors_used": bool — True if calibration phrases detected
        - "validation_errors": list of validation errors if any
    """
    result: dict[str, Any] = {
        "score": None,
        "delta": None,
        "analysis": {},
        "defect": None,
        "feedback_items": [],
        "improvement_suggestion": None,
        "warnings": [],
        "calibration_anchors_used": False,
        "validation_errors": [],
    }

    # Validate first; bail out on hard errors
    _is_valid, issues = validate_evaluator_output(text, evaluator_type)
    errors = [i for i in issues if not i.startswith("WARNING:")]
    warnings = [i.replace("WARNING: ", "") for i in issues if i.startswith("WARNING:")]
    if errors:
        result["error"] = "Invalid evaluator output: " + "; ".join(errors)
        result["validation_errors"] = errors
        return result
    if warnings:
        result["warnings"] = warnings

    # Calibration anchor detection — simple presence check
    _CALIBRATION_PHRASES = (
        "SCORING CALIBRATION", "0-10 scale", "score ≤ 5", "score ≥ 8",
        "critical failure", "core goal achieved", "risk assessment",
    )
    result["calibration_anchors_used"] = any(
        phrase.lower() in text.lower() for phrase in _CALIBRATION_PHRASES
    )

    # Extract delta text
    if "DELTA VS PRIOR BEST:" in text:
        result["delta"] = text.split("DELTA VS PRIOR BEST:")[1].split("\n")[0].strip()

    # Extract score
    score_match = _STRICT_RE.findall(text)
    if score_match:
        result["score"] = float(score_match[-1])
        result["warnings"].extend(validate_score_calibration(result["score"], evaluator_type, context))

    # Extract analysis dimensions (use first pattern that produces results)
    if "ANALYSIS:" in text:
        analysis_section = text.split("ANALYSIS:")[1].split("\n\n")[0]
        # Try "A. Correctness: 8.5" format first, then plain "Correctness: 8.5"
        for pattern in (
            r"[A-D]\.\s+([^:—\n]+):\s*(\d+(?:\.\d+)?)",  # A. Correctness: 8.5
            r"^([^:\n—]+):\s*(\d+(?:\.\d+)?)",            # Correctness: 8.5 (line start)
        ):
            for match in re.findall(pattern, analysis_section, re.MULTILINE):
                dim, score = match
                if dim.strip():
                    result["analysis"][dim.strip()] = float(score)
            if result["analysis"]:
                break  # Stop after first pattern that produces results

    # Extract defect/risk
    defect_key = "TOP DEFECT:" if evaluator_type == "basic" else "KEY RISK:"
    if defect_key in text:
        defect_text = text.split(defect_key)[1].split("\n")[0].strip()
        if defect_text.lower() != "none":
            result["defect"] = defect_text

    # Extract feedback items
    feedback_section_name = "ACTIONABLE FEEDBACK:" if evaluator_type == "basic" else "ACTIONABLE MITIGATIONS:"
    if feedback_section_name in text:
        after_feedback = text.split(feedback_section_name)[1]
        # Trim at next major section
        for section in ("WHAT WOULD MAKE THIS 10/10:", "SCORE:", "FINAL SCORE:", "COMBINED_SCORE:"):
            idx = after_feedback.find(section)
            if idx != -1:
                after_feedback = after_feedback[:idx]
        for line in after_feedback.splitlines():
            line = line.strip()
            if not line:
                continue
            # Strip leading numbering or bullets
            for pat in (r"^\s*\d+[.)]\s+", r"^\s*[-*•]\s+"):
                m = re.match(pat, line)
                if m:
                    line = line[m.end():]
                    break
            result["feedback_items"].append(line)

    # Extract improvement suggestion
    if "WHAT WOULD MAKE THIS 10/10:" in text:
        improvement = text.split("WHAT WOULD MAKE THIS 10/10:")[1].split("\n")[0].strip()
        if improvement.lower() != "already perfect":
            result["improvement_suggestion"] = improvement

    return result


def validate_score_calibration(score: float, evaluator_type: str = "basic", context: dict[str, Any] | None = None) -> list[str]:
    """Return a short list of targeted calibration warnings for the given score.

    Deliberately lean: at most 3 warnings.  Verbose checklists and mode-specific
    rubrics already live in the system prompt (BASIC_SYSTEM / DIFFUSION_SYSTEM),
    so repeating them here only adds noise to the LLM's context window.
    """
    warnings: list[str] = []
    ctx = context or {}

    # 1. Out-of-range: always worth flagging, likely a parse error.
    if not (0.0 <= score <= 10.0):
        warnings.append(f"Score {score} is outside the 0-10 range; check for parsing error.")
        return warnings

    mode = ctx.get("mode", "")

    # 2. Mode–score mismatch: specific, actionable signal the system prompt cannot provide
    #    because it doesn't know the runtime score yet.
    if mode == "debate" and score >= 9.5:
        warnings.append(
            f"Score {score} is very high for a debate (text-only) round. "
            "Reserve 9.5+ for proposals that cite exact file::function paths and cover all edge cases."
        )
    elif mode == "implement" and score <= 3.0:
        warnings.append(
            f"Score {score} is very low for implement mode — confirm that the code is truly broken "
            "or tests are failing, not just incomplete."
        )

    # 3. Extreme scores need a check regardless of mode — flag once, concisely.
    if score == 10.0:
        warnings.append("Score 10 claimed — confirm every criterion is fully satisfied with no improvements possible.")
    elif score == 0.0:
        warnings.append("Score 0 claimed — confirm the response is entirely absent or meaningless, not just poor.")

    return warnings


def _score_is_in_code_block(text: str, score_line_start: int) -> bool:
    """Check if a SCORE: line starting at score_line_start is inside a markdown code block.

    Tracks two mutually exclusive states:
    - in_fenced: inside a triple-backtick fenced block (``` ... ```)
    - in_inline: inside a single-backtick inline code span (` ... `)

    A run of 3+ consecutive backticks toggles the fenced state (when not in inline code).
    A single backtick toggles the inline state (when not in a fenced block).

    Args:
        text: The full text to analyze
        score_line_start: The character index where the SCORE: line starts

    Returns:
        True if the SCORE: line is inside a code block, False otherwise
    """
    in_fenced = False  # inside a triple-backtick fenced block
    in_inline = False  # inside a single-backtick inline code span
    i = 0

    while i < score_line_start and i < len(text):
        if text[i] == "`":
            # Count the run of consecutive backticks
            j = i + 1
            while j < score_line_start and j < len(text) and text[j] == "`":
                j += 1
            count = j - i

            if count >= 3:
                # Triple+ backticks toggle the fenced-block state
                if not in_inline:
                    in_fenced = not in_fenced
            elif count == 1:
                # Single backtick toggles inline-code state
                if not in_fenced:
                    in_inline = not in_inline
            # count == 2: double backticks are ambiguous; treat as no effect

            i = j  # skip past the entire backtick run
        else:
            i += 1

    return in_fenced or in_inline


def validate_calibration_anchors(
    text: str,
    evaluator_type: str = "basic",
    mode: str | None = None,
) -> list[str]:
    """Validate that extreme scores (≤1 or ≥9) reference calibration anchors.
    
    The evaluator prompts (BASIC_SYSTEM, DIFFUSION_SYSTEM) include explicit
    CALIBRATION ANCHORS blocks. When scores are near extremes, the evaluator
    should reference the anchor language to demonstrate proper calibration.
    
    Enhanced validation now includes:
    1. Anchor keyword presence check
    2. Justification quality assessment (minimum 15 words for extreme scores)
    3. Score-justification consistency check
    4. Mode-specific anchor validation for debate/implement modes
    
    Args:
        text: Evaluator output text to validate
        evaluator_type: "basic" or "diffusion" to check appropriate anchors
        mode: Optional mode ("debate" or "implement") for mode-specific validation
        
    Returns:
        List of issues found, empty if none
    """
    issues = []
    
    # Extract score from text
    score_match = re.search(r'SCORE:\s*([0-9]+(?:\.[0-9]+)?)', text)
    if not score_match:
        # If no score found, can't validate anchors
        return []
    
    try:
        score = float(score_match.group(1))
    except ValueError:
        # Invalid score format
        return []
    
    # Check if score is near extremes
    is_low_extreme = score <= 1.5  # Allow small margin for rounding
    is_high_extreme = score >= 8.5  # Allow small margin for rounding
    
    if not (is_low_extreme or is_high_extreme):
        # Score is not extreme, no anchor validation needed
        return []
    
    # Define anchor keywords based on evaluator type and score range
    if evaluator_type == "basic":
        if is_low_extreme:
            # Low extreme anchors for basic evaluator
            anchor_keywords = [
                "broken", "dangerous", "off-topic", "fundamentally wrong",
                "complete rewrite", "trivial case", "major requirement missed",
                "partially correct", "missing core functionality", "fail basic tests",
                "critical issue", "severe flaw", "unusable", "incomplete"
            ]
        else:  # is_high_extreme
            # High extreme anchors for basic evaluator
            anchor_keywords = [
                "correct + specific", "testable", "tested", "measurable",
                "covers main requirement", "pass code review", "edge cases",
                "named test", "metric", "every claim backed", "comprehensive",
                "thorough", "well-structured", "actionable", "specific"
            ]
    else:  # diffusion
        if is_low_extreme:
            # Low extreme anchors for diffusion evaluator
            anchor_keywords = [
                "catastrophic", "irreversible", "systemically destabilising",
                "dangerous", "breaks unrelated functionality", "no mitigation",
                "concerning", "significant cascade", "explicit mitigation",
                "high risk", "unacceptable risk", "severe impact", "cascading failure"
            ]
        else:  # is_high_extreme
            # High extreme anchors for diffusion evaluator
            anchor_keywords = [
                "minor", "trivial ripple", "easily addressed", "negligible",
                "zero maintenance", "trivial rollback", "bounded effects",
                "clear mitigation", "minimal impact", "low risk", "acceptable",
                "contained", "manageable", "isolated"
            ]
    
    # Mode-specific anchor keywords
    if mode == "debate":
        # Debate mode anchors - focus on reasoning quality
        if is_low_extreme:
            anchor_keywords.extend([
                "vague", "unspecific", "no concrete plan", "missing details",
                "poor reasoning", "unclear", "incomplete analysis"
            ])
        else:
            anchor_keywords.extend([
                "specific plan", "concrete steps", "clear reasoning",
                "detailed analysis", "well-justified", "comprehensive plan"
            ])
    elif mode == "implement":
        # Implement mode anchors - focus on code quality
        if is_low_extreme:
            anchor_keywords.extend([
                "syntax error", "broken code", "untested", "import error",
                "test failure", "buggy", "incorrect implementation"
            ])
        else:
            anchor_keywords.extend([
                "working code", "tests pass", "clean implementation",
                "well-tested", "correct syntax", "proper imports"
            ])
    
    # Check if any anchor keywords appear in the analysis section
    analysis_section = ""
    if "ANALYSIS:" in text:
        # Extract analysis section up to next section
        analysis_start = text.find("ANALYSIS:")
        analysis_text = text[analysis_start:]
        
        # Find end of analysis section (next section header)
        section_end = len(analysis_text)
        for section in ["TOP DEFECT:", "KEY RISK:", "DELTA VS PRIOR BEST:", 
                       "ACTIONABLE FEEDBACK:", "ACTIONABLE MITIGATIONS:", 
                       "WHAT WOULD MAKE THIS 10/10:", "SCORE:"]:
            if section in analysis_text:
                section_pos = analysis_text.find(section)
                if section_pos > 0 and section_pos < section_end:
                    section_end = section_pos
        
        analysis_section = analysis_text[:section_end]
    
    # Check for anchor keyword presence
    found_anchor = False
    anchor_matches = []
    analysis_lower = analysis_section.lower()
    
    for keyword in anchor_keywords:
        if keyword.lower() in analysis_lower:
            found_anchor = True
            anchor_matches.append(keyword)
    
    # Enhanced validation: Check justification quality for extreme scores
    if analysis_section:
        # Count words in analysis (excluding section header)
        analysis_words = len(analysis_section.replace("ANALYSIS:", "").split())
        
        # Extreme scores require more detailed justification — advisory warnings, not hard errors
        if is_low_extreme or is_high_extreme:
            if analysis_words < 15:
                issues.append(
                    f"WARNING: Extreme score {score} has brief justification "
                    f"({analysis_words} words). Extreme scores benefit from ≥15 words "
                    f"of detailed analysis referencing calibration anchors."
                )
            
            # Check for justification of extreme nature
            justification_indicators = ["because", "since", "due to", "as", "given that"]
            has_justification = any(indicator in analysis_lower for indicator in justification_indicators)
            
            if not has_justification and analysis_words < 25:
                issues.append(
                    f"WARNING: Extreme score {score} lacks explicit justification connectors "
                    f"('because', 'since', 'due to', etc.). Extreme scores benefit from "
                    f"clear causal reasoning."
                )
    
    if not found_anchor:
        score_range = "low" if is_low_extreme else "high"
        issues.append(
            f"WARNING: Extreme {score_range} score ({score}) without reference to calibration anchors. "
            f"When scoring {score_range} extremes, the analysis should reference the "
            f"calibration anchor language (e.g., '{anchor_keywords[0]}', '{anchor_keywords[1]}')."
        )
    elif anchor_matches:
        # Log successful anchor matches for debugging
        log.debug(f"Found calibration anchors for score {score}: {', '.join(anchor_matches[:3])}")
    
    # Additional validation: Check for score-justification consistency
    if analysis_section and found_anchor:
        # For low extreme scores, check for negative language
        if is_low_extreme:
            negative_indicators = ["good", "excellent", "great", "well", "properly", "correctly"]
            positive_count = sum(1 for indicator in negative_indicators if indicator in analysis_lower)
            if positive_count > 2:
                issues.append(
                    f"Low extreme score {score} has inconsistent positive language "
                    f"in analysis. Low scores should align with negative assessment."
                )
        
        # For high extreme scores, check for negative language
        elif is_high_extreme:
            negative_indicators = ["bad", "poor", "wrong", "incorrect", "flawed", "broken", "missing"]
            negative_count = sum(1 for indicator in negative_indicators if indicator in analysis_lower)
            if negative_count > 2:
                issues.append(
                    f"High extreme score {score} has inconsistent negative language "
                    f"in analysis. High scores should align with positive assessment."
                )
    
    return issues


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
        defect_section = "TOP DEFECT:"
        feedback_section = "ACTIONABLE FEEDBACK:"
    else:  # diffusion
        required_sections = ["ANALYSIS:", "KEY RISK:", "SCORE:"]
        defect_section = "KEY RISK:"
        feedback_section = "ACTIONABLE MITIGATIONS:"
    
    # DELTA VS PRIOR BEST is optional but if present must have descriptive text
    if "DELTA VS PRIOR BEST:" in text:
        delta_lines = [line for line in text.split('\n') if line.strip().startswith("DELTA VS PRIOR BEST:")]
        if delta_lines:
            delta_line = delta_lines[0]
            delta_text = delta_line.split("DELTA VS PRIOR BEST:")[1].strip()
            if not delta_text or len(delta_text) < 5:
                issues.append("WARNING: 'DELTA VS PRIOR BEST:' has very short text — add descriptive comparison")
    
    # Check for required sections — SCORE: is a hard error; others are advisory warnings
    for section in required_sections:
        if section not in text:
            if section == "SCORE:":
                issues.append(f"Missing required section: {section}")
            else:
                issues.append(f"WARNING: Missing section: {section} — include for complete evaluation")
    
    # SECURITY GUARD: Check if SCORE: line is inside a markdown code block using state machine
    lines = text.split('\n')
    for line_num, line in enumerate(lines, 1):
        if 'SCORE:' in line.upper():
            # Find the character index where this line starts
            line_start = 0
            for i in range(line_num - 1):
                line_start += len(lines[i]) + 1  # +1 for newline
            
            # Check if this SCORE: line is inside a code block
            if _score_is_in_code_block(text, line_start):
                issues.append(f"SCORE: found inside markdown code block at line {line_num}")
                # Don't break - continue checking all lines to report all violations
    
    # Check for SCORE format - prefer score at line start, but accept embedded SCORE: as warning
    score_lines = [line for line in text.split('\n') if line.strip().startswith('SCORE:')]
    any_score_match = _STRICT_UNANCHORED_RE.search(text)  # SCORE: anywhere in text
    
    if score_lines:
        score_line = score_lines[-1].strip()
        # Check for proper SCORE: X.X format (allows trailing text)
        if not re.match(r'^SCORE:\s+\d+(?:\.\d+)?\b', score_line):
            issues.append(f"SCORE line malformed: '{score_line}' - expected 'SCORE: X.X' with optional trailing text")
        # Check that score is the last thing in the output (most reliable)
        # Allow for trailing whitespace after the score line
        text_stripped = text.strip()
        score_line_stripped = score_line.strip()
        if not text_stripped.endswith(score_line_stripped) and not text_stripped.endswith(score_line_stripped + '\n'):
            issues.append("WARNING: SCORE should be the last line of the output for reliable parsing")
        
        # Extract and validate score calibration
        try:
            score_match = re.search(r'SCORE:\s+(\d+(?:\.\d+)?)', score_line)
            if score_match:
                score = float(score_match.group(1))
                calibration_warnings = validate_score_calibration(score, evaluator_type, mode)
                for warning in calibration_warnings:
                    issues.append(f"WARNING: {warning}")
        except (ValueError, AttributeError):
            pass  # Already caught by format check above
    elif any_score_match:
        # SCORE: exists but not at line start — advisory warning, not a hard failure
        issues.append("WARNING: SCORE: found but not at line start — place 'SCORE: X.X' on its own line")
    else:
        issues.append("No SCORE line found")
    
    # Check ANALYSIS section has proper structure with strict pattern matching
    if "ANALYSIS:" in text:
        analysis_section = text.split("ANALYSIS:")[1].split("\n\n")[0]
        # Check each analysis line matches pattern: ^[A-D]\. .+: [0-9.]+ — .+
        analysis_lines = [line.strip() for line in analysis_section.split('\n') if line.strip()]
        for line in analysis_lines:
            if re.match(r'^[A-D]\.\s+.+:\s*\d+(?:\.\d+)?\s*—\s*.+$', line):
                continue
            elif line.startswith(('A.', 'B.', 'C.', 'D.')):
                issues.append(f"Analysis line doesn't match required format '^[A-D]\\. .+: [0-9.]+ — .+': {line}")
        
        # Check for dimension scores - warn if missing but don't fail validation
        if evaluator_type == "basic":
            dimensions = ["A. Correctness", "B. Completeness", "C. Specificity", "D. Architecture fit"]
        else:
            dimensions = ["A. Caller impact", "B. Maintenance debt", "C. Emergent behaviour", "D. Rollback safety"]
        
        for dim in dimensions:
            if dim not in analysis_section:
                # Only warn about missing dimensions, don't fail validation
                issues.append(f"WARNING: ANALYSIS missing dimension: {dim}")
    
    # Check defect/risk section has concrete reference with path sanitization
    if defect_section in text:
        defect_text = text.split(defect_section)[1].split("\n")[0].strip()
        if defect_text.lower() != "none":
            # Should contain file::function reference
            if "::" not in defect_text:
                issues.append(f"{defect_section} should reference file::function, got: '{defect_text}'")
            else:
                # Sanitize path - check for path traversal attempts
                file_part = defect_text.split("::")[0].strip()
                if file_part.startswith(('..', '/', '\\')) or '..' in file_part:
                    issues.append(f"{defect_section} contains potential path traversal: {file_part}")
                # Only allow alphanumerics, underscores, dots, and :: in the file::function part
                # Allow descriptive text after the file::function reference
                file_func_part = defect_text.split("—")[0].strip() if "—" in defect_text else defect_text
                if not re.match(r'^[\w\.\-]+::[\w\.\-]+$', file_func_part.replace(' ', '')):
                    issues.append(f"{defect_section} file::function part contains invalid characters: {file_func_part}")
    
    # Check for structured feedback if present
    if feedback_section in text:
        feedback_text = text.split(feedback_section)[1].split("\n\n")[0]
        # Check for numbered items
        lines = [line.strip() for line in feedback_text.split('\n') if line.strip()]
        numbered_items = [line for line in lines if re.match(r'^\d+\.\s', line)]
        if not numbered_items:
            issues.append(f"{feedback_section} should contain numbered items (1., 2., etc.)")
        else:
            # Check that at least one numbered item contains concrete file/function reference
            # Pattern matches: filename.py: function_name or filename.py function_name
            has_concrete_reference = False
            for item in numbered_items:
                # Look for file.py: function or file.py function patterns
                if re.search(r'\b\w+\.py\s*[: ]+\s*\w+\b', item):
                    has_concrete_reference = True
                    break
                # Also check for file::function format
                if '::' in item:
                    has_concrete_reference = True
                    break
            
            if not has_concrete_reference:
                issues.append(f"WARNING: {feedback_section} lacks explicit file/function references (e.g., 'file.py: function_name' or 'file::function') — add for actionability")
    
    # Enhanced validation: Check for concrete references in critique/improvement content
    # Look for critique or improvement_suggestions patterns in the text
    critique_patterns = [
        r'critique[:\s]+(.+?)(?=\n\n|\n[A-Z]|$)',
        r'improvement[_ ]suggestions[:\s]+(.+?)(?=\n\n|\n[A-Z]|$)',
        r'key findings[:\s]+(.+?)(?=\n\n|\n[A-Z]|$)',
        r'analysis[:\s]+(.+?)(?=\n\n|\n[A-Z]|$)'
    ]
    
    has_concrete_critique_reference = False
    critique_sections_found = []
    
    for pattern in critique_patterns:
        matches = re.findall(pattern, text, re.IGNORECASE | re.DOTALL)
        for match in matches:
            if isinstance(match, tuple):
                match_text = match[0]
            else:
                match_text = match
            
            critique_sections_found.append(match_text.strip())
            
            # Check for concrete file/function references in critique text
            if re.search(r'\b\w+\.py\s*[: ]+\s*\w+\b', match_text) or '::' in match_text:
                has_concrete_critique_reference = True
                break
    
    # If critique sections were found but no concrete references, add a warning
    if critique_sections_found and not has_concrete_critique_reference:
        # Only add as warning, not error, to avoid breaking existing validations
        issues.append("WARNING: Critique/analysis sections should contain concrete file/function references for better traceability")
    
    # Check WHAT WOULD MAKE THIS 10/10 section if present
    if "WHAT WOULD MAKE THIS 10/10:" in text:
        ten_text = text.split("WHAT WOULD MAKE THIS 10/10:")[1].split("\n")[0].strip()
        if ten_text.lower() != "already perfect" and len(ten_text) < 10:
            issues.append("WHAT WOULD MAKE THIS 10/10: should provide concrete improvement")
    
    # Mode-specific validation
    if mode and mode in _MODE_HEADERS:
        mode_header = _MODE_HEADERS[mode]  # noqa: F841 — used in f-string below
        # Check for mode-appropriate content
        if mode == "debate":
            # Debate mode should mention text proposals or planning
            if "text proposal" not in text.lower() and "planning round" not in text.lower():
                issues.append("Debate mode output should mention 'text proposal' or 'planning round'")
            # Debate mode should focus on reasoning quality, not execution
            if "executed code" in text.lower() or "tool calls" in text.lower():
                issues.append("WARNING: Debate mode should focus on reasoning quality, not execution details")
        elif mode == "implement":
            # Implement mode should mention executed code or code state
            if "executed code" not in text.lower() and "code state" not in text.lower():
                issues.append("Implement mode output should mention 'executed code' or 'code state'")
            # Implement mode should reference specific file changes
            if "file::" not in text and "function::" not in text:
                issues.append("WARNING: Implement mode should reference specific file/function changes")
    
    # Enhanced calibration validation
    calibration_issues = validate_calibration_anchors(text, evaluator_type, mode)
    issues.extend(calibration_issues)
    
    # Token budget check (warning only)
    if len(text) > 8000:  # Rough estimate: ~2000 tokens
        issues.append("WARNING: Evaluator output may exceed token budget (consider truncation)")
    
    # Only ERROR issues (no "WARNING:" prefix) make the output invalid.
    # WARNING-only issues are advisory and do not fail validation.
    is_valid = all(issue.startswith("WARNING:") for issue in issues)
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
        # Process line by line to handle edge cases where SCORE: might be on same line as backticks
        lines = text.split('\n')
        in_code_block = False
        cleaned_lines = []
        
        for line in lines:
            stripped = line.strip()
            
            # Check if this line contains backticks that might toggle code block state
            if "```" in stripped:
                # Toggle code block state when we see triple backticks
                # This handles both opening and closing
                in_code_block = not in_code_block
                
                # Check if SCORE: is on the same line as backticks
                if "SCORE:" in line.upper():
                    # Keep this line for score parsing even if it's in/on a code block boundary
                    cleaned_lines.append(line)
                continue
            
            # Only add lines that are not inside code blocks
            if not in_code_block:
                cleaned_lines.append(line)
        
        clean_text = '\n'.join(cleaned_lines)

    # Strip inline code spans that contain SCORE patterns, so scores appearing
    # inside backtick-quoted code examples are not extracted as real scores.
    # We only strip spans that contain SCORE to avoid changing line-anchor status
    # for lines where inline code precedes a real (post-backtick) SCORE value.
    clean_text = _INLINE_SCORE_RE.sub("", clean_text)

    # Two-tier score extraction strategy:
    # 1. Prefer the LAST occurrence of SCORE: that appears at the START of its line
    #    (possibly preceded by whitespace).  Within a single line, the LAST SCORE:
    #    token on that line wins over earlier tokens on the same line.
    # 2. Fall back to the last unanchored SCORE: occurrence if no line-start match.
    # 3. Final fallback: loose pattern.

    anchored_values: list[float] = []  # SCORE: that starts its line (last per line)
    last_per_line: list[float] = []    # last SCORE: per line, any position
    _anchor_re = re.compile(r'^\s*SCORE:\s+', re.IGNORECASE)

    for line in clean_text.splitlines():
        line_matches = list(_STRICT_UNANCHORED_RE.finditer(line))
        if not line_matches:
            continue
        last_val = float(line_matches[-1].group(1))
        last_per_line.append(last_val)
        if _anchor_re.match(line):
            anchored_values.append(last_val)

    if anchored_values:
        raw = anchored_values[-1]
        log.debug("parse_score: found line-anchored SCORE: %.2f", raw)
    elif last_per_line:
        raw = last_per_line[-1]
        log.debug("parse_score: found unanchored SCORE: %.2f", raw)
    else:
        # Loose fallback pattern
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
    
    # ENHANCED: Fractional score discrimination guidance for critical 4-7 range
    if 4.0 <= clamped <= 7.0:
        log.debug(
            "parse_score: critical range score %.2f - ensure proper discrimination between adjacent scores",
            clamped
        )
        # Enhanced discrimination guidance with fractional score ranges
        if 4.0 <= clamped < 4.5:
            log.debug("  Score ~4.0-4.4: Generic approach without specific implementation")
        elif 4.5 <= clamped < 5.0:
            log.debug("  Score ~4.5-4.9: Generic approach with some specific elements, but not enough for full 5")
        elif 5.0 <= clamped < 5.5:
            log.debug("  Score ~5.0-5.4: Partial success with specific elements")
        elif 5.5 <= clamped < 6.0:
            log.debug("  Score ~5.5-5.9: Specific but incomplete with some edge cases addressed")
        elif 6.0 <= clamped < 6.5:
            log.debug("  Score ~6.0-6.4: Specific implementation with gaps")
        elif 6.5 <= clamped < 7.0:
            log.debug("  Score ~6.5-6.9: Mostly complete with some testability elements, but not enough for 7")
        elif 7.0 <= clamped < 8.0:
            log.debug("  Score ~7.0-7.9: Mostly complete implementation with minor edge cases missing")
        
        # Log fractional score validation
        if clamped % 1 != 0:  # Is fractional
            log.debug("  Fractional score detected: %.2f - ensure justification in evaluator output", clamped)
    
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
        
        # Create critique strings from structured feedback
        basic_critique = format_critique_from_feedback(basic_feedback)
        diffusion_critique = format_critique_from_feedback(diffusion_feedback)
        
        # Calculate proper combined score using DualScore.combined property
        temp_dual_score = DualScore(
            basic=ScoreItem(basic_score, basic_critique),
            diffusion=ScoreItem(diffusion_score, diffusion_critique)
        )
        combined_score = temp_dual_score.combined
        
        log.info(
            "DualEvaluator[%s]: basic=%.1f (valid=%s, feedback=%d items) diffusion=%.1f (valid=%s, feedback=%d items) combined=%.1f",
            mode, basic_score, basic_valid, len(basic_feedback.get("feedback_items", [])),
            diffusion_score, diffusion_valid, len(diffusion_feedback.get("feedback_items", [])),
            combined_score,
        )
        
        # Log key findings for debugging
        if basic_feedback.get("defect"):
            log.debug("Basic evaluator top defect: %s", basic_feedback["defect"])
        if diffusion_feedback.get("defect"):
            log.debug("Diffusion evaluator key risk: %s", diffusion_feedback["defect"])

        return DualScore(
            basic=ScoreItem(basic_score, basic_critique),
            diffusion=ScoreItem(diffusion_score, diffusion_critique),
        )
