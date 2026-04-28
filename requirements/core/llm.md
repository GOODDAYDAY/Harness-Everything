# LLM Integration

User stories for API resilience, conversation management, context optimization, and the tool execution loop.

---

# API Resilience

## US-01: As a cycle, I need transient API errors retried with exponential backoff so that momentary provider overloads or network glitches do not abort the entire run

API providers regularly return rate-limit, overload, and transient server errors that resolve on their own within seconds. The system must automatically retry these errors with increasing delays, while immediately propagating permanent errors (bad credentials, malformed requests) without wasting time on retries.

### Acceptance Criteria
- Given a rate-limit, overload, server error, connection failure, or timeout response from the provider, when the API call fails, then it is retried up to a configured maximum number of attempts
- Given successive transient failures, when each retry is scheduled, then the delay between retries doubles (with a jitter factor to prevent synchronized retry storms across concurrent calls) up to a maximum delay cap
- Given a non-transient error (authentication failure, bad request), when the API call fails, then the error is propagated immediately without any retry
- Given all retry attempts exhausted, when the final attempt still fails, then the last error is propagated with a log entry stating the total number of attempts

## US-02: As a cycle, I need each API call governed by a per-call timeout so that a hung provider connection does not block the run indefinitely

Individual API calls can stall when the provider is severely overloaded or the network connection is half-open. A per-call wall-clock timeout ensures the system moves on (via retry or error propagation) rather than waiting forever.

### Acceptance Criteria
- Given an API call that does not complete within the configured timeout, when the timeout fires, then the call is treated as a transient error and eligible for retry
- Given a normally completing API call, when the call finishes before the timeout, then the timeout has no effect

## US-03: As an operator, I need concurrent API calls limited by a process-wide cap so that parallel execution paths do not overwhelm the provider's rate limit

Multiple execution paths (parallel evaluators, concurrent tool calls, planning tasks) can issue API calls simultaneously. Without a concurrency cap, the provider will start returning rate-limit errors for every call, degrading throughput rather than improving it.

### Acceptance Criteria
- Given multiple concurrent tasks issuing API calls, when the concurrency cap is reached, then additional calls queue and wait for a slot rather than firing immediately
- Given a configured concurrency limit below one, when the system starts, then the limit is clamped to one (preventing deadlock)
- Given a configured concurrency limit above the maximum safe value, when the system starts, then the limit is clamped to the maximum

## US-04: As an operator, I need token usage and latency logged for every API call so that I can monitor cost and performance without consulting the provider dashboard

Each API call should produce a structured log entry showing input tokens, output tokens, cache statistics (when available), model name, stop reason, and wall-clock latency. This is the primary observability signal for cost management and performance tuning.

### Acceptance Criteria
- Given an API call that completes successfully, when the response is received, then a log entry is emitted containing token counts, stop reason, and latency
- Given a response from a provider that supports prompt caching, when cache read or cache creation token counts are non-zero, then they are included in the log entry
- Given a response from a provider that does not report cache statistics, when the log entry is formatted, then cache fields are omitted rather than showing zeros

## US-05: As a cycle, I need suspiciously short responses without tool calls flagged with a warning so that truncated or failed generations are visible in the logs

A very short text response with no tool calls almost always indicates a problem: the model was truncated, hit a stop sequence prematurely, or received a context that left nothing meaningful to say. These near-empty responses look valid but carry no useful content, leading to downstream scoring failures if not caught early.

### Acceptance Criteria
- Given an API response whose text is shorter than the minimum plausible length and contains no tool calls, when the response is processed, then a warning is logged identifying the response length, stop reason, and model
- Given an API response with tool calls (regardless of text length), when the response is processed, then no short-response warning is emitted

---

# Conversation Management

## US-06: As a cycle, I need the conversation history pruned when it grows too large so that the provider's context window is not exceeded, which would cause a cryptic request error

Long tool loops accumulate large tool-result payloads that are re-sent with every API call. Without pruning, the conversation eventually overflows the model's context window, producing an opaque "prompt too long" error that is easily misdiagnosed as a schema problem.

### Acceptance Criteria
- Given a conversation whose estimated character size exceeds the pruning threshold, when pruning is triggered, then the text content of older tool-result blocks is shortened until the total size drops to the target level
- Given a conversation undergoing pruning, when tool-result text is shortened, then the 3 most recent assistant+user pairs are always preserved verbatim so the model retains its freshest context
- Given a conversation undergoing pruning, when tool-result blocks are shortened, then no messages are removed or reordered (structural integrity required by the API is maintained)
- Given the system prompt, when pruning runs, then the system prompt is never at risk because it is passed separately from the conversation messages

## US-07: As a cycle, I need old tool results proactively compacted into signal-preserving summaries so that context is reclaimed incrementally rather than only when the conversation hits a critical size

Instead of waiting for the conversation to reach a dangerous size, older tool results should be replaced with compact stubs after the conversation has accumulated enough turns. This keeps context growth linear rather than unbounded, reducing both cost and latency.

### Acceptance Criteria
- Given a conversation that has accumulated at least the minimum number of turns for compaction, when a new turn completes, then old tool-result blocks (beyond the protected recent window) are replaced with compact summaries
- Given a tool result being compacted, when the summary is generated, then it preserves the original size, a content preview, and any high-signal lines (errors, test verdicts, scores, stack traces)
- Given a tool result that is already shorter than the compaction threshold for its tool category, when compaction runs, then it is left unchanged

## US-08: As a cycle, I need tool results compacted with category-aware thresholds so that high-signal outputs (test results, lint errors) are preserved longer than low-signal outputs (file listings, directory searches)

Not all tool outputs are equally valuable. Test runner output containing pass/fail verdicts is critical for planning the next step, while a directory listing from five turns ago is almost never re-read. Compaction thresholds should reflect this difference.

### Acceptance Criteria
- Given a high-signal tool output (test runner, evaluation, lint checker), when compaction is considered, then a generous character threshold is used before compaction triggers
- Given a medium-signal tool output (shell commands, code analysis), when compaction is considered, then a moderate threshold is used
- Given a low-signal tool output (file search, directory listing, log display), when compaction is considered, then an aggressive threshold is used
- Given a shell command output that contains test or compilation indicators (pass/fail lines, traceback headers, exit codes), when compaction is considered, then it is promoted to high-signal treatment regardless of the tool category

---

# Context Optimization

## US-09: As a cycle, I need file-read results cached within a single tool loop so that re-reading the same file does not inject duplicate multi-kilobyte payloads into the conversation

Tool loops commonly read the same file multiple times (read, edit, re-read to verify). Caching read results within one loop invocation avoids injecting identical content into the conversation repeatedly, directly reducing input-token growth per turn.

### Acceptance Criteria
- Given a file that has been read once in the current loop, when the same file is read again with the same parameters, then the cached result is returned without a disk read
- Given a file that has been written to during the current loop, when it is subsequently read, then the cache entry for that file is invalidated and a fresh read is performed
- Given a batch read request where some paths have already been read this loop, when the batch is processed, then only the uncached paths are fetched from disk and a note is prepended listing the skipped paths
- Given a batch read request where all paths have already been read this loop, when the batch is processed, then a hint is returned telling the model to review earlier results rather than re-reading

## US-10: As a cycle, I need a persistent scratchpad within the tool loop so that important findings survive conversation pruning and remain visible to the model on every subsequent turn

Conversation pruning can compact or remove the tool output where the model first learned a key fact. A scratchpad allows the model to explicitly save findings, which are re-injected into the system prompt on every turn, surviving any amount of conversation pruning.

### Acceptance Criteria
- Given a note saved to the scratchpad, when the next API call is made, then the note appears in the system prompt
- Given multiple notes saved across different turns, when the system prompt is built, then all notes are present in the order they were saved
- Given more notes than the configured maximum, when a new note is saved, then the oldest notes are evicted to stay within the cap
- Given a note that exceeds the maximum note length, when it is saved, then it is truncated with a visible marker

## US-11: As a cycle, I need a budget introspection tool so that the model can see how many turns and tokens it has consumed and pace its work accordingly

Without visibility into its own resource consumption, the model cannot make informed decisions about when to wrap up versus when to continue exploring. A budget tool provides live statistics (current turn, total tokens, tool calls, scratchpad size) so the model can self-regulate.

### Acceptance Criteria
- Given a tool loop in progress, when the budget tool is called, then it returns the current turn number and maximum, cumulative input and output token counts, total tool calls, and scratchpad note count
- Given the budget tool output, when the model reads it, then all values reflect the state as of the current turn (not stale data from an earlier turn)

---

# Tool Loop

## US-12: As a cycle, I need a tool execution loop that alternates between model reasoning and tool execution until the model signals completion so that multi-step tasks can be executed autonomously

The core execution pattern is a loop: call the model, execute any tool calls it requests, feed results back, repeat until the model produces a final text response with no further tool calls. This loop is the fundamental mechanism for autonomous task execution.

### Acceptance Criteria
- Given a model response that contains tool calls, when the tools are executed, then their results are appended to the conversation and another model call is made
- Given a model response that contains only text (no tool calls), when the response is processed, then the loop terminates and the text is returned as the final output along with the full execution log
- Given a model response that contains both text and tool calls, when the response is processed, then the tool calls are executed (the text is retained in the conversation but the loop continues)

## US-13: As a cycle, I need a configurable turn budget for the tool loop so that a confused or looping model cannot run indefinitely and consume unbounded tokens

Without a turn cap, a model that gets stuck in a read-edit-read cycle can run for hundreds of turns, consuming massive token budgets. A configurable maximum ensures the loop terminates and reports a partial status so the planner can adjust.

### Acceptance Criteria
- Given a tool loop that reaches the maximum turn count, when the limit is hit, then the loop terminates and returns a structured partial-completion report indicating the loop was cut off
- Given a partial-completion report, when it is returned, then it explicitly states the loop was cut off, how many tool calls were made, and that the plan was not fully executed, so downstream evaluation does not mistakenly treat an incomplete run as successful

## US-14: As a cycle, I need read-only tool calls within a single turn executed in parallel so that turns with multiple independent reads complete faster without sacrificing safety for write operations

A single model turn often requests several file reads, searches, or analyses simultaneously. These are safe to parallelize because they have no side effects. Write operations (file edits, shell commands) must remain sequential because their order matters and they share filesystem state.

### Acceptance Criteria
- Given a model turn containing multiple read-only tool calls, when those tools are executed, then they run concurrently
- Given a model turn containing write/mutating tool calls, when those tools are executed, then they run sequentially in the order requested
- Given a model turn containing a mix of read-only and mutating calls, when the turn is processed, then read-only calls run in parallel first, followed by mutating calls in sequence
- Given a tool call that fails with an exception during parallel execution, when the exception occurs, then it is caught and converted to an error result rather than crashing the entire batch

## US-15: As an operator, I need a warning when the tool loop's cumulative output token spend exceeds a safety threshold so that I can identify and abort unproductive loops before they consume the full budget

A tool loop that has spent many times its per-call token budget on output tokens is almost certainly spinning without making progress. An early warning (before the turn cap is hit) lets operators intervene.

### Acceptance Criteria
- Given a tool loop whose cumulative output tokens exceed a multiple of the configured per-call token limit, when the threshold is crossed, then a warning is logged identifying the turn number, cumulative spend, and threshold
- Given a tool loop whose cumulative output tokens remain below the threshold, when each turn completes, then no spend warning is emitted

## US-16: As an operator, I need a warning when a single tool turn takes unusually long so that I can spot model overload or degraded performance before the loop times out

A tool turn that takes much longer than normal suggests the provider is overloaded or the context window has grown very large. Logging this as a warning lets operators see performance degradation in real time.

### Acceptance Criteria
- Given a tool turn that takes longer than the stall threshold, when the turn completes, then a warning is logged identifying the turn number, elapsed time, and the threshold
- Given a tool turn that completes within normal time, when the turn completes, then no stall warning is emitted

## US-17: As a cycle, I need the tool loop to handle a model response with no text and no tool calls gracefully so that the conversation remains valid and the loop can continue or exit cleanly

In rare edge cases, the model may produce an empty response (no text, no tool calls). The API requires non-empty message content, so the loop must inject a synthetic placeholder to maintain conversational integrity.

### Acceptance Criteria
- Given a model response with neither text nor tool calls, when the response is processed, then a synthetic placeholder text block is injected into the assistant message
- Given a synthetic placeholder injection, when it occurs, then a warning is logged so operators can see this degenerate condition

## US-18: As an operator, I need a structured summary logged when the tool loop completes so that I can see the total turns, tool calls, token usage, and elapsed time at a glance

When the tool loop exits (either by model completion or turn cap), a summary log entry should capture the full loop's resource consumption for cost tracking and performance analysis.

### Acceptance Criteria
- Given a tool loop that completes normally (model signals done), when the loop exits, then an informational log entry is emitted with total turns, tool call count, cumulative input and output tokens, and elapsed time
- Given a tool loop that hits the turn cap, when the loop exits, then a warning-level log entry is emitted with the same statistics plus the turn cap value

## US-19: As an operator, I need per-tool-call output in the execution log truncated to a reasonable size so that a single large file read does not cause unbounded memory growth in the debug log

The execution log is a debugging aid, not an archive. When a tool produces very large output (e.g., reading a multi-thousand-line file), the log entry should keep the beginning and end of the output with a visible truncation marker in the middle.

### Acceptance Criteria
- Given a tool call whose output exceeds the per-entry size cap, when the execution log entry is written, then the output is truncated to the first and last halves of the cap with a marker showing how many characters were removed
- Given a tool call whose output is within the cap, when the execution log entry is written, then the full output is preserved
