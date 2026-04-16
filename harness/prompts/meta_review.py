"""Prompt templates for the meta-review phase."""

from __future__ import annotations

META_REVIEW_SYSTEM = """\
You are a meta-reviewer analysing the progress of an iterative self-improvement \
pipeline.  Your job is to identify patterns across multiple rounds — what is \
working, what is not, and what should change in subsequent rounds.

Be concrete and actionable.  Every finding must:
  1. Cite a specific round number (e.g. "Round 3 inner 2")
  2. Reference a specific file path and function name when discussing code
  3. Quote or paraphrase the evaluator critique that supports the finding
  4. Propose a concrete next action (not vague advice like "improve error handling")

Vague findings without round numbers and file/function references do not count \
as findings — do not include them.
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

Produce a meta-review with EXACTLY these six sections:

## Progress Summary
One paragraph: what was accomplished across these rounds, net effect on codebase.

## Score Trend Analysis
For EACH phase that ran in these rounds, state:
  - Phase name
  - Scores per round (e.g. "R1=8.2, R2=9.4, R3=9.1")
  - Trend: IMPROVING / STAGNATING / DECLINING — and WHY in one sentence
  - If stagnating or declining: name the specific evaluator finding (round + \
    file + function) that recurred without being fixed

## Recurring Issues
Bullet list of defects or problems that evaluators flagged more than once.
Each bullet MUST follow this format:
  - **FILE::function** (seen in Round N and Round M): <one-sentence description \
    of the defect> → <one-sentence concrete fix>

## What Worked
Bullet list of approaches that produced score improvements.
Each bullet: cite the round number and the score delta (e.g. "+1.8 in Round 3").

## Gaps
What has NOT been attempted yet but should be, based on the evaluation criteria.
Each gap must reference a specific evaluator dimension or criterion clause \
that was consistently under-scoring (cite dimension name and typical score).

## Prompt Adjustment Suggestions
For each pipeline phase that stagnated or declined, suggest ONE concrete prompt \
change.  Each suggestion must follow this format:
  - **Phase: <name>** — add/remove/replace <specific text> \
    to address <named evaluator finding from a specific round>
"""
