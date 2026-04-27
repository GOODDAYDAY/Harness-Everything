# LLM Client Requirements

This document defines the requirements for the LLM client: the component responsible for making API calls to the language model, managing the conversation lifecycle, and executing multi-step tool loops.

The LLM client answers one question: **how does the harness communicate with the model reliably, efficiently, and without losing context?**

---

## 1. API Resilience

### R-LLM-01: Automatic retry with exponential backoff on transient failures

When the LLM API returns a transient error (rate limit, server overload, connection drop, timeout), the client must automatically retry with increasing delays between attempts, up to a maximum number of retries.

**Why:** Transient API errors are normal in production -- a 429 rate-limit or 529 overload may resolve in seconds. Without automatic retry, every transient error would abort the entire agent run, wasting all prior work. Manual restart is impractical for unattended runs.

**Acceptance criteria:**

- Given a rate-limit (429) response, when the client retries, then it waits progressively longer between attempts (not a fixed interval).
- Given a server overload (529) that resolves after the second retry, when the client retries, then the call succeeds on the third attempt and returns a normal response.
- Given a transient error that persists through all retry attempts, when retries are exhausted, then the original error is raised (not swallowed).
- Given a non-transient error (authentication failure, bad request), when it is received, then the client raises immediately without retry.

### R-LLM-02: Jittered retry timing to prevent thundering herd

Retry delays must include random jitter so that multiple concurrent calls that hit the same rate-limit window do not all retry at the same instant.

**Why:** Without jitter, parallel calls that all fail at time T will all retry at time T+delay, creating a synchronized burst that triggers another rate-limit. Jitter spreads retries across a time window, giving the API breathing room.

**Acceptance criteria:**

- Given two concurrent calls that both fail at the same time, when they retry, then their actual wait times differ (not identical).
- Given any retry, the actual wait time is within a bounded percentage of the nominal delay (not unbounded randomness).

### R-LLM-03: Per-call timeout protection

Every individual API call must be bounded by a configurable timeout. A call that exceeds the timeout must be cancelled and treated as a retryable error.

**Why:** A model that is silently hung (no response, no error) would block the agent indefinitely. The timeout ensures the system detects and recovers from silent failures.

**Acceptance criteria:**

- Given an API call that does not respond within the timeout, when the timeout expires, then the call is cancelled and a retryable timeout error is raised.
- Given a normal API call that completes within the timeout, when it returns, then the timeout has no effect on the response.

### R-LLM-04: Concurrency limiting across the process

The total number of in-flight API calls must be capped by a configurable semaphore, regardless of how many concurrent tasks are running.

**Why:** The agent architecture can spawn multiple concurrent LLM calls (parallel evaluators, debate rounds, planning). Without a process-wide cap, the provider's rate limit is hit immediately, causing cascading 429 errors and wasted retries.

**Acceptance criteria:**

- Given a concurrency cap of 4 and 8 tasks attempting simultaneous API calls, when the calls are dispatched, then at most 4 are in-flight at any time; the rest wait.
- Given a misconfigured concurrency cap of 0, when the client initializes, then the cap is clamped to 1 (not deadlocked).
- Given a misconfigured concurrency cap of 100, when the client initializes, then the cap is clamped to a safe maximum.

---

## 2. Conversation Management

### R-LLM-05: Structural integrity of conversation messages

The conversation passed to the API must always satisfy the provider's structural requirements: no empty content arrays, and every tool-use block must have a matching tool-result block with the same identifier.

**Why:** The Anthropic API returns HTTP 400 on structurally invalid messages. These errors look identical to schema errors, causing operators to debug tool definitions when the real problem is a missing tool-result block. Structural integrity must be maintained by construction, not by hope.

**Acceptance criteria:**

- Given a model response that produces neither text nor tool calls, when the assistant message is appended, then it contains a synthetic placeholder so the content array is never empty.
- Given a tool-use block with a specific ID, when the tool result is appended, then it carries the same ID.
- Given multiple tool calls in a single turn, when results are appended, then they appear in the same order as the original tool-use blocks.

### R-LLM-06: Token usage logging

Every API call must log input and output token counts when the provider reports them. Cache hit/miss information must be included when available.

**Why:** Token usage is the primary cost signal for agent runs. Without per-call logging, operators cannot identify which phases or tools are consuming the most tokens, and cannot tune budgets effectively.

**Acceptance criteria:**

- Given an API call that returns usage information, when the call completes, then a log entry is emitted containing input tokens, output tokens, and latency.
- Given an API call with prompt caching, when cache read/creation counts are present, then they appear in the log entry.
- Given an API call where usage information is absent (older SDK), when the call completes, then a log entry is still emitted with the available information (no crash).

### R-LLM-07: Short/empty response detection

When the model returns a very short text response with no tool calls, the system must log a warning. Such responses almost always indicate truncation, context overflow, or a failed generation -- not a valid empty answer.

**Why:** A near-empty response looks like a valid result to downstream scoring, which silently returns zero. Without the warning, operators don't realize the model failed until they inspect artifacts hours later.

**Acceptance criteria:**

- Given a response shorter than 50 characters with no tool calls, when it is received, then a warning is logged indicating possible truncation, including the stop reason and model name.
- Given a response shorter than 50 characters WITH tool calls, when it is received, then no warning is logged (tool calls are the primary output).

---

## 3. Context Window Optimization

### R-LLM-08: Proactive compaction of old tool results

After each tool-result message is appended, older tool-result messages must be replaced with compact, signal-preserving summaries. The most recent tool-result messages are preserved verbatim; older ones are replaced with signal-preserving stubs. Compaction must happen continuously, not just when the conversation is about to overflow.

**Why:** Each API call re-sends the entire conversation. Without compaction, a 30-turn loop with file reads accumulates 200K+ characters, causing input token costs to grow quadratically with turn count. Waiting until overflow to prune causes a sudden quality cliff as the model loses context mid-run.

**Acceptance criteria:**

- Given a conversation with many tool-result messages, when compaction runs, then the most recent tool-result messages are preserved verbatim and older tool-result messages are replaced with stubs.
- Given a compacted tool-result message, when the model reads it, then it can see what tool was called, how many characters the original output was, a preview of the content, and any high-signal lines (errors, test results, scores).

### R-LLM-09: Signal-aware compaction thresholds

Different tool types must be compacted at different thresholds based on how much future value their output carries. The system uses three tiers plus sub-categories:

- **Low-signal** (approximately 200-char preview): Search and listing tools whose output has been consumed (e.g., grep, glob, directory listing). Within this tier, *list-output tools* (those returning structured lists such as glob results, directory listings, tree output, git log) receive no preview at all; *short-preview tools* (grep, git status, git diff) receive a brief preview (approximately 100 characters).
- **Medium-signal** (approximately 1500-char preview): Tools that produce moderately reusable output such as shell commands, symbol extraction, and code analysis.
- **High-signal** (approximately 2000-char preview): Test runners, linters, evaluators, and other tools whose output directly drives the next action.

**Why:** A `grep_search` result listing 50 file paths has low future value (the agent already decided which files to read). A `test_runner` result showing 3 failures has high future value (the agent needs those failure messages to fix the code). Uniform compaction either loses critical test output or retains useless file listings. The medium tier preserves enough context from shell and analysis output without consuming as much space as high-signal tools.

**Acceptance criteria:**

- Given output from a list-output tool (glob, directory listing, tree, git log), when compaction runs, then it is compacted with no content preview.
- Given output from a short-preview tool (grep, git status, git diff), when compaction runs, then it is compacted with a brief preview (approximately 100 characters).
- Given output from a medium-signal tool (shell commands, symbol extraction, code analysis), when it exceeds a medium threshold (approximately 1500 characters), then it is compacted with a moderately sized preview.
- Given output from a test runner or linter, when it exceeds a high threshold (approximately 2000 characters), then it is compacted with more preserved signal lines.
- Given bash output that contains test/compile/lint indicators (pytest summaries, tracebacks, syntax errors), when compaction runs, then it is promoted to high-signal treatment.

### R-LLM-10: Emergency pruning on context window overflow

When the total conversation size approaches the model's context window limit, the system must perform aggressive pruning of older tool results to bring the total below a safe target.

**Why:** If the conversation exceeds the context window, the API returns HTTP 400 "prompt too long." This error message is indistinguishable from a schema error, and the agent has no way to recover. Emergency pruning prevents the overflow from ever occurring.

**Acceptance criteria:**

- Given a conversation that exceeds 300,000 characters, when pruning is triggered, then old tool results are truncated until the total drops to approximately 200,000 characters.
- Given a conversation being pruned, when the system prompt and initial user message are present, then they are never modified (the system prompt is passed separately to the API and is physically unreachable by pruning).
- Given pruning in progress, when tool-result blocks are truncated, then no messages are removed or reordered (structural integrity is preserved).

### R-LLM-11: Scratchpad notes for surviving compaction

The tool loop must support a scratchpad mechanism where the agent can save important findings that persist across compaction. Scratchpad notes must be re-injected into the system prompt on every subsequent turn.

**Why:** Compaction necessarily discards information. If the agent discovers something critical in turn 3 (e.g., "the database schema uses snake_case"), that finding is lost when turn 3 is compacted. The scratchpad lets the agent explicitly preserve key insights.

**Acceptance criteria:**

- Given a scratchpad note saved in turn 5, when turn 12 is processed (and turn 5 has been compacted), then the note is still visible to the model in the system prompt.
- Given more scratchpad notes than the maximum capacity, when a new note is saved, then the oldest notes are evicted and the newest are retained.
- Given an empty scratchpad note, when it is submitted, then the tool returns an error (empty notes waste system prompt space).

---

## 4. Tool Execution Loop

### R-LLM-12: Bounded tool loop with partial-completion reporting

The tool-use loop must enforce a configurable maximum number of turns. When the limit is reached, the loop must exit with a structured status report indicating that execution was incomplete.

**Why:** Without a turn limit, a confused or looping model could make hundreds of API calls, consuming unbounded tokens. The partial-completion report is critical because downstream evaluators need to distinguish "finished successfully" from "cut off mid-flight" -- treating a partial run as complete produces false-positive evaluations.

**Acceptance criteria:**

- Given a max-turns limit of 30 and a model that keeps requesting tools, when turn 30 is reached, then the loop exits.
- Given a loop that hits the turn limit, when the result is returned, then it includes a structured status indicating `PARTIAL`, the number of tool calls made, and the elapsed time.
- Given a loop where the model stops requesting tools before the limit, when the last text response is received, then the loop exits immediately with the model's final text.

### R-LLM-13: Parallel execution of read-only tools

When the model requests multiple tool calls in a single turn, read-only tools (file reads, searches, git queries, static analysis) must be executed in parallel. Mutating tools (file writes, bash commands) must be executed sequentially.

**Why:** A typical turn requests 3-5 file reads. Executing them serially adds unnecessary latency (each read is I/O-bound, not CPU-bound). But file writes and shell commands must be serial because they have side effects that depend on execution order.

**Acceptance criteria:**

- Given a turn with 4 read-only tool calls, when they are executed, then all 4 run concurrently (total time is approximately the duration of the slowest, not the sum).
- Given a turn with 2 file writes, when they are executed, then they run in the order the model specified.
- Given a turn with a mix of reads and writes, when they are executed, then all reads run in parallel first, then writes run sequentially (or reads and writes are correctly interleaved by dependency).

### R-LLM-14: File-read deduplication within a tool loop

Within a single tool loop invocation, repeated reads of the same file (same path, offset, and limit) must return cached results without re-executing. File writes must invalidate the cache for the written path.

**Why:** Models frequently re-read files they already have in context (e.g., reading a file, editing it, then re-reading the original to verify). Each redundant read injects another multi-KB tool result into the conversation, accelerating context window exhaustion.

**Acceptance criteria:**

- Given a read of file X, followed by another read of file X with the same parameters, when the second read executes, then it returns the cached result (no disk I/O).
- Given a read of file X, followed by a write to file X, followed by another read of file X, when the third read executes, then it reads from disk (cache was invalidated by the write).
- Given a batch read of files [A, B, C] where A and B were already read individually, when the batch executes, then only C is fetched from disk.

### R-LLM-15: Token spend monitoring and stall detection

The tool loop must track cumulative token usage and per-turn latency, and warn when either exceeds a threshold.

**Why:** A runaway loop that makes 30 calls without progress is expensive but hard to detect from the outside. Token spend warnings let operators set up alerts, and stall detection flags turns where the model took abnormally long (indicating overload or context bloat).

**Acceptance criteria:**

- Given cumulative output tokens exceeding 4x the configured max_tokens, when the threshold is crossed, then a warning is logged indicating the loop may be unproductive.
- Given a single turn that takes more than 90 seconds, when it completes, then a warning is logged indicating possible model overload or context bloat.

### R-LLM-16: Execution log for observability

Every tool call must be recorded in an execution log with the tool name, input parameters, output (or error), duration, and error status. Output in the log must be size-capped to prevent memory exhaustion.

**Why:** The execution log is the primary debugging artifact. When an agent run produces wrong output, operators trace through the log to find which tool call went wrong. Without size capping, a single large file-read result can bloat the log to hundreds of MB.

**Acceptance criteria:**

- Given a tool call that succeeds, when the loop completes, then the execution log contains an entry with the tool name, input, output, and duration.
- Given a tool call that fails, when the loop completes, then the execution log entry includes the error and is marked as an error.
- Given a tool call with output exceeding 4000 characters, when it is logged, then the output is truncated symmetrically (head and tail preserved, middle replaced with a truncation marker).

### R-LLM-17: Built-in context budget introspection tool

The tool loop must provide a built-in `context_budget` tool that returns live loop statistics: turn count, tokens used, tool calls made, and scratchpad note count. The agent can call this tool at any point during the loop to query its own resource consumption.

**Why:** Without visibility into its own resource usage, the agent cannot make informed decisions about when to stop exploring and start producing output. The context budget tool lets the agent self-regulate -- for example, noticing it has used 80% of its turn budget and switching from exploration to synthesis.

**Acceptance criteria:**

- Given a tool loop in progress, when the agent calls the `context_budget` tool, then it receives a response containing the current turn count, cumulative token usage, total tool calls made, and the number of scratchpad notes saved.
- Given the `context_budget` tool, when it is called, then it returns current values (not stale or cached data from a previous turn).
