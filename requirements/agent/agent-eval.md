# Agent Evaluation

> Evaluation orchestration -- DualEvaluator integration, score tracking, formatting, periodic meta-review

Source: `harness/agent/agent_eval.py` (208 lines)

## Overview

Wraps `DualEvaluator` calls, score formatting, and periodic meta-review into free functions so `agent_loop.py` stays pure orchestration. All public symbols are functions or a single dataclass -- no classes with mutable state. The agent loop owns the state (`_score_history`, `_meta_review_context`) and passes it in.

## Constants

- `_MAX_SCORE_HISTORY = 50` -- maximum score entries kept in memory

## MetaReviewResult Dataclass

| Field | Type | Description |
|---|---|---|
| `context` | `str` | Strategic direction text injected into subsequent system prompts |
| `head_hash` | `str` | HEAD hash at time of review, used as `since_hash` for next delta |

## Evaluation Functions

### `run_evaluation(evaluator, cycle, diff_text, mission, *, has_diff=True) -> DualScore | None`

Runs `DualEvaluator` on a cycle's deliverable.

**Parameters:**
- `evaluator: DualEvaluator | None` -- returns `None` immediately if `None`
- `cycle: int` -- 0-indexed cycle number
- `diff_text: str` -- the content to evaluate (diff or agent text)
- `mission: str` -- mission statement for context
- `has_diff: bool` -- selects evaluation mode

**Behavior:**
1. If `evaluator is None`: returns `None`
2. If `diff_text` is empty/whitespace: replaces with `"(empty cycle -- agent produced no output)"`
3. Truncates mission to 200 chars; falls back to `"autonomous maintenance"` if empty
4. Sets `mode = "implement"` when `has_diff=True`, `mode = "reasoning"` when False
5. Sets context label: `"code changes"` or `"agent reasoning (no code changes)"`
6. Calls `evaluator.evaluate(subject=diff_text, context=f"Mission: {mission_ctx}\nAgent cycle {cycle+1} {context_label}.", mode=mode)`
7. Logs: `"agent_eval: cycle N -- basic=X.X diffusion=X.X combined=X.X"`
8. On exception: logs warning, returns `None`

### `record_score(score, cycle, score_history) -> None`

Appends a dict to `score_history`:
```python
{"cycle": cycle + 1, "basic": score.basic.score, "diffusion": score.diffusion.score, "combined": score.combined}
```

Enforces `_MAX_SCORE_HISTORY` cap by popping the oldest entry (index 0) when exceeded.

### `persist_eval_scores(score, cycle, write_fn) -> None`

Writes JSON to artifacts via `write_fn(json_str, f"cycle_{cycle+1}", "eval_scores.json")`.

JSON content:
```json
{
  "basic": <score.basic.score>,
  "diffusion": <score.diffusion.score>,
  "combined": <score.combined>,
  "basic_critique": "<score.basic.critique[:500]>",
  "diffusion_critique": "<score.diffusion.critique[:500]>"
}
```

Logs warning on exception, does not raise.

## Formatting Functions

### `format_eval_oneliner(score) -> str`

One-line summary for git commit messages (no critique):

```
basic=7.2 diffusion=6.8 combined=7.0
```

All values formatted as `.1f`.

### `format_eval_notes(score) -> str`

Multi-line format for `agent_notes.md`:

```
[eval] basic=7.2 diffusion=6.8 combined=7.0
  basic critique: <first 200 chars>
  diffusion critique: <first 200 chars>
```

Critique lines included only if the respective critique is non-empty. Critiques truncated to 200 chars.

### `format_score_history(score_history) -> str`

Markdown table of the last 20 entries from `score_history`:

```markdown
| Cycle | Basic | Diffusion | Combined |
|-------|-------|-----------|----------|
| 1 | 7.2 | 6.8 | 7.0 |
```

Returns `"(no scores recorded yet)"` if history is empty.

## Meta-Review

### `run_meta_review(llm, cycle, score_history, since_hash, current_notes, repo_path, write_fn) -> MetaReviewResult`

Runs the periodic strategic meta-review LLM call.

**Parameters:**
- `llm: LLM` -- LLM instance for the review call
- `cycle: int` -- 0-indexed cycle number
- `score_history: list[dict]` -- score entries from `record_score`
- `since_hash: str` -- HEAD hash from last review (or empty)
- `current_notes: str` -- current contents of `agent_notes.md`
- `repo_path: Path` -- primary repository path for git delta
- `write_fn` -- artifact write callable

**Behavior:**
1. Logs "running meta-review after cycle N"
2. Formats score history as markdown table via `format_score_history()`
3. Gets git delta via `agent_git.get_review_git_delta(repo_path, since_hash or "HEAD~20")`
4. Truncates `current_notes` to 3000 chars (from the end) if longer, with debug log
5. Builds user message from `meta_review_prompts.AGENT_META_REVIEW_USER` template by replacing:
   - `$score_history` with score table
   - `$git_delta` with git delta text
   - `$current_notes` with (possibly truncated) notes
6. Calls `llm.call(messages, system=meta_review_prompts.AGENT_META_REVIEW_SYSTEM)`
7. Gets HEAD hash via `agent_git.get_head_hash(repo_path)`
8. Persists review to `cycle_N/meta_review.md` (silently ignores write failures)
9. Returns `MetaReviewResult(context=response_text, head_hash=head_hash)`
10. On exception: logs warning, returns `MetaReviewResult(context="", head_hash=since_hash)`

### Meta-Review Prompt Structure

System prompt (`AGENT_META_REVIEW_SYSTEM`): Strategic advisor role. Guidelines:
- Be concrete -- name files, functions, metrics
- Focus on actionable direction
- If scores >= 8: suggest stretch goals
- If scores dropping: diagnose root cause
- If repeated no-code-change cycles: direction is EXHAUSTED, propose new focus areas
- If no scores available: focus on git delta and notes
- Under 500 words

User prompt (`AGENT_META_REVIEW_USER`): Template with `$score_history`, `$git_delta`, `$current_notes` placeholders. Requires output in exactly six sections:
1. Progress Summary
2. Score Trend
3. Recurring Issues
4. What Worked
5. Gaps
6. Direction Adjustment

## Integration with Agent Loop

The agent loop calls these functions in this order per cycle:

1. `run_evaluation()` -- every cycle when `auto_evaluate=True`
2. `record_score()` -- if evaluation returned a score
3. `persist_eval_scores()` -- if evaluation returned a score
4. `format_eval_notes()` -- for `agent_notes.md`
5. `format_eval_oneliner()` -- for git commit message
6. `run_meta_review()` -- every `meta_review_interval` cycles (checked as `cycles_run % interval == 0`)
