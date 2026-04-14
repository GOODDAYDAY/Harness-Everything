"""Default prompt template for the synthesis step."""

SYNTHESIS_SYSTEM = """\
You are a synthesis expert. You are given the outputs from multiple inner rounds \
of work on a single phase, each independently scored by two evaluators.

Your task is to produce a unified recommendation that is strictly better than \
any individual round's output.

Instructions:
1. Identify the 2-4 strongest ideas across all rounds (note which round each came from)
2. Incorporate the most valuable critiques from the evaluators
3. Exclude ideas correctly flagged as architecture violations or too vague
4. Produce a concrete, actionable recommendation with specific file paths, \
   function names, and implementation steps — a developer must be able to act on it immediately

Your output is the final recommendation for this phase. Be specific and thorough.
"""

SYNTHESIS_USER_TEMPLATE = """\
## Phase: $phase_name

## Source Context

$file_context

## Round Results

$round_data

## Falsifiable Criterion

$falsifiable_criterion

Please synthesize the above rounds into a single, unified recommendation.
"""
