"""Prompt templates for the meta-review phase."""

from __future__ import annotations

META_REVIEW_SYSTEM = """\
You are a meta-reviewer analysing the progress of an iterative self-improvement \
pipeline.  Your job is to identify patterns across multiple rounds — what is \
working, what is not, and what should change in subsequent rounds.

Be concrete and actionable.  Reference specific round numbers, scores, and \
evaluator critiques when making recommendations.
"""

META_REVIEW_USER = """\
## Pipeline Progress Report (Rounds $start_round – $end_round)

### Score Trend
$score_trend

### Git Changes Since Last Review
$git_delta

### Evaluator Critiques (Recent Rounds)
$critiques

### Cross-Round Memory
$memory_context

---

Produce a meta-review with EXACTLY these five sections:

## Progress Summary
One paragraph: what was accomplished across these rounds, net effect on codebase.

## Recurring Issues
Bullet list of defects or problems that evaluators flagged more than once.

## What Worked
Bullet list of approaches that produced score improvements.

## Gaps
What has NOT been attempted yet but should be, based on the evaluation criteria.

## Prompt Adjustment Suggestions
For each pipeline phase, suggest ONE concrete prompt change that would \
address the issues above.  Format each suggestion as:
- **Phase: <name>** — <what to change and why>
"""
