# Tools Domain

The tools domain provides the agent's hands: every action the LLM takes on a codebase -- reading a file, searching for a symbol, running tests, editing code -- flows through a tool. The domain's job is to make the agent capable while preventing it from causing damage.

## Scope

This domain covers four concern areas:

| Concern | Document | Core question |
|---------|----------|---------------|
| File operations | [file-operations.md](file-operations.md) | How does the agent read, write, edit, move, copy, and delete files safely and efficiently? |
| Search and analysis | [search-and-analysis.md](search-and-analysis.md) | What questions can the agent answer about a codebase without executing code? |
| Execution and system | [execution-and-system.md](execution-and-system.md) | How does the agent run commands, tests, network requests, and git operations under safety constraints? |

The tool registry and base framework are cross-cutting concerns that affect all three areas. They are described inline below rather than in a separate document, because they exist to serve the tools above, not as an independent capability.

## Key actors

- **Agent** -- the LLM instance that selects and invokes tools via structured tool-use calls.
- **Registry** -- the dispatch layer that receives a tool name + parameters from the agent, validates them, routes to the correct tool implementation, and returns a uniform result.
- **Tool** -- a single capability (read a file, run grep, etc.) with a declared schema, security tags, and an async execute method.
- **Framework** -- the surrounding runtime (config, security module, hooks) that tools depend on but do not control.

## Tool framework -- inline requirements

### R-TOOL-01: Uniform result contract

Every tool execution must return a single `ToolResult` containing output text, error text, an error flag, elapsed time, and optional metadata. The agent never receives raw exceptions -- all failures are translated into error results with actionable messages.

**Why:** The LLM consumes tool results as text. If different tools returned different shapes (some raise, some return dicts, some return strings), the agent would need per-tool parsing logic. A uniform contract means the agent can treat every tool result identically.

**Acceptance criteria:**
- A tool that encounters an I/O error returns `is_error=True` with a message describing the problem, never raises to the caller.
- A tool that succeeds returns `is_error=False` with output text the agent can read.
- Elapsed time is recorded on every result, success or failure.

### R-TOOL-02: Schema-driven dispatch

Every tool must declare a JSON Schema describing its inputs. The registry uses this schema for two purposes: (1) exporting tool definitions to the LLM API so the model knows what tools exist, and (2) validating parameters before dispatch so malformed calls fail fast with clear error messages.

**Why:** Without schema validation, a missing required parameter surfaces as a cryptic Python `TypeError` deep in the tool's execute method. The agent would waste a tool turn retrying with the same broken arguments. Schema validation catches this before execution and gives the LLM a precise signal ("missing required parameter `path`") that it can correct on the next call.

**Acceptance criteria:**
- A call with an unknown parameter name is rejected before the tool executes.
- A call missing a required parameter is rejected with a message listing the required parameters.
- The error category is distinguishable from permission errors and runtime errors, so the agent knows to fix its parameters rather than change the path.

### R-TOOL-03: Parameter alias normalization

The registry must silently correct common LLM parameter-name mistakes (e.g., `file_path` -> `path`, `old_string` -> `old_str`) before dispatch. Correction only applies when the alias target exists in the tool's schema and the correct name is not already present.

**Why:** LLMs frequently hallucinate parameter names that are close but not exact. Without alias normalization, every such mistake burns a full tool turn on a TypeError-retry cycle. Normalizing the most common mistakes lets the agent's first attempt succeed.

**Acceptance criteria:**
- A call with `file_path="foo.py"` to a tool that expects `path` succeeds without error.
- If the agent sends both `file_path` and `path`, the explicit `path` wins and `file_path` is not applied.
- Aliases only rewrite keys that exist in the target tool's schema -- a tool that genuinely has a `file_path` parameter is not affected.

### R-TOOL-04: Categorized error reporting

Tool execution errors must be classified into three categories: schema errors (wrong parameters), permission errors (path outside allowed scope), and tool errors (everything else). Each category must be labeled in the error message.

**Why:** The corrective action differs by category. A schema error means "fix your parameters." A permission error means "the path is outside allowed directories." A tool error means "something unexpected happened." If all three look the same, the agent guesses at the fix and often guesses wrong.

**Acceptance criteria:**
- A TypeError during dispatch is labeled `SCHEMA ERROR`.
- A PermissionError is labeled `PERMISSION ERROR` and includes the allowed paths.
- Any other exception is labeled `TOOL ERROR`.

### R-TOOL-05: Extensibility via subclassing

Adding a new tool must require only: (1) creating a Python class that subclasses the base tool and implements name, description, schema, and execute; (2) registering it in the tool list. No changes to the registry, dispatch, or framework code should be necessary.

**Why:** The tool set evolves faster than the framework. If adding a tool required modifying dispatch logic or schema-export code, every new tool would be a framework change with framework-level risk. Subclassing isolates tool-specific logic.

**Acceptance criteria:**
- A new tool file added to the tools directory, imported and appended to the default list, appears in the LLM's tool definitions on the next run with no other changes.
- The new tool inherits path validation, error handling, and result formatting from the base class.

### R-TOOL-06: Tag-based filtering

Tools must declare categorical tags (e.g., `file_read`, `file_write`, `search`, `git`, `analysis`, `execution`). The registry must support filtering to a subset of tools by tag, so the framework can restrict tool availability per phase or per task.

**Why:** Not every phase needs every tool. A read-only analysis phase should not offer write tools. A code-generation phase may not need git tools. Tag filtering lets the framework narrow the tool set without hardcoding tool names.

**Acceptance criteria:**
- A registry filtered to `{"search"}` contains only tools tagged `search`, plus any untagged tools.
- A tool with no tags is included in every filtered view (backward compatibility).

### R-TOOL-07: Default vs. optional tool separation

Tools that require network access or have high schema cost must be registered as optional, not default. They are only available when explicitly enabled via configuration.

**Why:** Including network tools by default would (a) add schema weight to every LLM call even when unused, and (b) allow outbound network access in air-gapped environments. Keeping them opt-in prevents accidental network calls and keeps the default schema lean.

**Acceptance criteria:**
- A fresh registry built with no extra configuration does not include network-access tools.
- Setting `extra_tools=["web_search"]` in config adds the web search tool to the registry.
- An unknown tool name in `extra_tools` logs a warning and is skipped, not a fatal error.

### R-TOOL-08: Tool allowlist enforcement

When the config specifies an `allowed_tools` list, only tools in that list may execute, even if they are registered. This is enforced at dispatch time, not just at registration.

**Why:** Registration and dispatch are separate steps. A restrictive config must be honored at runtime even if the registry was built with all tools. This prevents a permissive registry from bypassing a restrictive config.

**Acceptance criteria:**
- A registered tool not in `allowed_tools` returns a `PERMISSION ERROR` when called.
- An empty `allowed_tools` list means no filter (all registered tools are allowed).

## Design constraints

- **Async everywhere.** All tool execute methods are async. Blocking I/O (file reads, subprocess calls) must be wrapped in `asyncio.to_thread` or use async subprocess APIs. A tool that blocks the event loop degrades the entire agent's responsiveness.
- **No tool may bypass path security.** Any tool that touches the filesystem must validate paths through the security layer. There is no "trusted tool" exemption.
- **Tools are stateless.** A tool instance holds no mutable state between calls. All context comes from the config and parameters. This makes tools safe to share across concurrent agents (future capability).
- **Output budgets.** Tools that can produce unbounded output (search results, AST dumps, subprocess stdout) must enforce output caps. Flooding the LLM context window with a 500 KB grep result is worse than returning nothing.
