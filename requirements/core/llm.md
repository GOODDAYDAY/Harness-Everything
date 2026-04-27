# S-02 LLM Client

> Thin async wrapper around the Anthropic Claude API with retry, pruning, caching, and tool-use loop.

**Source**: `harness/core/llm.py`

---

## Background

The `LLM` class is the harness's sole interface to the Claude API. It wraps the `anthropic.AsyncAnthropic` client with exponential-backoff retry on transient errors, per-process concurrency limiting via a semaphore, and a full tool-use agent loop (`call_with_tools`). The tool loop includes conversation-history pruning to avoid context-window overflow, proactive compaction of old tool results, a file-read cache to reduce duplicate disk I/O, parallel execution of read-only tools, and an in-conversation scratchpad for persistent notes.

---

## Requirements

### Retry Policy

**F-01 Retryable exceptions**

The following exception types are retried with backoff:
- `anthropic._exceptions.OverloadedError` (HTTP 529)
- `anthropic.RateLimitError` (HTTP 429)
- `anthropic.InternalServerError` (HTTP 500)
- `anthropic.APIConnectionError` (network failure)
- `anthropic.APITimeoutError` (SDK timeout)
- `asyncio.TimeoutError` (wall-clock timeout)

All other exceptions propagate immediately.

**F-02 Retry parameters**

| Constant | Value |
|:---|:---|
| `_MAX_RETRIES` | `4` (5 total attempts) |
| `_INITIAL_DELAY` | `2.0` seconds |
| `_BACKOFF_FACTOR` | `2.0` |
| `_MAX_DELAY` | `60.0` seconds |

**F-03 `_call_with_retry(coro_factory, *, max_retries)` behaviour**

- `coro_factory` is a zero-argument callable returning a fresh coroutine (coroutines cannot be awaited twice).
- On each transient error, waits `delay + jitter` seconds where jitter is `delay * 0.2 * (2 * random.random() - 1)` (plus/minus 20%).
- Delay is doubled after each attempt, capped at `_MAX_DELAY`.
- After all retries exhausted, raises the last exception.
- Logs `WARNING` on each retry attempt, `ERROR` on final failure.

### Data Classes

**F-04 `Message` dataclass**

- `role: str` ("user" or "assistant")
- `content: Any` (str or list of content blocks)

**F-05 `LLMResponse` dataclass**

- `text: str = ""`
- `tool_calls: list[dict[str, Any]] = []` (factory)
- `stop_reason: str = ""`
- `raw: Any = None`

Each tool call dict has keys `"id"`, `"name"`, `"input"`.

### LLM Client

**F-06 Constructor `LLM.__init__(config: HarnessConfig)`**

- Resolves `base_url` from: `config.base_url` > `HARNESS_BASE_URL` env var > empty (Anthropic default).
- Resolves `api_key` from: `config.api_key` > `HARNESS_API_KEY` > `ANTHROPIC_AUTH_TOKEN` > `ANTHROPIC_API_KEY` > empty.
- Creates `anthropic.AsyncAnthropic(**kwargs)` with resolved values.
- Creates `self._api_semaphore` as `asyncio.Semaphore` with value clamped to `[1, 20]` from `config.max_concurrent_llm_calls`.

**F-07 `LLM.call()` method**

Signature: `async call(messages, *, system="", tools=None, max_tokens=None, timeout=300) -> LLMResponse`

- Builds kwargs dict with `model`, `max_tokens` (falls back to `config.max_tokens`), `messages`.
- Adds `system` kwarg only if non-empty.
- Adds `tools` kwarg only if provided.
- Acquires `self._api_semaphore` before the API call.
- Wraps the API call in `_call_with_retry` with `asyncio.wait_for(timeout=timeout)`.
- Parses response content blocks: `"text"` blocks are joined with `"\n"`, `"tool_use"` blocks become tool call dicts.
- Logs usage info: `input_tokens`, `output_tokens`, `cache_read_input_tokens` (if non-zero), `cache_creation_input_tokens` (if non-zero), latency.
- Warns when response text is shorter than `_SHORT_RESPONSE_CHARS` (50) and has no tool calls (possible truncation).

### Conversation History Pruning

**F-08 Character estimation**

`_estimate_conversation_chars(conversation)` sums the length of all text strings in message content, handling both string content and list content (text blocks, tool_use input, tool_result content blocks). Intentionally an undercount.

**F-09 Pruning thresholds**

| Constant | Value |
|:---|:---|
| `_CONV_PRUNE_THRESHOLD_CHARS` | `300_000` |
| `_CONV_PRUNE_TARGET_CHARS` | `200_000` |
| `_CONV_PRUNE_KEEP_RECENT_PAIRS` | `3` |

**F-10 Tool signal classification**

Three tiers of tools with different pruning thresholds:

| Tier | Tools | Stub threshold | Max signal lines | Max line chars |
|:---|:---|:---|:---|:---|
| High-signal | `test_runner`, `python_eval`, `lint_check` | 2,000 | 15 | 300 |
| Medium-signal | `bash`, `symbol_extractor`, `code_analysis`, `cross_reference`, `call_graph`, `data_flow`, `dependency_analyzer`, `diff_files`, `feature_search`, `project_map` | 1,500 | 12 | 250 |
| Low-signal | `grep_search`, `glob_search`, `todo_scan`, `list_directory`, `tree`, `tool_discovery`, `git_log`, `git_status`, `git_diff` | 200 | 5 | 120 |
| Default (all others) | | 500 | 8 | 200 |

**F-11 Content-aware bash override**

`_bash_is_test_output(text)` returns `True` when the text contains any of these case-insensitive patterns: `"passed"`, `"failed"`, `"::test_"`, `"===="`, `"----"`, `"short test summary"`, `"syntaxerror"`, `"traceback ("`, `"exit code:"`, `"ruff check"`, `" error["`, `"warning["`, `"import error"`, `".pyc"`. When `True`, bash outputs are treated as high-signal (threshold 2,000 instead of the medium-signal 1,500).

**F-12 `_prune_conversation_tool_outputs()` reactive pruning**

- Triggered when conversation exceeds `_CONV_PRUNE_THRESHOLD_CHARS`.
- Identifies user messages containing `tool_result` blocks.
- Protects the last `_CONV_PRUNE_KEEP_RECENT_PAIRS` tool-result message indices.
- Builds a `tool_id_to_name` mapping from assistant messages.
- For unprotected messages, replaces long tool-result text with compact stubs via `_make_compact_stub()`.
- Only replaces when stub is shorter than original.
- Safety invariants: system prompt is never at risk (passed separately as `system` kwarg); plain-text initial user messages are never pruned (string content is skipped); no messages are removed or reordered (preserves tool_use/tool_result pairing).
- Returns `(conversation, msgs_pruned, chars_removed)`.

### Compact Stub Generation

**F-13 `_make_compact_stub(tool_name, text, max_signal_lines=8, max_line_chars=200)`**

Stub structure:
1. Header: `"[{tool_name}: {n} chars, compacted]"`
2. Optional summary line for `test_runner` / `bash`: extracted by `_PYTEST_SUMMARY_RE` (`r"\d+\s+(?:passed|failed|error)"`) scanning from the last line backwards.
3. For list-output tools (`glob_search`, `list_directory`, `tree`, `git_log`): shows a non-blank-line count with a tool-specific label (files/entries/nodes/commits) instead of a preview.
4. For short-preview tools (`grep_search`, `git_status`, `git_diff`, `tool_discovery`, `todo_scan`, `feature_search`): 100-char preview.
5. For all other tools: 300-char preview (`_COMPACT_PREVIEW_CHARS`), truncated at a newline boundary if possible (last `\n` above position 20).
6. Signal lines: lines containing any of `_HIGH_SIGNAL_PATTERNS` (case-insensitive), deduplicated, truncated to `max_line_chars`, capped at `max_signal_lines`.

`_HIGH_SIGNAL_PATTERNS` keywords: `"passed"`, `"failed"`, `"error"`, `"warning"`, `"exception"`, `"traceback"`, `"assertionerror"`, `"typeerror"`, `"valueerror"`, `"nameerror"`, `"attributeerror"`, `"syntaxerror"`, `"importerror"`, `"modulenotfounderror"`, `"keyerror"`, `"indexerror"`, `"runtimeerror"`, `"filenotfounderror"`, `"score"`, `"verdict"`, `"assert "`, `"assert:"`, `"critical"`, `"fatal"`, plus Unicode symbols `"✓"` (U+2713), `"✗"` (U+2717), `"✘"` (U+2718).

### Proactive Compaction

**F-14 Proactive compaction constants**

| Constant | Value |
|:---|:---|
| `_COMPACT_MIN_TURNS` | `6` |
| `_COMPACT_KEEP_RECENT` | `3` |
| `_COMPACT_MIN_TEXT_LEN` | `500` |
| `_COMPACT_PREVIEW_CHARS` | `300` |
| `_SHORT_PREVIEW_CHARS` | `100` |

**F-15 `_compact_old_tool_results(conversation)` behaviour**

- Runs every turn after turn index >= `_COMPACT_MIN_TURNS`.
- Finds user messages with `tool_result` blocks; protects the last `_COMPACT_KEEP_RECENT`.
- Extracts tool names from preceding assistant messages.
- Applies tiered thresholds (same as F-10) to decide which blocks to compact.
- Replaces text with `_make_compact_stub()` only when the stub is shorter.
- Returns the number of blocks compacted.

### File-Read Cache

**F-16 `_CachedToolRegistry` wrapper**

- Wraps a `ToolRegistry` instance.
- Maintains `_cache: dict[tuple[str, int, int], ToolResult]` keyed by `(path, offset, limit)`.
- `_dirty_paths: set[str]` tracks paths invalidated by writes.
- `_read_seen: dict[str, set[tuple[int, int]]]` tracks already-fetched path/offset/limit combinations.

**F-17 Write tool invalidation**

Write tools (`write_file`, `edit_file`, `file_patch`, `find_replace`, `batch_edit`, `batch_write`) add affected paths to `_dirty_paths` and remove them from `_read_seen` **before** execution, so a failed write still clears the stale entry.

**F-18 `read_file` caching**

- Cache key: `(path_str, offset, limit)` where offset defaults to `1`, limit to `2000`.
- Returns cached result if key exists and path is not dirty.
- On successful read, stores result in cache and clears the dirty flag.

**F-19 `batch_read` deduplication**

- Compares requested paths against `_read_seen` for the same `(offset, limit)`.
- All paths already seen: returns a hint message (`"[batch_read cache] All N path(s) were already read..."`) with `metadata={"cache_hit_all": True}`.
- Partial overlap: fetches only uncached paths, prepends a note listing skipped paths.
- No overlap: full fetch, records all paths in `_read_seen`.

### Tool-Use Agent Loop

**F-20 `call_with_tools()` method**

Signature: `async call_with_tools(messages, registry, *, system="", max_turns=30) -> tuple[str, list[dict[str, Any]]]`

Returns `(final_text, execution_log)` where each execution log entry is `{"tool": name, "input": {...}, "output": "...", "duration_ms": int, "is_error": bool}`.

**F-21 Per-loop scratchpad**

- `scratchpad_notes: list[str]` accumulates notes within a single loop invocation.
- Notes are injected at the top of the system prompt each turn as a `"## Your Exploration Notes"` block.
- Note text is capped at 2,000 characters, truncated with `"... [truncated]"`.
- Max notes capped at `_SCRATCHPAD_MAX_NOTES` (30); oldest evicted.

**F-22 `context_budget` inline tool**

Returns live loop statistics:
- Turn progress: `"{turn+1}/{max_turns} ({percentage}%)"`.
- Input/output tokens used.
- Total tool calls count.
- Scratchpad notes count.

**F-23 Tool call classification (three lanes)**

1. **Scratchpad**: handled inline, no I/O.
2. **`context_budget`**: handled inline, no I/O.
3. **Read-only tools** (`_READ_ONLY_TOOL_NAMES` frozenset): executed in parallel via `asyncio.gather`. Includes: `batch_read`, `read_file`, `tree`, `list_directory`, `diff_files`, `grep_search`, `glob_search`, `feature_search`, `todo_scan`, `git_status`, `git_diff`, `git_log`, `git_search`, `code_analysis`, `symbol_extractor`, `cross_reference`, `data_flow`, `call_graph`, `dependency_analyzer`, `json_transform`, `tool_discovery`, `scratchpad`, `file_info`, `lint_check`, `project_map`, `context_budget`.
4. **Mutating tools**: executed sequentially (all others).

**F-24 Result ordering and API compliance**

- Tool results are emitted in the same order as the original tool_use blocks.
- Each result block has `type: "tool_result"`, `tool_use_id`, `content`, and `is_error`.
- Empty assistant content (no text and no tool calls) is replaced with `{"type": "text", "text": "(no output)"}` and logged at WARNING.

**F-25 Execution log output capping**

Each tool call output stored in `execution_log` is capped at `_EXEC_LOG_MAX_OUTPUT_CHARS` (4,000). Content exceeding the cap is split: first 2,000 chars + truncation marker + last 2,000 chars.

**F-26 Token budget warning**

When cumulative `total_out_tokens` exceeds `config.max_tokens * 4`, a WARNING is logged indicating a potentially unproductive loop.

**F-27 Turn stall warning**

When a single tool turn takes longer than `_TURN_STALL_WARN_SECS` (90.0 seconds), a WARNING is logged.

**F-28 Max turns exhaustion**

When the loop reaches `max_turns` without the model stopping:
- Logs a WARNING with turn count, elapsed time, tool call count, and token totals.
- Returns a structured partial summary with `STATUS: PARTIAL` so the evaluator can detect incomplete execution. Includes `COMPLETED: unknown`, `SKIPPED: unknown`, and an `ISSUES` field naming the root cause.

**F-29 Pruning integration**

- After every tool-result batch is appended, estimates conversation size.
- If > `_CONV_PRUNE_THRESHOLD_CHARS`: runs `_prune_conversation_tool_outputs()`.
- After turn index >= `_COMPACT_MIN_TURNS`: runs `_compact_old_tool_results()`.

**F-30 `_summarise_tool_input()` helper**

Produces a short log-line summary of tool call parameters:
- File-path tools: shows `path`, `source`, or `destination` (truncated to 80 chars).
- Bash: shows command with `"$ "` prefix (truncated to 80 chars).
- Search tools: shows `pattern=...`.
- Fallback: first key=value pair (value truncated to 60 chars).

---

## Implementation Approach

- `LLM` class wraps `anthropic.AsyncAnthropic`.
- Retry logic is a standalone async function `_call_with_retry`.
- Pruning/compaction are pure functions operating on the conversation list (list of dicts).
- `_CachedToolRegistry` is a transparent wrapper (same interface as `ToolRegistry`).
- All module-level constants are private (`_`-prefixed).

---

## Expected Effects

- Transient API errors (429, 529, 500, connection drops) are retried transparently.
- Context-window overflow is prevented by two complementary mechanisms: reactive pruning (triggered at 300K chars) and proactive compaction (every turn after turn 6).
- Read-only tools execute in parallel, reducing wall-clock time for exploration-heavy turns.
- File-read caching prevents duplicate multi-KB tool results from inflating the conversation.
- The scratchpad survives conversation pruning (injected into system prompt, not message content).
- Execution log provides a complete audit trail with per-call timing and error status.

---

## Acceptance Criteria

- Transient errors are retried up to 4 times with exponential backoff; non-transient errors propagate immediately.
- Conversation pruning preserves the 3 most recent tool-result pairs and never removes/reorders messages.
- Compact stubs include a preview, signal lines, and the original character count.
- Read-only tools are gathered in parallel; mutating tools run sequentially.
- `call_with_tools` returns `STATUS: PARTIAL` when max turns are exhausted.
- Scratchpad notes appear in the system prompt on every subsequent turn.
