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
    
    # Enhanced calibration anchor detection with mode-specific anchors
    calibration_phrases = [
        # General calibration anchors
        "SCORING CALIBRATION",
        "0-10 scale",
        "score ≤ 5",
        "score ≥ 8",
        "critical failure",
        "perfect — no issues",
        "core goal achieved",
        "risk assessment",
        # Mode-specific calibration anchors
        "debate mode scoring",
        "implement mode scoring",
        "analysis phase scoring",
        "improvement phase scoring",
        "framework change scoring",
        # Score range anchors
        "score range 0-3",
        "score range 4-6",
        "score range 7-8",
        "score range 9-10",
        # Dimension-specific anchors
        "correctness anchor",
        "completeness anchor",
        "clarity anchor",
        "test coverage anchor",
        "risk mitigation anchor",
        # Context-aware anchors
        "given the context",
        "considering the phase",
        "based on mode",
        "relative to expectations"
    ]
    
    # Enhanced detection with weighting
    anchor_details = []
    lines = text.split('\n')
    for line in lines:
        for phrase in calibration_phrases:
            if phrase.lower() in line.lower():
                anchor_details.append({
                    "phrase": phrase,
                    "context": line.strip(),
                    "weight": 1.0 if phrase in ["SCORING CALIBRATION", "0-10 scale"] else 0.8
                })
                break  # Only count each line once
    
    anchor_count = len(anchor_details)
    weighted_anchor_score = sum(detail["weight"] for detail in anchor_details)
    
    # Enhanced calibration anchor detection logic
    result["calibration_anchors_used"] = weighted_anchor_score >= 1.5
    result["calibration_anchor_details"] = anchor_details
    result["calibration_anchor_score"] = min(weighted_anchor_score / 3.0, 1.0)  # Normalize to 0-1
    
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
        
        # Enhanced analysis structure scoring with mode-awareness
        analysis_structure_score = 0.0
        
        # Dimension count scoring
        dimension_count = len(result["analysis"])
        if dimension_count >= 4:
            analysis_structure_score += 0.4
        elif dimension_count >= 3:
            analysis_structure_score += 0.3
        elif dimension_count >= 2:
            analysis_structure_score += 0.2
        elif dimension_count >= 1:
            analysis_structure_score += 0.1
        
        # Score distribution quality
        scores = list(result["analysis"].values())
        if scores:
            score_range = max(scores) - min(scores)
            if score_range >= 2.0:  # Good discrimination between dimensions
                analysis_structure_score += 0.2
            elif score_range >= 1.0:
                analysis_structure_score += 0.1
            
            # Check for critical dimension scoring (scores ≤ 5 indicate critical analysis)
            if any(score <= 5.0 for score in scores):
                analysis_structure_score += 0.2
        
        # Mode-specific analysis structure expectations
        if mode == "debate":
            # Debate mode should have strong reasoning dimensions
            debate_dims = ["reasoning", "clarity", "specificity", "feasibility"]
            debate_dim_count = sum(1 for dim in result["analysis"].keys() 
                                  if any(debate_word in dim.lower() for debate_word in debate_dims))
            if debate_dim_count >= 2:
                analysis_structure_score += 0.2
        
        elif mode == "implement":
            # Implement mode should have strong execution dimensions
            implement_dims = ["correctness", "completeness", "test", "maintainability", "performance"]
            implement_dim_count = sum(1 for dim in result["analysis"].keys() 
                                     if any(impl_word in dim.lower() for impl_word in implement_dims))
            if implement_dim_count >= 2:
                analysis_structure_score += 0.2
        
        # Update critique structure breakdown
        result["critique_structure_breakdown"]["analysis_structure"] = min(analysis_structure_score, 1.0)
        result["critique_structure_score"] += min(analysis_structure_score, 1.0)
    
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
            r"^\s*(\d+)\.\s+(.+)$",      # 1. item
            r"^\s*(\d+)\)\s+(.+)$",      # 1) item
            r"^\s*\((\d+)\)\s+(.+)$",    # (1) item
            r"^\s*[-*•]\s+(.+)$",        # - item, * item, or • item
        ]
        
        structured_feedback = []
        for line in feedback_section.split('\n'):
            line = line.strip()
            if not line:
                continue
                
            priority = None
            feedback_text = line
            
            # Try numbered formats
            numbered_match = None
            for pattern in [r"^\s*(\d+)\.\s+(.+)$", r"^\s*(\d+)\)\s+(.+)$", r"^\s*\((\d+)\)\s+(.+)$"]:
                numbered_match = re.match(pattern, line)
                if numbered_match:
                    break
            
            if numbered_match:
                priority = int(numbered_match.group(1))
                feedback_text = numbered_match.group(2)
            else:
                # Try bullet format
                bullet_match = re.match(r"^\s*[-*•]\s+(.+)$", line)
                if bullet_match:
                    feedback_text = bullet_match.group(1)
            
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
            - "has_structure_issues": bool indicating if output structure issues were found
            - "has_calibration_anchors": bool indicating if calibration anchors were used
            - "critique_structure_score": float 0-1 rating of critique structure quality
    
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
    has_structure_issues = context.get("has_structure_issues", False) if context else False
    has_calibration_anchors = context.get("has_calibration_anchors", False) if context else False
    critique_structure_score = context.get("critique_structure_score", 0.0) if context else 0.0
    has_balanced_risk_assessment = context.get("has_balanced_risk_assessment", False) if context else False
    
    # Score calibration anchors based on phase mode and context
    if evaluator_type == "basic":
        # Basic evaluator calibration rules with phase-mode adaptation
        if mode == "debate":
            # Debate mode: evaluating text proposals with enhanced discrimination
            # Score calibration anchors for debate mode with strict mode discrimination
            if score < 4.0 and not has_critical_issues:
                warnings.append(f"Score {score} seems too low for debate mode without critical issues - typical debate scores range 5-9")
            elif score > 9.0 and file_count == 0 and line_count == 0:
                warnings.append(f"Score {score} seems too high for debate-only proposal with no code changes - debate proposals rarely score >9")
            # Debate proposals with concrete file/function references should score higher
            if score > 7.0 and file_count == 0:
                warnings.append(f"Score {score} for debate mode with no specific file references - high scores require concrete file::function citations")
                # Log a warning for debate mode scores without file references
                log.warning(f"Debate mode score {score} lacks file reference in {evaluator_type} evaluator")

            # Enhanced discrimination: Check for calibration anchor usage with mode-specific anchors
            if score >= 8.0 and not has_calibration_anchors:
                warnings.append(f"High score {score} without calibration anchor references - debate mode scores ≥8 should explicitly reference calibration criteria")
            # Mode-specific anchor check: debate mode should reference reasoning quality anchors
            if score >= 7.0 and not has_calibration_anchors:
                warnings.append(f"Score {score} in debate mode without calibration anchors - debate evaluations should reference reasoning quality criteria")

            # Enhanced discrimination: Check critique structure quality with mode-specific expectations
            if score >= 7.0 and critique_structure_score < 0.6:
                warnings.append(f"Score {score} has weak critique structure (score={critique_structure_score:.1f}) - debate mode requires well-structured reasoning analysis")
            # Debate mode should have strong reasoning structure
            if score >= 6.0 and critique_structure_score < 0.4:
                warnings.append(f"Score {score} for debate mode has insufficient structure - debate evaluations require clear argument analysis")

            # Phase-specific calibration anchors with enhanced discrimination
            if "analysis" in phase_name.lower():
                # Analysis phases should have more nuanced scoring with strict discrimination
                if score > 8.5 and not has_tests:
                    warnings.append(f"Score {score} seems high for analysis phase without test considerations - analysis phases focus on reasoning quality")
                # Analysis phases should have strong structure with mode-specific expectations
                if score > 7.0 and critique_structure_score < 0.7:
                    warnings.append(f"Score {score} for analysis phase has weak structure - analysis requires clear dimension scoring and rationale")
                # Analysis phases should show dimension discrimination
                if score > 6.0 and critique_structure_score < 0.5:
                    warnings.append(f"Score {score} for analysis phase lacks dimension discrimination - analysis should show clear scoring differences")

            # Debate mode specific: Check for reasoning depth indicators
            if score >= 8.0 and line_count == 0:
                warnings.append(f"Score {score} for debate mode with no code changes - verify reasoning depth and argument quality")

        elif mode == "implement":
            # Implement mode: evaluating executed code with enhanced discrimination
            # Score calibration anchors for implement mode with strict phase adaptation
            if score < 5.0 and not has_critical_issues:
                warnings.append(f"Score {score} seems too low for implement mode without critical issues - working implementations typically score ≥6")
            elif score > 9.5 and not has_tests:
                warnings.append(f"Score {score} seems too high for implement mode without test coverage - scores >9.5 require comprehensive testing")

            # Enhanced discrimination: Check for calibration anchor usage with mode-specific anchors
            if score >= 8.0 and not has_calibration_anchors:
                warnings.append(f"High score {score} without calibration anchor references - implement mode scores ≥8 should reference execution quality anchors")
            # Mode-specific anchor check: implement mode should reference execution quality anchors
            if score >= 7.0 and not has_calibration_anchors:
                warnings.append(f"Score {score} in implement mode without calibration anchors - implement evaluations should reference execution quality criteria")

            # Enhanced discrimination: Check critique structure quality with mode-specific expectations
            if score >= 7.0 and critique_structure_score < 0.7:
                warnings.append(f"Score {score} has weak critique structure (score={critique_structure_score:.1f}) - implement evaluations require detailed analysis of code changes")
            # Implement mode should have strong execution analysis
            if score >= 6.0 and critique_structure_score < 0.5:
                warnings.append(f"Score {score} for implement mode has insufficient structure - implement evaluations require clear code analysis")

            # Phase-specific implement mode calibration with enhanced discrimination
            if "improvement" in phase_name.lower():
                # Improvement phases should have stricter scoring with enhanced discrimination
                if score > 8.0 and not has_tests:
                    warnings.append(f"Score {score} seems high for improvement phase without test coverage - improvements require validation")
                if score > 9.0 and has_syntax_errors:
                    warnings.append(f"Score {score} seems too high for improvement phase with syntax errors - syntax errors are critical failures")
                # Improvement phases need strong structure with mode-specific expectations
                if score > 7.0 and critique_structure_score < 0.8:
                    warnings.append(f"Score {score} for improvement phase has weak structure - improvements require clear before/after analysis")
                # Improvement phases should show measurable impact
                if score > 8.0 and line_count < 10:
                    warnings.append(f"Score {score} for improvement phase with minimal changes - verify impact and justification")

            elif "framework" in phase_name.lower():
                # Framework changes need careful evaluation with enhanced discrimination
                if score > 8.5 and file_count > 2:
                    warnings.append(f"Score {score} for framework change affecting {file_count} files - verify backward compatibility and impact analysis")
                # Framework changes require excellent structure with mode-specific expectations
                if score > 7.0 and critique_structure_score < 0.9:
                    warnings.append(f"Score {score} for framework change has weak structure - framework evaluations require comprehensive impact analysis")
                # Framework changes should have architectural analysis
                if score > 8.0 and critique_structure_score < 0.8:
                    warnings.append(f"Score {score} for framework change lacks architectural analysis - framework changes require impact assessment")

            # Implementations with many changes but perfect score are suspicious
            if score == 10.0 and line_count > 50:
                warnings.append(f"Perfect score {score} for large change ({line_count} lines) - verify no edge cases missed and all tests pass")

            # Perfect scores require perfect structure with mode-specific expectations
            if score == 10.0 and critique_structure_score < 0.95:
                warnings.append(f"Perfect score {score} has imperfect structure (score={critique_structure_score:.1f}) - perfect scores require flawless critique structure")
            # Perfect implement scores require comprehensive testing
            if score == 10.0 and not has_tests:
                warnings.append(f"Perfect score {score} without test coverage - perfect implement scores require comprehensive testing")

            # Critical issues should significantly lower scores with enhanced discrimination
            if score > 7.0 and has_critical_issues:
                warnings.append(f"Score {score} seems high despite critical issues - critical issues should reduce scores to ≤6")
            # Critical issues in implement mode are severe
            if score > 6.0 and has_critical_issues:
                warnings.append(f"Score {score} seems high with critical issues - implement mode critical issues typically reduce scores to ≤5")

            # Import errors should severely impact scores with enhanced discrimination
            if score > 6.0 and has_import_errors:
                warnings.append(f"Score {score} seems high with import errors - import errors typically reduce scores to ≤5")
            # Import errors in implement mode are critical
            if score > 5.0 and has_import_errors:
                warnings.append(f"Score {score} seems high with import errors - implement mode import errors are critical failures")

            # Test failures should impact scores with enhanced discrimination
            if score > 8.0 and has_test_failures:
                warnings.append(f"Score {score} seems high with test failures - test failures typically reduce scores to ≤7")
            # Test failures in implement mode are significant
            if score > 7.0 and has_test_failures:
                warnings.append(f"Score {score} seems high with test failures - implement mode test failures indicate functional issues")

            # Structure issues should impact scores with enhanced discrimination
            if score > 8.0 and has_structure_issues:
                warnings.append(f"Score {score} seems high with structure issues - output structure problems indicate evaluation quality issues")
            # Structure issues in implement mode affect reliability
            if score > 7.0 and has_structure_issues:
                warnings.append(f"Score {score} seems high with structure issues - implement mode structure issues affect code reliability")

        else:
            # Generic basic evaluator rules with enhanced discrimination
            if score < 3.0 and not has_critical_issues:
                warnings.append(f"Score {score} seems too low for basic evaluator without critical issues - typical minimum for functional proposals is 4")
            elif score > 9.5:
                warnings.append(f"Score {score} seems too high for basic evaluator - check for score inflation and verify calibration anchors")

            # Check for calibration anchor usage with enhanced discrimination
            if score >= 8.0 and not has_calibration_anchors:
                warnings.append(f"High score {score} without calibration anchor references - scores ≥8 should explicitly justify near-perfect assessment")
            # Generic evaluator should still use calibration anchors
            if score >= 7.0 and not has_calibration_anchors:
                warnings.append(f"Score {score} without calibration anchors - evaluator should reference calibration criteria for consistency")

    else:  # diffusion evaluator
        # Diffusion evaluator focuses on risk assessment with enhanced discrimination
        # Adjusted calibration to improve discrimination (Spearman ρ) - align with basic evaluator while maintaining risk focus
        if score < 3.0 and not has_critical_issues:
            warnings.append(f"Score {score} seems too low for diffusion evaluator without critical issues - minimal risk changes typically score ≥4")
        elif score > 9.5:
            warnings.append(f"Score {score} seems too high for diffusion evaluator - risk assessment may be too optimistic, verify mitigation analysis")

        # Enhanced discrimination: Check for calibration anchor usage with mode-specific anchors
        # Aligned calibration anchor requirements with basic evaluator for consistency
        if score >= 8.0 and not has_calibration_anchors:
            warnings.append(f"High risk score {score} without calibration anchor references - diffusion scores ≥8 should explicitly reference risk assessment criteria")
        # Diffusion evaluator should use risk anchors for high scores (aligned with basic evaluator threshold)
        if score >= 7.0 and not has_calibration_anchors:
            warnings.append(f"Score {score} in diffusion evaluator without calibration anchors - high risk assessments should reference risk criteria")
        # Low scores with critical issues should reference anchors
        if score <= 3.0 and not has_calibration_anchors and has_critical_issues:
            warnings.append(f"Low risk score {score} without calibration anchors - critical risk findings should reference risk criteria")

        # Enhanced discrimination: Check critique structure quality with mode-specific expectations
        # Aligned structure requirements with basic evaluator for better discrimination
        if score >= 7.0 and critique_structure_score < 0.7:
            warnings.append(f"Score {score} has weak critique structure (score={critique_structure_score:.1f}) - risk assessments require detailed impact analysis")
        # Diffusion evaluator needs strong risk analysis structure (aligned with basic implement mode)
        if score >= 6.0 and critique_structure_score < 0.5:
            warnings.append(f"Score {score} for diffusion evaluator has insufficient structure - risk assessments require clear impact analysis")
        # Very high scores need excellent structure (aligned with basic evaluator)
        if score >= 8.0 and critique_structure_score < 0.8:
            warnings.append(f"Score {score} has insufficient structure for high risk assessment (score={critique_structure_score:.1f}) - detailed risk analysis required")

        # Diffusion evaluator should penalize risky changes with enhanced discrimination
        # Adjusted thresholds to improve discrimination while maintaining risk focus
        if score > 8.0 and file_count > 3 and line_count > 100:
            warnings.append(f"Score {score} seems high for large, complex change in diffusion evaluation - complex changes typically have higher risk scores (≥8 requires exceptional justification)")
        # Large changes in diffusion evaluation need careful assessment
        if score > 7.0 and file_count > 2 and line_count > 50:
            warnings.append(f"Score {score} for moderate change in diffusion evaluation - verify risk assessment accounts for complexity (consider lowering to 6-7 range)")
        # Small changes should have appropriate risk scores (not necessarily lower)
        if score > 9.0 and file_count <= 1 and line_count <= 20:
            warnings.append(f"Score {score} seems high for trivial change in diffusion evaluation - trivial changes rarely have significant second-order effects")

        # Perfect diffusion scores are extremely rare with enhanced discrimination
        if score == 10.0:
            warnings.append(f"Perfect diffusion score {score} is extremely rare - verify no second-order effects and trivial rollback (requires explicit justification)")
        # High diffusion scores need justification (aligned with basic evaluator threshold)
        if score >= 9.0:
            warnings.append(f"Very high diffusion score {score} - verify comprehensive risk mitigation analysis (should include specific mitigation steps)")
        # Moderate scores should show balanced risk assessment
        if 5.0 <= score <= 7.0 and not has_balanced_risk_assessment:
            warnings.append(f"Score {score} in moderate range - verify risk assessment shows balanced view of pros/cons, not just one-sided analysis")

    # Check for suspicious patterns with enhanced discrimination
    if score == 0.0 or score == 10.0:
        warnings.append(f"Extreme score {score} - verify calibration anchors were used and justification is explicit")
        if score == 10.0 and not has_calibration_anchors:
            warnings.append(f"Perfect score {score} without calibration anchors - perfect scores require explicit reference to calibration criteria")
        # Extreme scores need strong justification
        if score == 0.0 and not has_critical_issues:
            warnings.append(f"Zero score {score} without critical issues - verify justification for complete failure assessment")

    # Round number scores (5.0, 6.0, etc.) might indicate lazy scoring with enhanced discrimination
    if score % 1.0 == 0.0 and 3.0 <= score <= 8.0:
        warnings.append(f"Round number score {score} - check if proper calibration anchors were used and fractional scoring considered")
        # Enhanced: Check if this is in a range where fractional scores are expected
        if 5.0 <= score <= 7.0:
            warnings.append(f"Score {score} in middle range where fractional scores (5.5, 6.5, etc.) provide better discrimination")
        # Round scores in critical ranges need verification
        if score == 5.0 or score == 6.0:
            warnings.append(f"Round score {score} at decision boundary - verify calibration anchors support this exact score")

    # Enhanced discrimination: Check score distribution patterns with mode awareness
    if 4.0 <= score <= 6.0 and not has_critical_issues:
        # Middle scores without critical issues should have clear rationale
        warnings.append(f"Middle score {score} without critical issues - ensure dimension scores justify this middle-ground assessment")
        # Middle scores should show dimension discrimination
        if mode == "debate" and score >= 5.0:
            warnings.append(f"Middle debate score {score} - verify reasoning quality and argument structure")
        elif mode == "implement" and score >= 5.0:
            warnings.append(f"Middle implement score {score} - verify code quality and execution correctness")
    
    # NEW: Enhanced discrimination for critical 4-7 range (most important for Spearman ρ)
    if 4.0 <= score <= 7.0:
        # Check for proper discrimination between adjacent scores
        if score == 4.0 and not has_critical_issues:
            warnings.append(f"Score {score} at lower boundary - verify this isn't a 5 (partial success) or 3 (fundamental issues)")
        elif score == 5.0 and not has_critical_issues:
            warnings.append(f"Score {score} in partial success range - ensure clear distinction from 4 (generic) and 6 (specific)")
        elif score == 6.0 and not has_critical_issues:
            warnings.append(f"Score {score} in specific but incomplete range - ensure clear distinction from 5 (partial) and 7 (mostly complete)")
        elif score == 7.0 and not has_critical_issues:
            warnings.append(f"Score {score} at upper boundary - verify this isn't a 6 (missing edge cases) or 8 (testable)")
        
        # Check for proper calibration anchor usage in critical range
        if not has_calibration_anchors and not has_critical_issues:
            warnings.append(f"Score {score} in critical discrimination range without calibration anchors - scores 4-7 require explicit reference to calibration criteria")
        
        # Check for dimension-based discrimination
        if mode == "debate" and not has_dimension_scores:
            warnings.append(f"Score {score} in debate mode without dimension scores - critical range scores should show multi-dimensional assessment")
        elif mode == "implement" and not has_dimension_scores:
            warnings.append(f"Score {score} in implement mode without dimension scores - critical range scores should show code quality dimensions")

    # Mode-specific score distribution validation
    if mode == "debate" and score > 8.0:
        # High debate scores need strong reasoning justification
        warnings.append(f"High debate score {score} - verify reasoning depth and argument quality")
    elif mode == "implement" and score > 8.0:
        # High implement scores need comprehensive validation
        warnings.append(f"High implement score {score} - verify comprehensive testing and code quality")

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
        
        # Extreme scores require more detailed justification
        if is_low_extreme or is_high_extreme:
            if analysis_words < 15:
                issues.append(
                    f"Extreme score {score} has insufficient justification "
                    f"({analysis_words} words). Extreme scores require at least 15 words "
                    f"of detailed analysis referencing calibration anchors."
                )
            
            # Check for justification of extreme nature
            justification_indicators = ["because", "since", "due to", "as", "given that"]
            has_justification = any(indicator in analysis_lower for indicator in justification_indicators)
            
            if not has_justification and analysis_words < 25:
                issues.append(
                    f"Extreme score {score} lacks explicit justification connectors "
                    f"('because', 'since', 'due to', etc.). Extreme scores require "
                    f"clear causal reasoning."
                )
    
    if not found_anchor:
        score_range = "low" if is_low_extreme else "high"
        issues.append(
            f"Extreme {score_range} score ({score}) without reference to calibration anchors. "
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
                calibration_warnings = validate_score_calibration(score, evaluator_type, mode)
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
                issues.append(f"{feedback_section} should contain at least one concrete file/function reference (e.g., 'file.py: function_name' or 'file::function')")
    
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
        mode_header = _MODE_HEADERS[mode]
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
    
    # NEW: Enhanced discrimination logging for critical 4-7 range
    if 4.0 <= clamped <= 7.0:
        log.debug(
            "parse_score: critical range score %.2f - ensure proper discrimination between adjacent scores",
            clamped
        )
        # Log discrimination guidance for critical range
        if 4.0 <= clamped < 5.0:
            log.debug("  Score ~4: Should show generic approach without specific implementation")
        elif 5.0 <= clamped < 6.0:
            log.debug("  Score ~5: Should show partial success with specific elements")
        elif 6.0 <= clamped < 7.0:
            log.debug("  Score ~6: Should show specific implementation with gaps")
        elif 7.0 <= clamped < 8.0:
            log.debug("  Score ~7: Should show mostly complete implementation with minor edge cases missing")
    
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
