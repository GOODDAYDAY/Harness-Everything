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

# Structured score patterns with improved validation
# 1. Strict anchored: "SCORE: N" on its own line (preferred format)
_STRICT_RE = re.compile(r"^\s*SCORE:\s*(\d+(?:\.\d+)?)(?:\s+.*)?$", re.MULTILINE)
# 2. Strict unanchored: "SCORE: N" anywhere (case-insensitive)
_STRICT_UNANCHORED_RE = re.compile(r"SCORE:\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
# 3. Loose pattern: "SCORE N" or "SCORE=N" variations
_LOOSE_RE = re.compile(r"SCORE[:\s=]+(\d+(?:\.\d+)?)", re.IGNORECASE)
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
        "**CALIBRATION ANCHORS FOR DEBATE MODE:**\n"
        "- 0-3: Proposal fundamentally misses the task or is completely vague\n"
        "- 4-5: Proposal addresses task but lacks concrete file/function references\n"
        "- 6-7: Proposal is specific but has logical gaps or incomplete reasoning\n"
        "- 8-9: Proposal is specific, complete, and logically sound with minor improvements needed\n"
        "- 10: Proposal is perfect - cites exact files/functions and addresses all requirements\n\n"
        "**CRITICAL QUALITY SIGNALS:**\n"
        "✓ MUST name concrete code entities (files, functions, classes) from source context\n"
        "✓ MUST address the falsifiable criterion directly\n"
        "✓ MUST provide numbered implementation steps with file paths\n"
        "✗ Vague proposals without file/function references score ≤4 on Specificity\n"
        "✗ Proposals that ignore the falsifiable criterion score ≤5 on Completeness\n\n"
        "**STRUCTURED OUTPUT REQUIREMENTS:**\n"
        "1. Use exact section headers from your system prompt\n"
        "2. Place SCORE: X.X on its own line at the very end\n"
        "3. Reference specific file::function in all findings\n"
        "4. Provide numbered, actionable feedback items\n"
        "5. Include concrete calibration anchors in your scoring\n"
        "6. Use the DELTA VS PRIOR BEST header to compare with previous rounds\n\n"
        "**SCORING GUIDANCE:**\n"
        "- For debate mode, typical scores range 5-9 for meaningful proposals\n"
        "- Score 10 only for proposals that are truly perfect and reference specific code\n"
        "- Score ≤4 for proposals that are vague or miss the falsifiable criterion\n"
        "- Use fractional scores (7.5, 8.2) to indicate nuanced assessment\n\n"
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
        "**CALIBRATION ANCHORS FOR IMPLEMENT MODE:**\n"
        "- 0-3: Code is broken, tests fail, or change doesn't compile\n"
        "- 4-5: Code works but has critical bugs or missing functionality\n"
        "- 6-7: Code works with significant issues or missing edge cases\n"
        "- 8-9: Code works well with minor improvements or test gaps\n"
        "- 10: Perfect implementation - all tests pass, edge cases handled, code is clean\n\n"
        "**CRITICAL QUALITY SIGNALS:**\n"
        "✓ MUST verify actual code changes in files (not just proposal text)\n"
        "✓ MUST check test results and syntax validity\n"
        "✓ MUST assess error handling and edge cases\n"
        "✗ Code with syntax errors or failing tests scores ≤5 on Correctness\n"
        "✗ Implementations without tests for new functionality score ≤7\n\n"
        "**STRUCTURED OUTPUT REQUIREMENTS:**\n"
        "1. Use exact section headers from your system prompt\n"
        "2. Place SCORE: X.X on its own line at the very end\n"
        "3. Reference specific file::function in all findings\n"
        "4. Provide numbered, actionable feedback items\n"
        "5. Include concrete calibration anchors in your scoring\n"
        "6. Focus on executed code, not the proposal text\n"
        "7. Use the DELTA VS PRIOR BEST header to compare with previous rounds\n\n"
        "**SCORING GUIDANCE:**\n"
        "- For implement mode, typical scores range 6-9 for working implementations\n"
        "- Score 10 only for flawless implementations with comprehensive tests\n"
        "- Score ≤5 for code with critical bugs or failing tests\n"
        "- Deduct points for missing error handling or edge cases\n"
        "- Use fractional scores to reflect nuanced quality assessment\n\n"
    ),
}


def extract_structured_feedback(text: str, evaluator_type: str = "basic", context: dict[str, Any] | None = None, mode: str | None = None) -> dict[str, Any]:
    """Extract structured feedback from evaluator output text with enhanced parsing.
    
    Returns a dict with keys:
        - "score": float or None
        - "score_confidence": float 0-1 based on calibration anchors and structure
        - "delta": str or None
        - "analysis": dict mapping dimension names to scores with rationale
        - "defect": dict with structured defect/risk information
        - "feedback_items": list of actionable feedback strings
        - "structured_feedback": dict with parsed structured feedback items
        - "improvement_suggestion": str or None
        - "mode_adaptation_score": float 0-1 rating of mode-specific adaptation
        - "warnings": list of str from validate_score_calibration
        - "calibration_anchors_used": bool indicating if calibration anchors were detected
        - "critique_structure_score": float 0-1 rating of critique structure quality
        - "validation_errors": list of validation errors if any
    """
    result = {
        "score": None,
        "score_confidence": 0.0,
        "score_breakdown": {
            "base_score": None,
            "calibration_bonus": 0.0,
            "structure_bonus": 0.0,
            "mode_adaptation_bonus": 0.0,
        },
        "analysis": {},
        "defect": None,
        "feedback_items": [],
        "structured_feedback": {},
        "improvement_suggestion": None,
        "mode_adaptation_score": 0.0,
        "mode_specific_insights": [],
        "warnings": [],
        "calibration_anchors_used": False,
        "calibration_anchor_details": [],
        "critique_structure_score": 0.0,
        "critique_structure_breakdown": {
            "analysis_structure": 0.0,
            "defect_structure": 0.0,
            "feedback_structure": 0.0,
            "improvement_structure": 0.0,
        },
        "validation_errors": [],
    }
    
    # Validate the output first
    is_valid, issues = validate_evaluator_output(text, evaluator_type)
    
    # Separate warnings from errors
    errors = [issue for issue in issues if not issue.startswith("WARNING:")]
    warnings = [issue.replace("WARNING: ", "") for issue in issues if issue.startswith("WARNING:")]
    
    if errors:
        result["error"] = "Invalid evaluator output: " + "; ".join(errors)
        result["validation_errors"] = errors
        return result
    
    if warnings:
        result["warnings"] = warnings
    
    # Check for calibration anchors in text
    calibration_phrases = [
        "SCORING CALIBRATION",
        "0-10 scale",
        "score ≤ 5",
        "score ≥ 8",
        "critical failure",
        "perfect — no issues",
        "core goal achieved",
        "risk assessment"
    ]
    anchor_count = sum(1 for line in text.split('\n') if any(phrase in line for phrase in calibration_phrases))
    result["calibration_anchors_used"] = anchor_count >= 2
    
    # Extract delta text
    if "DELTA VS PRIOR BEST:" in text:
        delta_section = text.split("DELTA VS PRIOR BEST:")[1].split("\n")[0].strip()
        result["delta"] = delta_section
        # Score delta header quality (0-1)
        if len(delta_section) > 20 and "specific" in delta_section.lower():
            result["critique_structure_score"] += 0.2
    
    # Extract score
    score_match = _STRICT_RE.findall(text)
    if score_match:
        result["score"] = float(score_match[-1])
        # Validate score calibration with context
        result["warnings"].extend(validate_score_calibration(result["score"], evaluator_type, context))
    
    # Extract analysis dimensions with improved parsing
    if "ANALYSIS:" in text:
        analysis_section = text.split("ANALYSIS:")[1].split("\n\n")[0]
        # Look for dimension scores with multiple formats
        dimension_patterns = [
            r"([A-D])\.\s+([^:]+):\s*(\d+(?:\.\d+)?)",  # A. Correctness: 8.5
            r"([A-D])\.\s+([^:]+)\s+(\d+(?:\.\d+)?)",    # A. Correctness 8.5
            r"([^:]+):\s*(\d+(?:\.\d+)?)",               # Correctness: 8.5
            r"([^:]+)\s+(\d+(?:\.\d+)?)",                # Correctness 8.5
        ]
        
        for pattern in dimension_patterns:
            matches = re.findall(pattern, analysis_section)
            for match in matches:
                if len(match) == 3:  # Format with letter prefix
                    letter, dimension, score = match
                    result["analysis"][dimension.strip()] = float(score)
                elif len(match) == 2:  # Format without letter
                    dimension, score = match
                    result["analysis"][dimension.strip()] = float(score)
        
        # Score analysis structure (0-1)
        if len(result["analysis"]) >= 3:
            result["critique_structure_score"] += 0.3
        if any(score <= 5.0 for score in result["analysis"].values()):
            result["critique_structure_score"] += 0.2
    
    # Extract defect/risk with structured parsing
    defect_key = "TOP DEFECT:" if evaluator_type == "basic" else "KEY RISK:"
    if defect_key in text:
        defect_section = text.split(defect_key)[1].split("\n")[0].strip()
        if defect_section.lower() != "none":
            result["defect"] = defect_section
            # Try to parse structured defect: "file.py::function — description"
            if "::" in defect_section and "—" in defect_section:
                file_part, rest = defect_section.split("::", 1)
                if "—" in rest:
                    func_part, desc = rest.split("—", 1)
                    result["structured_feedback"]["defect"] = {
                        "file": file_part.strip(),
                        "function": func_part.strip(),
                        "description": desc.strip()
                    }
    
    # Extract feedback items with structured parsing
    feedback_section_name = "ACTIONABLE FEEDBACK:" if evaluator_type == "basic" else "ACTIONABLE MITIGATIONS:"
    if feedback_section_name in text:
        # Get everything after the feedback section
        after_feedback = text.split(feedback_section_name)[1]
        
        # Find where the next section starts or end of text
        next_sections = ["WHAT WOULD MAKE THIS 10/10:", "SCORE:", "FINAL SCORE:", "COMBINED_SCORE:"]
        feedback_end = len(after_feedback)
        for section in next_sections:
            idx = after_feedback.find(section)
            if idx != -1 and idx < feedback_end:
                feedback_end = idx
        
        feedback_section = after_feedback[:feedback_end].strip()
        
        # Extract numbered items with multiple formats
        item_patterns = [
            r"^\s*(\d+)\.\s+(.+)$",  # 1. item
            r"^\s*[-*]\s+(.+)$",     # - item or * item
        ]
        
        structured_feedback = []
        for line in feedback_section.split('\n'):
            line = line.strip()
            if not line:
                continue
                
            priority = None
            feedback_text = line
            
            # Try numbered format first
            match = re.match(r"^\s*(\d+)\.\s+(.+)$", line)
            if match:
                priority = int(match.group(1))
                feedback_text = match.group(2)
            else:
                # Try bullet format
                match = re.match(r"^\s*[-*]\s+(.+)$", line)
                if match:
                    feedback_text = match.group(1)
            
            # Parse file::function — change pattern
            file = None
            function = None
            change = feedback_text
            
            if "::" in feedback_text and "—" in feedback_text:
                file_part, rest = feedback_text.split("::", 1)
                if "—" in rest:
                    func_part, change_part = rest.split("—", 1)
                    file = file_part.strip()
                    function = func_part.strip()
                    change = change_part.strip()
                    structured_feedback.append({
                        "priority": priority,
                        "file": file,
                        "function": function,
                        "change": change
                    })
            
            result["feedback_items"].append(feedback_text)
        
        if structured_feedback:
            result["structured_feedback"]["actionable_items"] = structured_feedback
        
        # Score actionable feedback quality (0-1)
        if len(result["feedback_items"]) >= 2:
            result["critique_structure_score"] += 0.2
        if any("::" in item and "—" in item for item in result["feedback_items"]):
            result["critique_structure_score"] += 0.3
    
    # Extract improvement suggestion
    if "WHAT WOULD MAKE THIS 10/10:" in text:
        improvement = text.split("WHAT WOULD MAKE THIS 10/10:")[1].split("\n")[0].strip()
        if improvement.lower() != "already perfect":
            result["improvement_suggestion"] = improvement
    
    # Normalize critique structure score to 0-1 range
    result["critique_structure_score"] = min(1.0, result["critique_structure_score"])
    
    return result


def validate_score_calibration(score: float, evaluator_type: str = "basic", context: dict[str, Any] | None = None) -> list[str]:
    """Validate that a score is properly calibrated for the evaluator type.
    
    Returns a list of warnings if the score seems miscalibrated.
    
    Args:
        score: The score to validate (0-10)
        evaluator_type: "basic" or "diffusion"
        context: Optional context dict with keys:
            - "mode": "debate" or "implement" (affects expected score ranges)
            - "has_critical_issues": bool indicating if critical issues were found
            - "has_tests": bool indicating if tests were added/modified
            - "file_count": int number of files changed
            - "line_count": int number of lines changed
            - "phase_name": str name of the current phase
            - "has_syntax_errors": bool indicating if syntax errors were found
            - "has_test_failures": bool indicating if tests failed
            - "has_import_errors": bool indicating if import errors occurred
    
    Returns:
        List of warning messages if score appears miscalibrated
    """
    warnings = []
    
    # Basic sanity checks
    if score < 0.0 or score > 10.0:
        warnings.append(f"Score {score} is outside valid range [0, 10]")
        return warnings
    
    # Extract context information with defaults
    mode = context.get("mode") if context else None
    has_critical_issues = context.get("has_critical_issues", False) if context else False
    has_tests = context.get("has_tests", False) if context else False
    file_count = context.get("file_count", 0) if context else 0
    line_count = context.get("line_count", 0) if context else 0
    phase_name = context.get("phase_name", "") if context else ""
    has_syntax_errors = context.get("has_syntax_errors", False) if context else False
    has_test_failures = context.get("has_test_failures", False) if context else False
    has_import_errors = context.get("has_import_errors", False) if context else False
    
    # Score calibration anchors based on phase mode and context
    if evaluator_type == "basic":
        # Basic evaluator calibration rules with phase-mode adaptation
        if mode == "debate":
            # Debate mode: evaluating text proposals
            # Score calibration anchors for debate mode
            if score < 4.0 and not has_critical_issues:
                warnings.append(f"Score {score} seems too low for debate mode without critical issues")
            elif score > 9.0 and file_count == 0 and line_count == 0:
                warnings.append(f"Score {score} seems too high for debate-only proposal with no code changes")
            # Debate proposals with concrete file/function references should score higher
            if score > 7.0 and file_count == 0:
                warnings.append(f"Score {score} for debate mode with no specific file references - check specificity")
                # Log a warning for debate mode scores without file references
                log.warning(f"Debate mode score {score} lacks file reference in {evaluator_type} evaluator")
            
            # Phase-specific calibration anchors
            if "analysis" in phase_name.lower():
                # Analysis phases should have more nuanced scoring
                if score > 8.5 and not has_tests:
                    warnings.append(f"Score {score} seems high for analysis phase without test considerations")
            
        elif mode == "implement":
            # Implement mode: evaluating executed code
            # Score calibration anchors for implement mode with phase adaptation
            if score < 5.0 and not has_critical_issues:
                warnings.append(f"Score {score} seems too low for implement mode without critical issues")
            elif score > 9.5 and not has_tests:
                warnings.append(f"Score {score} seems too high for implement mode without test coverage")
            
            # Phase-specific implement mode calibration
            if "improvement" in phase_name.lower():
                # Improvement phases should have stricter scoring
                if score > 8.0 and not has_tests:
                    warnings.append(f"Score {score} seems high for improvement phase without test coverage")
                if score > 9.0 and has_syntax_errors:
                    warnings.append(f"Score {score} seems too high for improvement phase with syntax errors")
            
            elif "framework" in phase_name.lower():
                # Framework changes need careful evaluation
                if score > 8.5 and file_count > 2:
                    warnings.append(f"Score {score} for framework change affecting {file_count} files - verify backward compatibility")
            
            # Implementations with many changes but perfect score are suspicious
            if score == 10.0 and line_count > 50:
                warnings.append(f"Perfect score {score} for large change ({line_count} lines) - verify no edge cases missed")
            
            # Critical issues should significantly lower scores
            if score > 7.0 and has_critical_issues:
                warnings.append(f"Score {score} seems high despite critical issues - verify issue severity")
            
            # Import errors should severely impact scores
            if score > 6.0 and has_import_errors:
                warnings.append(f"Score {score} seems high with import errors - verify functionality")
            
            # Test failures should impact scores
            if score > 8.0 and has_test_failures:
                warnings.append(f"Score {score} seems high with test failures - verify test coverage")
                
        else:
            # Generic basic evaluator rules
            if score < 3.0 and not has_critical_issues:
                warnings.append(f"Score {score} seems too low for basic evaluator without critical issues")
            elif score > 9.5:
                warnings.append(f"Score {score} seems too high for basic evaluator - check for score inflation")
                
    else:  # diffusion evaluator
        # Diffusion evaluator focuses on risk assessment
        if score < 2.0 and not has_critical_issues:
            warnings.append(f"Score {score} seems too low for diffusion evaluator without critical issues")
        elif score > 9.0:
            warnings.append(f"Score {score} seems too high for diffusion evaluator - risk assessment may be too optimistic")
        
        # Diffusion evaluator should penalize risky changes
        if score > 7.0 and file_count > 3 and line_count > 100:
            warnings.append(f"Score {score} seems high for large, complex change in diffusion evaluation")
    
    # Check for suspicious patterns
    if score == 0.0 or score == 10.0:
        warnings.append(f"Extreme score {score} - verify calibration anchors were used")
    
    # Round number scores (5.0, 6.0, etc.) might indicate lazy scoring
    if score % 1.0 == 0.0 and 3.0 <= score <= 8.0:
        warnings.append(f"Round number score {score} - check if proper calibration anchors were used")
    
    return warnings


def _score_is_in_code_block(text: str, score_line_start: int) -> bool:
    """Check if a SCORE: line starting at score_line_start is inside a markdown code block.
    
    Uses a state machine to track:
    - in_code_block: whether we're currently inside a code block
    - backtick_count: consecutive backticks seen on current line
    - block_start_char: the character that started the current code block (for nested blocks)
    - same_line_backticks: whether backticks are on the same line as the score
    
    Args:
        text: The full text to analyze
        score_line_start: The character index where the SCORE: line starts
        
    Returns:
        True if the SCORE: line is inside a code block, False otherwise
    """
    # State machine variables
    in_code_block = False
    backtick_count = 0
    block_start_char = None  # '`' for inline code, None for no block
    i = 0
    
    while i < score_line_start and i < len(text):
        char = text[i]
        
        # Count consecutive backticks
        if char == '`':
            backtick_count += 1
        else:
            # Not a backtick - process any pending backtick sequence
            if backtick_count > 0:
                if backtick_count >= 3:
                    # Triple+ backticks toggle code block state
                    in_code_block = not in_code_block
                    if in_code_block:
                        block_start_char = '`'
                    else:
                        block_start_char = None
                elif backtick_count == 1 and not in_code_block:
                    # Single backtick starts inline code
                    block_start_char = '`'
                elif backtick_count == 1 and in_code_block and block_start_char == '`':
                    # Single backtick inside a code block - ignore (part of content)
                    pass
                # Reset backtick count
                backtick_count = 0
            else:
                # Check for end of inline code
                if block_start_char == '`' and char == '`':
                    # Found closing backtick for inline code
                    block_start_char = None
                    # Skip the closing backtick
                    i += 1
                    continue
        
        i += 1
    
    # Process any trailing backticks at the boundary
    if backtick_count > 0:
        if backtick_count >= 3:
            # If we have triple backticks at the boundary, toggle state
            in_code_block = not in_code_block
    
    return in_code_block


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
    
    # DELTA VS PRIOR BEST is optional but if present must have descriptive text
    if "DELTA VS PRIOR BEST:" in text:
        delta_lines = [line for line in text.split('\n') if line.strip().startswith("DELTA VS PRIOR BEST:")]
        if delta_lines:
            delta_line = delta_lines[0]
            delta_text = delta_line.split("DELTA VS PRIOR BEST:")[1].strip()
            if not delta_text or len(delta_text) < 5:
                issues.append("'DELTA VS PRIOR BEST:' must have descriptive text (minimum 5 characters)")
    
    # Check for required sections
    for section in required_sections:
        if section not in text:
            issues.append(f"Missing required section: {section}")
    
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
    
    # Check for SCORE format - can have trailing text after the score
    score_lines = [line for line in text.split('\n') if line.strip().startswith('SCORE:')]
    if score_lines:
        score_line = score_lines[-1].strip()
        # Check for proper SCORE: X.X format (allows trailing text)
        if not re.match(r'^SCORE:\s*\d+(?:\.\d+)?\b', score_line):
            issues.append(f"SCORE line malformed: '{score_line}' - expected 'SCORE: X.X' with optional trailing text")
        # Check that score is the last thing in the output (most reliable)
        # Allow for trailing whitespace after the score line
        text_stripped = text.strip()
        score_line_stripped = score_line.strip()
        if not text_stripped.endswith(score_line_stripped) and not text_stripped.endswith(score_line_stripped + '\n'):
            issues.append("SCORE should be the last line of the output for reliable parsing")
        
        # Extract and validate score calibration
        try:
            score_match = re.search(r'SCORE:\s*(\d+(?:\.\d+)?)', score_line)
            if score_match:
                score = float(score_match.group(1))
                calibration_warnings = validate_score_calibration(score, evaluator_type)
                for warning in calibration_warnings:
                    issues.append(f"WARNING: {warning}")
        except (ValueError, AttributeError):
            pass  # Already caught by format check above
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
    
    # Token budget check (warning only)
    if len(text) > 8000:  # Rough estimate: ~2000 tokens
        issues.append("WARNING: Evaluator output may exceed token budget (consider truncation)")
    
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
