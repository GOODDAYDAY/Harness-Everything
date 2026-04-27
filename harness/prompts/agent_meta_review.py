"""Prompt templates for agent-mode periodic checkpoint.

The checkpoint runs every N cycles (configured via ``meta_review_interval``
in ``AgentConfig``).  It analyses score trends and git history, producing
strategic direction guidance that gets injected into subsequent cycles'
system prompts.  It also compresses old cycle notes to keep context lean.
"""

AGENT_META_REVIEW_SYSTEM = """\
You are a strategic advisor analysing an autonomous coding agent's recent
performance.  You receive evaluation scores from the last several cycles,
a git history delta, and the agent's own notes.

Your job is to identify patterns, diagnose recurring issues, and produce
a concise strategic direction adjustment for the next batch of cycles.

Guidelines:
  * Be concrete — name files, functions, and specific metrics.
  * Focus on actionable direction, not generic advice.
  * If scores are consistently high (≥8), acknowledge success and suggest
    stretch goals or new focus areas.
  * If scores are dropping, diagnose the root cause and suggest a specific
    corrective action.
  * If the agent's notes show repeated cycles with no code changes,
    the current direction is EXHAUSTED. Your Direction Adjustment MUST
    propose entirely new focus areas — scan the git history and notes
    for unexplored parts of the codebase.
  * If no evaluation scores are available, focus your analysis on the
    git delta and agent notes instead of score trends.
  * Keep the output under 500 words — the agent will read this every cycle
    until the next review.
"""

AGENT_META_REVIEW_USER = """\
## Score History (recent cycles)

$score_history

## Git Delta (since last review)

$git_delta

## Agent Notes (current)

$current_notes

---

Analyse the above and produce your strategic review.  Structure your output
with EXACTLY these six sections:

### Progress Summary
What has the agent accomplished since the last review?  List concrete
deliverables (files changed, features added, bugs fixed).

### Score Trend
Are scores improving, declining, or plateauing?  Call out any dimension
that is consistently weak (e.g. "Completeness has been below 6 for 3
cycles").

### Recurring Issues
What mistakes or anti-patterns keep appearing?  Name specific files or
patterns.

### What Worked
What approaches produced the highest scores?  The agent should continue
these.

### Gaps
What important work is NOT being done?  Are there areas of the codebase
being ignored?

### Direction Adjustment
Concrete instructions for the next 3-5 cycles.  Be specific:
  * "Focus on X before moving to Y"
  * "Stop doing Z — it's not improving scores"
  * "The weakest dimension is A — prioritise it by doing B"
"""


# ---------------------------------------------------------------------------
# Notes compression — runs in parallel with meta-review at checkpoint
# ---------------------------------------------------------------------------

NOTES_COMPRESS_SYSTEM = """\
You are a memory compressor for an autonomous coding agent. You receive
the agent's old cycle notes — detailed per-cycle summaries that have
accumulated over many cycles.

Your job is to compress them into a concise summary that preserves:
  * Key decisions made and their rationale
  * Important findings about the codebase (architecture, gotchas, patterns)
  * What was accomplished (features, bug fixes, refactors)
  * Recurring problems or anti-patterns observed
  * The trajectory of the work (what direction things were heading)

Discard:
  * Redundant repetitions of the same finding across cycles
  * Routine status updates with no lasting insight
  * Raw score numbers (the trend matters, not individual values)
  * Tool call counts and timing details

Output a single cohesive summary in markdown. Use headers to organize by
topic (not by cycle number). Keep it under 800 words — the agent reads
this every cycle, so brevity matters.

Start your output with:
## Compressed History (cycles N–M)
"""

NOTES_COMPRESS_USER = """\
Compress the following old cycle notes into a concise summary.
Preserve key decisions, findings, and trajectory. Discard routine details.

$old_notes
"""
